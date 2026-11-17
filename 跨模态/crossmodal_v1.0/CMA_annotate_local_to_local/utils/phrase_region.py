"""算子 18 业务规则层：字段解析、候选框规范化、词-区域关系与分诊台账。

表1 #18「标注局部对局部」口径：
- 输入：描述、图像、#17 已有候选框；没有框时给人工空白列表；
- 用中文分词把描述切成词，词与候选框裁图一起进**已加载的** CLIP 算分；
- 达到 ``min_clip_score`` 才预填，低于阈值的词标为**待人工**；
- 文件名不能当词；没有人工确认不能交付；不新建多语言模型。

核心约定（诚实边界）：
- ``human_verified`` 恒为 ``False``、``deliverable`` 恒为 ``False``：
  本算子没有任何人工复核平台，运行期产物**一律是"待人工确认"的候选**；
- 「工具开启后没有对应关系」判定为：**有候选框但分词后不存在任何有效词**
  （描述就是文件名、纯标点等），此时样本进「样本隔离库（样本-语义不通）」。
  没有候选框、缺描述、缺图像等属于流程状态，留产线不隔离。
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import io as io_utils
from . import scoring
from . import tokenization
from .schemas import (CLIP_MODEL_NAME, FORMAL_OPERATOR_NUMBER, OPERATOR_NAME,
                      OPERATOR_VERSION, STAGE_ID)

__all__ = [
    "ACTION_ISOLATE",
    "ACTION_ORIGIN",
    "ACTION_PENDING",
    "ACTION_PREFILL",
    "ACTION_SKIP",
    "CLIP_MODEL_NAME",
    "DEST_MANUAL",
    "DEST_NEXT",
    "DEST_ORIGIN",
    "DEST_PIPELINE",
    "DEST_SAMPLE",
    "DependencyError",
    "BoxError",
    "LABEL_FLOW_DISABLED",
    "LABEL_FLOW_MISSING_DESCRIPTION",
    "LABEL_FLOW_NO_BOXES",
    "LABEL_FLOW_NO_IMAGE",
    "LABEL_FLOW_PENDING_MANUAL",
    "LABEL_ORIGIN_BROKEN",
    "LABEL_ORIGIN_DUPLICATE",
    "LABEL_ORIGIN_ENCODING",
    "LABEL_ORIGIN_MISSING_FIELD",
    "LABEL_ORIGIN_UNPARSABLE",
    "LABEL_SCORE_PENDING_HUMAN",
    "LABEL_SEMANTIC_UNMATCHED",
    "LINEAGE_NAME",
    "LOG_NAME",
    "MAIN_NAME_DISABLED",
    "MAIN_NAME_ENABLED",
    "MANIFEST_NAME",
    "MEDIA_DIR_NAME",
    "PENDING_NAME",
    "REASON_BELOW_THRESHOLD",
    "REASON_DESCRIPTION_IS_FILENAME",
    "REASON_DUPLICATE_IDENTIFIER",
    "REASON_IMAGE_MISSING",
    "REASON_IMAGE_UNREADABLE",
    "REASON_MISSING_DESCRIPTION",
    "REASON_MODEL_UNAVAILABLE",
    "REASON_NO_AVAILABLE_REGION",
    "REASON_NO_BOXES",
    "REASON_NO_IMAGE_REFERENCE",
    "REASON_NO_VALID_TOKENS",
    "REASON_ROUTE_DISABLED",
    "REASON_SCORE_ERROR",
    "REASON_UNSUPPORTED_MEDIA",
    "REJECTS_NAME",
    "SAMPLE_QUARANTINE_NAME",
    "STAGE_CONTEXT_NAME",
    "STATUS_DEGRADED",
    "STATUS_ISOLATED",
    "STATUS_PENDING_MANUAL",
    "STATUS_PREFILLED",
    "STATUS_SKIPPED",
    "attach_node",
    "box_image_path",
    "crop_rect",
    "crop_rects",
    "encode_crops_by_source",
    "evaluate_record",
    "make_ledger_entry",
    "normalize_box",
    "origin_node",
    "pending_node",
    "resolve_candidate_boxes",
    "resolve_description",
    "resolve_frame_reference",
    "resolve_image_reference",
    "resolve_media_file",
    "resolve_record",
    "sample_isolated_node",
    "skipped_node",
    "unknown",
]

# --------------------------------------------------------------------------- #
# 常量：文件名、标签、原因码、去向
# --------------------------------------------------------------------------- #

MAIN_NAME_ENABLED = "18_phrase_region_annotations.jsonl"
MAIN_NAME_DISABLED = "18_passthrough.jsonl"
SAMPLE_QUARANTINE_NAME = "18_sample_quarantine.jsonl"
PENDING_NAME = "18_pending_manual.jsonl"
REJECTS_NAME = "18_rejects.jsonl"
LOG_NAME = "18_run.log"
LINEAGE_NAME = "lineage.json"
STAGE_CONTEXT_NAME = "stage_context.json"
MANIFEST_NAME = "manifest.json"
MEDIA_DIR_NAME = "media"

ACTION_SKIP = "skip"
ACTION_PREFILL = "prefill"
ACTION_PENDING = "pending"
ACTION_ISOLATE = "isolate"
ACTION_ORIGIN = "origin"

STATUS_SKIPPED = "skipped"
STATUS_PREFILLED = "prefilled"
STATUS_PENDING_MANUAL = "pending_manual"
STATUS_DEGRADED = "degraded"
STATUS_ISOLATED = "isolated"

# [v4.2] status 双字段（§2.10）：输出记录 int status（本算子处理型：1=成功/正常流程态，
# 0=失败/过滤——无对应进样本隔离库、原始问题进原始隔离区）+ 原字符串迁入 process_status
_STATUS_AS_INT = {
    STATUS_SKIPPED: 1,
    STATUS_PREFILLED: 1,
    STATUS_PENDING_MANUAL: 1,
    STATUS_DEGRADED: 1,
    STATUS_ISOLATED: 0,
}


def status_int(value: Optional[str]) -> int:
    """记录级字符串状态 → [v4.2] int status；未登记的一律按正常流程态 1。"""
    return _STATUS_AS_INT.get(value, 1)

REASON_ROUTE_DISABLED = "route_disabled"
REASON_MISSING_DESCRIPTION = "missing_description"
REASON_DESCRIPTION_IS_FILENAME = "description_is_filename"
REASON_NO_IMAGE_REFERENCE = "no_image_reference"
REASON_UNSUPPORTED_MEDIA = "unsupported_media_path"
REASON_IMAGE_MISSING = "image_missing"
REASON_IMAGE_UNREADABLE = "image_unreadable"
REASON_NO_BOXES = "no_boxes"
REASON_NO_VALID_TOKENS = "no_valid_tokens"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_NO_AVAILABLE_REGION = "no_available_region"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_SCORE_ERROR = "score_error"
REASON_DUPLICATE_IDENTIFIER = "duplicate_identifier"

REASON_JSON_PARSE = "json_parse_error"
REASON_NOT_OBJECT = "record_not_object"
REASON_ENCODING = "encoding_error"
REASON_MISSING_SAMPLE_ID = "missing_field: sample_id"

LABEL_ORIGIN_UNPARSABLE = "原始文件-无法解析"
LABEL_ORIGIN_BROKEN = "原始文件-损坏"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_ORIGIN_ENCODING = "原始文件-编码异常"
LABEL_ORIGIN_DUPLICATE = "原始文件-重复标识"
LABEL_SEMANTIC_UNMATCHED = "样本-语义不通"
LABEL_SCORE_PENDING_HUMAN = "样本-评分：待人工"
LABEL_FLOW_DISABLED = "流程-局部对局部未启用"
LABEL_FLOW_MISSING_DESCRIPTION = "流程-待补描述"
LABEL_FLOW_NO_IMAGE = "流程-无图像"
LABEL_FLOW_NO_BOXES = "流程-无候选框"
LABEL_FLOW_PENDING_MANUAL = "流程-待人工确认"

DEST_ORIGIN = "原始隔离区"
DEST_SAMPLE = "样本隔离库"
DEST_MANUAL = "人工确认队列"
DEST_PIPELINE = "产线待处理"
DEST_NEXT = "#19"

CONFIRMATION_AUTO_PREFILL = "auto_prefill"
CONFIRMATION_PENDING_MANUAL = "pending_manual_review"

MAX_TOKENS_HARD_LIMIT = 512
DEFAULT_PIXEL_EPSILON = 0.0
NORMALIZED_EPSILON = 0.01


class BoxError(ValueError):
    """候选框缺字段、非数值、退化或不可裁剪。"""


class DependencyError(RuntimeError):
    """模型或外部依赖不可用（严格模式）。"""


def unknown(value: Any) -> Any:
    return value


# --------------------------------------------------------------------------- #
# 描述 / 图像 / 候选框解析
# --------------------------------------------------------------------------- #

def _pair_records(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 #11 成对结构（left/right）拍平为一条可处理记录。"""
    if not isinstance(record, dict):
        return None
    sides = [record.get("left"), record.get("right")]
    if not any(isinstance(side, (dict,)) for side in sides):
        return None
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

    for side in sides:
        if not isinstance(side, dict):
            continue
        inner = side.get("record") if isinstance(side.get("record"), dict) else side
        merge(merged, inner)
    merge(merged, {key: value for key, value in record.items() if key not in {"left", "right"}})
    return merged


