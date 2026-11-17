"""算子 15「评分时空一致性」业务规则模块（表1 #15，规则型，无模型）。

表1 #15 冻结口径
----------------
- **类型**：规则型；**默认关闭**；可选；优先级 P1；**无模型**（``model=null``，不产生模型分数）。
- **输入**：视频帧时间戳、语音片段时间戳、字幕/文本时间戳；也接受无时间戳的图文记录。
- **处理边界**：开关打开时用**线性插值**校准时间差，画面/声音/字幕时间差默认不超过 **0.5 秒**
  （可配 0.2–2.0）。三者缺一则本样本 **不适用**，不隔离，也不能写成时序已通过。
  纯图文不执行时序判定。不做目标检测、OCR、长文本配多图。
- **输出与去向**：
  - 时序通过 → 主产物 ``15_temporal_alignment.jsonl``（留样本池，交 #19 / #16）；
  - 时间差超过上限 → ``15_sample_quarantine.jsonl``（标签 ``样本-语义不通``，交 #21 样本隔离库）；
  - 缺任一时间轴（含纯图文）→ 主产物内 ``status=not_applicable``（标签 ``流程-时序不适用``，不隔离）；
  - 损坏/缺字段/时间戳不可解析/sidecar 不可读/编码异常 → ``15_rejects.jsonl``（原始隔离区）。
- **插值**：以 ``temporal_anchors`` 的 (source_sec → video_sec) 锚点对构建线性时间映射，
  默认分段线性插值（区间外按边界段斜率外推），可选全局最小二乘直线。
  锚点不足时显式标记 ``interpolation.applied=false`` 并按**原始时间轴**计算真实差值，
  绝不伪造“已校准”；``require_calibration=true`` 时改判为待人工校准（留产线，不隔离）。

本模块只做规则计算与台账，不加载任何模型、不联网、不生成随机或固定分数。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils import io as io_utils
from utils.schemas import FORMAL_OPERATOR_NUMBER

# --------------------------------------------------------------------------- #
# 身份与产物名
# --------------------------------------------------------------------------- #

OPERATOR = "temporal_consistency"
OPERATOR_VERSION = "v1.0.0"
# ``FORMAL_OPERATOR_NUMBER`` 由 utils.schemas 导入（编号单一来源，供配置校验与业务常量共用）
STAGE_ID = "S3"

MAIN_NAME_ENABLED = "15_temporal_alignment.jsonl"
MAIN_NAME_DISABLED = "15_passthrough.jsonl"
SAMPLE_QUARANTINE_NAME = "15_sample_quarantine.jsonl"
PENDING_NAME = "15_pending.jsonl"
REJECTS_NAME = "15_rejects.jsonl"

#: 表1 来源功能点（FGW/Cosmic）
SOURCE_REFERENCE = "F-ALIGN-039；F-ALIGN-040"

# --------------------------------------------------------------------------- #
# 状态 / 标签 / 动作 / 去向（表1 统一台账字段口径）
# --------------------------------------------------------------------------- #

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_PENDING_CALIBRATION = "pending_calibration"
STATUS_SKIPPED = "skipped"

# [v4.2] status 双字段（§2.10）：输出记录 int status（判定型：1=通过/不适用/待人工，
# 0=不通过/失败）+ 原字符串值迁入 process_status，内部判定逻辑仍用字符串常量。
_STATUS_AS_INT = {
    STATUS_PASSED: 1,
    STATUS_FAILED: 0,
    STATUS_NOT_APPLICABLE: 1,
    STATUS_PENDING_CALIBRATION: 1,
    STATUS_SKIPPED: 1,
}


def status_int(value: Optional[str]) -> int:
    """记录级字符串状态 → [v4.2] int status；未登记的一律按不通过 0。"""
    return _STATUS_AS_INT.get(value, 0)

LABEL_PASS = "样本-评分：通过"
LABEL_FAIL = "样本-语义不通"
LABEL_NOT_APPLICABLE = "流程-时序不适用"
LABEL_PENDING_CALIBRATION = "流程-时序待人工校准"
LABEL_ORIGIN_UNPARSABLE = "原始文件-无法解析"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_ORIGIN_DAMAGED = "原始文件-损坏"
LABEL_ORIGIN_ENCODING = "原始文件-编码异常"

ACTION_KEEP = "keep"
ACTION_ISOLATE = "isolate"
ACTION_RECORD = "record"
ACTION_WAITING_MANUAL = "waiting_manual"

DEST_SAMPLE_POOL = "sample_pool"
DEST_SAMPLE_QUARANTINE = "sample_quarantine"
DEST_ORIGIN_QUARANTINE = "origin_quarantine"
DEST_PIPELINE_WAIT = "pipeline_wait"

# 原因码
REASON_JSON_PARSE_ERROR = "json_parse_error"
REASON_RECORD_NOT_OBJECT = "record_not_object"
REASON_MISSING_SAMPLE_ID = "missing_sample_id"
REASON_NO_TIMESTAMP = "no_timestamp_timeline"
REASON_MISSING_VIDEO_TIMELINE = "missing_video_timeline"
REASON_MISSING_AUDIO_TIMELINE = "missing_audio_timeline"
REASON_MISSING_TEXT_TIMELINE = "missing_text_timeline"
REASON_NO_BINDING = "no_temporal_binding"
REASON_MAX_DELTA_EXCEEDED = "max_delta_exceeded"
REASON_TIMELINE_INVALID = "timeline_invalid"
REASON_TIMELINE_UNREADABLE = "timeline_unreadable"
REASON_ENCODING_ERROR = "encoding_error"
REASON_CALIBRATION_UNAVAILABLE = "calibration_unavailable"
REASON_TOO_MANY_BINDINGS = "too_many_bindings"
REASON_ROUTE_DISABLED = "route_disabled"

#: 三段时间轴
TIMELINE_KINDS = ("video", "audio", "text")

_ID_KEYS = ("id", "frame_id", "segment_id", "unit_id", "key", "name")
_VIDEO_TIME_KEYS = ("timestamp_sec", "time_sec", "ts_sec", "sec", "timestamp")
_SEGMENT_START_KEYS = ("start_sec", "start", "begin_sec", "from_sec")
_SEGMENT_END_KEYS = ("end_sec", "end", "stop_sec", "to_sec")

_TIMELINE_ALIASES = {
    "video": ("video", "video_timeline", "frames"),
    "audio": ("audio", "audio_timeline", "speech"),
    "text": ("text", "text_timeline", "subtitle", "subtitle_timeline", "subs"),
}


class TimelineError(ValueError):
    """时间轴或时间戳非法（→ 原始隔离区，不静默丢弃）。"""


class TimelineUnreadable(TimelineError):
    """时间轴 sidecar 无法读取或不是合法 JSON。"""


class BindingLimitExceeded(TimelineError):
    """单条记录的时序绑定数超过配置上限（→ 原始隔离区）。"""


# --------------------------------------------------------------------------- #
# 数值与取值工具
# --------------------------------------------------------------------------- #

def to_seconds(value: Any, field_name: str) -> float:
    """把时间戳规整为有限非负浮点秒；不可解析直接抛 ``TimelineError``。"""
    if isinstance(value, bool):
        raise TimelineError("%s 不允许布尔值：%r" % (field_name, value))
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise TimelineError("%s 不是可解析的时间戳：%r" % (field_name, value)) from exc
    else:
        raise TimelineError("%s 必须是数字：%r" % (field_name, value))
    if number != number or number in (float("inf"), float("-inf")):
        raise TimelineError("%s 必须是有限数字：%r" % (field_name, value))
    if number < 0:
        raise TimelineError("%s 不能为负：%r" % (field_name, value))
    return number


def _pick(mapping: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


# --------------------------------------------------------------------------- #
# 时间轴
# --------------------------------------------------------------------------- #

class Timeline:
    """单条时间轴：有序时间点 + 按 id/下标检索。"""

    def __init__(self, kind: str, points: List[Dict[str, Any]]):
        self.kind = kind
        self.points = points
        self.by_key: Dict[str, Dict[str, Any]] = {}
        for index, point in enumerate(points):
            self.by_key.setdefault(point["key"], point)
            self.by_key.setdefault("#%d" % index, point)

    def __len__(self) -> int:
        return len(self.points)

    def get(self, reference: Any, *, binding_id: str) -> Dict[str, Any]:
        """按 id（字符串）或下标（整数 / 数字字符串）取时间点。"""
        if isinstance(reference, bool):
            raise TimelineError("绑定 %s 的 %s 引用非法：%r" % (binding_id, self.kind, reference))
        if isinstance(reference, int):
            if 0 <= reference < len(self.points):
                return self.points[reference]
            raise TimelineError("绑定 %s 的 %s 下标越界：%r" % (binding_id, self.kind, reference))
        text = str(reference).strip()
        point = self.by_key.get(text)
        if point is None and text.lstrip("-").isdigit():
            index = int(text)
            if 0 <= index < len(self.points):
                point = self.points[index]
        if point is None:
            raise TimelineError("绑定 %s 引用了不存在的 %s 时间点：%s"
                                % (binding_id, self.kind, reference))
        return point


def normalize_timeline(entries: Any, kind: str) -> Optional[Timeline]:
    """把原始条目规整为 ``Timeline``；``None`` 表示该时间轴缺失或为空。"""
    if entries is None:
        return None
    if not isinstance(entries, list):
        raise TimelineError("%s 时间轴必须是数组，实际 %s" % (kind, type(entries).__name__))
    points: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        field = "%s[%d]" % (kind, index)
        if isinstance(entry, dict):
            key = _pick(entry, _ID_KEYS)
            key = str(key) if key is not None and str(key).strip() else "#%d" % index
            if kind == "video":
                raw_time = _pick(entry, _VIDEO_TIME_KEYS)
                if raw_time is None:
                    raw_time = _pick(entry, _SEGMENT_START_KEYS)
            else:
                raw_time = _pick(entry, _SEGMENT_START_KEYS)
                if raw_time is None:
                    raw_time = _pick(entry, _VIDEO_TIME_KEYS)
            if raw_time is None:
                raise TimelineError("%s 缺少时间戳字段" % field)
            start = to_seconds(raw_time, "%s.time" % field)
            raw_end = _pick(entry, _SEGMENT_END_KEYS)
            end = to_seconds(raw_end, "%s.end" % field) if raw_end is not None else None
            if end is not None and end < start:
                raise TimelineError("%s 的结束时间早于开始时间" % field)
        elif isinstance(entry, (int, float)) and not isinstance(entry, bool):
            key = "#%d" % index
            start = to_seconds(entry, "%s.time" % field)
            end = None
        else:
            raise TimelineError("%s 既不是对象也不是数字：%r" % (field, entry))
        points.append({"key": key, "time": start, "end": end})
    if not points:
        return None
    points.sort(key=lambda item: item["time"])
    return Timeline(kind, points)


def read_timeline_payload(path: Path) -> Dict[str, Any]:
    """读取 sidecar 时间轴 JSON（严格 UTF-8），失败显式抛错。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TimelineUnreadable("时间轴 sidecar 无法读取：%s" % type(exc).__name__) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TimelineUnreadable("时间轴 sidecar 不是 UTF-8") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TimelineUnreadable("时间轴 sidecar 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise TimelineUnreadable("时间轴 sidecar 顶层必须是对象")
    return payload


def resolve_media_file(roots: Sequence[Path], relative: str) -> Optional[Path]:
    """在给定根目录中定位媒体相对路径；找不到返回 ``None``，越界直接抛错。"""
    for root in roots:
        candidate = io_utils.resolve_media(root, relative)
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# 记录解析
# --------------------------------------------------------------------------- #

def record_sample_id(record: Dict[str, Any]) -> Optional[str]:
    """取样本标识：``sample_id`` 优先，兼容 ``sampleId``/``sample_ID``/``id``。"""
    for key in ("sample_id", "sampleId", "sample_ID", "id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _native_pair(record: Dict[str, Any]) -> bool:
    pair_id = record.get('pair_id')
    return (isinstance(pair_id, str) and bool(pair_id.strip())
            and all(isinstance(record.get(side), dict)
                    and isinstance(record[side].get('record'), dict)
                    for side in ('left', 'right')))


def normalize_native_pair(record: Dict[str, Any]) -> Dict[str, Any]:
    """Use the producer's pair identity; retain both native records and provenance."""
    if _native_pair(record) and not record_sample_id(record):
        record['sample_id'] = record['pair_id'].strip()
    return record


def _timeline_source(record: Dict[str, Any], media_roots: Sequence[Path]) -> Tuple[Dict[str, Any], List[str]]:
    """定位时间轴来源：内联 ``timeline`` 对象优先，其次 ``timeline_file`` sidecar。"""
    inline = record.get("timeline")
    if isinstance(inline, dict):
        return inline, []

    reference = record.get("timeline_file")
    if isinstance(reference, str) and reference.strip():
        relative = reference.strip()
        target = resolve_media_file(media_roots, relative)
        if target is None:
            raise TimelineUnreadable("时间轴 sidecar 不存在：%s" % relative)
        return read_timeline_payload(target), [relative]

    # 顶层散列写法：video_timeline / audio_timeline / subtitle_timeline ...
    # 只接受「数组」值，避免把 ``video``/``audio`` 等媒体引用对象误当成时间轴。
    scattered: Dict[str, Any] = {}
    found = False
    for kind, aliases in _TIMELINE_ALIASES.items():
        for alias in aliases:
            value = record.get(alias)
            if isinstance(value, list):
                scattered[kind] = value
                found = True
                break
    return (scattered if found else {}), []


def _pair_timeline_source(record: Dict[str, Any], media_roots: Sequence[Path]):
    """Merge explicit pair timelines only; conflicting definitions require review.

    Derived M11 modality views may repeat the same timeline. Equal definitions
    are one source, while distinct modalities can contribute separate timelines.
    Bindings and calibration anchors must be supplied, never inferred from IDs.
    """
    merged, refs, provenance = {}, [], {}

    def merge(key, value, origin):
        if value is None:
            return
        if key in merged and merged[key] != value:
            raise TimelineError('原生候选时间轴定义冲突 (timeline conflict)：%s' % key)
        merged[key] = value
        provenance.setdefault(key, []).append(origin)

    components = [('record', record)] + [
        (side + '.record', record[side]['record']) for side in ('left', 'right')]
    for origin, component in components:
        source, sidecars = _timeline_source(component, media_roots)
        for kind, aliases in _TIMELINE_ALIASES.items():
            for alias in aliases:
                if alias in source:
                    merge(kind, source[alias], origin)
        for key, aliases in (('temporal_bindings', ('temporal_bindings', 'bindings')),
                             ('temporal_anchors', ('temporal_anchors', 'anchors'))):
            for container in (component, source):
                for alias in aliases:
                    if alias in container:
                        merge(key, container[alias], origin)
        for relative in _collect_refs(component, source) + sidecars:
            if relative not in refs:
                refs.append(relative)
    return merged, refs, provenance


def _collect_refs(record: Dict[str, Any], source: Dict[str, Any]) -> List[str]:
    """收集需要随包搬运的相对路径（尽力而为，不阻断判定）。"""
    refs: List[str] = []

    def _push(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            refs.append(value.strip())
        elif isinstance(value, dict):
            inner = value.get("path")
            if isinstance(inner, str) and inner.strip():
                refs.append(inner.strip())

    for key in ("timeline_file",):
        _push(record.get(key))
    media = record.get("media")
    if isinstance(media, list):
        for item in media:
            _push(item)
    elif media is not None:
        _push(media)
    for key in ("video", "audio", "image", "frames", "subtitle"):
        _push(record.get(key))
    for key in ("timeline_file", "media", "video", "audio", "subtitle"):
        _push(source.get(key))
    ordered: List[str] = []
    for item in refs:
        if item not in ordered:
            ordered.append(item)
    return ordered


def resolve_spec(record: Dict[str, Any], media_roots: Sequence[Path]) -> Dict[str, Any]:
    """把一条记录解析为待判定的 ``spec``。

    结构性/可读性问题直接抛 ``TimelineError``（含 ``TimelineUnreadable``），
    由调用方统一记账进原始隔离区，绝不静默丢弃。
    """
    normalize_native_pair(record)
    if _native_pair(record):
        source, sidecar_refs, provenance = _pair_timeline_source(record, media_roots)
    else:
        source, sidecar_refs = _timeline_source(record, media_roots)
        provenance = {}
    timelines: Dict[str, Optional[Timeline]] = {}
    for kind in TIMELINE_KINDS:
        entries = None
        for alias in _TIMELINE_ALIASES[kind]:
            if alias in source and source[alias] is not None:
                entries = source[alias]
                break
        timelines[kind] = normalize_timeline(entries, kind)

    bindings = record.get("temporal_bindings")
    if bindings is None:
        bindings = record.get("bindings")
    if bindings is None:
        bindings = source.get("temporal_bindings", source.get("bindings"))
    if bindings is not None and not isinstance(bindings, list):
        raise TimelineError("temporal_bindings 必须是数组")

    anchors = record.get("temporal_anchors")
    if anchors is None:
        anchors = record.get("anchors")
    if anchors is None:
        anchors = source.get("temporal_anchors", source.get("anchors"))
    if anchors is not None and not isinstance(anchors, list):
        raise TimelineError("temporal_anchors 必须是数组")

    refs: List[str] = []
    for item in _collect_refs(record, source) + list(sidecar_refs):
        if item not in refs:
            refs.append(item)

    return {
        "sample_id": record_sample_id(record),
        "record": record,
        "timelines": timelines,
        "bindings": list(bindings) if bindings else [],
        "anchors": list(anchors) if anchors else [],
        "media_refs": refs,
        "timeline_provenance": provenance,
    }


# --------------------------------------------------------------------------- #
# 线性插值时间映射
# --------------------------------------------------------------------------- #

class LinearTimeMap:
    """source 时钟 → video 时钟的线性映射（分段线性插值 / 全局最小二乘）。

    锚点不足（去重后的 source 少于 ``min_anchors``）时 ``applied=False``，
    ``map()`` 退化为恒等映射并在结果里显式标注，绝不假装已校准。
    """

    def __init__(self, pairs: Sequence[Tuple[float, float]], *,
                 method: str = "piecewise_linear", min_anchors: int = 2):
        merged: Dict[float, float] = {}
        for source_sec, video_sec in pairs:
            merged[round(float(source_sec), 6)] = float(video_sec)
        self.points: List[Tuple[float, float]] = sorted(merged.items())
        self.requested_method = method
        self.min_anchors = int(min_anchors)
        self.anchor_count = len(self.points)
        self.applied = self.anchor_count >= max(2, self.min_anchors)
        self.method = method if self.applied else "identity"
        self._slope = 1.0
        self._intercept = 0.0
        if self.applied and self.method == "global_linear":
            self._slope, self._intercept = self._least_squares()
        self.max_residual_sec = round(self._residual(), 6)

    def _least_squares(self) -> Tuple[float, float]:
        n = len(self.points)
        mean_s = sum(point[0] for point in self.points) / n
        mean_v = sum(point[1] for point in self.points) / n
        variance = sum((point[0] - mean_s) ** 2 for point in self.points)
        if variance == 0:  # pragma: no cover - 已在构造处保证 source 去重
            return 1.0, 0.0
        covariance = sum((point[0] - mean_s) * (point[1] - mean_v) for point in self.points)
        slope = covariance / variance
        return slope, mean_v - slope * mean_s

    def map(self, source_sec: float) -> float:
        value = float(source_sec)
        if not self.applied:
            return value
        if self.method == "global_linear":
            return self._slope * value + self._intercept
        points = self.points
        if len(points) == 1:  # pragma: no cover - applied 时至少 2 个锚点
            return points[0][1]
        if value <= points[0][0]:
            left, right = points[0], points[1]
        elif value >= points[-1][0]:
            left, right = points[-2], points[-1]
        else:
            left, right = points[0], points[1]
            for index in range(len(points) - 1):
                if points[index][0] <= value <= points[index + 1][0]:
                    left, right = points[index], points[index + 1]
                    break
        span = right[0] - left[0]
        if span == 0:  # pragma: no cover - source 已去重
            return left[1]
        return left[1] + (right[1] - left[1]) * (value - left[0]) / span

    def _residual(self) -> float:
        if not self.applied:
            return 0.0
        return max(abs(self.map(source) - video) for source, video in self.points)

    def describe(self) -> Dict[str, Any]:
        return {
            "type": "linear",
            "applied": bool(self.applied),
            "requested_method": self.requested_method,
            "method": self.method,
            "anchor_count": self.anchor_count,
            "min_anchors": self.min_anchors,
            "max_residual_sec": self.max_residual_sec,
        }


def build_time_map(anchors: Any, *, method: str, min_anchors: int) -> LinearTimeMap:
    """把 ``temporal_anchors`` 规整为 ``LinearTimeMap``。"""
    pairs: List[Tuple[float, float]] = []
    for index, anchor in enumerate(anchors or []):
        field = "temporal_anchors[%d]" % index
        if not isinstance(anchor, dict):
            raise TimelineError("%s 必须是对象" % field)
        raw_video = _pick(anchor, ("video_sec", "video", "video_time_sec", "frame_sec"))
        raw_source = _pick(anchor, ("source_sec", "source", "audio_sec", "text_sec",
                                    "subtitle_sec", "sec"))
        if raw_video is None or raw_source is None:
            raise TimelineError("%s 必须同时给出 video_sec 与 source_sec" % field)
        pairs.append((to_seconds(raw_source, "%s.source_sec" % field),
                      to_seconds(raw_video, "%s.video_sec" % field)))
    return LinearTimeMap(pairs, method=method, min_anchors=min_anchors)


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #

def _mutate_processing(record: Dict[str, Any], node: Dict[str, Any]) -> None:
    processing = record.get("processing")
    if not isinstance(processing, dict):
        processing = {}
        record["processing"] = processing
    processing["15"] = node


def skipped_node() -> Dict[str, Any]:
    """关闭路线：样本原样透传，不判定、不隔离。"""
    return {
        "operator": OPERATOR,
        "operator_version": OPERATOR_VERSION,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(STATUS_SKIPPED),
        "process_status": STATUS_SKIPPED,
        "decision": STATUS_SKIPPED,
        "reason_code": REASON_ROUTE_DISABLED,
        "review_required": False,
    }


def apply_skipped(record: Dict[str, Any]) -> Dict[str, Any]:
    """关闭路线：只在本算子命名空间登记 skipped，不写任何时序结论。"""
    _mutate_processing(record, skipped_node())
    return record


def make_ledger_entry(*, line: int, reason: str, label: str, action: str, destination: str,
                      event_time: str, sample_id: Optional[str] = None,
                      record: Optional[Dict[str, Any]] = None,
                      detail: Optional[str] = None) -> Dict[str, Any]:
    """构造统一台账条目（label/stage/operator/action/destination/event_time/batch_id/
    sample_id/retry_count，背景规则 §2.3）。"""
    batch_id = None
    retry_count = 0
    if isinstance(record, dict):
        raw_batch = record.get("batch_id")
        if isinstance(raw_batch, (str, int)) and not isinstance(raw_batch, bool):
            batch_id = raw_batch
        metadata = record.get("metadata")
        raw_retry = None
        if isinstance(metadata, dict):
            raw_retry = metadata.get("retry_count")
        if raw_retry is None:
            raw_retry = record.get("retry_count")
        if isinstance(raw_retry, int) and not isinstance(raw_retry, bool) and raw_retry >= 0:
            retry_count = raw_retry
    entry: Dict[str, Any] = {
        "label": label,
        "stage": STAGE_ID,
        "operator": FORMAL_OPERATOR_NUMBER,
        "operator_name": OPERATOR,
        "operator_version": OPERATOR_VERSION,
        "action": action,
        "destination": destination,
        "event_time": event_time,
        "batch_id": batch_id,
        "sample_id": sample_id,
        "retry_count": retry_count,
        "reason_code": reason,
        "line_number": line,
    }
    if detail:
        entry["detail"] = detail
    if record is not None:
        entry["record"] = record
    return entry


def _alignment_block(*, status: str, passed: Optional[bool], max_delta: Optional[float],
                     avg_delta: Optional[float], threshold: float, binding_count: int,
                     calibration: Dict[str, Any], evidence: List[Dict[str, Any]],
                     evidence_truncated: bool, reason_code: str, label: str, action: str,
                     destination: str, event_time: str,
                     review_required: bool = False) -> Dict[str, Any]:
    return {
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(status),
        "process_status": status,
        "passed": passed,
        "max_delta_sec": max_delta,
        "avg_delta_sec": avg_delta,
        "threshold_sec": threshold,
        "binding_count": binding_count,
        "interpolation": calibration,
        "evidence": evidence,
        "evidence_truncated": evidence_truncated,
        "reason_code": reason_code,
        "label": label,
        "action": action,
        "destination": destination,
        "review_required": review_required,
        "human_verified": False,
        "event_time": event_time,
        "source_reference": SOURCE_REFERENCE,
        "algorithm": "linear_time_interpolation_v1",
        "unit": "second",
    }


def apply_block(record: Dict[str, Any], block: Dict[str, Any], *, event_time: str) -> Dict[str, Any]:
    """把判定结果写进记录的本算子命名空间（不覆盖上游字段）。"""
    record["temporal_alignment"] = block
    record["operator_15"] = {
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": block["status"],
        "process_status": block.get("process_status"),
        "decision": block.get("process_status"),
        "reason_code": block["reason_code"],
        "evidence": {
            "max_delta_sec": block["max_delta_sec"],
            "avg_delta_sec": block["avg_delta_sec"],
            "threshold_sec": block["threshold_sec"],
            "binding_count": block["binding_count"],
            "interpolation": block["interpolation"],
            "evidence_truncated": block["evidence_truncated"],
        },
        "human_verified": False,
        "review_required": block["review_required"],
        "source_reference": block["source_reference"],
        "event_time": event_time,
    }
    _mutate_processing(record, {
        "operator": OPERATOR,
        "operator_version": OPERATOR_VERSION,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": block["status"],
        "process_status": block.get("process_status"),
        "decision": block.get("process_status"),
        "reason_code": block["reason_code"],
        "label": block["label"],
        "review_required": block["review_required"],
    })
    return record


def evaluate_record(spec: Dict[str, Any], *, line: int, config,
                    event_time: str) -> Dict[str, Any]:
    """对一条记录做时空一致性判定。

    返回 ``{"action": pass|isolate|not_applicable|pending|origin, "reason": ...,
    "record"|"entry": ...}``。结构性非法直接抛 ``TimelineError``（由调用方进原始隔离区）。
    """
    record: Dict[str, Any] = spec["record"]
    sample_id: Optional[str] = spec["sample_id"]
    timelines: Dict[str, Optional[Timeline]] = spec["timelines"]
    threshold = float(config.max_time_delta_sec)

    missing = [kind for kind in TIMELINE_KINDS if timelines.get(kind) is None]
    if missing:
        reason = REASON_NO_TIMESTAMP if len(missing) == len(TIMELINE_KINDS) else {
            "video": REASON_MISSING_VIDEO_TIMELINE,
            "audio": REASON_MISSING_AUDIO_TIMELINE,
            "text": REASON_MISSING_TEXT_TIMELINE,
        }[missing[0]]
        block = _alignment_block(
            status=STATUS_NOT_APPLICABLE, passed=None, max_delta=None, avg_delta=None,
            threshold=threshold, binding_count=0,
            calibration={"type": "linear", "applied": False, "method": "not_executed",
                         "reason": reason, "anchor_count": 0,
                         "min_anchors": int(config.min_anchors_for_calibration),
                         "max_residual_sec": None},
            evidence=[], evidence_truncated=False, reason_code=reason,
            label=LABEL_NOT_APPLICABLE, action=ACTION_KEEP, destination=DEST_SAMPLE_POOL,
            event_time=event_time)
        return {"action": "not_applicable", "reason": reason,
                "record": apply_block(record, block, event_time=event_time)}

    if not spec["bindings"]:
        block = _alignment_block(
            status=STATUS_NOT_APPLICABLE, passed=None, max_delta=None, avg_delta=None,
            threshold=threshold, binding_count=0,
            calibration={"type": "linear", "applied": False, "method": "not_executed",
                         "reason": REASON_NO_BINDING, "anchor_count": 0,
                         "min_anchors": int(config.min_anchors_for_calibration),
                         "max_residual_sec": None},
            evidence=[], evidence_truncated=False, reason_code=REASON_NO_BINDING,
            label=LABEL_NOT_APPLICABLE, action=ACTION_KEEP, destination=DEST_SAMPLE_POOL,
            event_time=event_time)
        return {"action": "not_applicable", "reason": REASON_NO_BINDING,
                "record": apply_block(record, block, event_time=event_time)}

    if len(spec["bindings"]) > int(config.max_bindings):
        raise BindingLimitExceeded("时序绑定数 %d 超过上限 %d"
                                   % (len(spec["bindings"]), config.max_bindings))

    time_map = build_time_map(spec["anchors"], method=config.interpolation_method,
                             min_anchors=config.min_anchors_for_calibration)
    calibration = time_map.describe()

    # 先解出全部绑定的三个时间戳（结构非法优先于校准状态）
    resolved: List[Tuple[str, float, float, float]] = []
    for index, binding in enumerate(spec["bindings"]):
        binding_id = "#%d" % index
        if isinstance(binding, dict):
            raw_id = _pick(binding, ("binding_id", "id", "key"))
            binding_id = str(raw_id) if raw_id is not None else binding_id
            refs = {kind: _pick(binding, (kind, "%s_id" % kind, "%s_segment" % kind))
                    for kind in TIMELINE_KINDS}
        elif isinstance(binding, list) and len(binding) == 3:
            refs = dict(zip(TIMELINE_KINDS, binding))
        else:
            raise TimelineError("时序绑定 %s 必须是对象或三元数组" % binding_id)
        if any(refs[kind] is None for kind in TIMELINE_KINDS):
            raise TimelineError("时序绑定 %s 必须同时给出 video/audio/text 引用" % binding_id)
        video_point = timelines["video"].get(refs["video"], binding_id=binding_id)
        audio_point = timelines["audio"].get(refs["audio"], binding_id=binding_id)
        text_point = timelines["text"].get(refs["text"], binding_id=binding_id)
        resolved.append((binding_id, video_point["time"], audio_point["time"], text_point["time"]))

    if config.require_calibration and not time_map.applied:
        block = _alignment_block(
            status=STATUS_PENDING_CALIBRATION, passed=None, max_delta=None, avg_delta=None,
            threshold=threshold, binding_count=len(resolved), calibration=calibration,
            evidence=[], evidence_truncated=False,
            reason_code=REASON_CALIBRATION_UNAVAILABLE, label=LABEL_PENDING_CALIBRATION,
            action=ACTION_WAITING_MANUAL, destination=DEST_PIPELINE_WAIT,
            event_time=event_time, review_required=True)
        return {"action": "pending", "reason": REASON_CALIBRATION_UNAVAILABLE,
                "entry": make_ledger_entry(
                    line=line, reason=REASON_CALIBRATION_UNAVAILABLE,
                    label=LABEL_PENDING_CALIBRATION, action=ACTION_WAITING_MANUAL,
                    destination=DEST_PIPELINE_WAIT, event_time=event_time,
                    sample_id=sample_id,
                    record=apply_block(record, block, event_time=event_time))}

    evidence: List[Dict[str, Any]] = []
    raw_deltas: List[float] = []
    limit = int(config.max_evidence_bindings)
    for binding_id, video_sec, audio_sec, text_sec in resolved:
        audio_calibrated = time_map.map(audio_sec)
        text_calibrated = time_map.map(text_sec)
        # 阈值比较用未取整的真实差值：先 round(6) 再比较会让 0.5000004 这类
        # 超出阈值的差值被舍入成 0.5 而误判通过（边界假通过）。
        raw_delta = max(abs(video_sec - audio_calibrated),
                        abs(video_sec - text_calibrated),
                        abs(audio_calibrated - text_calibrated))
        raw_deltas.append(raw_delta)
        if len(evidence) < limit:
            evidence.append({
                "binding_id": binding_id,
                "video_sec": round(video_sec, 6),
                "audio_sec": round(audio_sec, 6),
                "audio_calibrated_sec": round(audio_calibrated, 6),
                "text_sec": round(text_sec, 6),
                "text_calibrated_sec": round(text_calibrated, 6),
                "delta_sec": round(raw_delta, 6),
                "within_threshold": bool(raw_delta <= threshold),
            })

    max_delta = round(max(raw_deltas), 6)
    avg_delta = round(sum(raw_deltas) / len(raw_deltas), 6)
    evidence_truncated = len(raw_deltas) > len(evidence)
    # 判定以未取整差值为准，输出字段保留 6 位小数仅用于展示。
    passed = max(raw_deltas) <= threshold

    if passed:
        block = _alignment_block(
            status=STATUS_PASSED, passed=True, max_delta=max_delta, avg_delta=avg_delta,
            threshold=threshold, binding_count=len(resolved), calibration=calibration,
            evidence=evidence, evidence_truncated=evidence_truncated,
            reason_code="within_threshold", label=LABEL_PASS, action=ACTION_KEEP,
            destination=DEST_SAMPLE_POOL, event_time=event_time)
        return {"action": "pass", "reason": "within_threshold",
                "record": apply_block(record, block, event_time=event_time),
                "max_delta_sec": max_delta, "avg_delta_sec": avg_delta,
                "calibrated": bool(time_map.applied)}

    block = _alignment_block(
        status=STATUS_FAILED, passed=False, max_delta=max_delta, avg_delta=avg_delta,
        threshold=threshold, binding_count=len(resolved), calibration=calibration,
        evidence=evidence, evidence_truncated=evidence_truncated,
        reason_code=REASON_MAX_DELTA_EXCEEDED, label=LABEL_FAIL, action=ACTION_ISOLATE,
        destination=DEST_SAMPLE_QUARANTINE, event_time=event_time)
    augmented = apply_block(record, block, event_time=event_time)
    return {"action": "isolate", "reason": REASON_MAX_DELTA_EXCEEDED,
            "max_delta_sec": max_delta, "avg_delta_sec": avg_delta,
            "calibrated": bool(time_map.applied),
            "entry": make_ledger_entry(
                line=line, reason=REASON_MAX_DELTA_EXCEEDED, label=LABEL_FAIL,
                action=ACTION_ISOLATE, destination=DEST_SAMPLE_QUARANTINE,
                event_time=event_time, sample_id=sample_id, record=augmented)}
