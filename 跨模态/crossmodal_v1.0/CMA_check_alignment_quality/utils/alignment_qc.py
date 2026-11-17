"""算子 20「质检对齐产物」核心业务规则（表1 第 4 环节「数据质量评估」）。

处理边界（表1 #20）
  全量文件可打开、≥2 模态、视频可解码、音频非全静音、文本非空；
  困惑度 / BRISQUE / SNR 优先复用四条预训练产线，无上游才在 10% 子集补测；
  图文子集用 CLIP 反向检索；偏科 > 70% 出待补清单但**不整批隔离**。

质量处置（表1 #20，逐条落地）
  - 文件打不开 / 解不出 / 整段无声  →【原始文件-损坏】→ 原始隔离区；
  - 治理后内容不达标（模态不足 / 文本空）→【样本-语义不通】→ 样本隔离库；
  - 抽检尚未回写 → **不拦样本**，版本保持待发布（报告 status 固定 pending_writeback）；
  - 模型证据不足 / 反向验证不一致 → 旁路 flagged（仍留在主输出，供 #24 抽检）。

统计守恒
  ``input == output + rejected + origin_quarantined + sample_quarantined``；
  ``flagged`` 是主输出的**旁路子集**，不与主记录重复加总；
  ``not_applicable`` 只表示不适用，**不计失败**。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import media_probe, reverse_verify as rv, upstream_metrics as um
from .lineage import now_iso
from .media_carry import MediaCarrier

# --------------------------------------------------------------------------- #
# 身份与产物名
# --------------------------------------------------------------------------- #

OPERATOR_NAME = "alignment_qc"
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S4"
FORMAL_OPERATOR_NUMBER = 20
# [v4.2] §4.20：回写前报告状态只能 pending_writeback（M24 回写后变 completed）
REPORT_STATUS = "pending_writeback"
VERSION_STATE_PENDING = "pending_release"

MAIN_NAME = "20_alignment_qc.jsonl"
CHECKS_NAME = "20_quality_checks.jsonl"
REVERSE_NAME = "20_reverse_verification.jsonl"
ORIGINAL_QUARANTINE_NAME = "20_original_quarantine.jsonl"
SAMPLE_QUARANTINE_NAME = "20_sample_quarantine.jsonl"
FLAGGED_NAME = "20_flagged.jsonl"
REJECTS_NAME = "20_rejects.jsonl"
REPORT_NAME = "20_quality_report.json"
SUPPLEMENT_NAME = "20_supplement_list.json"
LOG_NAME = "run.log"

OUTPUT_NAMES: Tuple[str, ...] = (
    MAIN_NAME, CHECKS_NAME, REVERSE_NAME, ORIGINAL_QUARANTINE_NAME,
    SAMPLE_QUARANTINE_NAME, FLAGGED_NAME, REJECTS_NAME, REPORT_NAME,
    SUPPLEMENT_NAME, LOG_NAME,
)

# --------------------------------------------------------------------------- #
# 检查项 / 状态 / 决策 / 标签
# --------------------------------------------------------------------------- #

CHECK_TEXT_NONEMPTY = "text_nonempty"
CHECK_FILE_OPENABLE = "file_openable"
CHECK_MODALITIES = "min_modalities"
CHECK_VIDEO_DECODABLE = "video_decodable"
CHECK_AUDIO_NON_SILENT = "audio_non_silent"
CHECK_METRIC_PPL = "ppl"
CHECK_METRIC_BRISQUE = "brisque"
CHECK_METRIC_SNR = "snr"
CHECK_REVERSE_VERIFY = "reverse_verify"

CHECK_ORDER: Tuple[str, ...] = (
    CHECK_TEXT_NONEMPTY, CHECK_FILE_OPENABLE, CHECK_MODALITIES,
    CHECK_VIDEO_DECODABLE, CHECK_AUDIO_NON_SILENT,
    CHECK_METRIC_PPL, CHECK_METRIC_BRISQUE, CHECK_METRIC_SNR,
    CHECK_REVERSE_VERIFY,
)

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_PENDING = "pending_pool"

DECISION_ACCEPTED = "accepted"
DECISION_FLAGGED = "flagged"
DECISION_SAMPLE_QUARANTINE = "sample_quarantine"
DECISION_ORIGIN_QUARANTINE = "origin_quarantine"
DECISION_SKIPPED = "skipped"

LABEL_PASSED = "样本-质检通过"
LABEL_PENDING_REVIEW = "流程-待人工复核"
LABEL_SAMPLE_SEMANTIC = "样本-语义不通"
LABEL_ORIGIN_UNPARSABLE = "原始文件-无法解析"
LABEL_ORIGIN_BROKEN = "原始文件-损坏"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_ORIGIN_DUPLICATE = "原始文件-重复标识"
LABEL_DISABLED = "流程-质检未启用"

DESTINATION_MAIN = "质量报告待回写"
DESTINATION_REVIEW = "人工复核队列"
DESTINATION_SAMPLE = "样本隔离库"
DESTINATION_ORIGIN = "原始隔离区"

MODALITY_KEYS: Tuple[str, ...] = ("text", "image", "video", "audio")

#: 原始隔离的原因优先级（越靠前越先判定）
_ORIGIN_PRIORITY: Tuple[str, ...] = (
    "unsupported_media_path", "media_missing", "media_unreadable",
    "media_undecodable", "audio_silent", "text_unreadable",
)

_ORIGIN_LABELS: Dict[str, str] = {
    "unsupported_media_path": LABEL_ORIGIN_UNPARSABLE,
    "media_missing": LABEL_ORIGIN_UNPARSABLE,
    "media_unreadable": LABEL_ORIGIN_BROKEN,
    "media_undecodable": LABEL_ORIGIN_BROKEN,
    "audio_silent": LABEL_ORIGIN_BROKEN,
    "text_unreadable": LABEL_ORIGIN_UNPARSABLE,
}


def origin_label(reason: str) -> str:
    """原始隔离的中文标签（表1 #20 的两类：【原始文件-无法解析】/【原始文件-损坏】）。"""
    if reason.startswith("missing_field"):
        return LABEL_ORIGIN_MISSING_FIELD
    if reason.startswith("duplicate"):
        return LABEL_ORIGIN_DUPLICATE
    return _ORIGIN_LABELS.get(reason, LABEL_ORIGIN_BROKEN)

_PROCESSING_STATUS: Dict[str, str] = {
    DECISION_ACCEPTED: "success",
    DECISION_FLAGGED: "pending_review",
    DECISION_SAMPLE_QUARANTINE: "isolated",
    DECISION_ORIGIN_QUARANTINE: "failed",
    DECISION_SKIPPED: "skipped",
}

