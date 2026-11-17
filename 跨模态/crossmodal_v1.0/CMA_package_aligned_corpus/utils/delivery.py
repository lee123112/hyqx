"""★业务规则层：表1 #23「压缩交付对齐语料包」逐条落地。

表1 v4.1 #23 冻结口径（逐字，不得改写）：
  默认开启；否（必须）；类型 = 系统功能；优先级 = P0；模型 = ``--``
  输入：Token、已回写质量报告、抽检结论、隔离清单、版本说明
  处理边界：**材料齐全且 #24 已回写才把待发布改可交付；生成 ZIP 及 manifest；
           不打包隔离样本**
  输出和下游：可交付或待发布包、阶段血缘、ZIP/manifest；阶段末返回 outputFile
            内容和完整 lineageFile；交工程侧 OSS 保存及下一阶段重新上传
  质量处置：材料不齐或抽检未回写 → 不拦样本，版本保持待发布；
           已在隔离区的样本不进入语料包
  研发测边界：自动质量完成后仍待发布；隔离样本不得入包；版本说明声明启用路线和
            标注粒度；大包由工程侧 OSS/分片承载

本模块只做**确定性规则判定**：不加载任何模型、不产生任何分数、不伪造人工确认。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import io as io_utils
from . import material_handoff

__all__ = [
    "ACTION_EXCLUDE",
    "ACTION_ORIGIN",
    "ACTION_PACKAGE",
    "ALLOWED_ROUTES",
    "DEST_ORIGIN",
    "DEST_PACKAGE",
    "DEST_SAMPLE_ISOLATION",
    "EXCLUDED_NAME",
    "GATE_REQUIREMENTS",
    "LABEL_ISOLATED",
    "LABEL_ORIGIN_EMPTY",
    "LABEL_ORIGIN_UNREADABLE",
    "LABEL_PACKAGED",
    "LABEL_PENDING",
    "LABEL_DELIVERABLE",
    "LOG_NAME",
    "MAIN_NAME",
    "MATERIALS_FORMAT",
    "MATERIALS_NAME",
    "MATERIAL_ISOLATION_LIST",
    "MATERIAL_QUALITY_REPORT",
    "MATERIAL_ROLES",
    "MATERIAL_SAMPLING_CONCLUSION",
    "MATERIAL_TRAINING_FILES",
    "MATERIAL_VERSION_NOTES",
    "NAMESPACE",
    "OPERATOR",
    "OPERATOR_NUMBER",
    "OPERATOR_VERSION",
    "OWN_SIDECARS",
    "PACKAGE_DELIVERABLE_NAME",
    "PACKAGE_FORMAT",
    "PACKAGE_PENDING_NAME",
    "REJECTS_NAME",
    "REASON_DUPLICATE_IDENTIFIER",
    "REASON_EMPTY_TOKEN",
    "REASON_ENCODING",
    "REASON_JSON_PARSE",
    "REASON_MISSING_IDENTIFIER",
    "REASON_NOT_OBJECT",
    "REASON_PACKAGED",
    "REASON_UNSAFE_MEDIA_PATH",
    "REASON_UPSTREAM_ISOLATED",
    "SOURCE_REFERENCE",
    "STAGE_ID",
    "STATUS_LABEL_DELIVERABLE",
    "STATUS_LABEL_PENDING",
    "TRAIN_MEMBER",
    "VERSION_STATE_DELIVERABLE",
    "VERSION_STATE_PENDING",
    "classify_material_name",
    "evaluate_deliverable",
    "evaluate_record",
    "inspect_materials",
    "is_isolated",
    "make_ledger_entry",
    "make_reject_entry",
    "package_member_for_role",
    "parse_isolation_list",
    "parse_quality_report",
    "parse_sampling_conclusion",
    "parse_version_notes",
    "resolve_record",
]

# --------------------------------------------------------------------------- #
# 身份与产物名
# --------------------------------------------------------------------------- #

OPERATOR = "delivery_package"
OPERATOR_LABEL = "压缩交付对齐语料包"
OPERATOR_NUMBER = 23
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S6"                      # 表1 第 6 环节「数据集交付」
SOURCE_REFERENCE = "operator_23"
NAMESPACE = "operator_23"

MAIN_NAME = "23_delivery_package.jsonl"
EXCLUDED_NAME = "23_excluded_isolated.jsonl"
MATERIALS_NAME = "23_materials.json"
REJECTS_NAME = "23_rejects.jsonl"
LOG_NAME = "23_run.log"
PACKAGE_DELIVERABLE_NAME = "23_deliverable_package.zip"
PACKAGE_PENDING_NAME = "23_pending_package.zip"

OWN_SIDECARS: Sequence[str] = (MAIN_NAME, EXCLUDED_NAME, MATERIALS_NAME, REJECTS_NAME)

PACKAGE_FORMAT = "crossmodal-delivery-package-v1"
MATERIALS_FORMAT = "crossmodal-materials-v1"

TRAIN_MEMBER = "train/samples.jsonl"
PACKAGE_MANIFEST_MEMBER = "manifest.json"
PACKAGE_VERSION_MEMBER = "version.json"

MATERIAL_TRAINING_FILES = "training_files"
MATERIAL_QUALITY_REPORT = "quality_report"
MATERIAL_SAMPLING_CONCLUSION = "sampling_conclusion"
MATERIAL_ISOLATION_LIST = "isolation_list"
MATERIAL_VERSION_NOTES = "version_notes"

#: 表1 #23「包内要有训练文件、质量报告、抽检结论、隔离清单和版本说明」
MATERIAL_ROLES: Sequence[str] = (MATERIAL_TRAINING_FILES, MATERIAL_QUALITY_REPORT,
                                 MATERIAL_SAMPLING_CONCLUSION,
                                 MATERIAL_ISOLATION_LIST, MATERIAL_VERSION_NOTES)

#: 材料在包内的固定成员路径
PACKAGE_MEMBER_BY_ROLE: Dict[str, str] = {
    MATERIAL_QUALITY_REPORT: "materials/quality_report.json",
    MATERIAL_SAMPLING_CONCLUSION: "materials/sampling_conclusion.json",
    MATERIAL_ISOLATION_LIST: "materials/isolation_list.jsonl",
    MATERIAL_VERSION_NOTES: "materials/version_notes.json",
}

#: 表1 #23「什么时候可以从待发布改成可交付：三项缺一不可」（只读，不可配置放宽）
GATE_REQUIREMENTS: Sequence[str] = ("training_files_present",
                                    "quality_report_written_back",
                                    "sampling_written_back")

VERSION_STATE_PENDING = "pending_release"
VERSION_STATE_DELIVERABLE = "deliverable"
STATUS_LABEL_PENDING = "待发布"
STATUS_LABEL_DELIVERABLE = "可交付"
LABEL_PENDING = STATUS_LABEL_PENDING
LABEL_DELIVERABLE = STATUS_LABEL_DELIVERABLE

# --------------------------------------------------------------------------- #
# 动作、去向与标签（表1 质量处置口径）
# --------------------------------------------------------------------------- #

ACTION_PACKAGE = "package"
ACTION_EXCLUDE = "exclude"
ACTION_ORIGIN = "origin"

DEST_PACKAGE = "delivery_package"
DEST_SAMPLE_ISOLATION = "sample_isolation_library"
DEST_ORIGIN = "origin_quarantine"

LABEL_PACKAGED = "样本-已入交付包"
LABEL_ISOLATED = "样本-语义不通"
LABEL_ORIGIN_EMPTY = "原始文件-空"
LABEL_ORIGIN_UNREADABLE = "原始文件-无法解析"

REASON_PACKAGED = "packaged_into_delivery_package"
REASON_UPSTREAM_ISOLATED = "upstream_isolated"
REASON_EMPTY_TOKEN = "empty_field:token"
REASON_MISSING_IDENTIFIER = "missing_field:sample_id"
REASON_DUPLICATE_IDENTIFIER = "duplicate_identifier"
REASON_UNSAFE_MEDIA_PATH = "unsupported_media_path"
REASON_JSON_PARSE = "json_parse_error"
REASON_NOT_OBJECT = "record_not_object"
REASON_ENCODING = "encoding_error"

ALLOWED_ROUTES: Sequence[str] = ("image_text", "audio_text", "video_text")

_ISOLATED_STATUS = {"isolated", "quarantined", "sample_isolation_library",
                    "isolation", "isolate"}

_MEDIA_CONTAINERS = ("media", "image", "images", "audio", "video", "frames",
                     "asset", "assets", "files")
_REF_KEYS = ("path", "file", "uri", "url", "ref", "source", "image_path",
             "file_path", "relative_path")
_NESTED_KEYS = ("items", "frames", "files", "records", "media", "paths")


# --------------------------------------------------------------------------- #
# 记录解析
# --------------------------------------------------------------------------- #

def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_token(payload: Dict[str, Any]) -> str:
    token = payload.get("token")
    if isinstance(token, str):
        return token
    if isinstance(token, list):
        return " ".join(str(item) for item in token)
    tokens = payload.get("tokens")
    if isinstance(tokens, str):
        return tokens
    if isinstance(tokens, list):
        return " ".join(str(item) for item in tokens)
    # text.tokens属于单模态分词，不能替代#22生成的跨模态Token。
    processing=payload.get('processing')
    node = processing.get('22') if isinstance(processing,dict) else None
    if isinstance(node, dict) and isinstance(node.get('token_text'), str):
        return node['token_text']
    return ""


def _extract_route(payload: Dict[str, Any]) -> Optional[str]:
    routes = payload.get("routes") or payload.get("route")
    if isinstance(routes, str):
        return routes.strip() or None
    if isinstance(routes, list):
        for item in routes:
            if isinstance(item, str) and item.strip():
                return item.strip()
    node = payload.get("processing")
    if isinstance(node, dict):
        inner = node.get("22")
        if isinstance(inner, dict):
            return _first_text(inner.get("route"))
    return None


def _collect_refs(value: Any, media: List[str], external: List[str],
                  unsafe: List[str], seen: Optional[set] = None) -> None:
    seen = seen if seen is not None else set()
    if isinstance(value, str):
        _classify_ref(value, media, external, unsafe, seen)
        return
    if isinstance(value, dict):
        for key in _REF_KEYS:
            if isinstance(value.get(key), str):
                _classify_ref(value[key], media, external, unsafe, seen)
        for key in _NESTED_KEYS:
            if key in value:
                _collect_refs(value[key], media, external, unsafe, seen)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_refs(item, media, external, unsafe, seen)


def _classify_ref(raw: str, media: List[str], external: List[str],
                  unsafe: List[str], seen: set) -> None:
    candidate = raw.strip()
    if not candidate or candidate in seen:
        return
    seen.add(candidate)
    if io_utils.is_external_reference(candidate):
        external.append(candidate)
        return
    try:
        normalized = io_utils._normalize_relative(candidate)
    except io_utils.UnsupportedMediaPath:
        unsafe.append(candidate)
        return
    media.append(normalized)


def resolve_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    """解析一条 #22 Token 记录：标识、Token、路线、媒体/外部/非法引用。"""
    media: List[str] = []
    external: List[str] = []
    unsafe: List[str] = []
    seen: set = set()
    for container in _MEDIA_CONTAINERS:
        if container in payload:
            _collect_refs(payload[container], media, external, unsafe, seen)
    identifier = _first_text(payload.get("sample_id"), payload.get("id"))
    return {"identifier": identifier,
            "token": _extract_token(payload),
            "route": _extract_route(payload),
            "media_refs": media,
            "external_refs": external,
            "unsafe_refs": unsafe}


