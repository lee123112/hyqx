"""★业务规则层：表1 #26「生成交付样例」逐条落地。

表1 v4.1 / 表1_selected #26 冻结口径（逐字，不得改写）：
  环节 = 附属线（轻）第 8 环节「交付样例」；流程节点 = 交付样例；
  开关 = 默认开启；可选；类型 = 系统功能；优先级 = P2；模型列 = ``--``
  输入：已进入可交付语料包的样本。
  步骤：1）每条已开启路线最多抽 10 条；2）导出 Excel，字段为样本ID、模态、
        描述摘要、标注结论、评分结论；3）条数不足 10 条时按实际条数导出，
        并在报告写明。
  输出：样例清单。
  边界：不做预览页、波形图、HTML；失败时不用隔离区样本顶上。
  完成：验收人员只打开 Excel 就能看到这一版的样子。
  质量处置：样例生成失败不拦样本、不改用隔离区数据，只在质量报告中记载。

本模块只做**确定性规则判定**：不加载任何模型、不产生任何分数、不伪造人工确认；
分数/标注结论一律从上游命名空间**照实搬运**，取不到就写“未提供”。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import io as io_utils

__all__ = [
    "DEST_DELIVERY_SAMPLE",
    "DEST_ORIGIN",
    "DEST_SAMPLE_ISOLATION",
    "DEST_SAMPLE_POOL",
    "EXCEL_NAME",
    "EXCLUDED_NAME",
    "LABEL_EXPORTED",
    "LABEL_ISOLATED",
    "LABEL_NOT_DELIVERABLE",
    "LABEL_ORIGIN_UNREADABLE",
    "LABEL_SKIPPED",
    "LOG_NAME",
    "MAIN_NAME",
    "NAMESPACE",
    "OPERATOR",
    "OPERATOR_LABEL",
    "OPERATOR_NUMBER",
    "OPERATOR_VERSION",
    "REASON_DUPLICATE_IDENTIFIER",
    "REASON_ENCODING",
    "REASON_EXPORTED",
    "REASON_JSON_PARSE",
    "REASON_MISSING_IDENTIFIER",
    "REASON_NOT_DELIVERABLE",
    "REASON_NOT_OBJECT",
    "REASON_NO_ROUTE",
    "REASON_QUOTA_EXHAUSTED",
    "REASON_ROUTE_NOT_ENABLED",
    "REASON_UNSAFE_MEDIA_PATH",
    "REASON_UPSTREAM_ISOLATED",
    "REJECTS_NAME",
    "REPORT_MD_NAME",
    "REPORT_NAME",
    "ROUTE_LABELS",
    "ROUTE_MODALITIES",
    "ROUTE_ORDER",
    "ROUTE_SCORE_OPERATOR",
    "SKIPPED_NAME",
    "SOURCE_REFERENCE",
    "STAGE_ID",
    "TABLE1_MAX_SAMPLES_PER_ROUTE",
    "check_conservation",
    "delivery_marker",
    "extract_annotation",
    "extract_description",
    "extract_modalities",
    "extract_routes",
    "extract_score",
    "find_isolation",
    "format_annotation",
    "format_score",
    "make_ledger_entry",
    "make_reject_entry",
    "normalize_route",
    "resolve_record",
    "route_modal_label",
    "sampling_fingerprint",
    "select_samples",
    "summarize_description",
]

# --------------------------------------------------------------------------- #
# 身份与产物名
# --------------------------------------------------------------------------- #

OPERATOR = "delivery_sample"
OPERATOR_LABEL = "生成交付样例"
OPERATOR_NUMBER = 26
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S8"                      # [v4.2] 表1 第 8 环节「交付样例」（§4.26 修正原 S7）
SOURCE_REFERENCE = "operator_26"
NAMESPACE = "operator_26"

MAIN_NAME = "26_samples.jsonl"            # 机器可读样例清单（entrypoint）
EXCEL_NAME = "26_sample_list.xlsx"        # 业务交付物：Excel 样例清单
SKIPPED_NAME = "26_skipped.jsonl"         # 旁路台账：路线未开启/超出上限
EXCLUDED_NAME = "26_excluded.jsonl"       # 旁路台账：隔离/非可交付
REJECTS_NAME = "26_rejects.jsonl"         # 原始隔离台账
REPORT_NAME = "26_sample_report.json"
REPORT_MD_NAME = "26_sample_report.md"
LOG_NAME = "26_run.log"

OWN_SIDECARS: Sequence[str] = (MAIN_NAME, SKIPPED_NAME, EXCLUDED_NAME, REJECTS_NAME,
                               REPORT_NAME, REPORT_MD_NAME)

# --------------------------------------------------------------------------- #
# 表1 只读口径
# --------------------------------------------------------------------------- #

#: 表1 #26「每条已开启路线最多抽10条」——请求不可放宽
TABLE1_MAX_SAMPLES_PER_ROUTE = 10

ROUTE_ORDER: Sequence[str] = ("image_text", "audio_text", "video_text")
ROUTE_LABELS: Dict[str, str] = {"image_text": "图文", "audio_text": "音文",
                                "video_text": "视文"}
ROUTE_MODALITIES: Dict[str, Tuple[str, str]] = {"image_text": ("image", "text"),
                                               "audio_text": ("audio", "text"),
                                               "video_text": ("video", "text")}
#: 路线 → 表1 评分工具编号（#12 图文 / #13 音文 / #14 视文）
ROUTE_SCORE_OPERATOR: Dict[str, int] = {"image_text": 12, "audio_text": 13,
                                       "video_text": 14}

ROUTE_SOURCE_STATUS = "可交付"

MODALITY_ORDER: Sequence[str] = ("image", "audio", "video", "text")

LABEL_EXPORTED = "样例-已导出"
LABEL_SKIPPED = "流程-未抽入样例"
LABEL_NOT_DELIVERABLE = "流程-非可交付"
LABEL_ISOLATED = "样本-语义不通"
LABEL_ORIGIN_UNREADABLE = "原始文件-无法解析"

DEST_DELIVERY_SAMPLE = "delivery_sample"
DEST_SAMPLE_POOL = "sample_pool"
DEST_SAMPLE_ISOLATION = "sample_isolation_library"
DEST_ORIGIN = "origin_quarantine"

REASON_EXPORTED = "sampled_into_delivery_sample"
REASON_ROUTE_NOT_ENABLED = "route_not_enabled"
REASON_NO_ROUTE = "no_applicable_route"
REASON_QUOTA_EXHAUSTED = "route_quota_exhausted"
REASON_UPSTREAM_ISOLATED = "upstream_isolated"
REASON_NOT_DELIVERABLE = "record_not_deliverable"
REASON_MISSING_IDENTIFIER = "missing_field:sample_id"
REASON_DUPLICATE_IDENTIFIER = "duplicate_identifier"
REASON_UNSAFE_MEDIA_PATH = "unsupported_media_path"
REASON_JSON_PARSE = "json_parse_error"

# [v4.2] status 双字段（规则 §2.10，M26 处理型）：输出记录写 int status
# （1=样例生成成功，0=失败/未生成），原字符串流程态迁入 process_status。
STATUS_SAMPLED = "sampled"
STATUS_AS_INT: Dict[str, int] = {STATUS_SAMPLED: 1}


def status_int(value: Optional[str]) -> int:
    """[v4.2] 记录级字符串流程态 → int status；未登记的一律按成功 1。"""
    return STATUS_AS_INT.get(value, 1)
REASON_NOT_OBJECT = "record_not_object"
REASON_ENCODING = "encoding_error"

_ISOLATED_STATUS = {"isolated", "quarantined", "sample_isolation_library",
                    "isolation", "isolate", "locked"}
_NOT_DELIVERABLE = {"pending_release", "pending", "not_deliverable", "waiting_release",
                    "rejected", "excluded"}
_DELIVERABLE = {"deliverable", "可交付", "packaged", "delivered"}

_MEDIA_CONTAINERS = ("media", "image", "images", "audio", "video", "frames",
                     "asset", "assets", "files")
_REF_KEYS = ("path", "file", "uri", "url", "ref", "source", "image_path",
             "file_path", "relative_path")
_NESTED_KEYS = ("items", "frames", "files", "records", "media", "paths")

_ROUTE_ALIASES: Dict[str, str] = {"图文": "image_text", "音文": "audio_text",
                                  "视文": "video_text", "图_文": "image_text"}


# --------------------------------------------------------------------------- #
# 路线与模态
# --------------------------------------------------------------------------- #

def normalize_route(raw: Any) -> Optional[str]:
    """把各种写法的路线名归一为 ``image_text`` / ``audio_text`` / ``video_text``。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if text in _ROUTE_ALIASES:
        return _ROUTE_ALIASES[text]
    for separator in ("+", "-", " ", "/", "|", "&", "_"):
        text = text.replace(separator, "_")
    while "__" in text:
        text = text.replace("__", "_")
    text = text.strip("_")
    return text if text in ROUTE_ORDER else None


