"""上游结论读取层：从 #12–#18 的命名空间里取值并归一化（**只读，不改上游结论**）。

命名空间依据（各算子 ``dataset/DATA_SCHEMA.md`` 与源码）：
- #10 ``admission{license, modalities}`` + ``processing["10"]``
- #11 ``pair_id`` / ``left`` / ``right`` / ``group_id`` / ``evidence``
- #12 ``image_text_score{tier,...}`` + ``processing["12"]``
- #13 ``processing["13"]{route_enabled,passed,...}``
- #14 ``processing["14"]`` + 顶层 ``video_text_score``
- #15 ``temporal_alignment`` + ``operator_15`` + ``processing["15"]``
- #16 ``alignment.whole{decision,...}`` + ``processing["16"]``
- #17 ``local_annotations`` + ``processing["17"]``
- #18 ``processing["18"]`` + ``phrase_box_relations``
- #21 ``processing["21"]``（已隔离标记，若有）

本模块**不做业务判定**，只回答「上游说了什么、以及从哪个字段读到的」。
"""
from __future__ import annotations

import copy
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from . import io as io_utils

__all__ = [
    "DISABLED_STATUSES",
    "MEDIA_REF_PATHS",
    "ROUTE_IDS",
    "ROUTE_LABELS",
    "TEMPORAL_ROUTE_ID",
    "collect_media_refs",
    "enabled_routes",
    "flatten_record",
    "is_trainable",
    "read_license",
    "read_local_annotation",
    "read_routes",
    "read_temporal",
    "read_upstream_isolation",
    "read_whole_alignment",
    "resolve_identifier",
    "timeline_present",
    "unsafe_media_refs",
]

ROUTE_IDS: Tuple[str, ...] = ("12", "13", "14")
ROUTE_LABELS = {"12": "图文语义一致性", "13": "音文语义一致性", "14": "视文语义一致性"}

#: 时序路线的编号（#15 时空一致性）。它**不是** CLIP/CER 评分路线，
#: 只在三条评分路线都未生效时作为路线兜底证据使用（见 ``pool_filter._gate_route``）。
TEMPORAL_ROUTE_ID = "15"

#: 路线节点中表示「未生效 / 未启用」的状态
DISABLED_STATUSES = {"skipped", "route_closed", "not_applicable", "passthrough",
                     "closed", "disabled"}

#: #12 分档：这两个分档在路线级都不算失败（0.25–0.32 由 #16 转人工）
PASS_TIERS = {"auto_aligned", "pending_manual"}
FAIL_TIERS = {"isolated"}

TRAINABLE_VALUES = {"可训练", "trainable", "yes", "true"}

#: 授权结论可能出现的位置（优先级从前到后）
LICENSE_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("admission", "license"),
    ("metadata", "license"),
    ("metadata", "authorization"),
    ("metadata", "license_status"),
    ("metadata", "authorization_status"),
    ("metadata", "授权结论"),
    ("authorization", "status"),
    ("authorization", "license"),
    ("license",),
    ("license_status",),
    ("authorization_status",),
    ("授权结论",),
    ("processing", "10", "license"),
)

#: 媒体引用可能出现的位置（点路径；支持末级为字符串或 {path/file/name}）
MEDIA_REF_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("image",),
    ("image_path",),
    ("image_reference", "path"),
    ("media", "image"),
    ("media", "video"),
    ("media", "audio"),
    ("video", "path"),
    ("video_path",),
    ("audio", "path"),
    ("audio_path",),
    ("annotations", "image"),
    ("paths", "image"),
    ("paths", "video"),
    ("paths", "audio"),
    ("processing", "17", "path"),
    ("processing", "17", "paths", "image"),
    ("processing", "17", "paths", "video"),
    ("processing", "17", "paths", "audio"),
)

TIMELINE_KEYS = ("time_index", "timeline", "time_range", "time_ranges",
                 "bindings", "subtitles", "transcript_segments")


# --------------------------------------------------------------------------- #
# 基础取值
# --------------------------------------------------------------------------- #

