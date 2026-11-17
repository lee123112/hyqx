"""表1 #14「评分视文语义一致性」业务模块。

业务口径（表1 #14，2026-10-23 修正）：
- **默认关闭**：关闭时不加载模型、不生成视文产物、不隔离，样本原样继续流转；
- 开启后：对视频抽帧序列与文本描述复用 CLIP-ViT/B-32 计算余弦相似度；
  ``max_frames_per_segment``（1–8）是**整段视频合计**帧预算：单段时等价旧行为（≤8 帧、
  保序均匀），多段时把预算分配到各段（总帧数 ≤ 上限；段数 ≤ 上限时每段至少 1 帧、
  其余按候选帧数占比分配；段数 > 上限时按段均匀覆盖，0 帧段记无证据）；
- 段分 = 该段被选中帧的余弦均值；**整段视频分 = 所有被选中帧的均值**
  （不再是段均值的均值）；任一段分低于阈值（配置默认 0.23）即整段不通过 →
  样本进样本隔离库（交 #21），0 帧段不参与判定但记录说明；
- 输出：视文分数、段分、最低段分、通过结论、模型版本，以及
  ``frames_used``（恒 ≤ ``max_frames_per_segment``）等帧预算证据，交 #19 与 #16；
- **不新增视频模型**：只消费 #6 抽取的关键帧图像，不自行解码视频。

分流约定（与统计口径一一对应）：
- ``pass``    → 主输出（继续流转，交 #19/#16）
- ``pending`` → 主输出 + 待处理台账（无视频帧/无描述/模型不可用等**流程状态**，
  **不隔离**，绝不写 0 分或假分）
- ``isolate`` → 样本隔离库（``样本-语义不通``，交 #21）
- ``origin``  → 原始隔离区（帧缺失/损坏、路径非法等**原始文件问题**，与模型无关）

本文件不 import 任何兄弟算子；模型能力全部经 ``utils.backends`` 注入，
因此可用测试替身完整验证分段、聚合、阈值与分流逻辑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils import frames as frames_mod
from utils import io as io_utils
from utils import scoring as scoring_mod
from utils.schemas import FORMAL_OPERATOR_NUMBER, OPERATOR_NAME, OPERATOR_VERSION, STAGE_ID

OPERATOR = OPERATOR_NAME
#: 表1 固定模型族
CLIP_NAME = "CLIP-ViT/B-32"
#: 表1 默认低分阈值（复用图文评分低分阈值）
DEFAULT_THRESHOLD = 0.25

MAIN_NAME_ENABLED = "14_video_text_scores.jsonl"
MAIN_NAME_DISABLED = "14_passthrough.jsonl"
REJECTS_NAME = "14_rejects.jsonl"
SAMPLE_QUARANTINE_NAME = "14_sample_quarantine.jsonl"
PENDING_NAME = "14_pending.jsonl"
NOT_APPLICABLE_NAME = "14_not_applicable.jsonl"
LOG_NAME = "14_run.log"
LINEAGE_NAME = "lineage.json"
STAGE_CONTEXT_NAME = "stage_context.json"

#: 媒体搬运目录名
MEDIA_DIR_NAME = "frames"

# --------------------------------------------------------------------------- #
# 原因
# --------------------------------------------------------------------------- #
REASON_NO_VIDEO_TEXT_PAIR = "no_video_text_pair"
REASON_NO_FRAMES = "no_video_frames"
REASON_MISSING_DESCRIPTION = "missing_description"
REASON_UNSUPPORTED_MEDIA_PATH = "unsupported_media_path"
REASON_FRAME_MISSING = "frame_missing"
REASON_FRAME_UNREADABLE = "frame_unreadable"
REASON_SEGMENT_BELOW = "segment_below_threshold"
REASON_PASSED = "passed"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_SCORE_ERROR = "score_error"
#: 时序路线边界：记录自带 timeline/temporal_bindings/temporal_anchors 时，#14 不评分
#: （时序判定属于 #15；#14 的「视频-文本语义一致性」不是这类窗口的判据）。
REASON_TEMPORAL_ROUTE = "temporal_route_not_applicable"
REASON_NON_VIDEO_ROUTE = "non_video_route_not_applicable"

#: 行级（读入阶段）原因，由 ``inference`` 使用
REASON_JSON_PARSE_ERROR = "json_parse_error"
REASON_RECORD_NOT_OBJECT = "record_not_object"
REASON_ENCODING_ERROR = "encoding_error"

# --------------------------------------------------------------------------- #
# 标签（与规则文档产线四标签一致）
# --------------------------------------------------------------------------- #
LABEL_ORIGIN_UNPARSABLE = "原始文件-无法解析"
LABEL_ORIGIN_BROKEN = "原始文件-损坏"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_ORIGIN_ENCODING = "原始文件-编码异常"
LABEL_SAMPLE_SEMANTIC = "样本-语义不通"
LABEL_SCORE_PASS = "样本-评分：通过"
LABEL_FLOW_PENDING_SCORE = "流程-待评分"
LABEL_FLOW_NO_VIDEO_TEXT = "流程-无视频文本对"
LABEL_FLOW_NO_FRAMES = "流程-无视频帧"
LABEL_FLOW_PENDING_DESCRIPTION = "流程-待补描述"
LABEL_FLOW_ROUTE_DISABLED = "流程-视文路线未启用"
LABEL_FLOW_TEMPORAL_ROUTE = "流程-时序路线不适用视文评分"

# --------------------------------------------------------------------------- #
# 去向
# --------------------------------------------------------------------------- #
DEST_ORIGIN = "原始隔离区"
DEST_SAMPLE = "样本隔离库"
DEST_PIPELINE = "产线待处理"
DEST_NEXT = "#19"

STATUS_PASSED = "passed"
STATUS_PENDING = "pending"
STATUS_DEGRADED = "degraded"
STATUS_SKIPPED = "skipped"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_ISOLATED = "failed"

# [v4.2] status 双字段（§2.10）：输出记录 int status（本算子判定型：1=通过/不隔离，
# 0=过滤/隔离）+ 原字符串迁入 process_status。run() 顶层内部契约仍保持字符串。
_STATUS_AS_INT = {
    STATUS_PASSED: 1,
    STATUS_PENDING: 1,
    STATUS_DEGRADED: 1,
    STATUS_SKIPPED: 1,
    STATUS_NOT_APPLICABLE: 1,
    STATUS_ISOLATED: 0,
}


def status_int(value: Optional[str]) -> int:
    """[v4.2] 记录级流程态字符串 → int status；未登记的一律按不隔离 1。"""
    return _STATUS_AS_INT.get(value, 1)

ACTION_PASS = "pass"
ACTION_ISOLATE = "isolate"
ACTION_ORIGIN = "origin"
ACTION_PENDING = "pending"
ACTION_NOT_APPLICABLE = "not_applicable"


class DependencyError(RuntimeError):
    """CLIP 依赖或权重不可用且未允许降级：整个任务必须显式失败。"""


# --------------------------------------------------------------------------- #
# 字段解析
# --------------------------------------------------------------------------- #

def _text_of(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def resolve_description(record: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """解析文本描述：``text.content`` → ``text.description`` → ``metadata`` → 上游 ``processing``。"""
    if not isinstance(record, dict):
        return {"value": None, "source": None}

    text = record.get("text")
    if isinstance(text, str):
        value = _text_of(text)
        if value:
            return {"value": value, "source": "text"}
    elif isinstance(text, dict):
        for field in ("content", "description", "text", "caption"):
            value = _text_of(text.get(field))
            if value:
                return {"value": value, "source": "text.%s" % field}

    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for field in ("description", "caption", "title"):
            value = _text_of(metadata.get(field))
            if value:
                return {"value": value, "source": "metadata.%s" % field}

    processing = record.get("processing")
    if isinstance(processing, dict):
        for key in sorted(key for key in processing if str(key) != "14"):
            node = processing.get(key)
            if isinstance(node, dict):
                for field in ("description", "caption"):
                    value = _text_of(node.get(field))
                    if value:
                        return {"value": value, "source": "processing.%s.%s" % (key, field)}
    return {"value": None, "source": None}


def resolve_video_reference(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析视频引用；返回 ``{path, duration, video, frames, source}``，无视频返回 ``None``。

    只接受抽帧序列形态（``video.frames[]``）；视频本体路径仅作引用记录，不解码。
    """
    if not isinstance(record, dict):
        return None
    payload = record.get("video")

    if isinstance(payload, str):
        path = _text_of(payload)
        if not path:
            return None
        return {"path": path, "duration": None, "video": {"path": path, "frames": []},
                "frames": [], "source": "video"}

    if not isinstance(payload, dict):
        return None

    path = None
    for field in ("path", "file", "file_path", "uri"):
        path = _text_of(payload.get(field))
        if path:
            break
    if not path:
        return None

    return {
        "path": path,
        "duration": frames_mod.as_float(payload.get("duration")),
        "video": payload,
        "frames": frames_mod.resolve_frames(payload),
        "source": "video",
    }