def _route_sort_key(route: str) -> int:
    return list(ROUTE_ORDER).index(route)


def _iter_route_candidates(payload: Dict[str, Any]) -> Iterable[Any]:
    for key in ("routes", "route"):
        if key in payload:
            yield payload[key]
    for container in ("operator_22", "operator_23", "processing"):
        value = payload.get(container)
        if not isinstance(value, dict):
            continue
        if container == "processing":
            for inner_key in ("22", "23"):
                inner = value.get(inner_key)
                if isinstance(inner, dict):
                    yield inner.get("routes")
                    yield inner.get("route")
        else:
            yield value.get("routes")
            yield value.get("route")


def extract_routes(payload: Dict[str, Any]) -> List[str]:
    """按 `image_text → audio_text → video_text` 固定顺序返回去重后的路线列表。"""
    found: List[str] = []
    for candidate in _iter_route_candidates(payload):
        items = candidate if isinstance(candidate, (list, tuple)) else [candidate]
        for item in items:
            route = normalize_route(item)
            if route and route not in found:
                found.append(route)
    if not found:
        found = _routes_from_modalities(extract_modalities(payload))
    found.sort(key=_route_sort_key)
    return found


def _routes_from_modalities(modalities: Sequence[str]) -> List[str]:
    known = set(modalities)
    routes: List[str] = []
    if "text" in known:
        for route in ROUTE_ORDER:
            first = ROUTE_MODALITIES[route][0]
            if first in known:
                routes.append(route)
    return routes


