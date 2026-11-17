"""业务核心：标注局部对整体（表1 #17）逐条判定与证据书写。

表1 #17 冻结口径（本模块严格实现，不做相邻算子职责）：
- 输入：已有整体对齐结论的图像/音视频、描述、视频转写时间轴；
- 图像：仅保留 YOLOv8n 官方通用类别 **且** 与描述可对上的框；专用设备不预填；
- 音视频：沿用转写起止时间预填；
- 输出：只有 **人工确认** 的 bbox / 时间段可交付；未确认只能 ``pending_manual_review``；
- 工具开启后标注不完整 → 样本隔离库（标签 ``样本-语义不通``），不给可交付链；
- 工具关闭 → 不拦、留样本池、不产生任何框或时间戳；
- 不做词-区域一一对应；不得把预填写成人工确认；不得把规则分数写成模型概率。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils import detectors, io as io_utils
from utils.schemas import (DEFAULT_OPERATOR_VERSION, DETECTOR_MODEL,
                           FIXED_GENERAL_CLASSES_ONLY, FORMAL_OPERATOR_NUMBER,
                           GENERAL_CLASS_SOURCE, OPERATOR_NAME, OPERATOR_NAME_ZH,
                           STAGE_ID)

# --------------------------------------------------------------------------- #
# 身份与产物
# --------------------------------------------------------------------------- #

OPERATOR = OPERATOR_NAME
OPERATOR_VERSION = DEFAULT_OPERATOR_VERSION
FORMAL_NUMBER = FORMAL_OPERATOR_NUMBER
SOURCE_REFERENCE = "表1 #17 标注局部对整体（跨模态 demo_10.6）"
ALGORITHM = "yolov8n_general_prefill_v1+transcript_time_prefill_v1"

MAIN_NAME_ENABLED = "17_local_annotations.jsonl"
MAIN_NAME_DISABLED = "17_passthrough.jsonl"
PENDING_NAME = "17_pending.jsonl"
SAMPLE_QUARANTINE_NAME = "17_sample_quarantine.jsonl"
REJECTS_NAME = "17_rejects.jsonl"

#: 共享契约 §3/§4 的阶段控制文件名（与 demo_10.6 邻居算子保持一致）
LINEAGE_NAME = "lineage.json"
STAGE_CONTEXT_NAME = "stage_context.json"
LINEAGE_HISTORY_NAME = "lineage_history.json"
LOG_NAME = "run.log"

#: 下游 #18 消费的坐标系（归一到 [0,1] 的 xywh）
REGION_COORD = "xywh-normalized"

#: 处理状态（样本业务状态，非运行状态）
STATUS_CONFIRMED = "confirmed"
STATUS_PENDING = "pending_manual_review"
STATUS_INCOMPLETE = "incomplete"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_SKIPPED = "skipped"

# [v4.2] status 双字段（§2.10）：输出记录 int status（本算子处理型：1=成功/正常流程态，
# 0=失败/过滤——不完整进样本隔离库、原始问题进原始隔离区）+ 原字符串迁入 process_status
_STATUS_AS_INT = {
    STATUS_CONFIRMED: 1,
    STATUS_PENDING: 1,
    STATUS_NOT_APPLICABLE: 1,
    STATUS_SKIPPED: 1,
    STATUS_INCOMPLETE: 0,
    "rejected": 0,
}


def status_int(value: Optional[str]) -> int:
    """记录级字符串状态 → [v4.2] int status；未登记的一律按正常流程态 1。"""
    return _STATUS_AS_INT.get(value, 1)

#: 表1 质量标签
LABEL_CONFIRMED = "样本-局部标注：已人工确认"
LABEL_PENDING = "流程-局部标注：待人工确认"
LABEL_INCOMPLETE = "样本-语义不通"
LABEL_NOT_APPLICABLE = "流程-局部标注不适用"
LABEL_ROUTE_DISABLED = "流程-工具关闭：原样透传"
LABEL_ORIGIN_UNPARSABLE = "原始隔离-无法解析"
LABEL_ORIGIN_MISSING_FIELD = "原始隔离-缺关键字段"
LABEL_ORIGIN_INVALID_FIELD = "原始隔离-字段非法"
LABEL_ORIGIN_PATH_UNSAFE = "原始隔离-路径不安全"
LABEL_ORIGIN_DAMAGED = "原始隔离-文件损坏"

#: 动作与去向
ACTION_KEEP = "keep"
ACTION_WAITING_MANUAL = "waiting_manual"
ACTION_ISOLATE = "isolate"
ACTION_RECORD = "record"
DEST_SAMPLE_POOL = "sample_pool"
DEST_MANUAL_QUEUE = "manual_queue"
DEST_SAMPLE_QUARANTINE = "sample_quarantine"
DEST_ORIGIN_QUARANTINE = "origin_quarantine"

#: 判定路线（与状态同名但 confirmed→confirm），供调用方分桶统计
ROUTE_CONFIRM = "confirm"
ROUTE_PENDING = "pending"
ROUTE_INCOMPLETE = "incomplete"
ROUTE_NOT_APPLICABLE = "not_applicable"

#: 原因码
REASON_JSON_PARSE_ERROR = "json_parse_error"
REASON_RECORD_NOT_OBJECT = "record_not_object"
REASON_MISSING_SAMPLE_ID = "missing_sample_id"
REASON_MEDIA_PATH_UNSAFE = "media_path_unsafe"
REASON_IMAGE_MISSING = "image_missing"
REASON_IMAGE_UNREADABLE = "image_unreadable"
REASON_TRANSCRIPT_MISSING = "transcript_sidecar_missing"
REASON_TRANSCRIPT_UNREADABLE = "transcript_unreadable"
REASON_TRANSCRIPT_INVALID = "transcript_invalid"
REASON_HUMAN_ANNOTATION_INVALID = "human_annotation_invalid"
REASON_TOO_MANY_TIME_RANGES = "too_many_time_ranges"
REASON_NOT_APPLICABLE_MODALITY = "not_applicable_modality"
REASON_WHOLE_NOT_ALIGNED = "whole_alignment_not_aligned"
REASON_DETECTOR_UNAVAILABLE = "detector_unavailable"
REASON_NO_GENERAL_CLASS_MATCH = "no_general_class_match"
REASON_NO_TRANSCRIPT = "no_transcript_time_axis"
REASON_ANNOTATION_PENDING = "annotation_pending"
REASON_ANNOTATION_INCOMPLETE = "annotation_incomplete"
REASON_ROUTE_DISABLED = "route_disabled"

REASON_LABELS = {
    REASON_JSON_PARSE_ERROR: LABEL_ORIGIN_UNPARSABLE,
    REASON_RECORD_NOT_OBJECT: LABEL_ORIGIN_UNPARSABLE,
    REASON_MISSING_SAMPLE_ID: LABEL_ORIGIN_MISSING_FIELD,
    REASON_IMAGE_MISSING: LABEL_ORIGIN_MISSING_FIELD,
    REASON_TRANSCRIPT_MISSING: LABEL_ORIGIN_MISSING_FIELD,
    REASON_MEDIA_PATH_UNSAFE: LABEL_ORIGIN_PATH_UNSAFE,
    REASON_IMAGE_UNREADABLE: LABEL_ORIGIN_DAMAGED,
    REASON_TRANSCRIPT_UNREADABLE: LABEL_ORIGIN_DAMAGED,
    REASON_TRANSCRIPT_INVALID: LABEL_ORIGIN_INVALID_FIELD,
    REASON_HUMAN_ANNOTATION_INVALID: LABEL_ORIGIN_INVALID_FIELD,
    REASON_TOO_MANY_TIME_RANGES: LABEL_ORIGIN_INVALID_FIELD,
}

#: 输入字段别名
HUMAN_EVIDENCE_KEYS = ("human_annotations", "human_confirmation",
                       "local_human_annotations")
IMAGE_KEYS = ("image", "images", "image_path", "imagePaths", "image_reference")
VIDEO_KEYS = ("video", "videos", "video_path", "videoPaths")
AUDIO_KEYS = ("audio", "audios", "audio_path", "audioPaths")
#: 视频关键帧（#6 抽帧产物）：无图视频用帧做通用类别预填，帧路径随记录下发
FRAME_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
#: 转写来源优先级（规范 §4.17：输入「音视频引用 M08 时间索引」）：
#: M08 时间索引 ``time_index`` → M08 视觉时间索引 ``visual_time_index``（无声视频兜底）
#: → 记录内联转写 → sidecar（``TRANSCRIPT_FILE_KEYS``）。
TRANSCRIPT_KEYS = ("time_index", "visual_time_index",
                   "transcript", "transcript_segments", "transcript_data")
#: 非 sidecar 来源的来源标记（未列出的内联键记 ``inline``）。
TRANSCRIPT_SOURCES = {"time_index": "m08_time_index",
                      "visual_time_index": "m08_visual_index"}
TRANSCRIPT_FILE_KEYS = ("transcript_file", "transcript_path", "transcriptFile")
NEGATIVE_ALIGNMENT_STATUSES = {"not_aligned", "unaligned", "mismatch", "mismatched",
                              "not-matched", "failed"}


class LocalizationError(ValueError):
    """记录级业务/结构错误：调用方据此写入原始隔离清单。"""

    def __init__(self, reason_code: str, message: Optional[str] = None,
                 label: Optional[str] = None) -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.label = label or REASON_LABELS.get(reason_code, LABEL_ORIGIN_MISSING_FIELD)


# --------------------------------------------------------------------------- #
# 基础字段解析
# --------------------------------------------------------------------------- #

def record_sample_id(record: Dict[str, Any]) -> Optional[str]:
    for key in ("sample_id", "sampleId", "id"):
        if key not in record:
            continue
        value = record[key]
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                return text
    return None


def _as_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def truncate_text(text: Optional[str], max_chars: Optional[int]) -> Optional[str]:
    if text is None:
        return None
    if max_chars is None or max_chars <= 0:
        return text
    return text[:max_chars]


def resolve_description(record: Dict[str, Any], max_chars: Optional[int] = None) -> Optional[str]:
    """描述解析优先级：``text.content`` → ``metadata.description`` → ``description``/``caption``。"""
    text = record.get("text")
    if isinstance(text, dict):
        found = _as_text(text.get("content")) or _as_text(text.get("description"))
        if found:
            return truncate_text(found, max_chars)
    elif isinstance(text, str):
        found = _as_text(text)
        if found:
            return truncate_text(found, max_chars)

    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        found = _as_text(metadata.get("description"))
        if found:
            return truncate_text(found, max_chars)

    for key in ("description", "caption", "caption_zh"):
        found = _as_text(record.get(key))
        if found:
            return truncate_text(found, max_chars)
    return None


def _extract_paths(record: Dict[str, Any], keys: Iterable[str]) -> List[Any]:
    found: List[Any] = []
    for key in keys:
        if key not in record:
            continue
        value = record[key]
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                for inner in ("path", "file", "uri", "src", "filename", "name"):
                    if inner in item and item[inner] not in (None, ""):
                        found.append(item[inner])
                        break
            elif isinstance(item, str) and item.strip():
                found.append(item)
    return found


def _native_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Read M11 sides without letting empty fields hide media or root evidence."""
    if not any(isinstance(record.get(side), dict) for side in ("left", "right")):
        return record
    merged: Dict[str, Any] = {}

    def merge(target, source):
        for key, value in source.items():
            if value is None or value == [] or value == {} or (isinstance(value, str) and not value.strip()):
                continue
            if isinstance(value, dict):
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    for side in ("left", "right"):
        node = record.get(side)
        if isinstance(node, dict):
            inner = node.get("record") if isinstance(node.get("record"), dict) else node
            merge(merged, inner)
    merge(merged, record)
    if record_sample_id(merged) is None and _as_text(record.get("pair_id")):
        merged["sample_id"] = record["pair_id"]
    return merged