def resolve_pair(record: Dict[str, Any]) -> Dict[str, Any]:
    """解析视文配对：``#11`` 的 ``left``/``right`` 形态或单记录扁平形态。"""
    empty = {
        "mode": "flat", "identifier": None, "pair_id": None, "sample_id": None,
        "batch_id": None, "video": None, "frames": [], "description": {"value": None,
                                                                       "source": None},
        "record": record if isinstance(record, dict) else {},
    }
    if not isinstance(record, dict):
        return empty

    left = record.get("left")
    right = record.get("right")
    if isinstance(left, dict) and isinstance(right, dict):
        left_record = left.get("record") if isinstance(left.get("record"), dict) else left
        right_record = right.get("record") if isinstance(right.get("record"), dict) else right
        left_video = resolve_video_reference(left_record)
        right_video = resolve_video_reference(right_record)
        if left_video is None and right_video is not None:
            video_record, text_record = right_record, left_record
        else:
            video_record, text_record = left_record, right_record

        description = resolve_description(text_record)
        if description["value"] is None:
            description = resolve_description(video_record)

        pair_id = _text_of(record.get("pair_id"))
        video = left_video if video_record is left_record else right_video
        return {
            "mode": "pair",
            "identifier": pair_id or _text_of(video_record.get("sample_id")),
            "pair_id": pair_id,
            "sample_id": _text_of(video_record.get("sample_id")),
            "batch_id": _text_of(video_record.get("batch_id")),
            "video": video,
            "frames": list(video.get("frames") or []) if video else [],
            "description": description,
            "record": record,
        }

    video = resolve_video_reference(record)
    return {
        "mode": "flat",
        "identifier": _text_of(record.get("sample_id")),
        "pair_id": None,
        "sample_id": _text_of(record.get("sample_id")),
        "batch_id": _text_of(record.get("batch_id")),
        "video": video,
        "frames": list(video.get("frames") or []) if video else [],
        "description": resolve_description(record),
        "record": record,
    }