# [v4.2] status 双字段（§2.10）：输出记录 int status（处理型 1=成功/待复核等非硬失败，
# 0=硬失败/隔离）+ 原字符串迁入 process_status；run() 顶层内部契约仍保持字符串。
_PROCESS_STATUS_AS_INT = {
    "success": 1,
    "pending_review": 1,
    "isolated": 0,
    "failed": 0,
    "skipped": 1,
}


def status_int(value: Optional[str]) -> int:
    """[v4.2] 记录级流程态字符串 → int status；未登记的一律按成功 1。"""
    return _PROCESS_STATUS_AS_INT.get(value or "", 1)


def default_business(**overrides) -> Dict[str, Any]:
    """与部署配置同源的业务参数（测试与业务共用，避免两套默认值）。"""
    from . import schemas  # 延迟导入，避免与 schemas 形成导入环

    return schemas.deep_merge(schemas.default_config().business_dict(), overrides)


def _cfg(business: Dict[str, Any], key: str, default: Any) -> Any:
    value = business.get(key, default)
    return default if value is None else value


def _metrics_cfg(business: Dict[str, Any]) -> Dict[str, str]:
    value = business.get("metrics") or {}
    return {kind: str(value.get(kind, "upstream_first"))
            for kind in ("ppl", "brisque", "snr", "ffmpeg")}


def _reverse_cfg(business: Dict[str, Any]) -> Dict[str, Any]:
    value = business.get("reverse_verify") or {}
    return {
        "enabled": bool(value.get("enabled", True)),
        "top_k": int(value.get("top_k", 5)),
        "min_consistency": float(value.get("min_consistency", 0.25)),
        "clip": dict(value.get("clip") or {}),
    }


# --------------------------------------------------------------------------- #
# 模态解析
# --------------------------------------------------------------------------- #