def resolve_modality(record: Dict[str, Any]) -> Dict[str, Any]:
    """解析模态与媒体引用；``applicable`` 表示本算子是否适用。"""
    declared = record.get("modality") or record.get("modalities") or []
    if isinstance(declared, str):
        declared = [declared]
    declared_list = [str(item).strip().lower() for item in declared
                     if isinstance(item, (str,))] if isinstance(declared, list) else []

    images = [str(item) for item in _extract_paths(record, IMAGE_KEYS)]
    videos = [str(item) for item in _extract_paths(record, VIDEO_KEYS)]
    audios = [str(item) for item in _extract_paths(record, AUDIO_KEYS)]

    modalities: List[str] = []
    for item in list(declared_list):
        if item and item not in modalities:
            modalities.append(item)
    if images and "image" not in modalities:
        modalities.append("image")
    if videos and "video" not in modalities:
        modalities.append("video")
    if audios and "audio" not in modalities:
        modalities.append("audio")

    has_image = bool(images)
    has_video = bool(videos)
    has_audio = bool(audios)
    declared_image = any(item in ("image", "images", "图文", "img") for item in declared_list)
    declared_av = any(item in ("audio", "video", "av", "音视频", "音频", "视频", "音视频")
                      for item in declared_list)
    missing_image_reference = declared_image and not has_image

    # 声明的模态同样算适用：音视频记录可以只带转写时间轴（无容器文件也可预填时间段）
    has_av = has_video or has_audio or declared_av
    applicable = has_image or has_av or declared_image

    return {
        "modalities": modalities,
        "declared": declared_list,
        "images": images,
        "videos": videos,
        "audios": audios,
        "has_image": has_image,
        "has_video": has_video,
        "has_audio": has_audio,
        "declared_image": declared_image,
        "declared_av": declared_av,
        "has_av": has_av,
        "applicable": applicable,
        "missing_image_reference": missing_image_reference,
    }