def non_video_route_evidence(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Identify typed image/audio samples without waiving unresolved video claims.

    Native pair sides remain namespaced. A video declaration/reference anywhere
    on those active sides takes precedence over a non-video modality hint.
    Unknown or text-only records retain the existing pending behavior.
    """
    active_pair = record.get("modality_pair")
    active_pair = {item.strip().lower() for item in active_pair if isinstance(item, str)} \
        if isinstance(active_pair, list) and len(active_pair) == 2 else set()
    non_video_pair = active_pair in ({"text", "image"}, {"text", "audio"})
    nodes = [(record, False)]
    for side in ("left", "right"):
        wrapper = record.get(side)
        if isinstance(wrapper, dict):
            nodes.append((wrapper, False))
            if isinstance(wrapper.get("record"), dict):
                view_modalities = wrapper.get("modalities")
                view_modalities = {item.strip().lower() for item in view_modalities
                                   if isinstance(item, str)} if isinstance(view_modalities, list) else set()
                # M11's typed view retains the origin's broad modality hint.
                # Only explicit pair/view declarations override that hint;
                # actual video references on a side still block the waiver.
                typed_non_video_view = (non_video_pair and bool(view_modalities)
                                        and view_modalities.issubset(active_pair))
                nodes.append((wrapper["record"], typed_non_video_view))
    video_types = {"video", "videos", "video_text", "text_video", "av", "audio_video",
                   "视频", "视文", "音视频"}
    other_types = {"image", "images", "image_text", "text_image", "audio", "audios",
                   "audio_text", "text_audio", "图像", "图片", "图文", "音频", "音文"}
    found = set()
    for node, typed_non_video_view in nodes:
        declared = []
        for key in ("modality", "modalities", "modality_pair", "route", "routes"):
            if typed_non_video_view and key in ("modality", "modalities"):
                continue
            value = node.get(key)
            declared.extend(value if isinstance(value, (list, tuple)) else [value])
        for item in declared:
            if not isinstance(item, str):
                continue
            token = item.strip().lower().replace("-", "_")
            if token in video_types:
                return None
            if token in other_types:
                found.add(token)
        for key in ("video", "videos", "video_path", "video_reference"):
            value = node.get(key)
            if value is not None and (not isinstance(value, str) or value.strip()):
                return None
        for kind in ("image", "audio"):
            if node.get(kind) or node.get(kind + "_path"):
                found.add(kind)
        media = node.get("media")
        if isinstance(media, dict):
            if media.get("video"):
                return None
            found.update(kind for kind in ("image", "audio") if media.get(kind))
        elif isinstance(media, list):
            for item in media:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("modality") or item.get("role") or "").lower()
                if kind in video_types:
                    return None
                if kind in other_types:
                    found.add(kind)
        paths = node.get("paths")
        if isinstance(paths, dict) and paths.get("video"):
            return None
    return {"non_video_modalities": sorted(found)} if found else None


def resolve_media_file(media_roots: Sequence[Path], relative: str) -> Optional[Path]:
    """在若干候选根下定位媒体文件。

    路径**形式**非法（绝对路径、``..`` 逃逸、URL、反斜杠）时抛 ``UnsupportedMediaPath``；
    形式合法但文件不存在时返回 ``None``（交由调用方判为 ``frame_missing``）。
    """
    roots = [Path(root) for root in (media_roots or []) if root is not None]
    if not roots:
        roots = [Path.cwd()]

    form_error: Optional[Exception] = None
    for root in roots:
        try:
            io_utils.resolve_media(root, relative)
        except io_utils.UnsupportedMediaPath as exc:
            form_error = exc
            continue
        form_error = None
        break
    if form_error is not None:
        raise form_error

    for root in roots:
        try:
            candidate = io_utils.resolve_media(root, relative)
        except io_utils.UnsupportedMediaPath:
            continue
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# 台账与评分节点
# --------------------------------------------------------------------------- #

def make_ledger_entry(line: int, reason: str, label: str, action: str,
                      destination: str, event_time: str,
                      sample_id: Optional[str] = None, pair_id: Optional[str] = None,
                      batch_id: Optional[str] = None, retry_count: int = 0,
                      **extra: Any) -> Dict[str, Any]:
    """构造一条台账/异常清单条目（统一结构，便于 #21 读取）。"""
    entry: Dict[str, Any] = {
        "line": int(line),
        "stage": "14",
        "operator": OPERATOR,
        "operator_version": OPERATOR_VERSION,
        "sample_id": sample_id,
        "pair_id": pair_id,
        "batch_id": batch_id,
        "retry_count": int(retry_count),
        "reason": reason,
        "label": label,
        "action": action,
        "destination": destination,
        "event_time": event_time,
    }
    entry.update(extra)
    return entry


def _frame_budget(config: Any) -> int:
    """本次生效的**整段视频合计**帧预算（表1 范围 1–8）。"""
    return frames_mod.clamp_limit(getattr(config, "max_frames_per_segment",
                                          frames_mod.MAX_FRAMES_PER_SEGMENT))


def _budget_note(budget: int) -> str:
    return ("整段视频合计最多 %d 帧；未分配到帧的段记无证据、不参与隔离判定"
            % budget)


def _no_evidence_reason(frame_count: int, available: int) -> str:
    """段分为空的原因：配额为 0 / 无候选帧 / 有帧但拿不到分数。"""
    if available <= 0:
        return "no_candidate_frames"
    if frame_count <= 0:
        return "frame_quota_zero"
    return "frame_score_unavailable"


def skipped_node(config: Any = None) -> Dict[str, Any]:
    """路线关闭时的节点：明确声明未评分、未隔离，且不生成视文产物。

    ``low_score_threshold`` / ``max_frames_per_segment`` 回填**本次配置生效值**
    （与开启态同一取值口径），不再硬编码默认阈值；未传配置时退回表1 默认。
    """
    node = _base_node(config)
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    node.update({
        "status": status_int(STATUS_SKIPPED),
        "process_status": STATUS_SKIPPED,
        "route_enabled": False,
        "passed": None,
        "reason": "route_disabled",
        "label": LABEL_FLOW_ROUTE_DISABLED,
        "action": "passthrough",
        "destination": DEST_PIPELINE,
    })
    return node


#: 记录自带这些结构即视为「时序路线」样本（与 #15 的输入判据一致）。
TEMPORAL_STRUCTURE_KEYS = ("temporal_bindings", "temporal_anchors", "timeline")


def temporal_route_evidence(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """识别时序路线样本；不是时序样本返回 ``None``。

    判据只看**结构声明**，不猜内容：#15 用同一批字段做时间轴一致性，
    有这些字段的记录属于「时空一致性」路线，#14 的整段视频语义分不是它的判据。
    2026-11-05：此前 #14 对这些窗口照常打 CLIP 分，窗口分普遍低于 0.23，
    导致 96 条时序样本在 #14 被 `segment_below_threshold` 隔离（#15 判通过也无济于事）。
    """
    if not isinstance(record, dict):
        return None
    present = [key for key in TEMPORAL_STRUCTURE_KEYS
               if record.get(key) not in (None, [], {}, "")]
    if not present:
        return None
    timeline = record.get("timeline")
    bindings = record.get("temporal_bindings")
    anchors = record.get("temporal_anchors")
    #: 计数键始终存在（缺席记 0），下游读证据时不必做存在性判断
    counts: Dict[str, int] = {"timeline_tracks": 0, "timeline_items": 0,
                              "bindings": 0, "anchors": 0}
    if isinstance(timeline, dict):
        counts["timeline_tracks"] = len(timeline)
        counts["timeline_items"] = sum(
            len(items) for items in timeline.values() if isinstance(items, (list, tuple)))
    elif isinstance(timeline, (list, tuple)):
        counts["timeline_items"] = len(timeline)
    if isinstance(bindings, (list, tuple)):
        counts["bindings"] = len(bindings)
    if isinstance(anchors, (list, tuple)):
        counts["anchors"] = len(anchors)
    return {"declared_by": present, **counts}


def not_applicable_node(evidence: Optional[Dict[str, Any]] = None,
                        config: Any = None) -> Dict[str, Any]:
    """时序路线边界节点：显式声明**未评分、未隔离**，交 #15 判定。

    ``status=1``（不隔离）、``process_status=not_applicable``；
    不写 ``video_text_score``，避免下游把它当成真实分数。
    """
    node = _base_node(config)
    node.update({
        "status": status_int(STATUS_NOT_APPLICABLE),
        "process_status": STATUS_NOT_APPLICABLE,
        "route_enabled": bool(getattr(config, "enabled", True)),
        "passed": None,
        "reason": REASON_TEMPORAL_ROUTE,
        "label": LABEL_FLOW_TEMPORAL_ROUTE,
        "action": "passthrough",
        "destination": DEST_PIPELINE,
        "route": "temporal",
        "temporal_route_evidence": evidence,
        "scored_at": None,
    })
    return node


def _base_node(config: Any) -> Dict[str, Any]:
    budget = _frame_budget(config)
    return {
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(STATUS_PENDING),
        "process_status": STATUS_PENDING,
        "route_enabled": True,
        "passed": None,
        "reason": None,
        "label": None,
        "action": ACTION_PENDING,
        "destination": DEST_PIPELINE,
        "video_text_score": None,
        "segment_mean": None,
        "min_segment_score": None,
        "low_score_threshold": float(getattr(config, "low_score_threshold",
                                             DEFAULT_THRESHOLD)),
        "segment_count": 0,
        "total_segments": 0,
        "scored_frames": 0,
        # 帧预算证据（表1 #14 整段视频合计上限）：frames_used 恒 <= 上限
        "frames_used": 0,
        "max_frames_per_segment": budget,
        "frames_within_budget": True,
        "segments_without_frames": [],
        "frame_budget_note": _budget_note(budget),
        "clip_model": CLIP_NAME,
        "clip_model_version": None,
        "model": None,
        "segments": [],
        "degraded": False,
    }


def pending_node(reason: str, label: str, config: Any,
                 status: str = STATUS_PENDING) -> Dict[str, Any]:
    node = _base_node(config)
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    node["status"] = status_int(status)
    node["process_status"] = status
    node["reason"] = reason
    node["label"] = label
    return node


def origin_node(reason: str, label: str, config: Any,
                detail: Optional[str] = None) -> Dict[str, Any]:
    node = _base_node(config)
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    node["status"] = status_int(STATUS_ISOLATED)
    node["process_status"] = STATUS_ISOLATED
    node["reason"] = reason
    node["label"] = label
    node["action"] = ACTION_ORIGIN
    node["destination"] = DEST_ORIGIN
    if detail:
        node["detail"] = detail
    return node


# --------------------------------------------------------------------------- #
# 主判定
# --------------------------------------------------------------------------- #

def _outcome(action: str, reason: str, node: Dict[str, Any],
             entry: Dict[str, Any], record: Optional[Dict[str, Any]] = None,
             degraded: bool = False) -> Dict[str, Any]:
    return {"action": action, "reason": reason, "node": node, "entry": entry,
            "record": record, "degraded": degraded}


def _entry_for(spec: Dict[str, Any], line: int, event_time: str, node: Dict[str, Any],
               record: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entry = make_ledger_entry(
        line=line, reason=node["reason"], label=node["label"], action=node["action"],
        destination=node["destination"], event_time=event_time,
        sample_id=spec.get("sample_id"), pair_id=spec.get("pair_id"),
        batch_id=spec.get("batch_id"), video_text=node,
        video_path=(spec.get("video") or {}).get("path"),
    )
    if record is not None:
        entry["record"] = record
    return entry


def evaluate_record(spec: Dict[str, Any], line: int, *, media_roots: Sequence[Path],
                    config: Any, encoder: Any, event_time: str) -> Dict[str, Any]:
    """判定单条视文配对；返回 ``{action, reason, node, entry, record, degraded}``。"""
    record = spec.get("record") if isinstance(spec.get("record"), dict) else {}
    video = spec.get("video")
    description = spec.get("description") or {"value": None, "source": None}

    # 0) 路线边界：时序样本不归 #14 判（它的判据是 #15 的时间轴一致性）
    if bool(getattr(config, "temporal_route_exclusion", True)):
        evidence = temporal_route_evidence(record)
        if evidence is not None:
            node = not_applicable_node(evidence, config)
            return _outcome(ACTION_NOT_APPLICABLE, REASON_TEMPORAL_ROUTE, node,
                            _entry_for(spec, line, event_time, node))

    # 1) 流程状态：没有视文对 / 没有描述 / 没有可用帧 → 待处理，不隔离
    if not video:
        evidence = non_video_route_evidence(record)
        if evidence is not None:
            node = _base_node(config)
            node.update({"status": status_int(STATUS_NOT_APPLICABLE),
                         "process_status": STATUS_NOT_APPLICABLE,
                         "route_enabled": False, "applicable": False,
                         "reason": REASON_NON_VIDEO_ROUTE, "label": "流程-非视文样本",
                         "action": "passthrough", "destination": DEST_PIPELINE,
                         "route": "non_video", "non_video_route_evidence": evidence,
                         "scored_at": None})
            return _outcome(ACTION_NOT_APPLICABLE, REASON_NON_VIDEO_ROUTE, node,
                            _entry_for(spec, line, event_time, node))
    if not video:
        node = pending_node(REASON_NO_VIDEO_TEXT_PAIR, LABEL_FLOW_NO_VIDEO_TEXT, config)
        return _outcome(ACTION_PENDING, REASON_NO_VIDEO_TEXT_PAIR, node,
                        _entry_for(spec, line, event_time, node))

    if not description.get("value"):
        node = pending_node(REASON_MISSING_DESCRIPTION, LABEL_FLOW_PENDING_DESCRIPTION,
                            config)
        return _outcome(ACTION_PENDING, REASON_MISSING_DESCRIPTION, node,
                        _entry_for(spec, line, event_time, node))

    segments = frames_mod.resolve_segments(video.get("video"), description.get("value"),
                                           description.get("source"))
    # 整段视频合计帧预算：段内保序均匀，多段时把配额分配到各段（总帧数 ≤ 上限）
    plan = frames_mod.frame_plan(video.get("video"), segments,
                                 config.max_frames_per_segment)
    groups = plan["groups"]
    if plan["frames_used"] == 0:
        node = pending_node(REASON_NO_FRAMES, LABEL_FLOW_NO_FRAMES, config)
        return _outcome(ACTION_PENDING, REASON_NO_FRAMES, node,
                        _entry_for(spec, line, event_time, node))

    # 2) 原始文件问题：路径非法 / 帧文件缺失 → 原始隔离区（与模型无关）
    resolved_groups: List[List[Path]] = []
    for group in groups:
        resolved: List[Path] = []
        for frame in group:
            relative = frame["path"]
            try:
                target = resolve_media_file(media_roots, relative)
            except io_utils.UnsupportedMediaPath:
                node = origin_node(REASON_UNSUPPORTED_MEDIA_PATH, LABEL_ORIGIN_UNPARSABLE,
                                   config, detail="非法帧路径：%s" % relative)
                return _outcome(ACTION_ORIGIN, REASON_UNSUPPORTED_MEDIA_PATH, node,
                                _entry_for(spec, line, event_time, node))
            if target is None:
                node = origin_node(REASON_FRAME_MISSING, LABEL_ORIGIN_UNPARSABLE,
                                   config, detail="帧文件不存在：%s" % relative)
                return _outcome(ACTION_ORIGIN, REASON_FRAME_MISSING, node,
                                _entry_for(spec, line, event_time, node))
            resolved.append(target)
        resolved_groups.append(resolved)

    # 3) 模型不可用：显式失败或显式降级（绝不伪造分数）
    if encoder is None:
        if not bool(getattr(config, "allow_degraded", False)):
            raise DependencyError(
                "CLIP-ViT/B-32 不可用且未允许降级（allow_degraded=false）：%s" % CLIP_NAME)
        node = pending_node(REASON_MODEL_UNAVAILABLE, LABEL_FLOW_PENDING_SCORE, config,
                            status=STATUS_DEGRADED)
        node["degraded"] = True
        node["clip_model_version"] = None
        return _outcome(ACTION_PENDING, REASON_MODEL_UNAVAILABLE, node,
                        _entry_for(spec, line, event_time, node), degraded=True)

    # 4) 评分：段内逐帧余弦 → 段分；整段视频分 = 全部被选中帧的均值
    max_chars = int(getattr(config, "max_text_chars", 500))
    texts: List[str] = []
    truncated: List[bool] = []
    for segment in segments:
        raw = segment.get("text") or ""
        texts.append(raw[:max_chars])
        truncated.append(len(raw) > max_chars)

    flat_paths: List[Path] = [path for group in resolved_groups for path in group]
    active_indexes = [index for index, group in enumerate(groups) if group]
    try:
        # 只为有帧的段编码文本：0 帧段无证据，不参与判定也不消耗模型配额
        active_text_vectors = encoder.encode_texts(
            [texts[index] for index in active_indexes])
        text_vectors = dict(zip(active_indexes, active_text_vectors))
        image_vectors, errors = encoder.encode_images(flat_paths)
    except Exception as exc:  # noqa: BLE001 - 编码失败必须显式降级，不能伪造分数
        node = pending_node(REASON_SCORE_ERROR, LABEL_FLOW_PENDING_SCORE, config)
        node["detail"] = "编码失败：%s" % type(exc).__name__
        return _outcome(ACTION_PENDING, REASON_SCORE_ERROR, node,
                        _entry_for(spec, line, event_time, node))

    # 5) 逐帧相似度不可得（解码失败）→ 原始隔离区
    cursor = 0
    for group_index, group in enumerate(resolved_groups):
        for _frame in group:
            error = errors[cursor] if cursor < len(errors) else "media_missing"
            path = flat_paths[cursor]
            cursor += 1
            if error is not None:
                reason = (REASON_FRAME_MISSING if error == "media_missing"
                          else REASON_FRAME_UNREADABLE)
                label = (LABEL_ORIGIN_UNPARSABLE if error == "media_missing"
                         else LABEL_ORIGIN_BROKEN)
                node = origin_node(reason, label, config,
                                   detail="帧无法解码：%s" % Path(path).name)
                return _outcome(ACTION_ORIGIN, reason, node,
                                _entry_for(spec, line, event_time, node))

    segment_nodes: List[Dict[str, Any]] = []
    segment_means: List[Optional[float]] = []
    all_frame_scores: List[float] = []
    scored_frames = 0
    cursor = 0
    for index, segment in enumerate(segments):
        group = groups[index]
        text_vector = text_vectors.get(index)
        frame_scores: List[Optional[float]] = []
        frames_detail: List[Dict[str, Any]] = []
        for frame in group:
            image_vector = image_vectors[cursor] if cursor < len(image_vectors) else None
            path = flat_paths[cursor]
            cursor += 1
            timestamp = frame.get("timestamp") if isinstance(frame, dict) else None
            if image_vector is None or text_vector is None:
                frame_scores.append(None)
                frames_detail.append({"path": Path(path).name, "score": None,
                                      "timestamp": timestamp})
                continue
            score = round(scoring_mod.cosine_similarity(text_vector, image_vector), 6)
            frame_scores.append(score)
            all_frame_scores.append(score)
            frames_detail.append({"path": Path(path).name, "score": score,
                                  "timestamp": timestamp})
        segment_mean = scoring_mod.mean(frame_scores)
        segment_means.append(segment_mean)
        scored_segment_frames = sum(1 for value in frame_scores if value is not None)
        scored_frames += scored_segment_frames
        has_evidence = scored_segment_frames > 0
        segment_nodes.append({
            "index": index,
            "start": segment.get("start"),
            "end": segment.get("end"),
            "text_source": segment.get("text_source"),
            "text_chars": len(texts[index]),
            "text_truncated": truncated[index],
            # 该段分到的帧配额（整段视频合计预算的一部分）与实际选中帧数
            "frame_quota": int(plan["quotas"][index]),
            "frame_count": len(group),
            "scored_frames": scored_segment_frames,
            "segment_mean": segment_mean,
            "passed": scoring_mod.segment_passed(segment_mean,
                                                 config.low_score_threshold),
            "has_evidence": has_evidence,
            "no_evidence_reason": (None if has_evidence else
                                   _no_evidence_reason(len(group),
                                                       plan["available"][index])),
            "frames": frames_detail,
        })

    summary = scoring_mod.aggregate(segment_means, config.low_score_threshold,
                                    all_frame_scores)
    identity = encoder.model_identity() if hasattr(encoder, "model_identity") else None

    node = _base_node(config)
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    final_status = STATUS_PASSED if summary["passed"] else STATUS_ISOLATED
    node.update({
        "status": status_int(final_status),
        "process_status": final_status,
        "passed": summary["passed"],
        "segments": segment_nodes,
        "segment_count": summary["segment_count"],
        "total_segments": len(segments),
        "video_text_score": summary["video_text_score"],
        "segment_mean": summary["segment_mean"],
        "min_segment_score": summary["min_segment_score"],
        "scored_frames": scored_frames,
        # 帧预算证据（表1 #14）：整段视频合计 frames_used <= max_frames_per_segment
        "frames_used": plan["frames_used"],
        "max_frames_per_segment": plan["limit"],
        "frames_within_budget": plan["frames_within_budget"],
        "segments_without_frames": [item["index"] for item in segment_nodes
                                    if item["frame_count"] == 0],
        "frame_budget_note": _budget_note(plan["limit"]),
        "clip_model_version": (identity or {}).get("version"),
        "model": identity,
        "video_path": video.get("path"),
        "description_source": description.get("source"),
        "scored_at": event_time,
    })

    if summary["passed"]:
        node["reason"] = REASON_PASSED
        node["label"] = LABEL_SCORE_PASS
        node["action"] = ACTION_PASS
        node["destination"] = DEST_NEXT
        output_record = dict(record)
        processing = dict(output_record.get("processing") or {})
        processing["14"] = node
        output_record["processing"] = processing
        output_record["video_text_score"] = node
        output_record["video_text_score_value"] = summary["video_text_score"]
        entry = _entry_for(spec, line, event_time, node)
        return _outcome(ACTION_PASS, REASON_PASSED, node, entry, record=output_record)

    node["reason"] = REASON_SEGMENT_BELOW
    node["label"] = LABEL_SAMPLE_SEMANTIC
    node["action"] = ACTION_ISOLATE
    node["destination"] = DEST_SAMPLE
    node["failing_segments"] = summary["failing_segments"]
    entry = _entry_for(spec, line, event_time, node, record=record)
    return _outcome(ACTION_ISOLATE, REASON_SEGMENT_BELOW, node, entry)