def _deep_get(record: Any, path: Sequence[str]) -> Any:
    current = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _path_like(value: Any, keys: Sequence[str]) -> Optional[str]:
    """从 ``str`` / ``dict`` 中取出可搬运的相对路径。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _nested_items(value: Any, keys: Sequence[str]) -> List[Dict[str, Any]]:
    """取出子项列表（视频帧 / 音频分段），每项至少含 ``path``，并保留时间戳等字段。"""
    items: Any = None
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                items = value[key]
                break
    if not isinstance(items, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            result.append({"path": item.strip()})
            continue
        if not isinstance(item, dict):
            continue
        path = _path_like(item, ("path", "file", "image", "audio", "frame"))
        if not path:
            continue
        normalized: Dict[str, Any] = {"path": path}
        for extra in ("start", "end", "label", "score", "index"):
            if extra in item:
                normalized[extra] = item[extra]
        result.append(normalized)
    return result


def _resolve_text(record: Dict[str, Any], media_root: Optional[Path]) -> Optional[Dict[str, Any]]:
    candidate: Any = None
    source: Optional[str] = None
    if isinstance(record.get("text"), (str, dict, list)):
        candidate, source = record["text"], "text"
    else:
        for key in ("caption", "description", "transcript"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                candidate, source = value, key
                break
    if candidate is None:
        meta = record.get("metadata")
        if isinstance(meta, dict):
            for key in ("description", "caption", "transcript"):
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    candidate, source = value, "metadata.%s" % key
                    break
    if candidate is None:
        return None

    if not isinstance(candidate, dict):
        state = media_probe.text_state(candidate)
        return {"content": state["text"], "source": source, "path": None,
                "external": False, "lang": None, "chars": state["chars"],
                "nonempty": state["nonempty"], "error": None}

    state = media_probe.text_state(candidate)
    path = _path_like(candidate, ("path", "file", "text_path"))
    lang = candidate.get("lang") if isinstance(candidate.get("lang"), str) else None
    inner = state["source"]
    merged_source = source if (inner in ("none", "string", "list")) else "%s.%s" % (source, inner)

    if not state["nonempty"] and path:
        descriptor = {"content": "", "source": merged_source, "path": path,
                      "external": True, "lang": lang, "chars": 0,
                      "nonempty": False, "error": None}
        if media_root is None:
            return descriptor
        from . import io as io_utils  # 延迟导入

        try:
            resolved = io_utils.resolve_media(media_root, path)
        except io_utils.UnsupportedMediaPath:
            descriptor["error"] = "unsupported_media_path"
            return descriptor
        file_state = media_probe.text_from_file(resolved)
        descriptor.update({"content": file_state.get("text", ""),
                           "chars": file_state.get("chars", 0),
                           "nonempty": bool(file_state.get("nonempty")),
                           "error": file_state.get("error")})
        return descriptor

    return {"content": state["text"], "source": merged_source, "path": path,
            "external": bool(path and state["nonempty"]), "lang": lang,
            "chars": state["chars"], "nonempty": state["nonempty"], "error": None}


_IMAGE_GETTERS = (
    lambda r: r.get("image"),
    lambda r: r.get("images"),
    lambda r: r.get("image_reference"),
    lambda r: r.get("image_path"),
    lambda r: (r.get("media") or {}).get("image") if isinstance(r.get("media"), dict) else None,
    lambda r: (r.get("media") or {}).get("images") if isinstance(r.get("media"), dict) else None,
    lambda r: (r.get("media") or {}).get("image_reference") if isinstance(r.get("media"), dict) else None,
    lambda r: _deep_get(r, ("processing", "04", "paths", "image")),
    lambda r: _deep_get(r, ("processing", "4", "paths", "image")),
    lambda r: _deep_get(r, ("processing", "04", "image")),
)

_VIDEO_GETTERS = (
    lambda r: r.get("video"),
    lambda r: r.get("video_reference"),
    lambda r: r.get("video_path"),
    lambda r: (r.get("media") or {}).get("video") if isinstance(r.get("media"), dict) else None,
    lambda r: _deep_get(r, ("processing", "05", "paths", "video")),
    lambda r: _deep_get(r, ("processing", "5", "video")),
)

_AUDIO_GETTERS = (
    lambda r: r.get("audio"),
    lambda r: r.get("audio_segment"),
    lambda r: r.get("audio_path"),
    lambda r: (r.get("media") or {}).get("audio") if isinstance(r.get("media"), dict) else None,
    lambda r: _deep_get(r, ("processing", "06", "paths", "audio")),
    lambda r: _deep_get(r, ("processing", "6", "audio")),
)


_MEDIA_PATH_KEYS = ("path", "file", "relative_path", "file_path", "uri",
                    "original_path", "media_path", "name")


def _media_value(record, getters):
    """Choose one actual media object; lists and aliases do not merge assets."""
    fallback = None
    for getter in getters:
        value = getter(record)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item is None:
                continue
            if fallback is None:
                fallback = item
            if _path_like(item, _MEDIA_PATH_KEYS):
                return item
    return fallback


def resolve_modalities(record: Dict[str, Any],
                       media_root: Optional[Path] = None) -> Dict[str, Optional[Dict[str, Any]]]:
    """归一化样本的四种模态引用，供后续检查使用（不改动原记录）。"""
    resolved: Dict[str, Optional[Dict[str, Any]]] = {key: None for key in MODALITY_KEYS}

    resolved["text"] = _resolve_text(record, media_root)

    raw_image = _media_value(record, _IMAGE_GETTERS)
    if raw_image is not None:
        path = _path_like(raw_image, _MEDIA_PATH_KEYS)
        path = path.replace('\\', '/') if path else path
        resolved["image"] = {"path": path, "raw": raw_image}

    raw_video = _media_value(record, _VIDEO_GETTERS)
    if raw_video is not None:
        path = _path_like(raw_video, _MEDIA_PATH_KEYS)
        path = path.replace('\\', '/') if path else path
        resolved["video"] = {"path": path, "raw": raw_video,
                             "frames": _nested_items(raw_video, ("frames", "keyframes"))}

    raw_audio = _media_value(record, _AUDIO_GETTERS)
    if raw_audio is not None:
        path = _path_like(raw_audio, _MEDIA_PATH_KEYS)
        path = path.replace('\\', '/') if path else path
        resolved["audio"] = {"path": path, "raw": raw_audio,
                             "segments": _nested_items(raw_audio, ("segments", "clips"))}
    return resolved


def route_of(modalities: Sequence[str]) -> str:
    """模态组合的路由名，例如 ``image+text``（固定按字母序，便于统计口径统一）。"""
    return "+".join(sorted(set(modalities)))


def _is_native_pair(record):
    return (isinstance(record.get('pair_id'), str) and bool(record['pair_id'].strip())
            and all(isinstance(record.get(side), dict) for side in ('left', 'right')))


def _native_modality_sources(record):
    """Choose whole per-modality sources: right beats left; explicit root wins."""
    selected = {}
    components = []
    for side in ('left', 'right'):
        node = record[side]
        inner = node.get('record') if isinstance(node.get('record'), dict) else node
        components.append((side + '.record' if inner is not node else side, inner))
    components.append(('', record))
    for origin, component in components:
        for kind, descriptor in resolve_modalities(component).items():
            if descriptor and (descriptor.get('path') or
                               (kind == 'text' and descriptor.get('nonempty'))):
                selected[kind] = (origin, component, descriptor)
    return selected


def _metric_for_source(record, kind, descriptor=None):
    metric = um.resolve_upstream_metric(record, kind)
    if metric is not None:
        return dict(metric, _projection_path=metric['path'])
    modality = {'ppl': 'text', 'brisque': 'image', 'snr': 'audio'}[kind]
    if descriptor is None:
        descriptor = resolve_modalities(record).get(modality)
    if not descriptor or modality == 'text':
        return None
    view = dict(record)
    view[modality] = descriptor['raw']
    metric = um.resolve_upstream_metric(view, kind)
    if metric is None:
        return None
    metric = dict(metric, _projection_path=metric['path'])

    def locate(container, path=()):
        if container is descriptor['raw']:
            return '.'.join(path)
        if isinstance(container, list):
            for index, child in enumerate(container):
                result = locate(child, path + (str(index),))
                if result:
                    return result
        elif isinstance(container, dict):
            for key in ('image', 'images', 'image_reference', 'image_path',
                        'audio', 'audio_segment', 'audio_path', 'media'):
                if key in container:
                    result = locate(container[key], path + (key,))
                    if result:
                        return result
        return None

    origin = locate(record)
    if origin and metric['path'].startswith(modality + '.'):
        metric['path'] = origin + metric['path'][len(modality):]
    return metric


def _native_upstream_metric(record, kind, selected=None):
    """Root evidence wins; a side metric belongs only to its selected modality."""
    root_metric = _metric_for_source(record, kind)
    if root_metric is not None or not _is_native_pair(record):
        return root_metric
    selected = selected if selected is not None else _native_modality_sources(record)
    modality = {'ppl': 'text', 'brisque': 'image', 'snr': 'audio'}[kind]
    chosen = selected.get(modality)
    if not chosen or not chosen[0]:
        return None
    origin, source, _descriptor = chosen
    metric = _metric_for_source(source, kind, _descriptor)
    if metric is not None:
        metric = dict(metric)
        metric['path'] = origin + '.' + metric['path']
    return metric


def _native_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Project native pair evidence for QC, retaining original side namespaces.

    Empty placeholders on one side must not erase the other side's evidence.
    Root fields have priority; side identifiers never become pair identifiers.
    Flat input retains its existing behavior.
    """
    if not _is_native_pair(record):
        return record
    selected = _native_modality_sources(record)
    merged: Dict[str, Any] = {}

    def merge(target, source):
        for key, value in source.items():
            if value is None or value == [] or value == {} or (
                    isinstance(value, str) and not value.strip()):
                continue
            if key in ('text', 'image', 'images', 'image_reference', 'image_path',
                       'audio', 'audio_segment', 'audio_path',
                       'video', 'video_reference', 'video_path'):
                target[key] = copy.deepcopy(value)
            elif isinstance(value, dict):
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
    # Reverse retrieval verifies a combination. A side's old combination is not
    # evidence for this newly formed pair; keep it only in its provenance namespace.
    metric_paths = [path for paths in um.UPSTREAM_PATHS.values() for path in paths]
    for path in list(um.UPSTREAM_REVERSE_PATHS) + metric_paths:
        node = merged
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(path[-1], None)
    merged.pop("sample_id", None)
    merge(merged, record)
    # Preserve full provenance, including empty fields, and root stage decisions.
    for key in ("left", "right", "sample_id", "decision", "score"):
        if key in record:
            merged[key] = copy.deepcopy(record[key])
    if isinstance(record.get("processing"), dict):
        merged.setdefault("processing", {}).update(copy.deepcopy(record["processing"]))
    # Media objects are indivisible: never retain old quality fields while
    # replacing their path with another side's asset.
    for kind, (_origin, source, descriptor) in selected.items():
        if kind == 'text':
            value = source.get('text')
            if value is None:
                value = {'content': descriptor['content']}
                if descriptor.get('path'):
                    value['path'] = descriptor['path']
        else:
            value = descriptor['raw']
        merged[kind] = copy.deepcopy(value)
    # Re-publish only the selected source's quality evidence. Preserve original
    # side payloads for audit; unrelated side metrics stay in those namespaces.
    for kind in um.METRIC_KINDS:
        metric = _native_upstream_metric(record, kind, selected)
        if metric is None:
            continue
        path = metric['_projection_path'].split('.')
        node = merged
        for key in path[:-1]:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        node[path[-1]] = metric['value']
    return merged


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #

@dataclass
class RecordOutcome:
    sample_id: Optional[str]
    decision: str
    label: str
    destination: str
    action: str
    reason_code: str
    processing_status: str
    checks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    metrics: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    record: Optional[Dict[str, Any]] = None
    ledger: Optional[Dict[str, Any]] = None
    reverse: Optional[Dict[str, Any]] = None
    reverse_candidate: Optional[Dict[str, Any]] = None
    #: 图文反向检索的候选集合条目（无论上游是否已有结论，都要进池才能做真实检索）
    pool_entry: Optional[Dict[str, Any]] = None
    issues: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    degraded: bool = False

    @property
    def flagged(self) -> bool:
        return self.decision == DECISION_FLAGGED

    @property
    def in_main_output(self) -> bool:
        return self.decision in (DECISION_ACCEPTED, DECISION_FLAGGED, DECISION_SKIPPED)


def _check(status: str, reason: Optional[str] = None,
           evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence or {}}


def _ledger_base(sample_id: Optional[str], lineno: Optional[int],
                 outcome_decision: str, business: Dict[str, Any]) -> Dict[str, Any]:
    process_status = _PROCESSING_STATUS.get(outcome_decision, "unknown")
    return {
        "operator": OPERATOR_NAME,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": business.get("operator_version", OPERATOR_VERSION),
        "stage_id": business.get("stage_id", STAGE_ID),
        "sample_id": sample_id,
        "line": lineno,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(process_status),
        "process_status": process_status,
        "human_verified": False,
        "deliverable": False,
        "source_reference": "operator_20",
        "detected_at": now_iso(),
    }


def _origin_outcome(sample_id: Optional[str], reason: str, detail: str,
                    record: Optional[Dict[str, Any]], lineno: Optional[int],
                    business: Dict[str, Any], checks: Dict[str, Dict[str, Any]],
                    media_report: Dict[str, Any]) -> RecordOutcome:
    ledger = _ledger_base(sample_id, lineno, DECISION_ORIGIN_QUARANTINE, business)
    ledger.update({
        "action": "origin_quarantine",
        "decision": DECISION_ORIGIN_QUARANTINE,
        "reason": reason,
        "detail": detail,
        "label": origin_label(reason),
        "destination": DESTINATION_ORIGIN,
        "review_required": True,
        "evidence": media_report,
        "record": record,
    })
    return RecordOutcome(
        sample_id=sample_id, decision=DECISION_ORIGIN_QUARANTINE,
        label=ledger["label"], destination=DESTINATION_ORIGIN,
        action="origin_quarantine", reason_code=reason,
        processing_status="failed", checks=checks, record=None, ledger=ledger,
        issues=[reason], evidence={"media": media_report}, detail=detail)


def _sample_outcome(sample_id: Optional[str], reason: str, detail: str,
                    record: Dict[str, Any], lineno: Optional[int],
                    business: Dict[str, Any], checks: Dict[str, Dict[str, Any]],
                    media_report: Dict[str, Any], valid_modalities: List[str],
                    issues: Sequence[str]) -> RecordOutcome:
    ledger = _ledger_base(sample_id, lineno, DECISION_SAMPLE_QUARANTINE, business)
    ledger.update({
        "action": "isolate",
        "decision": DECISION_SAMPLE_QUARANTINE,
        "reason": reason,
        "detail": detail,
        "label": LABEL_SAMPLE_SEMANTIC,
        "destination": DESTINATION_SAMPLE,
        "review_required": True,
        "evidence": {"media": media_report, "valid_modalities": list(valid_modalities)},
        "record": record,
    })
    return RecordOutcome(
        sample_id=sample_id, decision=DECISION_SAMPLE_QUARANTINE,
        label=LABEL_SAMPLE_SEMANTIC, destination=DESTINATION_SAMPLE,
        action="isolate", reason_code=reason, processing_status="isolated",
        checks=checks, record=None, ledger=ledger, issues=list(issues),
        evidence={"media": media_report, "valid_modalities": list(valid_modalities)},
        detail=detail)


# --------------------------------------------------------------------------- #
# 单条记录评估
# --------------------------------------------------------------------------- #