def _frame_entries(value: Any) -> List[Dict[str, Any]]:
    """把 ``frames`` 列表归一成 ``{path, timestamp, frame_index}``（只搬用，不推断）。"""
    entries: List[Dict[str, Any]] = []
    if not isinstance(value, list):
        return entries
    for order, item in enumerate(value):
        if isinstance(item, str):
            path, timestamp, index = item, None, order
        elif isinstance(item, dict):
            path = (item.get("path") or item.get("file")
                    or item.get("relative_path") or item.get("uri"))
            timestamp = _first_number(item, ("timestamp", "time", "start", "start_sec"))
            index = item.get("frame_index", order)
        else:
            continue
        if not isinstance(path, str) or not path.strip():
            continue
        entries.append({"path": path.strip(), "timestamp": timestamp,
                        "frame_index": index})
    return entries


def video_frame_refs(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解析视频关键帧引用（#6 已抽好的帧文件，无图视频据此做通用类别预填）。

    优先 ``video.frames``，其次各 ``video.segments[].frames``；只接受图片扩展名，
    路径去重按首次出现；时间戳缺失的帧保留（仅少一条时间证据，不影响裁图）。
    """
    video = record.get("video")
    if not isinstance(video, dict):
        return []
    frames = _frame_entries(video.get("frames"))
    if not frames:
        for segment in video.get("segments") or []:
            if isinstance(segment, dict):
                frames.extend(_frame_entries(segment.get("frames")))
    unique: List[Dict[str, Any]] = []
    seen: List[str] = []
    for item in frames:
        path = item["path"]
        if not path.lower().endswith(FRAME_EXTENSIONS):
            continue
        try:
            relative = io_utils.normalize_relative_path(path)
        except io_utils.UnsupportedMediaPath:
            continue
        if relative in seen:
            continue
        seen.append(relative)
        unique.append(dict(item, path=relative))
    unique.sort(key=lambda item: (item["timestamp"] if isinstance(item["timestamp"], float)
                                  else float("inf"), item["frame_index"]
                                  if isinstance(item["frame_index"], int) else 0))
    return unique


def whole_alignment_status(record: Dict[str, Any]) -> Optional[str]:
    alignment = record.get("alignment")
    alignment = alignment if isinstance(alignment, dict) else {}
    processing = record.get("processing")
    processing = processing if isinstance(processing, dict) else {}
    for holder in (alignment.get("whole"), alignment, record.get("whole_alignment"),
                   processing.get("16")):
        if not isinstance(holder, dict):
            continue
        for key in ("decision", "process_status", "overall", "status", "conclusion", "result"):
            status = holder.get(key)
            if isinstance(status, str) and status.strip():
                return status.strip().lower()
    return None


def whole_alignment_not_aligned(record: Dict[str, Any]) -> bool:
    return whole_alignment_status(record) in NEGATIVE_ALIGNMENT_STATUSES


# --------------------------------------------------------------------------- #
# 转写与时间段
# --------------------------------------------------------------------------- #

def _to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _first_number(payload: Dict[str, Any], keys: Iterable[str]) -> Optional[float]:
    for key in keys:
        if key in payload:
            number = _to_number(payload[key])
            if number is not None:
                return number
    return None


def normalize_transcript(entries: Any) -> List[Dict[str, Any]]:
    """把转写条目归一化成 ``[{segment_id, start_sec, end_sec, start_ms, end_ms, text}]``。

    #8 v1.0.1 起提供整数毫秒 ``start_ms``/``end_ms``（``[start, end)``）。有毫秒对时
    以毫秒为准换算秒；没有（旧产物 / 其它来源）时回落秒字段，保持向后兼容。
    """
    if entries is None:
        return []
    if isinstance(entries, dict):
        entries = entries.get("segments", entries.get("entries", []))
    if not isinstance(entries, list):
        raise LocalizationError(REASON_TRANSCRIPT_INVALID,
                               "transcript must be a list of segments")
    segments: List[Dict[str, Any]] = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise LocalizationError(REASON_TRANSCRIPT_INVALID,
                                    "transcript segment #%d is not an object" % index)
        start_ms = _to_number(item.get("start_ms"))
        end_ms = _to_number(item.get("end_ms"))
        if start_ms is not None and end_ms is not None:
            # 毫秒优先：避免「秒浮点 → 毫秒」二次取整带来的 ±1ms 漂移
            start, end = start_ms / 1000.0, end_ms / 1000.0
        else:
            start = _first_number(item, ("start", "start_sec", "start_seconds", "begin"))
            end = _first_number(item, ("end", "end_sec", "end_seconds", "stop"))
        if start is None or end is None:
            raise LocalizationError(
                REASON_TRANSCRIPT_INVALID,
                "transcript segment #%d has no parseable start/end" % index)
        if start < 0 or end < 0:
            raise LocalizationError(REASON_TRANSCRIPT_INVALID,
                                    "transcript segment #%d has negative time" % index)
        if end <= start:
            raise LocalizationError(
                REASON_TRANSCRIPT_INVALID,
                "transcript segment #%d end must be greater than start" % index)
        identifier = item.get("id") or item.get("segment_id") or item.get("index")
        text = item.get("text") or item.get("content") or ""
        segment = {
            "segment_id": str(identifier) if identifier is not None else "seg-%d" % index,
            "start_sec": float(start),
            "end_sec": float(end),
            "start_ms": int(round(float(start) * 1000)),
            "end_ms": int(round(float(end) * 1000)),
            "text": str(text).strip(),
        }
        # 视觉时间索引（#8）的附加证据：出处、粒度与关键帧路径，原样带到下游时间段
        for key in ("source", "granularity", "frame_path", "frame_index",
                    "timestamp", "segment_start", "segment_end"):
            if item.get(key) is not None:
                segment[key] = item[key]
        segments.append(segment)
    return segments


def build_time_ranges(segments: List[Dict[str, Any]], *, max_time_ranges: int,
                      max_text_chars: int,
                      source: str = "transcript") -> List[Dict[str, Any]]:
    """表1：音视频沿用转写起止时间预填（真实起止时间，不做任何模型打分）。

    ``source`` 区分时间轴出处：语音转写为 ``transcript``；无声视频退回 M08
    视觉时间索引时为 ``visual_segment``（分段级）或 ``visual_frame``（关键帧级，
    条目自带出处时以条目为准）；均不做词-区域对应，也没有文本。
    """
    if len(segments) > int(max_time_ranges):
        raise LocalizationError(
            REASON_TOO_MANY_TIME_RANGES,
            "transcript has %d segments, limit is %d" % (len(segments), max_time_ranges))
    ranges: List[Dict[str, Any]] = []
    for segment in segments:
        start = float(segment["start_sec"])
        end = float(segment["end_sec"])
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        item = {
            "segment_id": segment["segment_id"],
            "start_sec": round(start, 6),
            "end_sec": round(end, 6),
            # #8 v1.0.1 整数毫秒，[start, end)；上游没给时按秒换算补齐
            "start_ms": int(start_ms) if isinstance(start_ms, int)
            else int(round(start * 1000)),
            "end_ms": int(end_ms) if isinstance(end_ms, int)
            else int(round(end * 1000)),
            "duration_sec": round(end - start, 6),
            "text_excerpt": truncate_text(segment.get("text") or None, max_text_chars),
            "source": segment.get("source") or source,
            "human_verified": False,
        }
        for key in ("granularity", "frame_path", "frame_index", "timestamp"):
            if segment.get(key) is not None:
                item[key] = segment[key]
        ranges.append(item)
    return ranges


def read_sidecar_json(path) -> Optional[Dict[str, Any]]:
    """读取随包 sidecar JSON；不存在返回 ``None``，损坏/编码异常显式报错。"""
    if path is None:
        return None
    target = Path(str(path))
    if not target.is_file():
        return None
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise LocalizationError(REASON_TRANSCRIPT_UNREADABLE, str(exc)) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LocalizationError(REASON_TRANSCRIPT_UNREADABLE,
                                "sidecar is not valid UTF-8: %s" % exc) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LocalizationError(REASON_TRANSCRIPT_UNREADABLE,
                                "sidecar is not valid JSON: %s" % exc) from exc


# --------------------------------------------------------------------------- #
# 人工确认证据
# --------------------------------------------------------------------------- #

def _normalize_human_box(item: Any, index: int) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID,
                                "human box #%d is not an object" % index)
    numbers: Optional[List[float]] = None
    if "bbox" in item:
        raw = item["bbox"]
        if isinstance(raw, dict):
            numbers = [_to_number(raw.get(key)) for key in
                       ("x_min", "y_min", "x_max", "y_max")]
        elif isinstance(raw, (list, tuple)) and len(raw) == 4:
            numbers = [_to_number(value) for value in raw]
    elif all(key in item for key in ("x_min", "y_min", "x_max", "y_max")):
        numbers = [_to_number(item.get(key)) for key in
                   ("x_min", "y_min", "x_max", "y_max")]
    elif all(key in item for key in ("x", "y", "w", "h")):
        x, y, w, h = (_to_number(item.get(key)) for key in ("x", "y", "w", "h"))
        if None not in (x, y, w, h):
            numbers = [x, y, x + w, y + h]
    if numbers is None or any(value is None for value in numbers):
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID,
                                "human box #%d has no valid coordinates" % index)
    x_min, y_min, x_max, y_max = numbers  # type: ignore[misc]
    if not (0.0 <= x_min < x_max <= 1.0 and 0.0 <= y_min < y_max <= 1.0):
        raise LocalizationError(
            REASON_HUMAN_ANNOTATION_INVALID,
            "human box #%d must be normalized xyxy within [0,1]" % index)
    identifier = item.get("id") or item.get("box_id") or "human-box-%d" % index
    label = item.get("label") or item.get("class_name") or item.get("name")
    return {
        "box_id": str(identifier),
        "label": str(label) if label else None,
        "x_min": round(float(x_min), 6),
        "y_min": round(float(y_min), 6),
        "x_max": round(float(x_max), 6),
        "y_max": round(float(y_max), 6),
        "coordinates": "normalized-xyxy",
        "source": "human",
        "human_verified": True,
    }


def _normalize_human_range(item: Any, index: int) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID,
                                "human range #%d is not an object" % index)
    start = _first_number(item, ("start_sec", "start", "begin"))
    end = _first_number(item, ("end_sec", "end", "stop"))
    if start is None or end is None:
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID,
                                "human range #%d has no parseable start/end" % index)
    if start < 0 or end <= start:
        raise LocalizationError(
            REASON_HUMAN_ANNOTATION_INVALID,
            "human range #%d must satisfy 0 <= start < end" % index)
    identifier = item.get("id") or item.get("range_id") or "human-range-%d" % index
    return {
        "range_id": str(identifier),
        "start_sec": round(float(start), 6),
        "end_sec": round(float(end), 6),
        "duration_sec": round(float(end) - float(start), 6),
        "text_excerpt": truncate_text(_as_text(item.get("text")), 120),
        "source": "human",
        "human_verified": True,
    }


def parse_human_evidence(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析人工确认证据；结构非法一律显式报错（原始隔离）。"""
    payload: Any = None
    for key in HUMAN_EVIDENCE_KEYS:
        if key in record and record[key] is not None:
            payload = record[key]
            break
    if payload is None:
        # 兼容直接把人工证据块当参数传入（便于单测与适配层复用）
        if any(marker in record for marker in
               ("human_verified", "verified", "time_ranges", "ranges", "annotator")):
            payload = record
        else:
            return None
    if not isinstance(payload, dict):
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID,
                                "human annotations must be an object")

    verified = payload.get("human_verified", payload.get("verified", False))
    if not isinstance(verified, bool):
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID,
                                "human_verified must be a boolean")
    complete = payload.get("complete", True)
    if not isinstance(complete, bool):
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID,
                                "complete must be a boolean")

    raw_boxes = payload.get("boxes", [])
    raw_ranges = payload.get("time_ranges", payload.get("ranges", []))
    if not isinstance(raw_boxes, list):
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID, "boxes must be a list")
    if not isinstance(raw_ranges, list):
        raise LocalizationError(REASON_HUMAN_ANNOTATION_INVALID,
                                "time_ranges must be a list")

    boxes = [_normalize_human_box(item, index) for index, item in enumerate(raw_boxes)]
    ranges = [_normalize_human_range(item, index)
              for index, item in enumerate(raw_ranges)]
    annotator = payload.get("annotator") or payload.get("operator") or payload.get("user")
    return {
        "human_verified": bool(verified),
        "complete": bool(complete),
        "annotator": str(annotator) if annotator else None,
        "boxes": boxes,
        "time_ranges": ranges,
    }