def extract_modalities(payload: Dict[str, Any]) -> List[str]:
    """返回声明或推导出的模态（``image``/``audio``/``video``/``text``）。"""
    declared = payload.get("modalities")
    if isinstance(declared, (list, tuple)):
        result: List[str] = []
        for item in declared:
            token = str(item).strip().lower()
            if token in MODALITY_ORDER and token not in result:
                result.append(token)
        if result:
            return [token for token in MODALITY_ORDER if token in set(result)]
    if isinstance(declared, str) and declared.strip().lower() in MODALITY_ORDER:
        return [declared.strip().lower()]

    derived: List[str] = []
    for modality in MODALITY_ORDER:
        if modality == "text":
            value = payload.get("text")
            present = isinstance(value, dict) and bool(value) or \
                isinstance(value, str) and bool(value.strip())
        else:
            value = payload.get(modality)
            present = isinstance(value, dict) and bool(value) or \
                isinstance(value, str) and bool(value.strip())
        if present:
            derived.append(modality)
    return derived


def route_modal_label(route: Optional[str]) -> str:
    if route is None:
        return "未知"
    return ROUTE_LABELS.get(route, "未知")


# --------------------------------------------------------------------------- #
# 描述摘要
# --------------------------------------------------------------------------- #

def extract_description(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按固定优先级取候选描述（图注 → 描述字段 → 标题 → 转写），不发明内容。"""
    text_block = payload.get("text") if isinstance(payload.get("text"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    candidates: List[Tuple[str, Any]] = [
        ("description", payload.get("description")),
        ("text.content", text_block.get("content")),
        ("text.description", text_block.get("description")),
        ("text.caption", text_block.get("caption")),
        ("caption", payload.get("caption")),
        ("alt", payload.get("alt")),
        ("title", payload.get("title")),
        ("transcript", payload.get("transcript")),
        ("metadata.description", metadata.get("description")),
    ]
    for field, value in candidates:
        if isinstance(value, str) and value.strip():
            source = text_block.get("description_source") \
                if field.startswith("text.") else None
            return {"text": value.strip(), "field": field,
                    "source": source if isinstance(source, str) else None}
    return {"text": None, "field": None, "source": None}


def summarize_description(text: Any, max_chars: int) -> Dict[str, Any]:
    """生成描述摘要（真实截断，不改写内容），并给出可核对的长度/摘要哈希。

    ``chars`` 为上游描述的真实字符数，``stored_chars`` 为摘要实际写入单元格的
    字符数；``sha256`` 是**完整描述**的哈希，便于逐条比对上游原文。
    """
    if not isinstance(text, str) or not text.strip():
        return {"text": None, "truncated": False, "chars": 0, "stored_chars": 0,
                "sha256": None}
    cleaned = text.strip()
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    limit = max(1, int(max_chars))
    if len(cleaned) <= limit:
        return {"text": cleaned, "truncated": False, "chars": len(cleaned),
                "stored_chars": len(cleaned), "sha256": digest}
    summary = cleaned[:limit] + "…"
    return {"text": summary, "truncated": True, "chars": len(cleaned),
            "stored_chars": len(summary), "sha256": digest}


# --------------------------------------------------------------------------- #
# 标注结论 / 评分结论（只搬运，不生成）
# --------------------------------------------------------------------------- #

def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _annotation_block(payload: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    processing = payload.get("processing") if isinstance(payload.get("processing"), dict) else {}
    block = processing.get("16")
    if isinstance(block, dict):
        return "processing.16", block
    block = payload.get("operator_16")
    if isinstance(block, dict):
        return "operator_16", block
    alignment = payload.get("alignment")
    if isinstance(alignment, dict):
        whole = alignment.get("whole")
        if isinstance(whole, dict) and whole:
            return "alignment.whole", whole
        if alignment:
            return "alignment", alignment
    if isinstance(alignment, str) and alignment.strip():
        return "alignment", {"conclusion": alignment}
    for number in (17, 18):
        block = processing.get(str(number)) or payload.get("operator_%d" % number)
        if isinstance(block, dict):
            return "processing.%d" % number, block
    return None


def extract_annotation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """抽取 #16/#17/#18 标注结论；取不到就如实返回 ``None``（不伪造）。"""
    entry = _annotation_block(payload)
    if entry is None:
        return {"conclusion": None, "human_verified": False, "source": None}
    source, block = entry
    conclusion = _first_text(block.get("conclusion"), block.get("decision"),
                             block.get("status"))
    method = _first_text(block.get("confirmation_method"), block.get("reviewer_kind"),
                         block.get("confirmed_by"))
    human = block.get("human_verified") is True
    if isinstance(method, str) and method.strip().lower() in {"human", "manual",
                                                              "人工"}:
        human = True
    return {"conclusion": conclusion, "human_verified": human, "source": source}


def format_annotation(spec: Dict[str, Any]) -> str:
    """把标注结论格式化为 Excel 单元格文本；缺结论写“未提供”。"""
    conclusion = spec.get("conclusion")
    if not conclusion:
        return "未提供"
    if spec.get("human_verified"):
        return "人工确认：%s" % conclusion
    return "%s（自动结论，未人工确认）" % conclusion


def _coerce_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _score_block(payload: Dict[str, Any], route: str
                 ) -> Optional[Tuple[str, Dict[str, Any]]]:
    number = ROUTE_SCORE_OPERATOR.get(route)
    if number is None:
        return None
    processing = payload.get("processing") if isinstance(payload.get("processing"), dict) else {}
    block = processing.get(str(number))
    if isinstance(block, dict) and block:
        return "processing.%d" % number, block
    block = payload.get("operator_%d" % number)
    if isinstance(block, dict) and block:
        return "operator_%d" % number, block
    key = "%s_score" % route
    block = payload.get(key)
    if isinstance(block, dict) and block:
        return key, block
    return None


def extract_score(payload: Dict[str, Any], route: Optional[str]) -> Dict[str, Any]:
    """按路线抽取 #12/#13/#14 的评分结论（分数字段照实搬运，不做任何加工）。"""
    empty = {"value": None, "tier": None, "label": None, "model": None, "source": None}
    if not route:
        return dict(empty)
    entry = _score_block(payload, route)
    if entry is None:
        return dict(empty)
    source, block = entry
    value = None
    for key in ("score", "value", "clip_score", "similarity", "cosine",
                "semantic_score", "score_value"):
        if key in block:
            value = _coerce_number(block.get(key))
            if value is not None:
                break
    model = block.get("model")
    model_name = model.get("name") if isinstance(model, dict) else model
    return {"value": value,
            "tier": _first_text(block.get("tier"), block.get("decision"),
                                block.get("status")),
            "label": _first_text(block.get("label"), block.get("tier_label")),
            "model": model_name if isinstance(model_name, str) else None,
            "source": source}


def format_score(spec: Dict[str, Any]) -> str:
    """把评分结论格式化为 Excel 单元格文本；没有分数就不写任何数字。"""
    value = spec.get("value")
    detail = spec.get("label") or spec.get("tier")
    model = spec.get("model")
    if value is None and not detail and not model:
        return "未提供"
    parts: List[str] = []
    if value is not None:
        parts.append("%.3f" % float(value))
    if detail:
        parts.append(str(detail))
    if model:
        parts.append(str(model))
    if len(parts) == 1:
        return parts[0]
    return "%s（%s）" % (parts[0], "；".join(parts[1:]))


# --------------------------------------------------------------------------- #
# 隔离 / 交付标记 / 媒体引用
# --------------------------------------------------------------------------- #

def _identifier(payload: Dict[str, Any]) -> Optional[str]:
    return _first_text(payload.get("sample_id"), payload.get("id"),
                       payload.get("pair_id"))


def find_isolation(payload: Dict[str, Any],
                   isolation_ids: Iterable[str]) -> Optional[Dict[str, Any]]:
    """判断记录是否已在隔离区（#21 标记、状态字段或隔离清单），返回证据或 ``None``。"""
    identifier = _identifier(payload)
    if identifier and identifier in set(isolation_ids):
        return {"source": "isolation_list", "reason_code": REASON_UPSTREAM_ISOLATED,
                "label": LABEL_ISOLATED, "detail": "样本 ID 出现在 #23 包内隔离清单中"}

    processing = payload.get("processing") if isinstance(payload.get("processing"), dict) else {}
    markers: List[Any] = [processing.get("21")]
    for key in ("17", "18"):
        node = processing.get(key)
        if isinstance(node, dict) and str(node.get("status") or "").lower() in _ISOLATED_STATUS:
            markers.append(node)
    markers.extend([payload.get("isolation"), payload.get("disposition"),
                    payload.get("sample_status"), payload.get("delivery_status"),
                    processing.get("21")])
    for marker in markers:
        verdict = _isolation_from_marker(marker)
        if verdict is not None:
            return verdict
    return None


def _isolation_from_marker(marker: Any) -> Optional[Dict[str, Any]]:
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
    status = str(marker.get("status") or marker.get("decision") or "").strip().lower()
    if marker.get("isolated") is True or status in _ISOLATED_STATUS:
        return {"source": "record_marker",
                "reason_code": REASON_UPSTREAM_ISOLATED,
                "label": _first_text(marker.get("label")) or LABEL_ISOLATED,
                "detail": _first_text(marker.get("detail"),
                                      "记录被上游标记为隔离（%s）" % (status or "isolated"))}
    return None


def delivery_marker(payload: Dict[str, Any]) -> Optional[str]:
    """读取 #23 交付标记（``deliverable`` / ``pending_release`` / ``None``）。"""
    processing = payload.get("processing") if isinstance(payload.get("processing"), dict) else {}
    blocks = [payload.get("operator_23"), processing.get("23")]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        value = _first_text(block.get("decision"), block.get("version_state"),
                            block.get("status"))
        if value:
            return value.strip().lower()
    return None


def _collect_unsafe_refs(payload: Dict[str, Any]) -> List[str]:
    unsafe: List[str] = []

    def visit(value: Any, seen: set) -> None:
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate or candidate in seen:
                return
            seen.add(candidate)
            if io_utils.is_external_reference(candidate):
                return
            if not io_utils.is_safe_relative(candidate):
                unsafe.append(candidate)
            return
        if isinstance(value, dict):
            for key in _REF_KEYS:
                if isinstance(value.get(key), str):
                    visit(value[key], seen)
            for key in _NESTED_KEYS:
                if key in value:
                    visit(value[key], seen)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item, seen)

    seen: set = set()
    for container in _MEDIA_CONTAINERS:
        if container in payload:
            visit(payload[container], seen)
    # 媒体引用也可能直接挂在 metadata / operator 命名空间下
    for key in ("media", "assets"):
        node = payload.get(key)
        if node is not None:
            visit(node, seen)
    return unsafe