def evaluate_record(record: Dict[str, Any], *, sample_id: Optional[str],
                    media_root: Path, carrier: MediaCarrier,
                    business: Dict[str, Any], fallback_selected: bool = False,
                    lineno: Optional[int] = None) -> RecordOutcome:
    """对单条样本执行表1 #20 的全部检查并给出分诊结论。"""
    source_record = record
    record = _native_record(record)
    checks: Dict[str, Dict[str, Any]] = {}
    issues: List[str] = []
    metrics: Dict[str, Dict[str, Any]] = {}
    media_report: Dict[str, Any] = {}

    # 0) 未启用：透传（表1 #20 默认开启，此处仅支持显式关闭与回滚）
    if not bool(_cfg(business, "enabled", True)):
        output = copy.deepcopy(record)
        node = _build_node(record, sample_id, DECISION_SKIPPED, LABEL_DISABLED,
                           DESTINATION_MAIN, "skip", "route_disabled", "success",
                           {}, {}, None, [], {}, "质检未启用，按配置透传", business)
        output.setdefault("processing", {})["20"] = node
        output["operator_20"] = _flat_summary(node)
        return RecordOutcome(
            sample_id=sample_id, decision=DECISION_SKIPPED, label=LABEL_DISABLED,
            destination=DESTINATION_MAIN, action="skip", reason_code="route_disabled",
            processing_status="skipped", checks={}, record=output,
            issues=["route_disabled"], detail="质检未启用，按配置透传")

    # 1) 标识缺失 → 原始隔离区
    if not sample_id:
        checks[CHECK_MODALITIES] = _check(STATUS_FAILED, "missing_field:sample_id")
        return _origin_outcome(None, "missing_field:sample_id",
                               "样本缺少 sample_id，无法建立血缘与去重",
                               record, lineno, business, checks, media_report)

    modalities = resolve_modalities(record, media_root)

    # 2) 文本非空
    text = modalities["text"]
    if text is None:
        checks[CHECK_TEXT_NONEMPTY] = _check(STATUS_NOT_APPLICABLE, "modality_absent")
    elif text.get("error") in ("encoding_error", "unsupported_media_path", "missing"):
        checks[CHECK_TEXT_NONEMPTY] = _check(STATUS_FAILED, "text_unreadable",
                                             {"error": text.get("error"),
                                              "path": text.get("path")})
        issues.append("text_unreadable")
    else:
        min_chars = int(_cfg(business, "min_text_chars", 1))
        ok = bool(text.get("nonempty")) and int(text.get("chars", 0)) >= min_chars
        checks[CHECK_TEXT_NONEMPTY] = _check(
            STATUS_OK if ok else STATUS_FAILED,
            None if ok else "text_empty",
            {"chars": text.get("chars"), "min_chars": min_chars,
             "source": text.get("source"), "external": text.get("external")})
        if not ok:
            issues.append("text_empty")

    # 3) 媒体检查：可打开 / 视频可解码 / 音频非全静音
    silence_threshold = float(_cfg(business, "silence_rms_threshold", 1e-4))
    max_frames = int(_cfg(business, "max_video_frames_probe", 8))
    no_backend = False
    for kind in ("image", "video", "audio"):
        descriptor = modalities[kind]
        if descriptor is None:
            continue
        relative = descriptor.get("path")
        if not relative:
            media_report[kind] = {"status": "unsupported_media_path",
                                  "reason": "missing_media_path"}
            issues.append("unsupported_media_path")
            continue
        try:
            source_path = carrier.resolve(relative)
        except Exception as exc:  # noqa: BLE001 - 只可能是路径不合法
            media_report[kind] = {"status": "unsupported_media_path",
                                  "path": relative, "reason": type(exc).__name__}
            issues.append("unsupported_media_path")
            continue

        if kind == "image":
            report = media_probe.probe_image(source_path)
        elif kind == "video":
            report = media_probe.probe_video(source_path, max_frames=max_frames)
        else:
            report = media_probe.probe_audio(source_path,
                                             silence_rms_threshold=silence_threshold)
        report["path"] = relative
        report["source"] = descriptor.get("source")
        media_report[kind] = report

        probe_status = report.get("status")
        if probe_status == "no_backend":
            no_backend = True
            descriptor["probe"] = "no_backend"
            continue
        if probe_status != "ok":
            issues.append({"missing": "media_missing",
                           "unreadable": "media_unreadable",
                           "undecodable": "media_undecodable"}.get(probe_status,
                                                                   "media_unreadable"))
            continue
        descriptor["probe"] = "ok"
        # 探测通过才搬运媒体（坏文件的字节不再进入交付包）
        try:
            carried = carrier.carry(relative)
            descriptor["carried"] = carried
        except Exception as exc:  # noqa: BLE001
            media_report[kind]["carry_error"] = type(exc).__name__
        if kind == "image" and report.get("format"):
            descriptor["format"] = report["format"]
            descriptor["size"] = [report.get("width"), report.get("height")]
        if kind == "audio" and report.get("silent"):
            issues.append("audio_silent")

    # 视频帧 / 音频分段（作为佐证搬运，不单独判失败）
    for kind, keys in (("video", "frames"), ("audio", "segments")):
        descriptor = modalities[kind]
        if descriptor is None:
            continue
        for extra in descriptor.get(keys) or []:
            extra_path = extra.get("path") if isinstance(extra, dict) else extra
            if not extra_path:
                continue
            try:
                carrier.carry(extra_path)
            except Exception:  # noqa: BLE001 - 佐证帧缺失不算失败
                continue

    # 聚合「全量文件可打开」
    media_kinds = [kind for kind in ("image", "video", "audio")
                   if modalities[kind] is not None]
    if not media_kinds:
        checks[CHECK_FILE_OPENABLE] = _check(STATUS_NOT_APPLICABLE, "no_media")
    else:
        failed = [kind for kind, report in media_report.items()
                  if report.get("status") not in ("ok", "no_backend")]
        pending = [kind for kind, report in media_report.items()
                   if report.get("status") == "no_backend"]
        if failed:
            checks[CHECK_FILE_OPENABLE] = _check(
                STATUS_FAILED, "%s_%s" % (failed[0], media_report[failed[0]].get("status")),
                media_report)
        elif pending:
            checks[CHECK_FILE_OPENABLE] = _check(
                STATUS_INSUFFICIENT, "%s_no_backend" % pending[0], media_report)
        else:
            checks[CHECK_FILE_OPENABLE] = _check(STATUS_OK, None, media_report)

    # 视频可解码 / 音频非全静音
    video_report = media_report.get("video")
    if modalities["video"] is None:
        checks[CHECK_VIDEO_DECODABLE] = _check(STATUS_NOT_APPLICABLE, "modality_absent")
    elif video_report.get("status") == "ok":
        checks[CHECK_VIDEO_DECODABLE] = _check(STATUS_OK, None, video_report)
    elif video_report.get("status") == "no_backend":
        checks[CHECK_VIDEO_DECODABLE] = _check(STATUS_INSUFFICIENT,
                                               "video_decoder_missing", video_report)
    else:
        checks[CHECK_VIDEO_DECODABLE] = _check(
            STATUS_FAILED, "media_%s" % video_report.get("status"), video_report)

    audio_report = media_report.get("audio")
    if modalities["audio"] is None:
        checks[CHECK_AUDIO_NON_SILENT] = _check(STATUS_NOT_APPLICABLE, "modality_absent")
    elif audio_report.get("status") == "ok" and not audio_report.get("silent"):
        checks[CHECK_AUDIO_NON_SILENT] = _check(STATUS_OK, None, audio_report)
    elif audio_report.get("status") == "no_backend":
        checks[CHECK_AUDIO_NON_SILENT] = _check(STATUS_INSUFFICIENT,
                                                "audio_decoder_missing", audio_report)
    elif audio_report.get("status") == "ok" and audio_report.get("silent"):
        checks[CHECK_AUDIO_NON_SILENT] = _check(STATUS_FAILED, "audio_silent",
                                                audio_report)
    else:
        checks[CHECK_AUDIO_NON_SILENT] = _check(
            STATUS_FAILED, "media_%s" % audio_report.get("status"), audio_report)

    # 4) 有效模态数（模态存在且自身检查通过才算「可用」）
    valid_modalities: List[str] = []
    if modalities["text"] is not None and checks[CHECK_TEXT_NONEMPTY]["status"] == STATUS_OK:
        valid_modalities.append("text")
    for kind, check_name in (("image", CHECK_FILE_OPENABLE),
                             ("video", CHECK_VIDEO_DECODABLE),
                             ("audio", CHECK_AUDIO_NON_SILENT)):
        if modalities[kind] is None:
            continue
        if kind == "image":
            status = media_report.get("image", {}).get("status")
            if status == "ok":
                valid_modalities.append("image")
        elif checks[check_name]["status"] == STATUS_OK:
            valid_modalities.append(kind)

    if len(valid_modalities) >= 2:
        checks[CHECK_MODALITIES] = _check(STATUS_OK, None,
                                          {"valid_modalities": valid_modalities,
                                           "min_required": 2})
    else:
        checks[CHECK_MODALITIES] = _check(STATUS_FAILED, "insufficient_modalities",
                                          {"valid_modalities": valid_modalities,
                                           "min_required": 2})
        issues.append("insufficient_modalities")

    route = route_of(valid_modalities)

    # 5) 指标：上游优先复用，无上游才在 10% 子集补测
    metric_modes = _metrics_cfg(business)
    metric_inputs = {
        "ppl": {"applicable": modalities["text"] is not None,
                "text": (text or {}).get("content")},
        "brisque": {"applicable": modalities["image"] is not None,
                    "image_path": _safe_source_path(carrier, modalities["image"])},
        "snr": {"applicable": modalities["audio"] is not None,
                "audio_path": _safe_source_path(carrier, modalities["audio"])},
    }
    check_names = {"ppl": CHECK_METRIC_PPL, "brisque": CHECK_METRIC_BRISQUE,
                   "snr": CHECK_METRIC_SNR}
    for kind in ("ppl", "brisque", "snr"):
        upstream = _native_upstream_metric(source_record, kind)
        item = um.evaluate_metric(
            kind, mode=metric_modes.get(kind, "upstream_first"), upstream=upstream,
            applicable=bool(metric_inputs[kind]["applicable"]),
            fallback_selected=bool(fallback_selected),
            audio_path=metric_inputs[kind].get("audio_path"),
            text=metric_inputs[kind].get("text"),
            image_path=metric_inputs[kind].get("image_path"))
        metrics[kind] = item
        checks[check_names[kind]] = _check(item["status"], item.get("reason"), item)

    # 6) 图文反向验证：优先复用上游结论，缺失时延后到整池检索
    reverse_cfg = _reverse_cfg(business)
    reverse_result: Optional[Dict[str, Any]] = None
    reverse_candidate: Optional[Dict[str, Any]] = None
    pool_entry: Optional[Dict[str, Any]] = None
    image_pair_ok = (modalities["image"] is not None
                     and modalities["text"] is not None
                     and checks[CHECK_TEXT_NONEMPTY]["status"] == STATUS_OK
                     and media_report.get("image", {}).get("status") == "ok")
    if not reverse_cfg["enabled"]:
        checks[CHECK_REVERSE_VERIFY] = _check(STATUS_NOT_APPLICABLE, "route_disabled",
                                              {"enabled": False})
    elif not image_pair_ok:
        checks[CHECK_REVERSE_VERIFY] = _check(STATUS_NOT_APPLICABLE,
                                              "image_text_pair_absent")
    else:
        pool_entry = {
            "sample_id": sample_id,
            "text": (text or {}).get("content") or "",
            "image": modalities["image"].get("path"),
            "image_path": str(carrier.resolve(modalities["image"]["path"])),
        }
        upstream_reverse = um.resolve_upstream_reverse(source_record)
        if upstream_reverse is not None:
            reverse_result = rv.upstream_reverse_result(sample_id, upstream_reverse)
            consistent = reverse_result.get("consistent")
            checks[CHECK_REVERSE_VERIFY] = _check(
                STATUS_OK if consistent else STATUS_FAILED,
                None if consistent else "reverse_inconsistent",
                {"source": "upstream", "path": upstream_reverse.get("path"),
                 "passed": upstream_reverse.get("passed"),
                 "score": upstream_reverse.get("score")})
            if not consistent:
                issues.append("reverse_inconsistent")
        else:
            # 上游没有结论 → 延后到整池检索（真正构建候选集合重新检索）
            checks[CHECK_REVERSE_VERIFY] = _check(
                STATUS_PENDING, "deferred_to_pool",
                {"source": "local",
                 "min_consistency": reverse_cfg["min_consistency"]})
            reverse_candidate = pool_entry

    evidence = {"modalities": list(modalities.keys()),
                "valid_modalities": valid_modalities, "route": route,
                "media": media_report, "metrics": metrics}

    # 7) 分诊
    origin_reason = next((code for code in _ORIGIN_PRIORITY if code in issues), None)
    if origin_reason is not None:
        detail = _origin_detail(origin_reason, media_report, modalities)
        return _origin_outcome(sample_id, origin_reason, detail, record, lineno,
                               business, checks, media_report)

    if checks[CHECK_MODALITIES]["status"] == STATUS_FAILED or "text_empty" in issues:
        reason = "text_empty" if "text_empty" in issues else "insufficient_modalities"
        detail = ("文本为空，治理后内容不达标" if reason == "text_empty"
                  else "有效模态不足 2 个（当前 %s）" % (valid_modalities or "无"))
        return _sample_outcome(sample_id, reason, detail, record, lineno, business,
                               checks, media_report, valid_modalities, issues)

    if reverse_result is not None and reverse_result.get("consistent") is False:
        decision = DECISION_FLAGGED
        label, destination, action = LABEL_PENDING_REVIEW, DESTINATION_REVIEW, "flag"
        reason_code = "reverse_inconsistent"
        detail = "上游反向验证结论为不一致，标记待人工复核（不拦样本）"
        review_required = True
    elif no_backend:
        decision = DECISION_FLAGGED
        label, destination, action = LABEL_PENDING_REVIEW, DESTINATION_REVIEW, "flag"
        reason_code = "probe_backend_missing"
        detail = "本地媒体探测后端缺失，未能完成可打开校验，待环境补齐后复核"
        review_required = True
    else:
        decision = DECISION_ACCEPTED
        label, destination, action = LABEL_PASSED, DESTINATION_MAIN, "accept"
        reason_code = "quality_check_passed"
        detail = "通过文件可打开/模态/解码/静音/文本与指标检查"
        review_required = False

    output = copy.deepcopy(record)
    node = _build_node(record, sample_id, decision, label, destination, action,
                       reason_code, _PROCESSING_STATUS.get(decision, "success"),
                       checks, metrics, reverse_result, valid_modalities,
                       evidence, detail, business)
    node["review_required"] = review_required
    node["reverse_verify"] = (reverse_result if reverse_result is not None
                             else checks[CHECK_REVERSE_VERIFY])
    output.setdefault("processing", {})["20"] = node
    output["operator_20"] = _flat_summary(node)

    ledger = None
    if decision == DECISION_FLAGGED:
        ledger = _ledger_base(sample_id, lineno, decision, business)
        ledger.update({
            "action": action, "decision": decision, "reason": reason_code,
            "detail": detail, "label": label, "destination": destination,
            "review_required": True, "evidence": evidence, "record": output,
            "route": route,
        })

    return RecordOutcome(
        sample_id=sample_id, decision=decision, label=label, destination=destination,
        action=action, reason_code=reason_code,
        processing_status=_PROCESSING_STATUS.get(decision, "success"),
        checks=checks, metrics=metrics, record=output, ledger=ledger,
        reverse=reverse_result, reverse_candidate=reverse_candidate,
        pool_entry=pool_entry, issues=issues, evidence=evidence, detail=detail,
        degraded=no_backend)


