"""上游字段归一化（**只读**解析）：#19 候选池 / #20 质量结论 / #21 隔离结果 /
#16 授权 / #17 局部标注 / #18 词-区域，以及记录内的文本与媒体引用。

本模块只做「取字段 + 归一化」，不做任何业务判定，也不改写上游结论。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import io as io_utils
from . import names

__all__ = [
    "ISOLATED_STATUSES",
    "MODALITY_ALIASES",
    "REASON_UNSAFE_MEDIA_PATH",
    "collect_media_links",
    "collect_modalities",
    "extract_text",
    "resolve_isolation",
    "resolve_license",
    "resolve_local_annotation",
    "resolve_quality",
    "resolve_record",
    "resolve_version",
    "normalize_modalities",
]

#: 媒体引用非法时的原因码（与 ``tokens.REASON_UNSAFE_MEDIA_PATH`` 同值）
REASON_UNSAFE_MEDIA_PATH = "unsupported_media_path"

MODALITY_ALIASES: Dict[str, str] = {}
for _alias, _modality in (
        ("image", "image"), ("img", "image"), ("picture", "image"), ("photo", "image"),
        ("图像", "image"), ("图片", "image"), ("图", "image"),
        ("audio", "audio"), ("voice", "audio"), ("sound", "audio"), ("speech", "audio"),
        ("音频", "audio"), ("语音", "audio"), ("声音", "audio"),
        ("video", "video"), ("movie", "video"), ("clip", "video"),
        ("视频", "video"), ("录像", "video"),
        ("text", "text"), ("txt", "text"), ("caption", "text"), ("description", "text"),
        ("文本", "text"), ("文字", "text"), ("描述", "text")):
    MODALITY_ALIASES[_alias] = _modality

#: 视为「已隔离」的上游状态
ISOLATED_STATUSES = {"isolated", "quarantined", "sample_quarantine",
                     "origin_quarantine", "locked"}

ABSENT_STATUSES = {"skipped", "disabled", "route_disabled", "not_applicable",
                   "passthrough", "closed", "route_closed"}

QUALITY_ACCEPTED_DECISIONS = {"accepted", "flagged", "pass", "passed", "keep",
                              "released", "tokenize"}
QUALITY_ACCEPTED_STATUSES = {"success", "pending_review", "accepted", "flagged",
                             "passed", "ok", "admitted"}

QUALITY_ISOLATED_DECISIONS = {"sample_quarantine", "origin_quarantine",
                              "quarantine", "isolated", "rejected", "rejected_for_token"}
QUALITY_ISOLATED_STATUSES = {"isolated", "failed", "rejected", "quarantined"}

TEXT_OBJECT_KEYS = ("content", "text", "value", "transcript")

NEGATIVE_LOCAL_STATUSES = {"skipped", "disabled", "route_disabled", "not_applicable",
                           "closed", "none"}
POSITIVE_LOCAL_STATUSES = {"confirmed", "pending_manual_review", "prefilled",
                           "auto_prefilled", "pending_manual", "manual_confirmed",
                           "human_confirmed"}


# --------------------------------------------------------------------------- #
# 模态
# --------------------------------------------------------------------------- #

def _modality_token(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("modality", "type", "kind", "name"):
            if isinstance(value.get(key), str):
                return _modality_token(value[key])
        return None
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if not token:
        return None
    if token in MODALITY_ALIASES:
        return MODALITY_ALIASES[token]
    for alias, modality in MODALITY_ALIASES.items():
        if len(alias) > 1 and alias in token:
            return modality
    return None


def normalize_modalities(raw: Any) -> Tuple[str, ...]:
    """把任意模态列表归一为去重、按 ``image/audio/video/text`` 排序的元组。"""
    if raw is None:
        return ()
    items: Sequence[Any]
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]
    found = set()
    for item in items:
        modality = _modality_token(item)
        if modality:
            found.add(modality)
    return tuple(name for name in names.MODALITY_ORDER if name in found)


# --------------------------------------------------------------------------- #
# 记录视图（支持 #11 成对记录）
# --------------------------------------------------------------------------- #

def _side_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把一对多结构的 ``left``/``right.record`` 展平为单一视图。

    左侧优先（``left`` 覆盖 ``right``），顶层字段最后覆盖。
    """
    left = payload.get("left")
    right = payload.get("right")
    if not isinstance(left, dict) and not isinstance(right, dict):
        return payload

    def _record(node: Any) -> Dict[str, Any]:
        if not isinstance(node, dict):
            return {}
        inner = node.get("record")
        base = dict(inner) if isinstance(inner, dict) else {}
        for key, value in node.items():
            if key == "record":
                continue
            base.setdefault(key, value)
        return base

    top = {key: value for key, value in payload.items()
           if key not in ("left", "right")}
    merged: Dict[str, Any] = {}
    for part in (_record(right), _record(left), top):
        for key, value in part.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = _merge(merged[key], value)
            else:
                merged[key] = value
    return merged


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #

def _pick_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in TEXT_OBJECT_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def extract_text(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按优先级提取对齐文本：``text.content`` → ``text`` → ``metadata.description``
    → ``description``。返回 ``{"content", "field", "length", "sha256"}``。"""
    candidates = [
        ("text.content", _pick_text(payload.get("text")) if isinstance(payload.get("text"), dict) else None),
        ("text", _pick_text(payload.get("text")) if isinstance(payload.get("text"), str) else None),
        ("metadata.description",
         _pick_text((payload.get("metadata") or {}).get("description"))
         if isinstance(payload.get("metadata"), dict) else None),
        ("description", _pick_text(payload.get("description"))),
    ]
    for field, content in candidates:
        if content:
            return {"content": content, "field": field, "length": len(content),
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    return {"content": None, "field": None, "length": 0, "sha256": None}


# --------------------------------------------------------------------------- #
# 媒体引用（原文件仅保留对象存储链接）
# --------------------------------------------------------------------------- #

_MEDIA_SOURCES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("image", ("image", "images", "image_path", "images_paths", "picture")),
    ("audio", ("audio", "audios", "audio_path", "voice")),
    ("video", ("video", "videos", "video_path", "movie")),
)

#: 按扩展名判定媒体模态（用于 #17 的「主引用」``path``）
_MEDIA_SUFFIXES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("image", (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff", ".heic")),
    ("video", (".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv",
               ".m4v", ".mpg", ".mpeg", ".ts")),
    ("audio", (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma")),
)


def _classify_media(path: str) -> Optional[str]:
    """按扩展名把路径归到 image/audio/video；认不出返回 ``None``。"""
    lowered = path.lower().split("?", 1)[0].strip()
    for modality, suffixes in _MEDIA_SUFFIXES:
        if lowered.endswith(suffixes):
            return modality
    return None


def _iter_refs(value: Any) -> List[str]:
    refs: List[str] = []
    if isinstance(value, str):
        if value.strip():
            refs.append(value.strip())
    elif isinstance(value, dict):
        candidate = value.get("path")
        if isinstance(candidate, str) and candidate.strip():
            refs.append(candidate.strip())
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(_iter_refs(item))
    return refs


def _extra_refs(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    """补充引用位置：``media.*`` / ``paths.*`` / ``*_reference.path`` / #17 标注路径。"""
    grouped: Dict[str, List[str]] = {"image": [], "audio": [], "video": []}

    media = payload.get("media")
    if isinstance(media, dict):
        for modality in names.MEDIA_MODALITIES:
            grouped[modality].extend(_iter_refs(media.get(modality)))

    paths = payload.get("paths")
    if isinstance(paths, dict):
        for modality in names.MEDIA_MODALITIES:
            grouped[modality].extend(_iter_refs(paths.get(modality)))

    for modality in names.MEDIA_MODALITIES:
        reference = payload.get("%s_reference" % modality)
        if isinstance(reference, dict):
            grouped[modality].extend(_iter_refs(reference.get("path")))
        elif isinstance(reference, str):
            grouped[modality].extend(_iter_refs(reference))

    annotations = payload.get("annotations")
    if isinstance(annotations, dict):
        grouped["image"].extend(_iter_refs(annotations.get("image")))

    processing = payload.get("processing")
    if isinstance(processing, dict):
        node17 = processing.get("17")
        if isinstance(node17, dict):
            nested = node17.get("paths")
            if isinstance(nested, dict):
                for modality in names.MEDIA_MODALITIES:
                    grouped[modality].extend(_iter_refs(nested.get(modality)))
            # `path` 是 #17 的「主引用」：有图取图、无图取视频/音频
            # （CMA_annotate_local_to_whole/utils/localization.py:781）。旧实现一律
            # 记成 image，视频样本就会带着指向 .avi/.mp4 的 <image> 标记出门。
            for path in _iter_refs(node17.get("path")):
                modality = _classify_media(path)
                if modality is None:
                    if isinstance(nested, dict):
                        continue        # 有分模态字段时不猜
                    modality = "image"  # 旧版 #17 的 path 只可能是图片
                grouped[modality].append(path)
    return grouped


def collect_media_links(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """收集媒体引用并逐条做路径安全判定。

    返回条目：``{"modality", "path", "safe", "reason"}``（``reason`` 仅在不安全时出现）。
    顺序：先按模态（image/audio/video）、再按记录中的出现顺序。
    """
    grouped = _extra_refs(payload)
    for modality, keys in _MEDIA_SOURCES:
        for key in keys:
            if key in payload:
                grouped[modality].extend(_iter_refs(payload.get(key)))

    links: List[Dict[str, Any]] = []
    seen = set()
    for modality in names.MEDIA_MODALITIES:
        for path in grouped[modality]:
            key = (modality, path)
            if key in seen:
                continue
            seen.add(key)
            entry: Dict[str, Any] = {"modality": modality, "path": path, "safe": True}
            if not io_utils.is_safe_relative(path):
                entry["safe"] = False
                entry["reason"] = REASON_UNSAFE_MEDIA_PATH
            links.append(entry)
    return links


def collect_modalities(payload: Dict[str, Any]) -> Tuple[str, ...]:
    """声明模态 ∪ 从引用推断出的模态。"""
    found = set(normalize_modalities(payload.get("modalities")))
    for link in collect_media_links(payload):
        found.add(link["modality"])
    if extract_text(payload)["content"]:
        found.add("text")
    return tuple(name for name in names.MODALITY_ORDER if name in found)


# --------------------------------------------------------------------------- #
# 质量结论（#20）
# --------------------------------------------------------------------------- #

def _first_dict(payload: Dict[str, Any], paths: Sequence[Sequence[str]]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    for path in paths:
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, dict):
            return ".".join(path), node
    return None, None


def resolve_quality(payload: Dict[str, Any]) -> Dict[str, Any]:
    """解析上游质量结论（#20）。

    ``outcome`` 三态：``accepted`` / ``isolated`` / ``absent``。
    注意：``flagged`` 属「不拦样本」的旁路子集（#20 口径），仍按 accepted 处理并打标。
    """
    source, node = _first_dict(payload, (
        ("processing", "20"), ("operator_20",), ("quality_conclusion",),
        ("qc", "quality_conclusion"), ("qc_conclusion",),
        ("processing", "qc"),
    ))
    if node is None:
        return {"present": False, "outcome": "absent", "source": None,
                "status": None, "decision": None, "label": None, "route": None,
                "flagged": False, "detail": "记录中不存在 #20 质量结论"}

    status = node.get("status")
    decision = node.get("decision")
    status_key = status.strip().lower() if isinstance(status, str) else None
    decision_key = decision.strip().lower() if isinstance(decision, str) else None

    if (status_key in ABSENT_STATUSES) or (decision_key in ABSENT_STATUSES):
        return {"present": False, "outcome": "absent", "source": source,
                "status": status, "decision": decision, "label": node.get("label"),
                "route": node.get("route"), "flagged": False,
                "detail": "质量结论为 skipped/不适用，按「还没有质量结论」处理"}

    if decision_key in QUALITY_ISOLATED_DECISIONS or status_key in QUALITY_ISOLATED_STATUSES:
        return {"present": True, "outcome": "isolated", "source": source,
                "status": status, "decision": decision, "label": node.get("label"),
                "route": node.get("route"), "flagged": False,
                "detail": "质量结论为隔离（%s/%s）" % (status, decision)}

    if decision_key in QUALITY_ACCEPTED_DECISIONS or status_key in QUALITY_ACCEPTED_STATUSES:
        flagged = decision_key == "flagged" or status_key == "flagged" \
            or node.get("flagged") is True
        return {"present": True, "outcome": "accepted", "source": source,
                "status": status, "decision": decision, "label": node.get("label"),
                "route": node.get("route"), "flagged": flagged,
                "detail": "质量结论通过（%s/%s）" % (status, decision)}

    return {"present": False, "outcome": "absent", "source": source,
            "status": status, "decision": decision, "label": node.get("label"),
            "route": node.get("route"), "flagged": False,
            "detail": "质量结论状态 %r 无法判定，按「还没有质量结论」处理" % (status,)}


# --------------------------------------------------------------------------- #
# 隔离结论（#21 / #20 / #19 / #17 / #18）
# --------------------------------------------------------------------------- #

def resolve_isolation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """解析上游隔离结论。返回 ``{"isolated", "reason", "source", "label"}``。

    规则 §2.3「同一条全产线最多 2 次重跑，第 3 次锁定样本隔离库」：本算子只**尊重**
    上游已写下的显式锁定标记（`locked` / `retry_locked` / `isolation_locked`，
    含 `metadata.*`），不自行按重跑次数推断，避免与 #19/#21 的口径冲突。
    """
    for key in ("locked", "retry_locked", "isolation_locked"):
        if payload.get(key) is True:
            return {"isolated": True, "reason": "upstream_locked", "source": key,
                    "label": None,
                    "detail": "记录已被上游显式锁定（%s），不再进入 Token" % key}
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("locked", "retry_locked", "isolation_locked"):
            if metadata.get(key) is True:
                return {"isolated": True, "reason": "upstream_locked",
                        "source": "metadata.%s" % key, "label": None,
                        "detail": "记录已被上游显式锁定（metadata.%s），不再进入 Token" % key}

    source, node = _first_dict(payload, (("processing", "21"), ("isolation",),
                                         ("operator_21",)))
    if node is not None:
        status = node.get("status")
        decision = node.get("decision")
        keys = {str(item).strip().lower() for item in (status, decision) if item}
        explicit = node.get("isolated")
        if keys & ISOLATED_STATUSES or explicit is True:
            return {"isolated": True, "reason": "upstream_21_isolated",
                    "source": source, "label": node.get("label"),
                    "detail": "上游 #21 已判隔离（%s）" % (status or decision)}
        if node.get("isolated") is False or keys:
            return {"isolated": False, "reason": None, "source": source,
                    "label": None, "detail": "上游 #21 未隔离"}

    source, node = _first_dict(payload, (("processing", "20"), ("operator_20",)))
    if node is not None:
        decision = node.get("decision")
        decision_key = str(decision).strip().lower() if decision else ""
        if decision_key in ("sample_quarantine", "origin_quarantine", "quarantine"):
            return {"isolated": True,
                    "reason": "quality_%s" % decision_key,
                    "source": source, "label": node.get("label"),
                    "detail": "上游 #20 质量结论为隔离（%s）" % decision}

    source, node = _first_dict(payload, (("processing", "19"), ("operator_19",)))
    if node is not None:
        keys = {str(item).strip().lower()
                for item in (node.get("status"), node.get("decision")) if item}
        if keys & ISOLATED_STATUSES:
            return {"isolated": True, "reason": "upstream_19_isolated",
                    "source": source, "label": node.get("label"),
                    "detail": "上游 #19 判隔离"}

    source, node = _first_dict(payload, (("processing", "17"), ("local_annotations",)))
    if node is not None:
        status = str(node.get("status") or "").strip().lower()
        if status == "incomplete":
            return {"isolated": True, "reason": "upstream_17_incomplete",
                    "source": source, "label": node.get("label"),
                    "detail": "上游 #17 局部标注不完整，已进样本隔离库"}

    source, node = _first_dict(payload, (("processing", "18"),))
    if node is not None:
        status = str(node.get("status") or "").strip().lower()
        if status == "isolated":
            return {"isolated": True, "reason": "upstream_18_isolated",
                    "source": source, "label": node.get("label"),
                    "detail": "上游 #18 词-区域关系隔离"}

    return {"isolated": False, "reason": None, "source": None, "label": None,
            "detail": "未发现上游隔离标记"}


# --------------------------------------------------------------------------- #
# 授权、版本、局部标注
# --------------------------------------------------------------------------- #

_LICENSE_PATHS: Tuple[Sequence[str], ...] = (
    ("admission", "license"), ("admission", "authorization"),
    ("metadata", "license"), ("metadata", "authorization"),
    ("metadata", "license_status"), ("metadata", "authorization_status"),
    ("metadata", "授权结论"),
    ("authorization", "status"), ("authorization", "license"),
    ("license",), ("license_status",), ("authorization_status",), ("授权结论",),
    ("processing", "10", "license"), ("processing", "10", "authorization"),
)


def resolve_license(payload: Dict[str, Any]) -> Optional[str]:
    """解析授权结论（#10）。返回清洗后的字符串或 ``None``。"""
    for path in _LICENSE_PATHS:
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node.strip():
            return node.strip()
    return None


_VERSION_PATHS: Tuple[Sequence[str], ...] = (
    ("metadata", "version"), ("version",), ("metadata", "corpus_version"),
    ("corpus_version",), ("metadata", "dataset_version"),
    ("processing", "23", "version"),
)


def resolve_version(payload: Dict[str, Any]) -> Optional[str]:
    for path in _VERSION_PATHS:
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node.strip():
            return node.strip()
    return None


def resolve_local_annotation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """局部标注证据（#17/#18）。``status`` 是权威判据，结构证据仅在无状态时兜底。"""
    status: Optional[str] = None
    source: Optional[str] = None
    for path in (("local_annotations", "status"), ("processing", "17", "status")):
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node.strip():
            status = node.strip().lower()
            source = ".".join(path)
            break

    if status in NEGATIVE_LOCAL_STATUSES:
        return {"present": False, "status": status, "source": source,
                "detail": "上游局部标注工具未生效（%s）" % status}
    if status in POSITIVE_LOCAL_STATUSES:
        return {"present": True, "status": status, "source": source,
                "detail": "存在局部标注证据（%s）" % status}

    if isinstance(payload.get("phrase_box_relations"), list) \
            and payload["phrase_box_relations"]:
        return {"present": True, "status": status, "source": "phrase_box_relations",
                "detail": "存在词-区域关系记录"}
    processing = payload.get("processing")
    node17 = processing.get("17") if isinstance(processing, dict) else None
    if isinstance(node17, dict):
        for key in ("regions", "boxes", "confirmed_boxes", "prefilled_boxes"):
            value = node17.get(key)
            if isinstance(value, list) and value:
                return {"present": True, "status": status,
                        "source": "processing.17.%s" % key,
                        "detail": "存在局部标注区域"}
    local = payload.get("local_annotations")
    if isinstance(local, dict):
        for key in ("regions", "boxes", "confirmed_boxes"):
            value = local.get(key)
            if isinstance(value, list) and value:
                return {"present": True, "status": status,
                        "source": "local_annotations.%s" % key,
                        "detail": "存在局部标注区域"}
    return {"present": False, "status": status, "source": source,
            "detail": "未发现局部标注证据"}


# --------------------------------------------------------------------------- #
# 综合解析
# --------------------------------------------------------------------------- #

def resolve_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把一条输入记录解析为业务判定所需的规格对象（只读，不改原记录）。"""
    view = _side_view(payload)
    sample_id = payload.get("sample_id")
    pair_id = payload.get("pair_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        sample_id_value = None
    else:
        sample_id_value = sample_id.strip()
    if not isinstance(pair_id, str) or not pair_id.strip():
        pair_id_value = None
    else:
        pair_id_value = pair_id.strip()
    identifier = pair_id_value or sample_id_value

    media_links = collect_media_links(view)
    return {
        "identifier": identifier,
        "sample_id": sample_id_value,
        "pair_id": pair_id_value,
        "is_pair": bool(pair_id_value),
        "view": view,
        "modalities": collect_modalities(view),
        "declared_modalities": normalize_modalities(view.get("modalities")),
        "text": extract_text(view),
        "media_links": [item for item in media_links if item.get("safe")],
        "unsafe_media": [item for item in media_links if not item.get("safe")],
        "quality": resolve_quality(view),
        "isolation": resolve_isolation(view),
        "license": resolve_license(view),
        "version": resolve_version(view),
        "local_annotation": resolve_local_annotation(view),
    }