# --------------------------------------------------------------------------- #
# 记录解析
# --------------------------------------------------------------------------- #

def resolve_record(payload: Dict[str, Any], *, enabled_routes: Sequence[str],
                   isolation_ids: Iterable[str],
                   description_chars: int) -> Dict[str, Any]:
    """把一条上游记录解析成本算子需要的证据集合（不改写原记录）。"""
    routes = extract_routes(payload)
    enabled = list(enabled_routes)
    primary = next((route for route in routes if route in enabled), None)
    description = extract_description(payload)
    return {
        "identifier": _identifier(payload),
        "routes": routes,
        "route": primary,
        "multi_route": len(routes) > 1,
        "modalities": extract_modalities(payload),
        "description": summarize_description(description["text"], description_chars),
        "description_field": description["field"],
        "description_source": description["source"],
        "annotation": extract_annotation(payload),
        "score": extract_score(payload, primary),
        "isolation": find_isolation(payload, isolation_ids),
        "delivery": delivery_marker(payload),
        "unsafe_refs": _collect_unsafe_refs(payload),
    }


# --------------------------------------------------------------------------- #
# 抽样（确定性伪随机，可复现）
# --------------------------------------------------------------------------- #

def select_samples(pool: Sequence[Dict[str, Any]], *, max_samples: int, mode: str,
                   seed: int) -> List[Dict[str, Any]]:
    """从一条路线的候选池中抽取至多 ``max_samples`` 条（确定性、可复现）。

    - 先按 ``sample_id`` 排序，消除输入文件行序的影响；
    - ``mode=random``：按 ``sha256(seed|sample_id)`` 排序后取前 N 条
      （固定种子的确定性伪随机置换，同输入 → 同结果）；
    - ``mode=first``：直接取排序后的前 N 条（便于逐条复核）。
    结果再按 ``sample_id`` 排序输出，保证 Excel 行序稳定。
    """
    ordered = sorted(pool, key=lambda item: str(item.get("identifier")))
    if int(max_samples) >= len(ordered):
        return ordered
    if mode == "first":
        picked = ordered[:int(max_samples)]
    else:
        def sort_key(item: Dict[str, Any]) -> Tuple[str, str]:
            token = "%d|%s" % (int(seed), item.get("identifier"))
            return (hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    str(item.get("identifier")))

        picked = sorted(ordered, key=sort_key)[:int(max_samples)]
    return sorted(picked, key=lambda item: str(item.get("identifier")))