_DESCRIPTION_GETTERS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("text.content", ("text", "content")),
    ("text.description", ("text", "description")),
    ("text.caption", ("text", "caption")),
    ("text", ("text",)),
    ("description", ("description",)),
    ("caption", ("caption",)),
    ("metadata.description", ("metadata", "description")),
    ("metadata.caption", ("metadata", "caption")),
    ("annotations.description", ("annotations", "description")),
)


def _dig(payload: Any, path: Sequence[str]) -> Any:
    current = payload
    for key in path:
        if isinstance(current, dict):
            if key not in current:
                return None
            current = current[key]
        else:
            return None
    return current


def resolve_description(record: Dict[str, Any],
                        image_path: Optional[Any] = None) -> Dict[str, Any]:
    """按固定优先级解析描述文本（不读本算子命名空间，避免自我引用）。"""
    payload: Any = record
    flattened = _pair_records(record)
    if flattened is not None:
        payload = flattened

    for source, path in _DESCRIPTION_GETTERS:
        value = _dig(payload, path)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        if image_path and text.lower() in tokenization.forbidden_tokens(image_path):
            return {"value": None, "source": source, "reason": REASON_DESCRIPTION_IS_FILENAME,
                    "raw": text}
        return {"value": text, "source": source, "reason": None, "raw": text}
    return {"value": None, "source": None, "reason": REASON_MISSING_DESCRIPTION, "raw": None}


#: 图片扩展名：解析「视频路径被当成图像」时的兜底判据
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

#: 视频关键帧候选（无图视频：帧文件来自 #6 抽帧，经 #17 下发）
_FRAME_GETTERS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("processing.17.video_frames", ("processing", "17", "video_frames")),
    ("video_frames", ("video_frames",)),
    ("video.frames", ("video", "frames")),
    ("frames", ("frames",)),
)