def _safe_source_path(carrier: MediaCarrier, descriptor: Optional[Dict[str, Any]]):
    if not descriptor or not descriptor.get("path"):
        return None
    try:
        return carrier.resolve(descriptor["path"])
    except Exception:  # noqa: BLE001 - 路径非法时不做本地补测
        return None


def _origin_detail(reason: str, media_report: Dict[str, Any],
                   modalities: Dict[str, Any]) -> str:
    for kind, report in media_report.items():
        status = report.get("status")
        if reason == "audio_silent" and kind == "audio" and report.get("silent"):
            return "音频整段无声（rms=%s <= 阈值）" % report.get("rms")
        if reason == "media_missing" and status == "missing":
            return "%s 引用文件不存在：%s" % (kind, report.get("path"))
        if reason == "media_unreadable" and status == "unreadable":
            return "%s 文件无法打开：%s（%s）" % (kind, report.get("path"),
                                                report.get("error"))
        if reason == "media_undecodable" and status == "undecodable":
            return "%s 文件无法解码：%s（%s）" % (kind, report.get("path"),
                                                report.get("error"))
        if reason == "unsupported_media_path" and status == "unsupported_media_path":
            return "%s 媒体路径不合法：%s" % (kind, report.get("reason"))
        if reason == "text_unreadable":
            return "文本文件无法读取：%s" % (modalities.get("text") or {}).get("path")
    return reason