def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dig(record: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    current: Any = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _business_status(node: Dict[str, Any]) -> Optional[str]:
    """v4.2 keeps the business state in process_status; status may be numeric."""
    return (_text(node.get("process_status")) or _text(node.get("status"))
            or _text(node.get("decision")))


def _status_source(node: Dict[str, Any], prefix: str) -> str:
    for key in ("process_status", "status", "decision"):
        if _text(node.get(key)):
            return prefix + "." + key
    return prefix + ".status"


# --------------------------------------------------------------------------- #
# 授权结论
# --------------------------------------------------------------------------- #

def read_license(record: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """读取授权结论；返回 ``(原值, 来源字段)``，都取不到则 ``(None, None)``。"""
    record = _dict(record)
    for path in LICENSE_PATHS:
        value = _dig(record, path)
        text = _text(value)
        if text:
            return text, ".".join(path)
    return None, None


def is_trainable(value: Any) -> Optional[bool]:
    """授权结论是否为「可训练」。

    - 字段缺失（``None`` 或非字符串）→ ``None``（表示「没有结论」）
    - 字段存在但取值不是「可训练」→ ``False``（包括空串）
    """
    if value is None or not isinstance(value, str):
        return None
    return value.strip().lower() in TRAINABLE_VALUES


# --------------------------------------------------------------------------- #
# 整体对齐结论（#16）
# --------------------------------------------------------------------------- #

def read_whole_alignment(record: Dict[str, Any]) -> Dict[str, Any]:
    """读取整体对齐结论：``alignment.whole.decision`` → ``processing["16"].status``。"""
    record = _dict(record)
    whole = _dict(record.get("alignment")).get("whole")
    node16 = _dict(_dict(record.get("processing")).get("16"))
    info = {"decision": None, "source": None, "human_veto_locked": False,
            "confirmation_method": None, "previous_decision": None,
            "score_source": None}

    if isinstance(whole, dict) and _text(whole.get("decision")):
        info["decision"] = _text(whole.get("decision"))
        info["source"] = "alignment.whole.decision"
        holder = whole
    elif isinstance(whole, dict) and _business_status(whole):
        info["decision"] = _business_status(whole)
        info["source"] = _status_source(whole, "alignment.whole")
        holder = whole
    elif _business_status(node16):
        info["decision"] = _business_status(node16)
        info["source"] = _status_source(node16, "processing.16")
        holder = node16
    elif _text(_dict(record.get("whole_alignment")).get("decision")):
        holder = _dict(record.get("whole_alignment"))
        info["decision"] = _text(holder.get("decision"))
        info["source"] = "whole_alignment.decision"
    else:
        return info

    info["human_veto_locked"] = bool(holder.get("human_veto_locked"))
    info["confirmation_method"] = _text(holder.get("confirmation_method"))
    info["previous_decision"] = _text(holder.get("previous_decision"))
    info["score_source"] = _text(holder.get("score_source"))
    return info


# --------------------------------------------------------------------------- #
# 评分路线（#12 / #13 / #14）
# --------------------------------------------------------------------------- #

_ROUTE_ALIASES = {
    "12": ("image_text_score", "scoring"),
    "13": ("audio_text",),
    "14": ("video_text_score",),
}


def _route_node(record: Dict[str, Any], route: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """返回 ``(节点, 来源)``；节点由 ``processing[NN]`` 与顶层别名合并而成。"""
    processing = _dict(record.get("processing"))
    primary = _dict(processing.get(route))
    alias: Dict[str, Any] = {}
    alias_source: Optional[str] = None
    for name in _ROUTE_ALIASES.get(route, ()):
        candidate = record.get(name)
        if isinstance(candidate, dict):
            alias = candidate
            alias_source = name
            break
    if primary:
        node = dict(primary)
        for key, value in alias.items():
            node.setdefault(key, value)
        return node, "processing.%s" % route
    if alias:
        return dict(alias), alias_source
    return {}, None


def read_routes(record: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """读取三条评分路线的生效状态、结论与原因（**不改写上游结论**）。"""
    record = _dict(record)
    result: Dict[str, Dict[str, Any]] = {}
    for route in ROUTE_IDS:
        node, source = _route_node(record, route)
        if not node:
            result[route] = {"present": False, "enabled": False, "passed": None,
                             "status": None, "tier": None, "reason": None,
                             "source": None, "label": ROUTE_LABELS[route]}
            continue
        status = _business_status(node)
        tier = _text(node.get("tier"))
        if "route_enabled" in node:
            enabled = bool(node.get("route_enabled"))
        else:
            enabled = status is None or status.lower() not in DISABLED_STATUSES
        passed = _route_passed(node, status, tier)
        reason = _text(node.get("reason")) or _text(node.get("reason_code"))
        configured_enabled = enabled
        applicable = True
        # The two new M13 gates and the M14 temporal boundary are intentional
        # non-scoring routes. Missing evidence on actual media remains pending.
        approved_na = {"13": {"language_not_applicable", "transcript_unreliable"},
                       "14": {"temporal_route_not_applicable"}}
        if passed is None and (
                (status == "not_applicable" and reason in approved_na.get(route, set()))
                or _missing_route_modality(record, route, reason)):
            applicable = False
            enabled = False
        elif passed is None and status == "not_applicable" and "route_enabled" not in node:
            # Only the documented applicability gates may waive a route. A
            # missing transcript/description/model remains unresolved evidence.
            enabled = True
            configured_enabled = True
        # A real failure is never erased by a contradictory skipped/NA status.
        if passed is False:
            applicable = True
            enabled = True
        if passed is None and reason is None:
            reason = status
        result[route] = {"present": True, "enabled": enabled, "passed": passed,
                         "status": status, "tier": tier, "reason": reason,
                         "configured_enabled": configured_enabled, "applicable": applicable,
                         "source": source, "label": ROUTE_LABELS[route]}
    return result


def _missing_route_modality(record: Dict[str, Any], route: str,
                            reason: Optional[str]) -> bool:
    expected = {"13": ("audio", "no_audio_text_pair"),
                "14": ("video", "no_video_text_pair")}
    if route not in expected:
        return False
    modality, missing_reason = expected[route]
    if reason != missing_reason:
        return False
    declared = record.get("modality") or record.get("modalities") or []
    if isinstance(declared, str):
        declared = [declared]
    pair_modalities = record.get("modality_pair")
    if isinstance(pair_modalities, list) and len(pair_modalities) == 2:
        normalized = [item.strip().lower() for item in pair_modalities if isinstance(item, str)]
        if len(set(normalized)) == 2 and set(normalized).issubset({"text", "image", "audio", "video"}):
            declared = list(declared) + normalized if isinstance(declared, list) else normalized
    if isinstance(declared, list) and any(str(item).lower() in {
            modality, "av", "音视频", "音频" if modality == "audio" else "视频"}
            for item in declared):
        return False
    # References can be direct, normalized, or only present in M17 typed paths.
    paths = ((modality,), (modality + "s",), (modality + "_path",),
             (modality + "_reference",), ("media", modality), ("paths", modality),
             ("processing", "17", "paths", modality),
             ("processing", "17", "paths", modality + "s"))
    if any(_dig(record, path) for path in paths):
        return False
    legacy = _ref_value(_dig(record, ("processing", "17", "path")))
    if legacy:
        suffix = PurePosixPath(legacy.replace("\\", "/")).suffix.lower()
        extensions = {"audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"},
                      "video": {".mp4", ".avi", ".mkv", ".mov", ".webm", ".mpeg", ".mpg", ".m4v"}}
        if suffix in extensions[modality]:
            return False
    return True


def _route_passed(node: Dict[str, Any], status: Optional[str],
                  tier: Optional[str]) -> Optional[bool]:
    explicit = node.get("passed")
    if isinstance(explicit, bool):
        return explicit
    if tier is not None:
        lowered = tier.lower()
        if lowered in PASS_TIERS:
            return True
        if lowered in FAIL_TIERS:
            return False
        return None
    if status is None:
        return None
    lowered = status.lower()
    if lowered == "passed":
        return True
    if lowered in ("failed", "isolated"):
        return False
    return None


def enabled_routes(routes: Dict[str, Dict[str, Any]]) -> List[str]:
    """当前记录中「已开启且在生效」的评分路线编号（按 12/13/14 顺序）。"""
    return [route for route in ROUTE_IDS
            if routes.get(route, {}).get("present") and routes[route].get("enabled")]


# --------------------------------------------------------------------------- #
# 时序结论（#15）与时间轴存在性
# --------------------------------------------------------------------------- #

#: #15 结论可能落在这三个字段里（``status`` 是 int 流程码，结论在另外两个）
_TEMPORAL_STATUS_KEYS = ("process_status", "status", "decision")


def _temporal_status(block: Dict[str, Any]) -> Optional[str]:
    """解析 #15 的结论字符串。

    ``status`` 是 int 流程码时不能作为结论。优先读 ``process_status``，
    兼容旧字符串 ``status``，最后读 ``decision``；布尔 ``passed`` 另行保留。
    """
    for key in _TEMPORAL_STATUS_KEYS:
        text = _text(block.get(key))
        if text:
            return text
    return None


def read_temporal(record: Dict[str, Any]) -> Dict[str, Any]:
    """读取时序结论：``temporal_alignment`` → ``operator_15`` → ``processing["15"]``。"""
    record = _dict(record)
    for name in ("temporal_alignment", "operator_15"):
        block = record.get(name)
        if isinstance(block, dict) and (_temporal_status(block) or isinstance(block.get("passed"), bool)):
            return _temporal_info(block, _status_source(block, name))
    node = _dict(_dict(record.get("processing")).get("15"))
    if node:
        return _temporal_info(node, _status_source(node, "processing.15"))
    return {"present": False, "status": None, "passed": None, "applicable": False,
            "reason_code": None, "source": None, "threshold_sec": None,
            "max_delta_sec": None}


def _temporal_info(block: Dict[str, Any], source: str) -> Dict[str, Any]:
    status = _temporal_status(block)
    passed = block.get("passed")
    if not isinstance(passed, bool):
        passed = None
    applicable = (passed is False
                  or (bool(status) and status.lower() not in ("not_applicable", "skipped"))
                  or (passed is True and status is None))
    return {"present": True, "status": status, "passed": passed,
            "applicable": applicable, "source": source,
            "reason_code": _text(block.get("reason_code")) or _text(block.get("reason")),
            "threshold_sec": block.get("threshold_sec"),
            "max_delta_sec": block.get("max_delta_sec")}


def timeline_present(record: Dict[str, Any]) -> bool:
    """记录是否**存在时间轴**（决定时序必过项是否适用）。"""
    record = _dict(record)
    temporal = read_temporal(record)
    if temporal["present"] and temporal["applicable"]:
        return True
    for key in TIMELINE_KEYS:
        value = record.get(key)
        if isinstance(value, (list, dict)) and value:
            return True
    video = _dict(record.get("video"))
    frames = video.get("frames")
    if isinstance(frames, list) and any(isinstance(item, dict) for item in frames):
        return True
    if isinstance(video.get("segments"), list) and video["segments"]:
        return True
    audio = _dict(record.get("audio"))
    if isinstance(audio.get("segments"), list) and audio["segments"]:
        return True
    return False


# --------------------------------------------------------------------------- #
# 局部标注（#17 / #18）与上游隔离标记
# --------------------------------------------------------------------------- #

def read_local_annotation(record: Dict[str, Any]) -> Dict[str, Any]:
    """读取局部标注状态（**不作为入池硬条件**，仅作证据）。"""
    record = _dict(record)
    processing = _dict(record.get("processing"))
    node17 = _dict(processing.get("17"))
    node18 = _dict(processing.get("18"))
    status17 = _business_status(_dict(record.get("local_annotations"))) \
        or _business_status(node17)
    status18 = _business_status(node18)
    source = None
    if status17:
        source = _status_source(node17, "processing.17")
    if status18:
        source = _status_source(node18, "processing.18")
    blocking = status17 == "incomplete" or status18 == "isolated"
    return {"operator17_status": status17, "operator18_status": status18,
            "blocking": bool(blocking), "source": source,
            "hard_requirement": False}


def read_upstream_isolation(record: Dict[str, Any]) -> Dict[str, Any]:
    """上游是否已把该样本判为「样本隔离库」（必须尊重，不得用分数加回）。"""
    record = _dict(record)
    processing = _dict(record.get("processing"))
    node21 = _dict(processing.get("21"))
    if _business_status(node21) == "isolated" or node21.get("isolated") is True:
        return {"isolated": True, "reason": "upstream_21_isolated",
                "source": _status_source(node21, "processing.21")}
    node17 = _dict(processing.get("17"))
    if _business_status(node17) == "incomplete":
        return {"isolated": True, "reason": "upstream_17_incomplete",
                "source": _status_source(node17, "processing.17")}
    node18 = _dict(processing.get("18"))
    if _business_status(node18) == "isolated":
        return {"isolated": True, "reason": "upstream_18_isolated",
                "source": _status_source(node18, "processing.18")}
    if record.get("isolated") is True:
        return {"isolated": True, "reason": "upstream_isolated_flag",
                "source": "isolated"}
    return {"isolated": False, "reason": None, "source": None}


# --------------------------------------------------------------------------- #
# 成对记录扁平化与标识
# --------------------------------------------------------------------------- #

def _merge_processing(base: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    for key, value in incoming.items():
        if value is None or value == [] or value == {} or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, dict):
            if not isinstance(base.get(key), dict):
                base[key] = {}
            _merge_processing(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def flatten_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把 #11 的成对记录（``left``/``right``）拍平为单条只读视图。

    拍平只用于**读取**上游结论；写出的记录仍是原始结构 + 本算子命名空间。
    """
    payload = _dict(payload)
    if "left" not in payload and "right" not in payload:
        return dict(payload)
    merged: Dict[str, Any] = {}
    for side in ("left", "right"):
        node = payload.get(side)
        if not isinstance(node, dict):
            continue
        record = node.get("record")
        if isinstance(record, dict):
            _merge_processing(merged, record)
        _merge_processing(merged, {key: value for key, value in node.items()
                                   if key != "record"})
    for key, value in payload.items():
        if key in ("left", "right"):
            continue
        _merge_processing(merged, {key: value})
    return merged


def resolve_identifier(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """返回 ``(sample_id, pair_id)``；成对记录会先看两侧 ``record.sample_id``。"""
    payload = _dict(payload)
    pair_id = _text(payload.get("pair_id"))
    sample_id = _text(payload.get("sample_id"))
    if "left" in payload or "right" in payload:
        for side in ("left", "right"):
            node = payload.get(side)
            if not isinstance(node, dict):
                continue
            record = node.get("record")
            if isinstance(record, dict) and _text(record.get("sample_id")):
                sample_id = _text(record.get("sample_id"))
                break
        if sample_id is None:
            for side in ("left", "right"):
                node = payload.get(side)
                if isinstance(node, dict) and _text(node.get("sample_id")):
                    sample_id = _text(node.get("sample_id"))
                    break
    return sample_id, pair_id


# --------------------------------------------------------------------------- #
# 媒体引用（只做路径合法性扫描；不读媒体内容，不伪造文件）
# --------------------------------------------------------------------------- #

def _ref_value(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("path", "file", "name", "relative_path"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def declared_media_refs(record: Dict[str, Any]) -> List[str]:
    """收集记录中声明的全部媒体引用（含不合法者，供分诊使用）。"""
    record = _dict(record)
    refs: List[str] = []
    for path in MEDIA_REF_PATHS:
        value = _dig(record, path)
        text = _ref_value(value)
        if text and text not in refs:
            refs.append(text)
    return refs


def unsafe_media_refs(record: Dict[str, Any]) -> List[str]:
    """声明了但**不可搬运**的媒体引用（绝对路径、穿越、协议地址、反斜杠）。"""
    return [ref for ref in declared_media_refs(record)
            if not io_utils.is_safe_relative(ref)]


def collect_media_refs(record: Dict[str, Any]) -> List[str]:
    """可搬运的相对媒体引用（用于尽力搬运与计数，缺失不阻断判定）。"""
    return [ref for ref in declared_media_refs(record)
            if io_utils.is_safe_relative(ref)]