def _is_image_path(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().endswith(IMAGE_EXTENSIONS)


def _legacy_m17_image(payload: Any, value: str) -> bool:
    """Typed paths win; extensionless legacy M17 paths still denote images."""
    if _is_image_path(value):
        return True
    if Path(value.strip()).suffix.lower() in {
            ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma",
            ".mp4", ".avi", ".mkv", ".mov", ".webm", ".mpeg", ".mpg", ".m4v"}:
        return False
    return not isinstance(_dig(payload, ("processing", "17", "paths")), dict)


def resolve_frame_reference(payload: Any) -> Optional[Dict[str, Any]]:
    """解析视频关键帧引用（无图视频的裁图源）；没有可用帧时返回 ``None``。"""
    for source, path in _FRAME_GETTERS:
        value = _dig(payload, path)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and _is_image_path(item):
                return {"path": item.strip(), "source": source}
            if isinstance(item, dict):
                for key in ("path", "file", "relative_path"):
                    inner = item.get(key)
                    if _is_image_path(inner):
                        return {"path": inner.strip(),
                                "source": "%s.%s" % (source, key)}
    return None


def resolve_image_reference(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析图像引用（相对任务目录的路径）。

    无图视频退回首个关键帧作为裁剪源（``source`` 以 ``frame`` 结尾，便于区分）；
    ``processing.17.path`` 按扩展名识别；未知扩展仅兼容无 typed paths 的旧版图片引用。
    """
    payload: Any = _pair_records(record) or record
    candidates = (
        ("image", ("image",)),
        ("image_reference", ("image_reference",)),
        ("image_path", ("image_path",)),
        ("media.image", ("media", "image")),
        ("annotations.image", ("annotations", "image")),
        ("processing.17.paths.image", ("processing", "17", "paths", "image")),
        ("processing.17.path", ("processing", "17", "path")),
        ("processing.17.image", ("processing", "17", "image")),
    )
    for source, path in candidates:
        value = _dig(payload, path)
        if isinstance(value, str) and value.strip():
            if source == "processing.17.path" and not _legacy_m17_image(payload, value):
                continue
            return {"path": value.strip(), "source": source}
        if isinstance(value, dict):
            for key in ("path", "file", "name", "relative_path"):
                inner = value.get(key)
                if isinstance(inner, str) and inner.strip():
                    if source == "processing.17.path" and not _legacy_m17_image(payload, inner):
                        continue
                    return {"path": inner.strip(), "source": "%s.%s" % (source, key)}
    return resolve_frame_reference(payload)


_BOX_GETTERS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("regions", ("regions",)),
    ("boxes", ("boxes",)),
    ("local_regions", ("local_regions",)),
    ("candidate_boxes", ("candidate_boxes",)),
    ("image.regions", ("image", "regions")),
    ("image.boxes", ("image", "boxes")),
    ("annotations.regions", ("annotations", "regions")),
    ("processing.17.regions", ("processing", "17", "regions")),
    ("processing.17.boxes", ("processing", "17", "boxes")),
    ("processing.17.local_regions", ("processing", "17", "local_regions")),
    ("processing.17.candidate_boxes", ("processing", "17", "candidate_boxes")),
)


def resolve_candidate_boxes(record: Dict[str, Any]) -> Dict[str, Any]:
    """解析 #17 提供的局部候选框列表。"""
    payload: Any = _pair_records(record) or record
    for source, path in _BOX_GETTERS:
        value = _dig(payload, path)
        if isinstance(value, list) and value:
            return {"boxes": value, "source": source}
    return {"boxes": [], "source": None}


_UPSTREAM_TOKEN_GETTERS: Sequence[Tuple[str, ...]] = (
    ("text", "tokens"),
    ("text", "words"),
    ("tokens",),
    ("processing", "04", "tokens"),
    ("processing", "4", "tokens"),
    ("processing", "03", "tokens"),
    ("processing", "3", "tokens"),
    ("processing", "04", "words"),
)


def resolve_upstream_tokens(record: Dict[str, Any]) -> Optional[List[Any]]:
    payload: Any = _pair_records(record) or record
    for path in _UPSTREAM_TOKEN_GETTERS:
        value = _dig(payload, path)
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            return list(value)
    return None


def resolve_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """把一条输入记录规范化为处理规格。"""
    payload: Any = _pair_records(record) or record
    sample_id = payload.get("sample_id") if isinstance(payload, dict) else None
    pair_id = record.get("pair_id") if isinstance(record, dict) else None
    identifier = sample_id or pair_id
    image = resolve_image_reference(payload)
    description = resolve_description(payload,
                                      image_path=image["path"] if image else None)
    boxes = resolve_candidate_boxes(payload)
    return {"identifier": str(identifier) if identifier is not None else None,
            "sample_id": sample_id,
            "pair_id": pair_id,
            "batch_id": payload.get("batch_id") if isinstance(payload, dict) else None,
            "modalities": payload.get("modalities") if isinstance(payload, dict) else None,
            "image": image,
            "description": description,
            "boxes": boxes["boxes"],
            "boxes_source": boxes["source"],
            "upstream_tokens": resolve_upstream_tokens(payload),
            "is_pair": _pair_records(record) is not None,
            "record": record}


# --------------------------------------------------------------------------- #
# 候选框规范化与裁剪
# --------------------------------------------------------------------------- #

def _as_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise BoxError("候选框 %s 必须是数值" % field)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BoxError("候选框 %s 必须是数值" % field)
    if math.isnan(number) or math.isinf(number):
        raise BoxError("候选框 %s 不能是 NaN/Inf" % field)
    return number


def _tidy(number: float, normalized: bool) -> Any:
    if normalized:
        return round(float(number), 6)
    return int(number) if float(number).is_integer() else round(float(number), 3)


def normalize_box(raw: Any, index: int = 0) -> Dict[str, Any]:
    """把多种候选框写法归一到 ``xywh`` + ``pixel|normalized``。

    支持：``{"bbox": {"x","y","w","h"}}``、``{"bbox": {"x1","y1","x2","y2"}}``、
    ``{"bbox": [x1,y1,x2,y2]}``（默认像素）、``{"x","y","w","h"}`` 扁平写法；
    ``coord``/``normalized`` 可显式声明坐标系，未声明时按**像素**处理（与共享 schema 一致）。
    """
    if not isinstance(raw, dict):
        raise BoxError("候选框必须是对象")

    box_id = raw.get("box_id") or raw.get("id") or raw.get("region_id") or raw.get("name")
    box_id = str(box_id) if box_id not in (None, "") else "box-%03d" % (index + 1)

    coord = raw.get("coord") or raw.get("coord_type") or raw.get("format")
    normalized = raw.get("normalized")
    kind: Optional[str] = None
    values: Optional[Tuple[Any, ...]] = None

    bbox = raw.get("bbox")
    if isinstance(bbox, dict):
        coord = bbox.get("coord", coord)
        normalized = bbox.get("normalized", normalized)
        width = bbox.get("w", bbox.get("width"))
        height = bbox.get("h", bbox.get("height"))
        if "x" in bbox and "y" in bbox and width is not None and height is not None:
            values = (bbox["x"], bbox["y"], width, height)
            kind = "xywh"
        elif "x1" in bbox and "y1" in bbox and "x2" in bbox and "y2" in bbox:
            values = (bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"])
            kind = "xyxy"
        else:
            raise BoxError("候选框 bbox 缺少坐标字段")
    elif isinstance(bbox, (list, tuple)):
        if len(bbox) != 4:
            raise BoxError("候选框 bbox 必须包含 4 个数值")
        values = tuple(bbox)
        kind = "xyxy"
    else:
        width = raw.get("w", raw.get("width"))
        height = raw.get("h", raw.get("height"))
        if "x" in raw and "y" in raw and width is not None and height is not None:
            values = (raw["x"], raw["y"], width, height)
            kind = "xywh"
        else:
            raise BoxError("候选框缺少坐标")

    numbers = [_as_number(item, "坐标") for item in values]
    if coord is not None:
        label = str(coord).strip().lower()
        if "norm" in label:
            normalized = True
        elif "pixel" in label or "abs" in label:
            normalized = False
    normalized = bool(normalized)

    x, y = numbers[0], numbers[1]
    if kind == "xyxy":
        x2, y2 = numbers[2], numbers[3]
        width_value, height_value = x2 - x, y2 - y
    else:
        width_value, height_value = numbers[2], numbers[3]

    if width_value <= 0 or height_value <= 0:
        raise BoxError("候选框宽高必须为正")
    epsilon = NORMALIZED_EPSILON if normalized else DEFAULT_PIXEL_EPSILON
    if x < -epsilon or y < -epsilon:
        raise BoxError("候选框原点不能为负")

    bbox_out = {"x": _tidy(x, normalized), "y": _tidy(y, normalized),
                "w": _tidy(width_value, normalized), "h": _tidy(height_value, normalized),
                "coord": "%s-%s" % (kind, "normalized" if normalized else "pixel")}

    class_name = raw.get("class_name") or raw.get("label") or raw.get("class")
    confidence = raw.get("confidence", raw.get("score"))
    if confidence is not None:
        try:
            confidence = round(float(confidence), 6)
        except (TypeError, ValueError):
            confidence = None

    frame_path = raw.get("frame_path")
    box = {"box_id": box_id,
           "bbox": bbox_out,
           "class_name": class_name if isinstance(class_name, str) else None,
           "confidence": confidence,
           "source": raw.get("source") if isinstance(raw.get("source"), str) else None,
           "raw_index": index}
    # 视频帧框：保留帧路径/时间，裁图按帧取图（图像框不写该字段）
    if isinstance(frame_path, str) and frame_path.strip():
        box["frame_path"] = frame_path.strip()
        if raw.get("frame_timestamp") is not None:
            box["frame_timestamp"] = raw.get("frame_timestamp")
    return box


def crop_rects(box: Dict[str, Any], image_width: int, image_height: int,
               padding: float = 0.0) -> Tuple[Tuple[int, int, int, int],
                                              Tuple[int, int, int, int]]:
    """框的 ``(外扩矩形, 框内矩形)``，均夹紧到图像范围内。

    ``padding`` 为相对框宽/高的扩边比例（0 = 紧贴框）。外扩后仍有框内区域，
    供「背景抑制」把框外环形区域填成纯色；紧贴框时两者相同。
    """
    bbox = box.get("bbox") or {}
    coord = str(bbox.get("coord") or "xywh-pixel")
    x, y = float(bbox.get("x", 0)), float(bbox.get("y", 0))
    width, height = float(bbox.get("w", 0)), float(bbox.get("h", 0))
    if coord.endswith("-normalized"):
        x1 = x * image_width
        y1 = y * image_height
        x2 = (x + width) * image_width
        y2 = (y + height) * image_height
    else:
        x1, y1, x2, y2 = x, y, x + width, y + height

    left = max(0, min(int(round(x1)), int(image_width)))
    top = max(0, min(int(round(y1)), int(image_height)))
    right = max(0, min(int(round(x2)), int(image_width)))
    bottom = max(0, min(int(round(y2)), int(image_height)))
    if right <= left or bottom <= top:
        raise BoxError("候选框裁剪区域为空（%r）" % (box.get("box_id"),))
    inner = (left, top, right, bottom)

    ratio = float(padding or 0.0)
    if ratio <= 0:
        return inner, inner
    pad_x = int(round((right - left) * ratio))
    pad_y = int(round((bottom - top) * ratio))
    outer = (max(0, left - pad_x), max(0, top - pad_y),
             min(int(image_width), right + pad_x), min(int(image_height), bottom + pad_y))
    if outer[2] <= outer[0] or outer[3] <= outer[1]:
        return inner, inner
    return outer, inner


def crop_rect(box: Dict[str, Any], image_width: int, image_height: int,
              padding: float = 0.0) -> Tuple[int, int, int, int]:
    """把规范化框换算成像素裁剪矩形（可外扩），并夹紧到图像范围内。"""
    return crop_rects(box, image_width, image_height, padding)[0]


# --------------------------------------------------------------------------- #
# 媒体解析
# --------------------------------------------------------------------------- #

def resolve_media_file(media_roots: Sequence[Path], relative: str) -> Optional[Path]:
    """在给定根目录中定位媒体文件；路径形式非法时抛 ``UnsupportedMediaPath``。

    返回第一个合法候选（即使文件不存在），便于把「声明了但缺失」记进原始隔离区。
    """
    first: Optional[Path] = None
    for root in media_roots:
        candidate = io_utils.resolve_media(Path(root), relative)
        if first is None:
            first = candidate
        if candidate.is_file():
            return candidate
    return first


def box_image_path(box: Dict[str, Any], primary: str) -> str:
    """候选框的裁图源：视频帧框取 ``frame_path``，图像框取主图像。"""
    frame = box.get("frame_path")
    return frame if isinstance(frame, str) and frame.strip() else primary


def encode_crops_by_source(encoder: Any, boxes: Sequence[Dict[str, Any]],
                           source_paths: Sequence[str],
                           resolved: Dict[str, Optional[Path]],
                           crop_options: Any = None
                           ) -> List[Optional[Sequence[float]]]:
    """按裁图源分组编码候选框，返回与 ``boxes`` 等长、逐框对齐的向量列表。

    同一帧的多个框只打开一次图像；缺失/不可读的源整组记 ``None``（计入
    ``crops_failed``），绝不用别的图像顶替，也不伪造分数。
    ``crop_options`` 携带扩边比例与背景抑制方式（见 ``schemas.CropConfig``）。
    """
    groups: Dict[str, List[int]] = {}
    for index, path in enumerate(source_paths):
        groups.setdefault(path, []).append(index)
    vectors: List[Optional[Sequence[float]]] = [None] * len(boxes)
    options = {"padding_ratio": getattr(crop_options, "padding_ratio", 0.0),
               "background_suppression": getattr(crop_options, "background_suppression",
                                                  "none")}
    custom_crop = bool(options["padding_ratio"]) or options["background_suppression"] != "none"
    for path, indices in groups.items():
        target = resolved.get(path)
        if target is None or not Path(target).is_file():
            continue
        subset = [boxes[index] for index in indices]
        if custom_crop:
            group_vectors, _errors = encoder.encode_crops(target, subset,
                                                          crop_options=options)
        else:
            group_vectors, _errors = encoder.encode_crops(target, subset)
        for position, vector in zip(indices, group_vectors):
            vectors[position] = vector
    return vectors


# --------------------------------------------------------------------------- #
# 节点与台账
# --------------------------------------------------------------------------- #

def base_node(*, status: str, action: str, reason: str, label: str,
              destination: str, route_enabled: bool, min_clip_score: float,
              tokenizer_mode: str, **extra: Any) -> Dict[str, Any]:
    node: Dict[str, Any] = {
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(status),
        "process_status": status,
        "route_enabled": bool(route_enabled),
        "action": action,
        "reason": reason,
        "label": label,
        "destination": destination,
        "correspondence_found": False,
        "prefilled": False,
        "degraded": False,
        "clip_model": CLIP_MODEL_NAME,
        "clip_model_version": None,
        "min_clip_score": float(min_clip_score),
        "tokenizer_mode": tokenizer_mode,
        "tokenizer_source": None,
        "tokenizer_backend": None,
        "token_count": 0,
        "tokens_dropped": 0,
        "tokens_dropped_detail": {},
        "tokens_truncated": False,
        "phrases_total": 0,
        "phrases_used": 0,
        "token_variants": 0,
        "crop_padding_ratio": 0.0,
        "background_suppression": "none",
        "boxes_received": 0,
        "boxes_used": 0,
        "boxes_invalid": 0,
        "crops_scored": 0,
        "crops_failed": 0,
        "relations": [],
        "relations_total": 0,
        "relations_prefilled": 0,
        "relations_pending": 0,
        "class_profile": {},
        "best_score": None,
        "image_path": None,
        "image_source": None,
        "description_source": None,
        "boxes_source": None,
        "model": None,
        "human_verified": False,
        "review_required": True,
        "deliverable": False,
        "scored_at": None,
        "detail": None,
    }
    node.update(extra)
    return node


def relation_entry(phrase: str, box: Optional[Dict[str, Any]],
                   score: Optional[float], confirmation_method: str,
                   reason: Optional[str], status: str,
                   best_score: Optional[float] = None,
                   matched_text: Optional[str] = None,
                   variant: str = "word",
                   phrase_tokens: Optional[Sequence[str]] = None,
                   assignment_score: Optional[float] = None) -> Dict[str, Any]:
    """一条词-区域关系（人工确认前一律为候选）。

    ``phrase`` 始终是分词得到的**词**（一词一框）；``matched_text`` 记录该分数
    实际用到的匹配文本：词本身（``variant=word``）或其所在名词短语
    （``variant=noun_phrase``，``phrase_tokens`` 为短语覆盖的词）。
    ``assignment_score`` 是按类别校准后用于指派的分数（仅解释用途，落盘判定仍用
    ``clip_score``）。
    """
    return {"phrase": phrase,
            "box_id": box["box_id"] if box else None,
            "bbox": dict(box["bbox"]) if box else None,
            "class_name": box.get("class_name") if box else None,
            "clip_score": scoring.round_score(score),
            "best_score": scoring.round_score(best_score if best_score is not None else score),
            "assignment_score": scoring.round_score(assignment_score),
            "matched_text": matched_text or phrase,
            "variant": variant,
            "phrase_tokens": list(phrase_tokens or [phrase]),
            "confirmation_method": confirmation_method,
            # [v4.2] status 双字段：int status + 原字符串迁入 process_status
            "status": status_int(status),
            "process_status": status,
            "reason": reason,
            "human_verified": False,
            "review_required": True,
            "source_reference": "operator_18"}


def make_ledger_entry(*, line: int, spec: Optional[Dict[str, Any]], node: Dict[str, Any],
                      detail: Optional[str] = None, detected_at: str = "") -> Dict[str, Any]:
    """拒绝清单/隔离台账/待人工台账的统一条目结构。"""
    evidence = {"relations_total": node.get("relations_total"),
                "relations_prefilled": node.get("relations_prefilled"),
                "boxes_received": node.get("boxes_received"),
                "boxes_used": node.get("boxes_used"),
                "boxes_invalid": node.get("boxes_invalid"),
                "crops_failed": node.get("crops_failed"),
                "token_count": node.get("token_count"),
                "tokens_dropped": node.get("tokens_dropped"),
                "phrases_total": node.get("phrases_total"),
                "phrases_used": node.get("phrases_used"),
                "best_score": node.get("best_score"),
                "min_clip_score": node.get("min_clip_score"),
                "clip_model": node.get("clip_model"),
                "tokenizer_source": node.get("tokenizer_source"),
                "tokenizer_backend": node.get("tokenizer_backend"),
                "image_path": node.get("image_path")}
    return {"line": line,
            "identifier": (spec or {}).get("identifier"),
            "sample_id": (spec or {}).get("sample_id"),
            "pair_id": (spec or {}).get("pair_id"),
            "operator": OPERATOR_NAME,
            "operator_number": FORMAL_OPERATOR_NUMBER,
            "operator_version": OPERATOR_VERSION,
            "stage_id": STAGE_ID,
            # [v4.2] status 双字段：int status + 原字符串迁入 process_status
            "status": node.get("status"),
            "process_status": node.get("process_status"),
            "action": node.get("action"),
            "reason": node.get("reason"),
            "label": node.get("label"),
            "destination": node.get("destination"),
            "human_verified": False,
            "review_required": True,
            "deliverable": False,
            "detail": detail or node.get("detail"),
            "evidence": evidence,
            "detected_at": detected_at,
            "source_reference": "operator_18"}


def attach_node(record: Dict[str, Any], node: Dict[str, Any]) -> Dict[str, Any]:
    """把算子结果写回记录：``processing["18"]`` 为权威节点，另加扁平摘要字段。"""
    output = dict(record)
    processing = output.get("processing")
    processing = dict(processing) if isinstance(processing, dict) else {}
    processing["18"] = node
    output["processing"] = processing
    output["phrase_box_relations"] = list(node.get("relations") or [])
    output["operator_18"] = {
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": node.get("status"),
        "process_status": node.get("process_status"),
        "decision": node.get("action"),
        "reason_code": node.get("reason"),
        "evidence": {"relations_total": node.get("relations_total"),
                     "relations_prefilled": node.get("relations_prefilled"),
                     "boxes_used": node.get("boxes_used"),
                     "phrases_total": node.get("phrases_total"),
                     "phrases_used": node.get("phrases_used"),
                     "best_score": node.get("best_score"),
                     "min_clip_score": node.get("min_clip_score"),
                     "clip_model": node.get("clip_model"),
                     "tokenizer_source": node.get("tokenizer_source")},
        "human_verified": False,
        "review_required": True,
        "deliverable": False,
        "source_reference": "operator_18",
    }
    return output


# --------------------------------------------------------------------------- #
# 节点构造
# --------------------------------------------------------------------------- #

def _new_node(config: Any, *, status: str, action: str, reason: str, label: str,
              destination: str, **extra: Any) -> Dict[str, Any]:
    return base_node(status=status, action=action, reason=reason, label=label,
                     destination=destination, route_enabled=bool(config.enabled),
                     min_clip_score=float(config.min_clip_score),
                     tokenizer_mode=config.tokenizer,
                     crop_padding_ratio=float(getattr(config.crop, "padding_ratio", 0.0)),
                     background_suppression=getattr(config.crop, "background_suppression",
                                                    "none"),
                     **extra)


def skipped_node(config: Any) -> Dict[str, Any]:
    """工具关闭：不拦样本、不产细粒度对应。"""
    return _new_node(config, status=STATUS_SKIPPED, action=ACTION_SKIP,
                     reason=REASON_ROUTE_DISABLED, label=LABEL_FLOW_DISABLED,
                     destination=DEST_PIPELINE,
                     detail="局部对局部工具未启用：原样放行且不产生细粒度对应")


def origin_node(config: Any, reason: str, label: str, **extra: Any) -> Dict[str, Any]:
    return _new_node(config, status=STATUS_ISOLATED, action=ACTION_ORIGIN,
                     reason=reason, label=label, destination=DEST_ORIGIN, **extra)


def sample_isolated_node(config: Any, reason: str, **extra: Any) -> Dict[str, Any]:
    return _new_node(config, status=STATUS_ISOLATED, action=ACTION_ISOLATE,
                     reason=reason, label=LABEL_SEMANTIC_UNMATCHED,
                     destination=DEST_SAMPLE, **extra)


def pending_node(config: Any, reason: str, label: str, status: str = STATUS_PENDING_MANUAL,
                 **extra: Any) -> Dict[str, Any]:
    return _new_node(config, status=status, action=ACTION_PENDING, reason=reason,
                     label=label, destination=DEST_MANUAL, **extra)


# --------------------------------------------------------------------------- #
# 单条记录评估
# --------------------------------------------------------------------------- #

def _outcome(node: Dict[str, Any], record: Dict[str, Any],
             spec: Optional[Dict[str, Any]] = None, *, line: int = 0,
             attach: bool = True, ledger: bool = True,
             event_time: str = "") -> Dict[str, Any]:
    output = attach_node(record, node) if attach else None
    entry = (make_ledger_entry(line=line, spec=spec, node=node, detected_at=event_time)
             if ledger else None)
    return {"status": node["status"], "action": node["action"], "reason": node["reason"],
            "label": node["label"], "destination": node["destination"],
            "node": node, "output": output, "ledger": entry,
            "degraded": bool(node.get("degraded"))}


def evaluate_record(spec: Dict[str, Any], line: int, *,
                    media_roots: Sequence[Path], config: Any,
                    encoder: Any = None, event_time: str = "") -> Dict[str, Any]:
    """按表1 #18 口径评估一条记录，返回动作、节点与台账条目。

    分支顺序（决定计数口径）：
    1. 工具关闭 → 跳过；
    2. 缺描述 / 缺图像引用 → **流程状态**，留产线并进人工队列；
    3. 图像文件缺失 / 引用非法 / 全部裁图不可读 → **原始隔离区**；
    4. 无可用候选框 → **流程状态**（给人工空白列表）；
    5. 分词后无有效词 → **样本-语义不通**，进样本隔离库；
    6. 模型不可用 → 严格模式显式失败，或按配置**显式降级**（不产生任何分数）；
    7. 词×框余弦 → 一对一匹配 → 达到阈值预填，否则待人工。
    """
    record = spec["record"]

    # 1. 工具关闭
    if not config.enabled:
        node = skipped_node(config)
        return _outcome(node, record, spec, line=line, ledger=False,
                        event_time=event_time)

    image_ref = spec["image"]
    description = spec["description"]

    # 2. 流程状态：缺描述（描述整体等于文件名时不算缺描述，见第 5 步）
    if description["value"] is None and description.get("reason") != REASON_DESCRIPTION_IS_FILENAME:
        node = pending_node(config, REASON_MISSING_DESCRIPTION,
                            LABEL_FLOW_MISSING_DESCRIPTION,
                            image_source=image_ref["source"] if image_ref else None,
                            description_source=description.get("source"),
                            boxes_source=spec["boxes_source"],
                            boxes_received=len(spec["boxes"]),
                            detail="缺少可用的描述文本，无法分词，留产线待补")
        return _outcome(node, record, spec, line=line, event_time=event_time)

    if image_ref is None:
        # 视频帧框自带裁图源：缺独立图像引用时退回首个 frame_path，不再判「无图像」
        frame_boxes = [item for item in spec["boxes"]
                       if isinstance(item, dict)
                       and isinstance(item.get("frame_path"), str)
                       and item.get("frame_path").strip()]
        if frame_boxes:
            chosen = None
            for item in frame_boxes:
                try:
                    located = resolve_media_file(media_roots, item["frame_path"].strip())
                except io_utils.UnsupportedMediaPath:
                    located = None
                if located is not None and Path(located).is_file():
                    chosen = item["frame_path"].strip()
                    break
            image_ref = {"path": chosen or frame_boxes[0]["frame_path"].strip(),
                         "source": "boxes.frame_path"}
        else:
            node = pending_node(config, REASON_NO_IMAGE_REFERENCE, LABEL_FLOW_NO_IMAGE,
                                description_source=description.get("source"),
                                boxes_source=spec["boxes_source"],
                                boxes_received=len(spec["boxes"]),
                                detail="缺少图像引用，无法裁剪局部区域，留产线待补")
            return _outcome(node, record, spec, line=line, event_time=event_time)

    # 3. 原始隔离区：媒体引用非法 / 文件缺失
    try:
        image_path = resolve_media_file(media_roots, image_ref["path"])
    except io_utils.UnsupportedMediaPath as exc:
        node = origin_node(config, REASON_UNSUPPORTED_MEDIA, LABEL_ORIGIN_UNPARSABLE,
                           image_source=image_ref["source"],
                           description_source=description.get("source"),
                           boxes_source=spec["boxes_source"],
                           boxes_received=len(spec["boxes"]),
                           detail=str(exc))
        return _outcome(node, record, spec, line=line, event_time=event_time)

    if image_path is None or not image_path.is_file():
        node = origin_node(config, REASON_IMAGE_MISSING, LABEL_ORIGIN_UNPARSABLE,
                           image_path=image_ref["path"],
                           image_source=image_ref["source"],
                           description_source=description.get("source"),
                           boxes_source=spec["boxes_source"],
                           boxes_received=len(spec["boxes"]),
                           detail="声明的图像文件不存在：%s" % image_ref["path"])
        return _outcome(node, record, spec, line=line, event_time=event_time)

    # 4. 候选框规范化；坏框计入 boxes_invalid，不静默丢弃
    boxes: List[Dict[str, Any]] = []
    boxes_invalid = 0
    for index, raw in enumerate(spec["boxes"]):
        try:
            boxes.append(normalize_box(raw, index))
        except BoxError:
            boxes_invalid += 1

    # 裁图源：图像框用主图像；视频帧框用各自 frame_path（缺失记 crops_failed）
    source_paths = [box_image_path(box, image_ref["path"]) for box in boxes]
    resolved: Dict[str, Optional[Path]] = {image_ref["path"]: image_path}
    for path in source_paths:
        if path in resolved:
            continue
        try:
            resolved[path] = resolve_media_file(media_roots, path)
        except io_utils.UnsupportedMediaPath:
            resolved[path] = None
    frame_scoped = sum(1 for box in boxes if box.get("frame_path"))

    common = {"image_path": image_ref["path"],
              "image_source": image_ref["source"],
              "description_source": description.get("source"),
              "boxes_source": spec["boxes_source"],
              "boxes_received": len(spec["boxes"]),
              "boxes_invalid": boxes_invalid,
              "crop_sources": len(set(source_paths)),
              "frame_scoped_boxes": frame_scoped}

    if not boxes:
        node = pending_node(config, REASON_NO_BOXES, LABEL_FLOW_NO_BOXES, **dict(
            common, detail="没有可用候选框：向人工提供空白列表"))
        return _outcome(node, record, spec, line=line, event_time=event_time)

    # 5. 分词；无有效词 → 样本-语义不通（工具开启后没有对应关系）
    report = tokenization.tokenize_description(
        description["value"] or "",
        image_path=image_ref["path"],
        upstream_tokens=spec["upstream_tokens"],
        mode=config.tokenizer,
        max_tokens=int(config.max_tokens),
        max_chars=int(config.max_text_chars),
        merge_phrases_enabled=bool(config.merge_phrases))
    tokens = report["tokens"]
    token_common = dict(common,
                        tokenizer_source=report["source"],
                        tokenizer_backend=report["backend"],
                        token_count=len(tokens),
                        tokens_dropped=report["dropped"],
                        tokens_dropped_detail=report["dropped_detail"],
                        tokens_truncated=report["truncated"],
                        phrases_total=report["phrase_count"],
                        token_variants=report["variant_count"])

    if not tokens:
        node = sample_isolated_node(
            config, REASON_NO_VALID_TOKENS, **dict(
                token_common,
                detail="工具开启后描述无法产生任何有效词（或描述即文件名），"
                       "不存在词-区域对应关系"))
        return _outcome(node, record, spec, line=line, event_time=event_time)

    # 6. 模型不可用：严格失败 or 显式降级（不产生任何分数）
    if encoder is None:
        if not config.allow_degraded:
            raise DependencyError(
                "CLIP 不可用且未开启显式降级：拒绝伪造分数（uniqueId=%s, line=%d）"
                % (spec.get("identifier"), line))
        node = pending_node(config, REASON_MODEL_UNAVAILABLE, LABEL_FLOW_PENDING_MANUAL,
                            status=STATUS_DEGRADED, degraded=True,
                            model=None, **dict(
                                token_common,
                                detail="CLIP 不可用：按配置显式降级为待人工，"
                                       "不产生任何相似度分数"))
        return _outcome(node, record, spec, line=line, event_time=event_time)

    model_info = _safe_identity(encoder)

    # 7. 词（+名词短语上下文）-区域打分
    variants = report.get("variants") or [[token] for token in tokens]
    phrase_texts = {item["text"]: item for item in report.get("phrases") or []}
    variant_texts = list(dict.fromkeys(text for unit in variants for text in unit))
    try:
        text_vectors = list(encoder.encode_texts(variant_texts))
        crop_vectors = encode_crops_by_source(encoder, boxes, source_paths, resolved,
                                              crop_options=config.crop)
    except Exception as exc:                       # noqa: BLE001 - 记录真实失败原因
        node = pending_node(config, REASON_SCORE_ERROR, LABEL_FLOW_PENDING_MANUAL,
                            model=model_info, **dict(
                                token_common,
                                detail="打分失败（%s）：不伪造分数，转待人工"
                                       % type(exc).__name__))
        return _outcome(node, record, spec, line=line, event_time=event_time)

    if len(text_vectors) != len(variant_texts) or len(crop_vectors) != len(boxes):
        node = pending_node(config, REASON_SCORE_ERROR, LABEL_FLOW_PENDING_MANUAL,
                            model=model_info, **dict(
                                token_common,
                                detail="编码器返回维度不一致：不伪造分数，转待人工"))
        return _outcome(node, record, spec, line=line, event_time=event_time)

    usable: List[Tuple[Dict[str, Any], Sequence[float]]] = []
    for box, vector in zip(boxes, crop_vectors):
        if vector is None:
            continue
        usable.append((box, vector))
    crops_failed = len(boxes) - len(usable)

    if not usable:
        node = origin_node(config, REASON_IMAGE_UNREADABLE, LABEL_ORIGIN_BROKEN,
                           model=model_info, crops_failed=crops_failed,
                           **token_common,
                           detail="全部候选框均无法从图像裁剪出有效区域")
        return _outcome(node, record, spec, line=line, event_time=event_time)

    vectors_by_text = dict(zip(variant_texts, text_vectors))
    try:
        # 每个词对每个框取「词本身 vs 所在名词短语」中的最高分；一词一框不变
        scores: List[List[Optional[float]]] = []
        winners: List[List[Optional[str]]] = []
        for unit in variants:
            row: List[Optional[float]] = []
            row_text: List[Optional[str]] = []
            for _box, vector in usable:
                best_score: Optional[float] = None
                best_text: Optional[str] = None
                for text in unit:
                    value = scoring.cosine_similarity(vectors_by_text[text], vector)
                    if best_score is None or value > best_score:
                        best_score, best_text = value, text
                row.append(best_score)
                row_text.append(best_text)
            scores.append(row)
            winners.append(row_text)
    except ValueError:
        node = pending_node(config, REASON_SCORE_ERROR, LABEL_FLOW_PENDING_MANUAL,
                            model=model_info, **dict(
                                token_common, crops_failed=crops_failed,
                                detail="向量维度异常：不伪造分数，转待人工"))
        return _outcome(node, record, spec, line=line, event_time=event_time)

    # 6.5 按类别校准：只影响「词→框」指派，不改原始余弦与 0.25 阈值判定
    calibration_stats: Dict[str, Dict[str, Any]] = {}
    assignment_scores = scores
    calibration_method = getattr(config, "class_calibration", scoring.CALIBRATION_NONE)
    calibration_node: Dict[str, Any] = {"method": calibration_method, "classes": 0}
    class_names = [box.get("class_name") for box, _vector in usable]
    if calibration_method == scoring.CALIBRATION_PRIOR:
        # 类别名先验：把「匹配文本 vs 框类别名」的余弦作为二次排序项，
        # 纠正大而常见目标抢词（yellow surfboard 被 person 框拿走）。
        known = sorted({str(name) for name in class_names if name})
        class_vectors = dict(zip(known, encoder.encode_texts(known))) if known else {}
        assignment_scores, calibration_node = scoring.class_prior(
            scores, vectors_by_text, winners, class_names, class_vectors,
            weight=float(getattr(config, "class_prior_weight", 0.2)))
        calibration_node["classes"] = len(class_vectors)
    elif calibration_method != scoring.CALIBRATION_NONE:
        assignment_scores, calibration_stats = scoring.class_calibrate(
            scores, class_names, calibration_method)
        calibration_node = {"method": calibration_method,
                            "classes": len(calibration_stats)}
    assignment = scoring.assign_one_to_one(assignment_scores)
    relations: List[Dict[str, Any]] = []
    for index, token in enumerate(tokens):
        row = scores[index]
        row_best = max(row) if row else None
        target = assignment[index]
        if target is None:
            matched = winners[index][0] if winners[index] else None
            phrase_hit = phrase_texts.get(matched or "")
            relations.append(relation_entry(
                token, None, None, CONFIRMATION_PENDING_MANUAL,
                REASON_NO_AVAILABLE_REGION, STATUS_PENDING_MANUAL, best_score=row_best,
                matched_text=matched, variant=_variant_of(matched, token),
                phrase_tokens=(phrase_hit or {}).get("tokens")))
            continue
        box = usable[target][0]
        score = row[target]
        assignment_score = assignment_scores[index][target]
        matched = winners[index][target]
        phrase_hit = phrase_texts.get(matched or "")
        variant = _variant_of(matched, token)
        if scoring.meets_threshold(score, config.min_clip_score):
            relations.append(relation_entry(
                token, box, score, CONFIRMATION_AUTO_PREFILL, None, STATUS_PREFILLED,
                matched_text=matched, variant=variant,
                phrase_tokens=(phrase_hit or {}).get("tokens"),
                assignment_score=assignment_score))
        else:
            relations.append(relation_entry(
                token, box, score, CONFIRMATION_PENDING_MANUAL,
                REASON_BELOW_THRESHOLD, STATUS_PENDING_MANUAL,
                matched_text=matched, variant=variant,
                phrase_tokens=(phrase_hit or {}).get("tokens"),
                assignment_score=assignment_score))

    relations_prefilled = sum(1 for item in relations
                              # [v4.2] 字符串态已迁入 process_status
                              if item["process_status"] == STATUS_PREFILLED)
    relations_pending = len(relations) - relations_prefilled
    phrases_used = sum(1 for item in relations if item.get("variant") == "noun_phrase")
    class_profile: Dict[str, Dict[str, Any]] = {}
    for item in relations:
        name = item.get("class_name")
        if not name:
            continue
        bucket = class_profile.setdefault(str(name), {"relations": 0, "prefilled": 0,
                                                      "best_score": None})
        bucket["relations"] += 1
        if item["process_status"] == STATUS_PREFILLED:
            bucket["prefilled"] += 1
        score = item.get("clip_score")
        if score is not None and (bucket["best_score"] is None or score > bucket["best_score"]):
            bucket["best_score"] = score
    # 类别校准统计（该类别在本样本所有框上的原始分均值/标准差），随 class_profile 输出
    for name, stat in calibration_stats.items():
        if not name or not isinstance(stat, dict):
            continue
        bucket = class_profile.setdefault(str(name), {"relations": 0, "prefilled": 0,
                                                      "best_score": None})
        bucket["calibration"] = {"method": calibration_method,
                                 "mean": stat.get("mean"),
                                 "std": stat.get("std"),
                                 "scores": stat.get("scores")}
    best = scoring.round_score(max((item["clip_score"] for item in relations
                                    if item["clip_score"] is not None), default=None))
    scored_common = dict(token_common,
                         boxes_used=len(usable),
                         crops_scored=len(usable),
                         crops_failed=crops_failed,
                         relations=relations,
                         relations_total=len(relations),
                         relations_prefilled=relations_prefilled,
                         relations_pending=relations_pending,
                         phrases_used=phrases_used,
                         class_profile=class_profile,
                         class_calibration=calibration_node,
                         best_score=best,
                         correspondence_found=True,
                         model=model_info,
                         clip_model_version=(model_info or {}).get("version"),
                         scored_at=event_time)

    if relations_prefilled:
        node = _new_node(config, status=STATUS_PREFILLED, action=ACTION_PREFILL,
                         reason=CONFIRMATION_AUTO_PREFILL,
                         label=LABEL_SCORE_PENDING_HUMAN,
                         destination=DEST_MANUAL, prefilled=True, **scored_common)
    else:
        node = pending_node(config, REASON_BELOW_THRESHOLD, LABEL_FLOW_PENDING_MANUAL,
                            **scored_common)
    return _outcome(node, record, spec, line=line, event_time=event_time)


def _variant_of(matched: Optional[str], token: str) -> str:
    """匹配文本是词本身还是名词短语上下文。"""
    return "word" if not matched or matched == token else "noun_phrase"


def _safe_identity(encoder: Any) -> Optional[Dict[str, Any]]:
    try:
        identity = encoder.model_identity()
    except Exception:                              # noqa: BLE001
        return None
    return identity if isinstance(identity, dict) else None