# --------------------------------------------------------------------------- #
# 通用类别框过滤
# --------------------------------------------------------------------------- #

def match_general_boxes(boxes: Optional[Iterable[Dict[str, Any]]],
                        description: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    """保留「通用类别 ∩ 与描述可对上」的框；返回 (命中框, 丢弃数)。"""
    if not boxes:
        return [], 0
    return detectors.filter_general_boxes(list(boxes), description)


# --------------------------------------------------------------------------- #
# 输入规格
# --------------------------------------------------------------------------- #

def _normalize_refs(refs: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    for reference in refs:
        try:
            relative = io_utils.normalize_relative_path(reference)
            if relative not in normalized:
                normalized.append(relative)
        except io_utils.UnsupportedMediaPath as exc:
            raise LocalizationError(REASON_MEDIA_PATH_UNSAFE, str(exc)) from exc
    return normalized


def resolve_transcript(record: Dict[str, Any],
                       media_roots: List[Any]) -> Tuple[List[Dict[str, Any]], Optional[str],
                                                        Optional[str]]:
    """解析转写：返回 (归一化 segments, 来源, 被引用的 sidecar 相对路径)。"""
    payload: Any = None
    source = "inline"
    for key in TRANSCRIPT_KEYS:
        value = record.get(key)
        if value is None or value == [] or value == {}:
            continue
        payload = value
        source = TRANSCRIPT_SOURCES.get(key, "inline")
        break
    if payload is not None:
        # 允许 `{start, end}` 单段写法，也允许 `{segments: [...]}` / `[...]`
        if isinstance(payload, dict) and "segments" not in payload \
                and "entries" not in payload and ("start" in payload or "end" in payload):
            payload = [payload]
        return normalize_transcript(payload), source, None

    for key in TRANSCRIPT_FILE_KEYS:
        reference = record.get(key)
        if not isinstance(reference, str) or not reference.strip():
            continue
        try:
            relative = io_utils.normalize_relative_path(reference)
        except io_utils.UnsupportedMediaPath as exc:
            raise LocalizationError(REASON_MEDIA_PATH_UNSAFE, str(exc)) from exc
        located = io_utils.resolve_media(media_roots, relative)
        if located is None:
            raise LocalizationError(
                REASON_TRANSCRIPT_MISSING,
                "declared transcript sidecar is missing: %s" % relative)
        payload = read_sidecar_json(located)
        if payload is None:
            raise LocalizationError(REASON_TRANSCRIPT_UNREADABLE,
                                    "transcript sidecar is empty: %s" % relative)
        return normalize_transcript(payload), "sidecar", relative
    return [], None, None


def collect_media_refs(record: Dict[str, Any]) -> List[str]:
    """收集记录引用的媒体/侧车相对路径（关闭路线也据此原样搬运，不做校验）。

    视频关键帧也算引用：无图视频的框来自帧，帧文件必须随 run 目录一起搬运，
    下游 #18 才有可裁剪的图像。
    """
    record = _native_record(record)
    refs: List[str] = []
    for keys in (IMAGE_KEYS, VIDEO_KEYS, AUDIO_KEYS, TRANSCRIPT_FILE_KEYS):
        for item in _extract_paths(record, keys):
            text = str(item)
            if text not in refs:
                refs.append(text)
    for frame in video_frame_refs(record):
        if frame["path"] not in refs:
            refs.append(frame["path"])
    return refs


def resolve_input_spec(record: Dict[str, Any], media_roots: List[Any]) -> Dict[str, Any]:
    """逐条解析输入：校验必需字段、路径安全、转写与人工证据。"""
    record = _native_record(record)
    sample_id = record_sample_id(record)
    if sample_id is None:
        raise LocalizationError(REASON_MISSING_SAMPLE_ID,
                                "record has no usable sample_id")

    modality = resolve_modality(record)
    images = _normalize_refs(modality["images"])
    videos = _normalize_refs(modality["videos"])
    audios = _normalize_refs(modality["audios"])
    modality = dict(modality)
    modality.update({"images": images, "videos": videos, "audios": audios})

    segments, transcript_source, transcript_ref = resolve_transcript(record, list(media_roots))
    human = parse_human_evidence(record)
    frames = video_frame_refs(record)

    media_refs: List[str] = []
    for reference in images + videos + audios:
        if reference not in media_refs:
            media_refs.append(reference)
    for frame in frames:
        if frame["path"] not in media_refs:
            media_refs.append(frame["path"])
    if transcript_ref and transcript_ref not in media_refs:
        media_refs.append(transcript_ref)

    sidecars: List[str] = []
    for key in TRANSCRIPT_FILE_KEYS:
        reference = record.get(key)
        if isinstance(reference, str) and reference.strip() and reference not in sidecars:
            sidecars.append(reference)

    return {
        "record": record,
        "sample_id": sample_id,
        "description": resolve_description(record, None),
        "modality": modality,
        "images": images,
        "videos": videos,
        "audios": audios,
        "transcript_segments": segments,
        "transcript_source": transcript_source,
        "transcript_ref": transcript_ref,
        "human": human,
        "frames": frames,
        "media_refs": media_refs,
        "sidecars": sidecars,
        "missing_image_reference": modality["missing_image_reference"],
    }


# --------------------------------------------------------------------------- #
# 记录书写
# --------------------------------------------------------------------------- #

def _processing_map(record: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``processing`` 映射；上游若给了非法结构则原样另存，绝不静默丢弃。"""
    current = record.get("processing")
    if isinstance(current, dict):
        return current
    if current is not None:
        record.setdefault("_upstream_processing_raw", current)
    mapping: Dict[str, Any] = {}
    record["processing"] = mapping
    return mapping


def _detector_block(config, detector_state: Dict[str, Any],
                    applied: bool) -> Dict[str, Any]:
    identity = detector_state.get("identity") or {}
    return {
        "name": DETECTOR_MODEL,
        "backend": config.detector_backend,
        "available": bool(detector_state.get("available")),
        "degraded": bool(detector_state.get("degraded")),
        "reason": detector_state.get("reason"),
        "real_backend": bool(identity.get("real_backend")),
        "test_double": bool(identity.get("test_double")),
        "weights": identity.get("weights"),
        "device": identity.get("device", config.detector.device),
        "general_classes_only": FIXED_GENERAL_CLASSES_ONLY,
        "class_source": GENERAL_CLASS_SOURCE,
        "conf_threshold": config.detector.conf_threshold,
        "iou_threshold": config.detector.iou_threshold,
        "max_boxes": config.detector.max_boxes,
        "image_size": config.detector.image_size,
        "applied": bool(applied),
        "network_access": False,
    }


def to_region_boxes(boxes: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """把内部归一化 xyxy 框转成下游 #18 可直接消费的 ``xywh`` 归一化框。

    坐标只做换算与裁剪校验，不改写类别与置信度；``human_verified`` 原样透传，
    避免把预填候选伪装成人工确认。
    """
    regions: List[Dict[str, Any]] = []
    for index, box in enumerate(boxes or []):
        if not isinstance(box, dict):
            continue
        x_min = _to_number(box.get("x_min"))
        y_min = _to_number(box.get("y_min"))
        x_max = _to_number(box.get("x_max"))
        y_max = _to_number(box.get("y_max"))
        if None in (x_min, y_min, x_max, y_max):
            continue
        if not (0.0 <= x_min < x_max <= 1.0 and 0.0 <= y_min < y_max <= 1.0):
            continue
        identifier = box.get("box_id") or box.get("id") or "box-%03d" % (index + 1)
        region = {
            "box_id": str(identifier),
            "bbox": {
                "x": round(float(x_min), 6),
                "y": round(float(y_min), 6),
                "w": round(float(x_max) - float(x_min), 6),
                "h": round(float(y_max) - float(y_min), 6),
                "coord": REGION_COORD,
            },
            "class_name": box.get("class_name") or box.get("label"),
            "confidence": box.get("confidence"),
            "source": box.get("source"),
            "human_verified": bool(box.get("human_verified")),
        }
        # 视频帧框：带上帧路径/时间，下游 #18 才知道从哪一帧裁图
        for key in ("frame_path", "frame_timestamp", "frame_index"):
            if box.get(key) is not None:
                region[key] = box[key]
        regions.append(region)
    return regions


def relative_media_refs(record: Dict[str, Any]) -> Dict[str, List[str]]:
    """收集记录引用的相对媒体路径（只做归一化，不做存在性校验）。"""
    modality = resolve_modality(record)
    refs: Dict[str, List[str]] = {}
    for key, values in (("images", modality["images"]),
                        ("videos", modality["videos"]),
                        ("audios", modality["audios"])):
        normalized: List[str] = []
        for item in values:
            try:
                text = io_utils.normalize_relative_path(str(item))
            except io_utils.UnsupportedMediaPath:
                continue
            if text not in normalized:
                normalized.append(text)
        refs[key] = normalized
    transcript: List[str] = []
    for key in TRANSCRIPT_FILE_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            try:
                transcript.append(io_utils.normalize_relative_path(value))
            except io_utils.UnsupportedMediaPath:
                pass
            break
    refs["transcript"] = transcript
    return refs


def downstream_fields(record: Dict[str, Any], block: Dict[str, Any]) -> Dict[str, Any]:
    """构造下游 #18 需要的局部框与媒体引用字段。

    ``regions``/``boxes`` 取「人工确认框优先，否则预填候选」；``candidate_boxes``
    与 ``confirmed_boxes`` 分别单列，保证下游能分清预填与人工确认。
    """
    confirmed = to_region_boxes(block.get("boxes"))
    candidates = to_region_boxes((block.get("prefilled") or {}).get("boxes"))
    regions = confirmed or candidates
    frame_scoped = any(item.get("frame_path") for item in regions)
    if confirmed:
        source = "human_confirmed"
    elif candidates:
        source = "yolov8n_frame_prefill" if frame_scoped else "yolov8n_prefill"
    else:
        source = "none"
    refs = relative_media_refs(record)
    frames = video_frame_refs(record)
    primary = (refs["images"] or refs["videos"] or refs["audios"] or [None])[0]
    return {
        "boxes_source": source,
        "regions": regions,
        "boxes": regions,
        "local_regions": candidates,
        "candidate_boxes": candidates,
        "confirmed_boxes": confirmed,
        "time_ranges": list(block.get("time_ranges") or []),
        "prefilled_time_ranges": list((block.get("prefilled") or {}).get("time_ranges") or []),
        "path": primary,
        "paths": {
            "image": (refs["images"] or [None])[0],
            "images": refs["images"],
            "video": (refs["videos"] or [None])[0],
            "videos": refs["videos"],
            "audio": (refs["audios"] or [None])[0],
            "audios": refs["audios"],
            "transcript": refs["transcript"][0] if refs["transcript"] else None,
            "frames": [frame["path"] for frame in frames],
        },
        "video_frames": frames,
        "modality": list(block.get("modality") or []),
    }


def apply_block(record: Dict[str, Any], block: Dict[str, Any],
                event_time: str, line: int) -> Dict[str, Any]:
    """把本算子命名空间写入记录，不覆盖上游结论。"""
    record["local_annotations"] = block
    record["operator_17"] = {
        "operator_number": FORMAL_NUMBER,
        "operator_version": OPERATOR_VERSION,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": block["status"],
        "process_status": block["process_status"],
        "decision": block["decision"],
        "reason_code": block["reason_code"],
        "human_verified": block["human_verified"],
        "review_required": block["review_required"],
        "source_reference": SOURCE_REFERENCE,
        "event_time": event_time,
        "line_number": line,
        "evidence": {
            "annotation_type": block["annotation_type"],
            "confirmed_count": block["confirmed_count"],
            "candidate_count": block["candidate_count"],
            "matched_classes": block["matched_classes"],
            "dropped_boxes": block["dropped_boxes"],
            "detector": {
                "name": block["detector"]["name"],
                "degraded": block["detector"]["degraded"],
                "reason": block["detector"]["reason"],
                "real_backend": block["detector"]["real_backend"],
            },
            "transcript_source": block["transcript_source"],
        },
    }
    processing = _processing_map(record)
    processing["17"] = {
        "operator": OPERATOR,
        "operator_name_zh": OPERATOR_NAME_ZH,
        "operator_version": OPERATOR_VERSION,
        "operator_number": FORMAL_NUMBER,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": block["status"],
        "process_status": block["process_status"],
        "decision": block["decision"],
        "reason_code": block["reason_code"],
        "label": block["label"],
        "action": block["action"],
        "destination": block["destination"],
        "deliverable": block["deliverable"],
        "review_required": block["review_required"],
        "human_verified": block["human_verified"],
        "event_time": event_time,
        "line_number": line,
        "source_reference": SOURCE_REFERENCE,
    }
    # 下游 #18（标注局部对局部）直接消费的局部框与媒体引用
    processing["17"].update(downstream_fields(record, block))
    return record


def apply_skipped(record: Dict[str, Any], *, event_time: Optional[str] = None,
                  line: int = 0) -> Dict[str, Any]:
    """工具关闭：只写状态，不产生框/时间戳，不隔离。"""
    processing = _processing_map(record)
    processing["17"] = {
        "operator": OPERATOR,
        "operator_name_zh": OPERATOR_NAME_ZH,
        "operator_version": OPERATOR_VERSION,
        "operator_number": FORMAL_NUMBER,
        # [v4.2] status 双字段：路线关闭属正常流程态（status=1），原字符串迁入 process_status
        "status": status_int(STATUS_SKIPPED),
        "process_status": STATUS_SKIPPED,
        "decision": STATUS_SKIPPED,
        "reason_code": REASON_ROUTE_DISABLED,
        "label": LABEL_ROUTE_DISABLED,
        "action": ACTION_KEEP,
        "destination": DEST_SAMPLE_POOL,
        "deliverable": False,
        "review_required": False,
        "human_verified": False,
        "event_time": event_time,
        "line_number": line,
        "source_reference": SOURCE_REFERENCE,
    }
    # 关闭路线绝不产生任何框/时间戳：这里显式给出空列表，而不是省略字段
    refs = relative_media_refs(record)
    primary = (refs["images"] or refs["videos"] or refs["audios"] or [None])[0]
    processing["17"].update({
        "boxes_source": "none",
        "regions": [],
        "boxes": [],
        "local_regions": [],
        "candidate_boxes": [],
        "confirmed_boxes": [],
        "time_ranges": [],
        "prefilled_time_ranges": [],
        "path": primary,
        "paths": {
            "image": (refs["images"] or [None])[0],
            "images": refs["images"],
            "video": (refs["videos"] or [None])[0],
            "videos": refs["videos"],
            "audio": (refs["audios"] or [None])[0],
            "audios": refs["audios"],
            "transcript": refs["transcript"][0] if refs["transcript"] else None,
        },
        "modality": [],
    })
    return record


def make_ledger_entry(*, line: int, reason: str, label: str, action: str,
                      destination: str, event_time: str,
                      sample_id: Optional[str] = None,
                      record: Optional[Dict[str, Any]] = None,
                      status: Optional[str] = None,
                      unique_id: Optional[str] = None,
                      run_id: Optional[str] = None,
                      extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造旁路台账条目（待人工 / 样本隔离 / 原始隔离）。"""
    source = record if isinstance(record, dict) else {}
    metadata = source.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    entry: Dict[str, Any] = {
        "operator": FORMAL_NUMBER,
        "operator_name": OPERATOR,
        "operator_name_zh": OPERATOR_NAME_ZH,
        "operator_version": OPERATOR_VERSION,
        "stage": STAGE_ID,
        "stage_id": STAGE_ID,
        "uniqueId": unique_id,
        "runId": run_id,
        "sample_id": sample_id,
        "line_number": line,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(status),
        "process_status": status,
        "reason": reason,
        "reason_code": reason,
        "label": label,
        "action": action,
        "destination": destination,
        "batch_id": source.get("batch_id") or metadata.get("batch_id"),
        "retry_count": (source.get("retry_count")
                        if isinstance(source.get("retry_count"), int)
                        else metadata.get("retry_count")),
        "source_reference": SOURCE_REFERENCE,
        "event_time": event_time,
        "record": source,
    }
    if extra:
        entry.update(extra)
    return entry


def skipped_node() -> Dict[str, Any]:
    return {
        "operator": OPERATOR,
        "operator_name_zh": OPERATOR_NAME_ZH,
        "operator_version": OPERATOR_VERSION,
        "operator_number": FORMAL_NUMBER,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(STATUS_SKIPPED),
        "process_status": STATUS_SKIPPED,
        "decision": STATUS_SKIPPED,
        "reason_code": REASON_ROUTE_DISABLED,
        "label": LABEL_ROUTE_DISABLED,
    }


# --------------------------------------------------------------------------- #
# 单条判定
# --------------------------------------------------------------------------- #

def _outcome(action: str, reason: str, record: Dict[str, Any],
             entry: Dict[str, Any]) -> Dict[str, Any]:
    return {"action": action, "reason": reason, "record": record, "entry": entry}


def evaluate_record(spec: Dict[str, Any], *, line: int, config, event_time: str,
                    boxes: Optional[Iterable[Dict[str, Any]]],
                    detector_state: Dict[str, Any]) -> Dict[str, Any]:
    """对一条记录给出状态、证据、标签、动作与去向。"""
    record = spec["record"]
    modality = spec["modality"]
    sample_id = spec["sample_id"]
    description = truncate_text(spec["description"], config.max_text_chars)

    def _entry(reason: str, label: str, action: str, destination: str,
               status: str) -> Dict[str, Any]:
        return make_ledger_entry(line=line, reason=reason, label=label,
                                 action=action, destination=destination,
                                 event_time=event_time, sample_id=sample_id,
                                 record=record, status=status)

    def _finish(status: str, reason: str, label: str, route: str,
                destination: str, *, ledger_action: str, deliverable: bool,
                human_verified: bool,
                confirmed_boxes: List[Dict[str, Any]],
                confirmed_ranges: List[Dict[str, Any]],
                prefilled_boxes: List[Dict[str, Any]],
                prefilled_ranges: List[Dict[str, Any]],
                dropped: int, matched_classes: List[str],
                annotator: Optional[str]) -> Dict[str, Any]:
        block = {
            "operator_number": FORMAL_NUMBER,
            "operator_version": OPERATOR_VERSION,
            "annotation_type": "local-to-whole",
            # [v4.2] status 双字段：int status + 原字符串迁入 process_status
            "status": status_int(status),
            "process_status": status,
            "decision": status,
            "deliverable": bool(deliverable),
            "human_verified": bool(human_verified),
            "confirmed_by": annotator,
            "coordinates": "normalized-xyxy",
            "boxes": confirmed_boxes,
            "time_ranges": confirmed_ranges,
            "prefilled": {
                "boxes": prefilled_boxes,
                "time_ranges": prefilled_ranges,
                "box_count": len(prefilled_boxes),
                "time_range_count": len(prefilled_ranges),
                "confirm_required": status == STATUS_PENDING,
            },
            "candidate_count": len(prefilled_boxes) + len(prefilled_ranges),
            "confirmed_count": len(confirmed_boxes) + len(confirmed_ranges),
            "matched_classes": matched_classes,
            "dropped_boxes": dropped,
            "detector": _detector_block(config, detector_state,
                                        applied=bool(prefilled_boxes)),
            "modality": modality["modalities"],
            "description_used": description is not None,
            "transcript_source": spec["transcript_source"],
            "frame_count": len(spec.get("frames") or []),
            "frame_scoped_boxes": sum(1 for item in prefilled_boxes
                                      if item.get("frame_path")),
            "reason_code": reason,
            "label": label,
            "action": ledger_action,
            "destination": destination,
            "review_required": status == STATUS_PENDING,
            "event_time": event_time,
            "source_reference": SOURCE_REFERENCE,
            "algorithm": ALGORITHM,
        }
        apply_block(record, block, event_time, line)
        return _outcome(route, reason, record,
                        _entry(reason, label, ledger_action, destination, status))

    # 1) 模态不适用：留样本池，不隔离，也绝不写成通过
    if not modality["applicable"]:
        return _finish(STATUS_NOT_APPLICABLE, REASON_NOT_APPLICABLE_MODALITY,
                       LABEL_NOT_APPLICABLE, ROUTE_NOT_APPLICABLE, DEST_SAMPLE_POOL,
                       ledger_action=ACTION_KEEP,
                       deliverable=False, human_verified=False,
                       confirmed_boxes=[], confirmed_ranges=[],
                       prefilled_boxes=[], prefilled_ranges=[], dropped=0,
                       matched_classes=[], annotator=None)

    # 2) 整体对齐结论为否定：本算子不做局部对整体预填
    if config.require_whole_aligned and whole_alignment_not_aligned(record):
        return _finish(STATUS_NOT_APPLICABLE, REASON_WHOLE_NOT_ALIGNED,
                       LABEL_NOT_APPLICABLE, ROUTE_NOT_APPLICABLE, DEST_SAMPLE_POOL,
                       ledger_action=ACTION_KEEP,
                       deliverable=False, human_verified=False,
                       confirmed_boxes=[], confirmed_ranges=[],
                       prefilled_boxes=[], prefilled_ranges=[], dropped=0,
                       matched_classes=[], annotator=None)

    has_image = modality["has_image"]
    has_av = modality["has_av"]

    # 3) 预填候选：图像/视频关键帧 = 通用类别 ∩ 描述；音视频 = 转写起止时间
    #    无图视频用 #6 抽好的关键帧做检测（框带 frame_path，供 #18 裁图），
    #    不改写 video_timestamp_source：时间段仍只来自转写/视觉索引。
    prefilled_boxes: List[Dict[str, Any]] = []
    dropped = 0
    if (has_image or spec.get("frames")) and boxes:
        prefilled_boxes, dropped = match_general_boxes(boxes, description)
    matched_classes = sorted({item["class_name"] for item in prefilled_boxes})

    prefilled_ranges: List[Dict[str, Any]] = []
    if has_av and spec["transcript_segments"]:
        prefilled_ranges = build_time_ranges(
            spec["transcript_segments"], max_time_ranges=config.max_time_ranges,
            max_text_chars=config.max_text_chars,
            source="visual_segment"
            if spec["transcript_source"] == "m08_visual_index" else "transcript")

    detector_available = bool(detector_state.get("available"))
    human = spec["human"]

    # 4) 无人工确认证据 → 待人工（候选保留、不可交付）
    if human is None:
        if (has_image or spec.get("frames")) and not detector_available:
            reason = REASON_DETECTOR_UNAVAILABLE
        elif has_image and not prefilled_boxes:
            reason = REASON_NO_GENERAL_CLASS_MATCH
        elif has_av and not prefilled_ranges and not prefilled_boxes:
            reason = REASON_NO_TRANSCRIPT
        else:
            reason = REASON_ANNOTATION_PENDING
        return _finish(STATUS_PENDING, reason, LABEL_PENDING,
                       ROUTE_PENDING, DEST_MANUAL_QUEUE,
                       ledger_action=ACTION_WAITING_MANUAL,
                       deliverable=False, human_verified=False,
                       confirmed_boxes=[], confirmed_ranges=[],
                       prefilled_boxes=prefilled_boxes,
                       prefilled_ranges=prefilled_ranges, dropped=dropped,
                       matched_classes=matched_classes, annotator=None)

    # 5) 有人工证据但尚未确认 → 待人工
    if not human["human_verified"]:
        return _finish(STATUS_PENDING, REASON_ANNOTATION_PENDING, LABEL_PENDING,
                       ROUTE_PENDING, DEST_MANUAL_QUEUE,
                       ledger_action=ACTION_WAITING_MANUAL,
                       deliverable=False, human_verified=False,
                       confirmed_boxes=[], confirmed_ranges=[],
                       prefilled_boxes=prefilled_boxes,
                       prefilled_ranges=prefilled_ranges, dropped=dropped,
                       matched_classes=matched_classes,
                       annotator=human["annotator"])

    # 6) 已确认但标注不完整 → 样本隔离库（表1 标签：样本-语义不通）
    satisfied = False
    if has_image and human["boxes"]:
        satisfied = True
    if has_av and human["time_ranges"]:
        satisfied = True
    if human["complete"] is False or not satisfied:
        return _finish(STATUS_INCOMPLETE, REASON_ANNOTATION_INCOMPLETE,
                       LABEL_INCOMPLETE, ROUTE_INCOMPLETE, DEST_SAMPLE_QUARANTINE,
                       ledger_action=ACTION_ISOLATE,
                       deliverable=False, human_verified=True,
                       confirmed_boxes=[], confirmed_ranges=[],
                       prefilled_boxes=prefilled_boxes,
                       prefilled_ranges=prefilled_ranges, dropped=dropped,
                       matched_classes=matched_classes,
                       annotator=human["annotator"])

    # 7) 人工确认完成 → 可交付
    return _finish(STATUS_CONFIRMED, "human_confirmed", LABEL_CONFIRMED,
                   ROUTE_CONFIRM, DEST_SAMPLE_POOL,
                   ledger_action=ACTION_KEEP,
                   deliverable=True, human_verified=True,
                   confirmed_boxes=list(human["boxes"]),
                   confirmed_ranges=list(human["time_ranges"]),
                   prefilled_boxes=prefilled_boxes,
                   prefilled_ranges=prefilled_ranges, dropped=dropped,
                   matched_classes=matched_classes,
                   annotator=human["annotator"])