def sampling_fingerprint(picked: Sequence[Dict[str, Any]]) -> Optional[str]:
    """抽样指纹：选中样本 ID 的 sha256，便于逐版本比对“这一版抽了哪些”。"""
    if not picked:
        return None
    joined = "\n".join(sorted(str(item.get("identifier")) for item in picked))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 台账条目
# --------------------------------------------------------------------------- #

def _bounded_record(payload: Dict[str, Any], max_embedded_bytes: int
                    ) -> Dict[str, Any]:
    try:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):                    # pragma: no cover - 自身产物
        return {"record_omitted": True, "record_bytes": None}
    if len(encoded) <= int(max_embedded_bytes):
        return {"record": payload}
    return {"record_omitted": True, "record_bytes": len(encoded)}


def make_ledger_entry(*, line: int, payload: Dict[str, Any], action: str, label: str,
                      destination: str, reason_code: str, event_time: str,
                      detail: Optional[str] = None, max_embedded_bytes: int = 32768,
                      extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """统一台账条目：表1/规则 §2.3 要求的字段 + 有界原始记录快照。"""
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
        "sample_id": _identifier(payload),
        "retry_count": _retry_count(metadata),
        "line": int(line),
    }
    entry.update(_bounded_record(payload, max_embedded_bytes))
    if extra:
        entry.update(extra)
    return entry