def _build_node(record: Dict[str, Any], sample_id: Optional[str], decision: str,
                label: str, destination: str, action: str, reason_code: str,
                processing_status: str, checks: Dict[str, Dict[str, Any]],
                metrics: Dict[str, Dict[str, Any]],
                reverse_result: Optional[Dict[str, Any]],
                valid_modalities: Sequence[str], evidence: Dict[str, Any],
                detail: str, business: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "operator": OPERATOR_NAME,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": business.get("operator_version", OPERATOR_VERSION),
        "stage_id": business.get("stage_id", STAGE_ID),
        "run_stage": "data_quality_assessment",
        "sample_id": sample_id,
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": status_int(processing_status),
        "process_status": processing_status,
        "action": action,
        "decision": decision,
        "label": label,
        "destination": destination,
        "reason_code": reason_code,
        "reason": detail,
        "checks": checks,
        "metrics": metrics,
        "modalities": list(valid_modalities),
        "route": route_of(valid_modalities),
        "human_verified": False,
        "deliverable": False,
        "source_reference": "operator_20",
        "evaluated_at": now_iso(),
    }


def _flat_summary(node: Dict[str, Any]) -> Dict[str, Any]:
    """扁平摘要，便于工程侧与人工抽检快速读取（权威节点仍是 ``processing.20``）。"""
    return {
        "operator": node["operator"],
        "operator_number": node["operator_number"],
        "operator_version": node["operator_version"],
        "stage_id": node["stage_id"],
        "sample_id": node["sample_id"],
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        "status": node["status"],
        "process_status": node.get("process_status"),
        "decision": node["decision"],
        "label": node["label"],
        "destination": node["destination"],
        "reason_code": node["reason_code"],
        "route": node["route"],
        "review_required": bool(node.get("review_required", False)),
        "human_verified": False,
        "deliverable": False,
        "source_reference": node["source_reference"],
        "evaluated_at": node["evaluated_at"],
    }


# --------------------------------------------------------------------------- #
# 台账 / 报告 / 偏科清单
# --------------------------------------------------------------------------- #

def metrics_config(business: Dict[str, Any]) -> Dict[str, str]:
    return _metrics_cfg(business)


def reverse_config(business: Dict[str, Any]) -> Dict[str, Any]:
    return _reverse_cfg(business)


def build_duplicate_entry(lineno: int, sample_id: str, record: Dict[str, Any],
                          business: Dict[str, Any], unique_id: str,
                          run_id: str) -> Dict[str, Any]:
    """重复标识的样本：进原始隔离区（保留先出现的样本，避免下游重复计数）。"""
    entry = _ledger_base(sample_id, lineno, DECISION_ORIGIN_QUARANTINE, business)
    entry.update({
        "uniqueId": unique_id,
        "runId": run_id,
        "action": "origin_quarantine",
        "decision": DECISION_ORIGIN_QUARANTINE,
        "reason": "duplicate_identifier",
        "detail": "sample_id 在同一批次内重复，保留首次出现的记录",
        "label": LABEL_ORIGIN_DUPLICATE,
        "destination": DESTINATION_ORIGIN,
        "review_required": True,
        "record": record,
    })
    return entry


def build_reject_entry(lineno: int, reason: str, raw_preview: str,
                       business: Dict[str, Any], unique_id: str, run_id: str) -> Dict[str, Any]:
    entry = _ledger_base(None, lineno, DECISION_ORIGIN_QUARANTINE, business)
    entry.update({
        "uniqueId": unique_id,
        "runId": run_id,
        "action": "reject",
        "decision": DECISION_ORIGIN_QUARANTINE,
        "reason": reason,
        "detail": "行级异常，未进入业务处理",
        "label": LABEL_ORIGIN_UNPARSABLE,
        "destination": DESTINATION_ORIGIN,
        "review_required": True,
        "raw_preview": raw_preview[:200],
    })
    return entry


def build_origin_ledger(outcome: RecordOutcome, business: Dict[str, Any],
                        unique_id: str, run_id: str) -> Dict[str, Any]:
    ledger = dict(outcome.ledger or {})
    ledger.update({"uniqueId": unique_id, "runId": run_id, "operator_number": FORMAL_OPERATOR_NUMBER})
    return ledger


def build_sample_ledger(outcome: RecordOutcome, business: Dict[str, Any],
                        unique_id: str, run_id: str) -> Dict[str, Any]:
    ledger = dict(outcome.ledger or {})
    ledger.update({"uniqueId": unique_id, "runId": run_id, "operator_number": FORMAL_OPERATOR_NUMBER})
    return ledger