def is_isolated(payload: Dict[str, Any],
                isolation_ids: Iterable[str]) -> Optional[Dict[str, Any]]:
    """判断样本是否已在隔离区（#21 标记或隔离清单），返回隔离证据或 ``None``。"""
    identifier = _first_text(payload.get("sample_id"), payload.get("id"))
    known = set(isolation_ids)
    if identifier and identifier in known:
        return {"source": "isolation_list", "reason_code": REASON_UPSTREAM_ISOLATED,
                "label": LABEL_ISOLATED, "detail": "样本 ID 出现在隔离清单中"}

    markers: List[Any] = []
    processing = payload.get("processing")
    if isinstance(processing, dict):
        markers.append(processing.get("21"))
        for key in ("17", "18"):                     # #17 incomplete / #18 isolated
            node = processing.get(key)
            if isinstance(node, dict) and str(node.get("status") or "").lower() in _ISOLATED_STATUS:
                markers.append(node)
    markers.extend([payload.get("disposition"), payload.get("isolation"),
                    payload.get("qc"), payload.get("sample_status"),
                    payload.get("delivery_status")])
    for marker in markers:
        verdict = _isolation_from_marker(marker, payload)
        if verdict is not None:
            return verdict
    return None


def _isolation_from_marker(marker: Any, payload: Dict[str, Any]
                           ) -> Optional[Dict[str, Any]]:
    if isinstance(marker, bool):
        if marker:
            return {"source": "record_marker", "reason_code": REASON_UPSTREAM_ISOLATED,
                    "label": LABEL_ISOLATED, "detail": "记录自带隔离布尔标记"}
        return None
    if isinstance(marker, str):
        if marker.strip().lower() in _ISOLATED_STATUS:
            return {"source": "record_marker", "reason_code": REASON_UPSTREAM_ISOLATED,
                    "label": LABEL_ISOLATED, "detail": "记录状态为 %s" % marker}
        return None
    if not isinstance(marker, dict):
        return None
    status = str(marker.get("status") or marker.get("decision") or "").lower()
    isolated = marker.get("isolated")
    if isolated is True or status in _ISOLATED_STATUS:
        return {"source": "record_marker",
                "reason_code": _first_text(marker.get("reason_code")) or REASON_UPSTREAM_ISOLATED,
                "label": _first_text(marker.get("label")) or LABEL_ISOLATED,
                "detail": _first_text(marker.get("detail"),
                                      "记录被上游标记为隔离（%s）" % (status or "isolated"))}
    return None