def make_reject_entry(*, line: int, reason: str, detail: str, detected_at: str,
                      raw_text: Optional[str] = None,
                      identifier: Optional[str] = None,
                      label: str = LABEL_ORIGIN_UNREADABLE,
                      max_preview_chars: int = 200) -> Dict[str, Any]:
    """原始隔离台账条目（坏行/编码错误/缺标识/非法引用/重复标识）。"""
    entry: Dict[str, Any] = {
        "label": label,
        "stage": STAGE_ID,
        "operator": SOURCE_REFERENCE,
        "action": "quarantine",
        "destination": DEST_ORIGIN,
        "reason_code": reason,
        "detail": detail,
        "detected_at": detected_at,
        "event_time": detected_at,
        "sample_id": identifier,
        "batch_id": None,
        "retry_count": 0,
        "line": int(line),
    }
    if raw_text is not None:
        preview = raw_text[:max_preview_chars]
        entry["raw_text_preview"] = preview
        entry["raw_text_truncated"] = len(raw_text) > len(preview)
    return entry


def _retry_count(metadata: Dict[str, Any]) -> int:
    value = metadata.get("retry_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


# --------------------------------------------------------------------------- #
# 样例行 / 命名空间
# --------------------------------------------------------------------------- #

def build_sample_row(payload: Dict[str, Any], spec: Dict[str, Any], *, seq: int,
                     route: str, event_time: str, package: Dict[str, Any],
                     config: Any) -> Dict[str, Any]:
    """构造一条样例清单记录：**保留上游全部字段** + ``operator_26`` 命名空间。"""
    record = copy.deepcopy(payload)
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    node = {
        "operator": OPERATOR,
        "operator_label": OPERATOR_LABEL,
        "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "source_reference": SOURCE_REFERENCE,
        "stage_id": STAGE_ID,
        # [v4.2] status 双字段（§2.10）：int status + 原字符串迁入 process_status
        "status": status_int(STATUS_SAMPLED),
        "process_status": STATUS_SAMPLED,
        "decision": "exported",
        "label": LABEL_EXPORTED,
        "action": "export",
        "destination": DEST_DELIVERY_SAMPLE,
        "reason_code": REASON_EXPORTED,
        "sample_id": spec["identifier"],
        "seq": int(seq),
        "route": route,
        "routes": list(spec["routes"]),
        "modal": route_modal_label(route),
        "modalities": list(spec["modalities"]),
        "description_summary": spec["description"]["text"],
        "annotation_conclusion": format_annotation(spec["annotation"]),
        "score_conclusion": format_score(spec["score"]),
        "evidence": {
            "description_field": spec["description_field"],
            "description_source": spec["description_source"],
            "description_chars": spec["description"]["chars"],
            "description_stored_chars": spec["description"]["stored_chars"],
            "description_truncated": spec["description"]["truncated"],
            "description_sha256": spec["description"]["sha256"],
            "annotation_source": spec["annotation"]["source"],
            "annotation_conclusion": spec["annotation"]["conclusion"],
            "score_source": spec["score"]["source"],
            "score_value": spec["score"]["value"],
            "score_tier": spec["score"]["tier"],
            "score_model": spec["score"]["model"],
            "delivery_marker": spec["delivery"],
            "multi_route": bool(spec["multi_route"]),
            "enabled_routes": list(package.get("enabled_routes") or []),
            "source_package": {
                "version_state": package.get("version_state"),
                "status": package.get("status"),
                "dataset_version": package.get("dataset_version"),
                "entrypoint": package.get("entrypoint"),
            },
        },
        "human_verified": False,
        "review_required": True,
        "event_time": event_time,
        "batch_id": _first_text(record.get("batch_id"), metadata.get("batch_id")),
        "retry_count": _retry_count(metadata),
        "max_samples_per_route": int(getattr(config, "max_samples_per_route", 10)),
        "sampling_mode": getattr(config, "sampling_mode", "random"),
        "sampling_seed": int(getattr(config, "sampling_seed", 0)),
    }
    record[NAMESPACE] = node
    processing = record.get("processing")
    if not isinstance(processing, dict):
        processing = {}
    processing = dict(processing)
    processing["26"] = {
        "operator": OPERATOR, "operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION, "stage_id": STAGE_ID,
        # [v4.2] status 双字段（§2.10）：int status + 原字符串迁入 process_status
        "status": status_int(STATUS_SAMPLED), "process_status": STATUS_SAMPLED,
        "decision": "exported", "label": LABEL_EXPORTED,
        "action": "export", "destination": DEST_DELIVERY_SAMPLE,
        "reason_code": REASON_EXPORTED, "route": route, "seq": int(seq),
        "source_reference": SOURCE_REFERENCE}
    record["processing"] = processing
    return record


def excel_row(record: Dict[str, Any], *, seq: int, package: Dict[str, Any]
              ) -> List[Any]:
    """把样例记录转成 Excel 一行（列为表1 要求的字段 + 可追溯附加列）。"""
    node = record.get(NAMESPACE) or {}
    return [int(seq),
            node.get("sample_id"),
            node.get("modal"),
            node.get("description_summary"),
            node.get("annotation_conclusion"),
            node.get("score_conclusion"),
            node.get("route"),
            node.get("batch_id"),
            node.get("evidence", {}).get("delivery_marker") or "",
            package.get("dataset_version") or ""]


EXCEL_COLUMNS: Sequence[Dict[str, Any]] = (
    {"header": "序号", "width": 6},
    {"header": "样本ID", "width": 18},
    {"header": "模态", "width": 8},
    {"header": "描述摘要", "width": 48},
    {"header": "标注结论", "width": 30},
    {"header": "评分结论", "width": 40},
    {"header": "路线", "width": 14},
    {"header": "来源批次", "width": 18},
    {"header": "交付标记", "width": 16},
    {"header": "数据版本", "width": 14},
)

REPORT_COLUMNS: Sequence[Dict[str, Any]] = (
    {"header": "项目", "width": 26},
    {"header": "取值", "width": 72},
)


# --------------------------------------------------------------------------- #
# 统计守恒
# --------------------------------------------------------------------------- #

def check_conservation(stats: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """核对「输入 = 各类去向」以及样例清单行数守恒（表1 #26 交付门禁）。"""
    def value(key: str) -> int:
        raw = stats.get(key, 0)
        return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) \
            else 0

    def total(key: str) -> int:
        bucket = stats.get(key) or {}
        if not isinstance(bucket, dict):
            return 0
        return sum(int(item) for item in bucket.values()
                   if isinstance(item, (int, float)) and not isinstance(item, bool))

    equality_checks = [
        ("input_records==valid_records+bad_lines+origin_quarantined",
         value("input_records"), value("valid_records") + value("bad_lines")
         + value("origin_quarantined")),
        ("valid_records==sampled+route_disabled+route_exhausted+excluded",
         value("valid_records"), value("sampled_records") + value("route_disabled_records")
         + value("route_exhausted_records") + value("excluded_records")),
        ("bad_lines==sum(bad_line_by_reason)", value("bad_lines"),
         total("bad_line_by_reason")),
        ("origin_quarantined==sum(origin_by_reason)", value("origin_quarantined"),
         total("origin_by_reason")),
        ("rejects_entries==bad_lines+origin_quarantined", value("rejects_entries"),
         value("bad_lines") + value("origin_quarantined")),
        ("excluded_records==sum(excluded_by_reason)", value("excluded_records"),
         total("excluded_by_reason")),
        ("route_disabled_records==sum(route_disabled_by_reason)",
         value("route_disabled_records"), total("route_disabled_by_reason")),
        ("samples_jsonl_rows==sampled_records", value("samples_jsonl_rows"),
         value("sampled_records")),
        ("sum(route_sampled)==sampled_records", total("route_sampled"),
         value("sampled_records")),
    ]
    failures = ["%s（实际 %s != 期望 %s）" % (name, left, right)
                for name, left, right in equality_checks if left != right]
    if value("input_records") < 0 or value("valid_records") < 0:
        failures.append("出现负数统计，说明计数逻辑异常")
    return (not failures), failures