def build_checks_entry(outcome: RecordOutcome, lineno: int) -> Dict[str, Any]:
    """检查明细一行：隔离与通过的样本都要有明细，便于 #24 抽检与人工复核。"""
    node = (outcome.record or {}).get("processing", {}).get("20") or {}
    evidence = outcome.evidence or {}
    modalities = node.get("modalities") or evidence.get("valid_modalities") or []
    route = node.get("route") or evidence.get("route") or route_of(modalities)
    return {
        "sample_id": outcome.sample_id,
        "line": lineno,
        "decision": outcome.decision,
        "label": outcome.label,
        "destination": outcome.destination,
        "reason_code": outcome.reason_code,
        "route": route,
        "modalities": list(modalities),
        "checks": outcome.checks,
        "metrics": outcome.metrics,
        "reverse_verify": node.get("reverse_verify"),
        "issues": outcome.issues,
        "checked_at": now_iso(),
        "source_reference": "operator_20",
    }


def build_supplement_list(route_counts: Dict[str, int], threshold: float, *,
                          unique_id: str, run_id: str, operator: str,
                          operator_version: str, stage_id: str,
                          generated_at: str) -> Dict[str, Any]:
    """偏科检测：单一路由占比 **严格大于** 阈值时出待补清单，但**不整批隔离**。"""
    total = sum(int(value) for value in route_counts.values())
    distribution: Dict[str, Dict[str, Any]] = {}
    dominant: Optional[str] = None
    dominant_share = 0.0
    for route, count in sorted(route_counts.items()):
        share = (float(count) / total) if total else 0.0
        distribution[route] = {"count": int(count), "share": round(share, 6)}
        if share > dominant_share:
            dominant, dominant_share = route, share

    limit = float(threshold)
    skewed = bool(total and dominant is not None and dominant_share > limit)
    targets: List[Dict[str, Any]] = []
    if skewed:
        for route, item in distribution.items():
            if route == dominant:
                continue
            shortfall = max(0.0, (limit - item["share"]))
            targets.append({
                "route": route,
                "count": item["count"],
                "share": item["share"],
                "share_gap_to_threshold": round(shortfall, 6),
                "suggested_supplement": "该模态组合占比不足，建议补充至占比 > %.2f" % limit,
            })
    return {
        "format": "crossmodal-supplement-list-v1",
        "operator": operator,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": operator_version,
        "stage_id": stage_id,
        "uniqueId": unique_id,
        "runId": run_id,
        "generated_at": generated_at,
        "topic_skew_threshold": limit,
        "total_records": total,
        "route_distribution": distribution,
        "dominant_route": dominant,
        "dominant_share": round(dominant_share, 6),
        "skewed": skewed,
        "supplement_targets": targets,
        "batch_isolation": False,
        "note": "偏科只出待补清单，不整批隔离（表1 #20）；样本处置仍需逐条依据检查结论",
    }


def build_quality_report(statistics: Dict[str, Any], checks: Dict[str, Dict[str, Any]],
                         metrics: Dict[str, Any], reverse: Dict[str, Any],
                         topic_skew: Dict[str, Any], *, unique_id: str, run_id: str,
                         operator: str, operator_version: str, stage_id: str,
                         generated_at: str, config_snapshot: Dict[str, Any],
                         reverse_available: bool,
                         routes: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """生成**待回写**质量报告；``status`` 固定 ``pending_writeback``。"""
    return {
        "format": "crossmodal-quality-report-v1",
        # [v4.2] §4.20 接口一：质量报告 ID（稳定可复现）
        "report_id": "qreport-%s-%s" % (unique_id, run_id),
        "operator": operator,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": operator_version,
        "stage_id": stage_id,
        "uniqueId": unique_id,
        "runId": run_id,
        "generated_at": generated_at,
        "status": REPORT_STATUS,
        "writeback": {
            "required": True,
            "writeback_by": "operator_24",
            "writeback_completed": False,
            "version_state": VERSION_STATE_PENDING,
        },
        "checks": checks,
        "metrics": metrics,
        "reverse_verification": reverse,
        "reverse_model_available": bool(reverse_available),
        "topic_skew": topic_skew,
        "route_distribution": routes or {},
        "isolation_suggestions": {
            "origin_quarantine": statistics.get("origin_quarantined", 0),
            "sample_quarantine": statistics.get("sample_quarantined", 0),
            "review_queue": statistics.get("flagged", 0),
            "handover": "operator_21",
            "batch_isolation": False,
        },
        "statistics": statistics,
        "config": config_snapshot,
    }


def checks_template() -> Dict[str, Dict[str, int]]:
    return {name: {"ok": 0, "failed": 0, "not_applicable": 0,
                   "insufficient_evidence": 0, "pending_pool": 0}
            for name in CHECK_ORDER}


def record_check_stats(counter: Dict[str, Dict[str, int]],
                       checks: Dict[str, Dict[str, Any]]) -> List[str]:
    """把单条记录的检查结果累加进全局计数，返回该记录触达的 not_applicable 项。"""
    touched: List[str] = []
    for name, entry in checks.items():
        bucket = counter.setdefault(name, {"ok": 0, "failed": 0, "not_applicable": 0,
                                           "insufficient_evidence": 0, "pending_pool": 0})
        status = entry.get("status")
        key = status if status in bucket else None
        if key:
            bucket[key] += 1
        if status == STATUS_NOT_APPLICABLE:
            touched.append(name)
    return touched


def summarize_reverse(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    summary = {"checked": 0, "consistent": 0, "inconsistent": 0,
               "insufficient_evidence": 0, "upstream_reused": 0, "local_scored": 0,
               "model_unavailable": 0, "not_applicable": 0}
    for item in results:
        status = item.get("check_status")
        if status == "checked":
            summary["checked"] += 1
            if item.get("consistent"):
                summary["consistent"] += 1
            else:
                summary["inconsistent"] += 1
        elif status == STATUS_NOT_APPLICABLE:
            summary["not_applicable"] += 1
        else:
            summary["insufficient_evidence"] += 1
        if item.get("reason") == "model_unavailable":
            summary["model_unavailable"] += 1
        source = item.get("source")
        if source == "upstream":
            summary["upstream_reused"] += 1
        elif source == "local":
            summary["local_scored"] += 1
    return summary


def digest_sample_ids(sample_ids: Sequence[str]) -> str:
    """计算接受样本标识摘要，便于与 #24 回写结果比对。"""
    hasher = hashlib.sha256()
    for sample_id in sorted(sample_ids):
        hasher.update(str(sample_id).encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