# --------------------------------------------------------------------------- #
# 三路分流
# --------------------------------------------------------------------------- #

def evaluate_record(payload: Dict[str, Any], *, isolation_ids: Iterable[str],
                    event_time: str, config: Any, line: int = 0,
                    version_state: str = VERSION_STATE_PENDING,
                    enabled_routes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """按「结构化 → 隔离 → 入包」顺序判定单条记录。

    ``enabled_routes`` 来自版本说明声明的启用路线，用于标注**模态适用性**
    （``applicable`` / ``not_applicable`` / ``unknown``）。不适用记录**保留在主输出**
    并单独计数，不丢样本、不计入拒绝，避免与主记录重复加总。
    """
    spec = resolve_record(payload)
    identifier = spec["identifier"]

    if not identifier:
        return _origin(spec, payload, REASON_MISSING_IDENTIFIER,
                       LABEL_ORIGIN_UNREADABLE, "记录缺少 sample_id，进入原始隔离区")
    if spec["unsafe_refs"]:
        return _origin(spec, payload, REASON_UNSAFE_MEDIA_PATH,
                       LABEL_ORIGIN_UNREADABLE,
                       "媒体引用不合法（绝对路径/穿越/反斜杠）：%s"
                       % spec["unsafe_refs"][0])
    if getattr(config, "require_token", True) and not spec["token"].strip():
        return _origin(spec, payload, REASON_EMPTY_TOKEN, LABEL_ORIGIN_EMPTY,
                       "Token 为空，不能作为训练样本进入交付包")

    verdict = is_isolated(payload, isolation_ids)
    if verdict is not None:
        entry = make_ledger_entry(line=line, payload=payload, action=ACTION_EXCLUDE,
                                  label=verdict["label"],
                                  destination=DEST_SAMPLE_ISOLATION,
                                  reason_code=REASON_UPSTREAM_ISOLATED,
                                  event_time=event_time, detail=verdict["detail"],
                                  upstream_reason_code=verdict["reason_code"],
                                  isolation_source=verdict["source"])
        return {"action": ACTION_EXCLUDE, "reason": REASON_UPSTREAM_ISOLATED,
                "label": verdict["label"], "destination": DEST_SAMPLE_ISOLATION,
                "detail": verdict["detail"], "entry": entry, "spec": spec,
                "isolation_source": verdict["source"]}

    applicability = _applicability(spec["route"], enabled_routes)
    output = _with_namespace(payload, spec, event_time=event_time,
                             version_state=version_state, config=config,
                             applicability=applicability)
    return {"action": ACTION_PACKAGE, "reason": REASON_PACKAGED,
            "label": LABEL_PACKAGED, "destination": DEST_PACKAGE,
            "detail": "材料/门禁判定见 23_materials.json", "output": output,
            "spec": spec, "applicability": applicability}


def _applicability(route: Optional[str],
                   enabled_routes: Optional[Sequence[str]]) -> str:
    """模态适用性：版本说明声明了启用路线时按路线比对，否则为 ``unknown``。"""
    if not enabled_routes:
        return "unknown"
    if not route:
        return "unknown"
    return "applicable" if route in set(enabled_routes) else "not_applicable"


def _origin(spec: Dict[str, Any], payload: Dict[str, Any], reason: str, label: str,
            detail: str) -> Dict[str, Any]:
    return {"action": ACTION_ORIGIN, "reason": reason, "label": label,
            "destination": DEST_ORIGIN, "detail": detail, "spec": spec}


def _with_namespace(payload: Dict[str, Any], spec: Dict[str, Any], *, event_time: str,
                    version_state: str, config: Any,
                    applicability: str = "unknown") -> Dict[str, Any]:
    record = copy.deepcopy(payload)
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    node = {
        "operator": OPERATOR,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "source_reference": SOURCE_REFERENCE,
        "status": 1,                       # [v4.2] int status（packaged=成功/不过滤）
        "process_status": "packaged",      # [v4.2] 字符串流程态（规则 §2.10）
        "decision": version_state,
        "label": LABEL_PACKAGED,
        "action": ACTION_PACKAGE,
        "destination": DEST_PACKAGE,
        "reason_code": REASON_PACKAGED,
        "evidence": {
            "member": TRAIN_MEMBER,
            "route": spec["route"],
            "token_present": bool(spec["token"].strip()),
            "media_refs": list(spec["media_refs"]),
            "external_refs": list(spec["external_refs"]),
            "applicability": applicability,
            "materials_source": "23_materials.json",
        },
        "member": TRAIN_MEMBER,
        "token_present": bool(spec["token"].strip()),
        "applicability": applicability,
        "human_verified": False,
        "review_required": True,
        "event_time": event_time,
        "batch_id": _first_text(record.get("batch_id"), metadata.get("batch_id")),
        "sample_id": spec["identifier"],
        "retry_count": int(metadata.get("retry_count") or 0),
        "route": spec["route"],
    }
    record[NAMESPACE] = node
    processing = record.get("processing")
    if not isinstance(processing, dict):
        processing = {}
    processing = dict(processing)
    processing["23"] = {"operator": OPERATOR, "operator_number": OPERATOR_NUMBER,
                        "status": 1, "process_status": "packaged",  # [v4.2] status 双字段
                        "decision": version_state,
                        "route": spec["route"], "member": TRAIN_MEMBER,
                        "source_reference": SOURCE_REFERENCE}
    record["processing"] = processing
    return record


# --------------------------------------------------------------------------- #
# 台账条目
# --------------------------------------------------------------------------- #

def make_ledger_entry(*, line: int, payload: Dict[str, Any], action: str, label: str,
                      destination: str, reason_code: str, event_time: str,
                      detail: Optional[str] = None, max_embedded_bytes: int = 32768,
                      upstream_reason_code: Optional[str] = None,
                      isolation_source: Optional[str] = None) -> Dict[str, Any]:
    """统一台账条目：保留表1 要求的最小字段集 + 可追溯证据。"""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    entry: Dict[str, Any] = {
        "label": label,
        "stage": STAGE_ID,
        "operator": SOURCE_REFERENCE,
        "action": action,
        "destination": destination,
        "reason_code": reason_code,
        "detail": detail,
        "event_time": event_time,
        "batch_id": _first_text(payload.get("batch_id"), metadata.get("batch_id")),
        "sample_id": _first_text(payload.get("sample_id"), payload.get("id")),
        "retry_count": int(metadata.get("retry_count") or 0),
        "line": line,
    }
    if upstream_reason_code:
        entry["upstream_reason_code"] = upstream_reason_code
    if isolation_source:
        entry["isolation_source"] = isolation_source
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= max_embedded_bytes:
        entry["record"] = payload
    else:
        entry["record_omitted"] = True
        entry["record_bytes"] = len(encoded)
    return entry


def make_reject_entry(*, line: int, reason: str, detail: str, detected_at: str,
                      raw_text: Optional[str] = None,
                      identifier: Optional[str] = None) -> Dict[str, Any]:
    """原始隔离区台账条目（坏行/编码错误/缺标识）。"""
    entry: Dict[str, Any] = {
        "line": line,
        "label": LABEL_ORIGIN_UNREADABLE,
        "stage": STAGE_ID,
        "operator": SOURCE_REFERENCE,
        "action": ACTION_ORIGIN,
        "destination": DEST_ORIGIN,
        "reason_code": reason,
        "detail": detail,
        "detected_at": detected_at,
        "event_time": detected_at,
        "sample_id": identifier,
        "batch_id": None,
        "retry_count": 0,
    }
    if raw_text is not None:
        preview = raw_text[:200]
        entry["raw_text_preview"] = preview
        entry["raw_text_truncated"] = len(raw_text) > len(preview)
    return entry


# --------------------------------------------------------------------------- #
# 材料分类与发现
# --------------------------------------------------------------------------- #

def classify_material_name(name: str) -> Optional[str]:
    """按文件名（不区分大小写）判断材料角色；无法判断返回 ``None``。"""
    if not isinstance(name, str) or not name:
        return None
    stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if stem.endswith(".partial"):
        return None
    if "isolation" in stem or "quarantine" in stem or "隔离" in stem:
        return MATERIAL_ISOLATION_LIST
    if "sampling" in stem or "inspection" in stem or "抽检" in stem:
        return MATERIAL_SAMPLING_CONCLUSION
    if "quality" in stem or "qc_report" in stem or "质检" in stem:
        return MATERIAL_QUALITY_REPORT
    if "version" in stem or "版本" in stem:
        return MATERIAL_VERSION_NOTES
    return None


def _classify_material_path(path: Path) -> Optional[str]:
    """先按名字，再按内容签名判断材料角色（只读小文件，不联网）。"""
    role = classify_material_name(path.name)
    if role is not None:
        return role
    if path.suffix.lower() not in {".json", ".jsonl", ".txt"}:
        return None
    try:
        if path.suffix.lower() == ".json":
            payload = io_utils.read_json_file(path)
        else:
            first = None
            for line in io_utils.iter_decoded_lines(path):
                if line.strip():
                    first = json.loads(line)
                    break
            payload = first
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    if "sampling" in payload or "sampling_rate" in payload:
        return MATERIAL_SAMPLING_CONCLUSION
    if "checks" in payload or "quality" in payload or "writeback" in payload and "checks" in payload:
        return MATERIAL_QUALITY_REPORT
    if "enabled_routes" in payload or "annotation_granularity" in payload:
        return MATERIAL_VERSION_NOTES
    return None


def parse_quality_report(payload: Any) -> Dict[str, Any]:
    """解析已回写质量报告：#24 未回写时**不得**判为可交付。"""
    if not isinstance(payload, dict):
        return {"written_back": False, "status": None,
                "evidence": {"error": "质量报告不是 JSON 对象"}}
    status = payload.get("status")
    writeback = payload.get("writeback") if isinstance(payload.get("writeback"), dict) else {}
    written = (payload.get("written_back") is True
               or payload.get("quality_report_written_back") is True
               or writeback.get("completed") is True
               or (isinstance(status, str)
                   and status.strip().lower() in {"written_back", "writeback_completed"}))
    return {"written_back": bool(written),
            "status": status if isinstance(status, str) else None,
            "evidence": {"operator": _first_text(payload.get("operator")),
                         "report_id": _first_text(payload.get("report_id")),
                         "writeback": writeback,
                         "version_state": _first_text(payload.get("version_state"))}}


def parse_sampling_conclusion(payload: Any) -> Dict[str, Any]:
    """解析抽检结论的回写状态（#24 结论未回写 → 版本保持待发布）。"""
    if not isinstance(payload, dict):
        return {"written_back": False, "status": None,
                "evidence": {"error": "抽检结论不是 JSON 对象"}}
    status = payload.get("status")
    writeback = payload.get("writeback") if isinstance(payload.get("writeback"), dict) else {}
    written = (payload.get("written_back") is True
               or payload.get("conclusion_written_back") is True
               or payload.get("sampling_written_back") is True
               or writeback.get("completed") is True
               or (isinstance(status, str)
                   and status.strip().lower() in {"written_back", "writeback_completed"}))
    return {"written_back": bool(written),
            "status": status if isinstance(status, str) else None,
            "sampling_rate": payload.get("sampling_rate"),
            "evidence": {"operator": _first_text(payload.get("operator")),
                         "writeback": writeback,
                         "conclusion": payload.get("conclusion")
                         if isinstance(payload.get("conclusion"), dict) else None}}


def parse_isolation_list(payload: Any) -> Dict[str, Any]:
    """解析隔离清单，返回样本 ID 集合（隔离样本不得进入交付包）。"""
    entries: Any = payload
    if isinstance(payload, dict):
        for key in ("entries", "records", "items", "isolated", "samples"):
            if key in payload:
                entries = payload[key]
                break
        else:
            if isinstance(payload.get("sample_ids"), list):
                entries = [{"sample_id": item} for item in payload["sample_ids"]]
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return {"valid": False, "sample_ids": [], "entries": 0,
                "detail": "隔离清单必须是记录数组"}
    ids: List[str] = []
    invalid = 0
    for item in entries:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            value = _first_text(item.get("sample_id"), item.get("id"))
            if value:
                ids.append(value)
            else:
                invalid += 1
        else:
            invalid += 1
    return {"valid": invalid == 0, "sample_ids": ids, "entries": len(entries),
            "invalid_entries": invalid,
            "detail": None if invalid == 0 else "%d 条隔离记录缺少 sample_id" % invalid}


def parse_version_notes(payload: Any) -> Dict[str, Any]:
    """解析版本说明：**必须声明启用路线与标注粒度**（表1 研发测边界）。"""
    if not isinstance(payload, dict):
        return {"valid": False, "routes": [], "granularity": None,
                "missing": ["enabled_routes", "annotation_granularity"],
                "detail": "版本说明不是 JSON 对象"}
    routes_raw = payload.get("enabled_routes")
    if routes_raw in (None, []):
        routes_raw = payload.get("routes")
    if routes_raw in (None, []) and isinstance(payload.get("启用路线"), list):
        routes_raw = payload["启用路线"]
    routes: List[str] = []
    invalid_routes: List[str] = []
    if isinstance(routes_raw, str):
        routes_raw = [item for item in routes_raw.replace(",", "|").split("|") if item.strip()]
    if isinstance(routes_raw, list):
        for item in routes_raw:
            token = str(item).strip().lower().replace("-", "_")
            if not token:
                continue
            if token in ALLOWED_ROUTES:
                if token not in routes:
                    routes.append(token)
            else:
                invalid_routes.append(token)
    granularity = payload.get("annotation_granularity")
    if not isinstance(granularity, str) or not granularity.strip():
        granularity = payload.get("granularity") if isinstance(payload.get("granularity"), str) else None
    if not isinstance(granularity, str) or not granularity.strip():
        granularity_value = None
    else:
        granularity_value = granularity.strip()

    missing: List[str] = []
    if not routes:
        missing.append("enabled_routes")
    if invalid_routes:
        missing.append("enabled_routes:unknown")
    if granularity_value is None:
        missing.append("annotation_granularity")
    detail = None
    if missing:
        detail = "版本说明缺少或非法：" + ", ".join(missing)
    return {"valid": not missing, "routes": routes, "granularity": granularity_value,
            "invalid_routes": invalid_routes, "missing": missing, "detail": detail,
            "dataset_version": _first_text(payload.get("dataset_version"),
                                           payload.get("version"))}


def _validate_material(role: str, path: Path) -> Dict[str, Any]:
    """读取并校验一份材料，返回角色条目（不含大小等元信息）。"""
    status = "found"
    detail: List[str] = []
    payload: Any = None
    if role == MATERIAL_ISOLATION_LIST:
        entries: List[Any] = []
        try:
            for line in io_utils.iter_decoded_lines(path):
                if line.strip():
                    entries.append(json.loads(line))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return {"status": "invalid", "detail": ["隔离清单无法解析：%s" % exc]}
        payload = entries
    else:
        try:
            payload = io_utils.read_json_file(path)
        except ValueError as exc:
            return {"status": "invalid", "detail": [str(exc)]}

    extra: Dict[str, Any] = {}
    if role == MATERIAL_QUALITY_REPORT:
        verdict = parse_quality_report(payload)
        extra["written_back"] = verdict["written_back"]
        extra["material_status"] = verdict["status"]
        if not verdict["written_back"]:
            extra["writeback_pending"] = True
    elif role == MATERIAL_SAMPLING_CONCLUSION:
        verdict = parse_sampling_conclusion(payload)
        extra["written_back"] = verdict["written_back"]
        extra["material_status"] = verdict["status"]
        if not verdict["written_back"]:
            extra["writeback_pending"] = True
    elif role == MATERIAL_ISOLATION_LIST:
        verdict = parse_isolation_list(payload)
        extra["sample_ids"] = verdict["sample_ids"]
        extra["entries"] = verdict["entries"]
        if not verdict["valid"]:
            status = "invalid"
            detail.append(verdict["detail"] or "隔离清单不合法")
    elif role == MATERIAL_VERSION_NOTES:
        verdict = parse_version_notes(payload)
        extra["routes"] = verdict["routes"]
        extra["granularity"] = verdict["granularity"]
        extra["dataset_version"] = verdict["dataset_version"]
        if not verdict["valid"]:
            status = "invalid"
            detail.append(verdict["detail"] or "版本说明不完整")
    return {"status": status, "detail": detail, **extra}


def inspect_materials(root: Path, *, config: Any, entry_path: Path) -> Dict[str, Any]:
    """发现并校验 #23 必需材料（训练文件、质量报告、抽检结论、隔离清单、版本说明）。

    查找顺序（每个角色取**第一个命中层级**）：
    1. 部署配置显式路径（请求不可覆盖，允许包外只读路径）；
    2. ``<root>/materials/`` 一层；
    3. ``<root>/`` 一层；
    4. ``<root>/`` 递归（记录 ``recursive_scan_used``）。
    """
    base = Path(root).resolve()
    entry_resolved = Path(entry_path).resolve()
    roles: Dict[str, Dict[str, Any]] = {}

    try:
        entry_relative = entry_resolved.relative_to(base).as_posix()
    except ValueError:
        entry_relative = entry_resolved.name

    training: Dict[str, Any] = {"name": MATERIAL_TRAINING_FILES, "status": "found",
                                "source": "entry", "path": str(entry_resolved),
                                "relative": entry_relative}
    if not entry_resolved.is_file():
        training.update({"status": "missing", "detail": ["入口 JSONL 不存在"]})
    roles[MATERIAL_TRAINING_FILES] = training

    material_config = getattr(config, "materials", None)
    if material_config is not None and hasattr(material_config, "explicit_paths"):
        explicit = material_config.explicit_paths()
    elif isinstance(material_config, dict):
        explicit = {key: value for key, value in material_config.items() if value}
    else:
        explicit = {}
    skip = [entry_resolved, base / "manifest.json"]
    bound = material_handoff.read_bound(base)
    skip.extend(base / item["path"] for item in bound)
    quality = [item for item in bound if item["role"] == MATERIAL_QUALITY_REPORT]
    if quality:
        roles[MATERIAL_QUALITY_REPORT] = _material_entry(
            MATERIAL_QUALITY_REPORT, base / quality[0]["path"], base, "material_handoff")
    ledgers = [item for item in bound if item["role"] in material_handoff.QUARANTINES]
    if ledgers:
        components = [_material_entry(MATERIAL_ISOLATION_LIST, base / item["path"],
                                      base, "material_handoff") for item in ledgers]
        already_read = {Path(item["absolutePath"]) for item in components}
        if explicit.get(MATERIAL_ISOLATION_LIST):
            configured = _discover_material(
                MATERIAL_ISOLATION_LIST, base=base,
                explicit_path=explicit[MATERIAL_ISOLATION_LIST], skip=skip)
            location = configured.get("absolutePath")
            if not location or Path(location) not in already_read:
                components.append(configured)
            if location:
                already_read.add(Path(location))
        # A handoff certifies its own ledgers. Local quarantine decisions still
        # apply, including those alongside a nested entry or under materials/.
        for path in io_utils.iter_candidate_files(base, [*skip, *already_read]):
            if (path.suffix.lower() in {".jsonl", ".json", ".txt"}
                    and classify_material_name(path.name) == MATERIAL_ISOLATION_LIST
                    and base in path.resolve().parents):
                components.append(_material_entry(MATERIAL_ISOLATION_LIST, path, base, "package"))
        roles[MATERIAL_ISOLATION_LIST] = {
            "name": MATERIAL_ISOLATION_LIST, "source": "material_handoff",
            "status": "found" if all(item["status"] == "found" for item in components) else "invalid",
            "sample_ids": sorted({sample for item in components for sample in item.get("sample_ids", [])}),
            "entries": sum(item.get("entries", 0) for item in components),
            "components": [{key: value for key, value in item.items() if key != "sample_ids"}
                           for item in components],
        }
    recursive_used = False

    for role in MATERIAL_ROLES:
        if role in roles:
            continue
        roles[role] = _discover_material(role, base=base, explicit_path=explicit.get(role),
                                         skip=skip)
        if roles[role].get("source") == "recursive":
            recursive_used = True

    found = [role for role in MATERIAL_ROLES if roles[role]["status"] == "found"]
    missing = [role for role in MATERIAL_ROLES if roles[role]["status"] == "missing"]
    invalid = [role for role in MATERIAL_ROLES if roles[role]["status"] == "invalid"]
    return {"format": MATERIALS_FORMAT, "root": str(base), "entry": entry_relative,
            "entryPath": str(entry_resolved), "roles": roles, "found": found,
            "missing": missing, "invalid": invalid,
            "recursive_scan_used": recursive_used}


def _discover_material(role: str, *, base: Path, explicit_path: Optional[str],
                       skip: Sequence[Path]) -> Dict[str, Any]:
    if explicit_path:
        try:
            target = io_utils.resolve_readonly(base, explicit_path)
        except ValueError as exc:
            return {"name": role, "status": "invalid", "source": "explicit",
                    "path": str(explicit_path), "detail": [str(exc)]}
        if not target.is_file():
            return {"name": role, "status": "missing", "source": "explicit",
                    "path": str(target), "detail": ["显式配置的材料文件不存在"]}
        return _material_entry(role, target, base, "explicit")

    levels = ((base / "materials", "materials_dir"), (base, "root_dir"))
    for level, source in levels:
        entry = _pick_from_level(role, level, base, skip, source=source)
        if entry is not None:
            return entry
    entry = _pick_from_candidates(role, io_utils.iter_candidate_files(base, skip),
                                  base, "recursive")
    if entry is not None:
        return entry
    return {"name": role, "status": "missing", "source": None, "path": None,
            "detail": ["未在输入包中找到该材料"]}


def _pick_from_level(role: str, level: Path, base: Path, skip: Sequence[Path],
                     source: str) -> Optional[Dict[str, Any]]:
    if not level.is_dir():
        return None
    candidates: List[Path] = []
    for child in sorted(level.iterdir()):
        if not child.is_file() or child.is_symlink():
            continue
        if child.resolve() in {Path(item).resolve() for item in skip}:
            continue
        if child.name.endswith(".partial"):
            continue
        candidates.append(child)
    return _pick_from_candidates(role, candidates, base, source)


def _pick_from_candidates(role: str, candidates: Iterable[Path], base: Path,
                          source: str) -> Optional[Dict[str, Any]]:
    matched: List[Path] = []
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if path.resolve().name == "manifest.json":
                continue
            if _classify_material_path(path) == role:
                matched.append(path)
        except OSError:                                # pragma: no cover
            continue
    if not matched:
        return None
    chosen = matched[0]
    entry = _material_entry(role, chosen, base, source)
    if len(matched) > 1:
        entry.setdefault("detail", []).append(
            "同层级存在 %d 个候选，按名称排序取第一个" % len(matched))
    return entry


def _material_entry(role: str, path: Path, base: Path, source: str) -> Dict[str, Any]:
    verdict = _validate_material(role, path)
    try:
        relative = path.resolve().relative_to(base).as_posix()
    except ValueError:
        relative = None
    entry: Dict[str, Any] = {"name": role, "status": verdict["status"], "source": source,
                             "path": relative if relative else str(path),
                             "relative": relative, "absolutePath": str(path.resolve()),
                             "size": path.stat().st_size,
                             "sha256": io_utils.file_sha256(path)}
    for key, value in verdict.items():
        if key in {"status", "detail"}:
            continue
        entry[key] = value
    if verdict.get("detail"):
        entry["detail"] = list(verdict["detail"])
    return entry


# --------------------------------------------------------------------------- #
# 交付门禁
# --------------------------------------------------------------------------- #

def evaluate_deliverable(materials: Dict[str, Any], *, packaged_records: int,
                         config: Any) -> Dict[str, Any]:
    """表1 #23 只读门禁：训练文件已生成 + 质量报告已写回 + 抽检已写回，缺一不可。"""
    roles = materials.get("roles") or {}
    training = roles.get(MATERIAL_TRAINING_FILES) or {}
    quality = roles.get(MATERIAL_QUALITY_REPORT) or {}
    sampling = roles.get(MATERIAL_SAMPLING_CONCLUSION) or {}

    requirements = {
        "training_files_present": (training.get("status") == "found"
                                   and int(packaged_records) > 0),
        "quality_report_written_back": (quality.get("status") == "found"
                                        and quality.get("written_back") is True),
        "sampling_written_back": (sampling.get("status") == "found"
                                  and sampling.get("written_back") is True),
    }

    missing: List[str] = [name for name in GATE_REQUIREMENTS if not requirements[name]]
    for role in MATERIAL_ROLES:
        if role == MATERIAL_TRAINING_FILES:
            continue
        status = (roles.get(role) or {}).get("status")
        if status != "found" and role not in missing:
            missing.append(role)

    deliverable = not missing
    if deliverable:
        version_state = VERSION_STATE_DELIVERABLE
        label = getattr(config, "status_after_review", STATUS_LABEL_DELIVERABLE)
    else:
        version_state = VERSION_STATE_PENDING
        label = getattr(config, "status_before_review", STATUS_LABEL_PENDING)

    return {"deliverable": deliverable,
            "version_state": version_state,
            "status_label": label,
            "requirements": requirements,
            "missing_requirements": missing,
            "materials_complete": all(
                (roles.get(role) or {}).get("status") == "found"
                for role in MATERIAL_ROLES),
            "package_name": (PACKAGE_DELIVERABLE_NAME if deliverable
                             else PACKAGE_PENDING_NAME)}


def package_member_for_role(role: str) -> Optional[str]:
    return PACKAGE_MEMBER_BY_ROLE.get(role)
