"""算子 21 业务模块：错配样本处置（六类台账字段 + 分类 + 退回/锁定预算）。

表1 #21 冻结口径（逐条实现，不越界）：
1. **统一记录六类台账字段**：``label``（标签）、``operator``/``stage``（工序）、
   ``action``（动作）、``destination``（去向）、``event_time``（时间）、``batch_id``（批次），
   并额外保留 ``sample_id`` 与 ``retry_count`` 作为台账主键与重跑计数（规则 §2.3 的
   完整字段表就是这 9 个键，见 :data:`LEDGER_FIELDS`）。
2. **原始文件问题进原始隔离区**：无法解析、损坏、编码异常、空文件/整段无声 → ``raw_isolation``。
3. **治理不达标进样本隔离库**：敏感、语义不通、评分低、人工不对齐、错配、标注不完整
   → ``sample_isolation``。
4. **评分分层不进隔离区**：``样本-评分：*``（通过 / 待人工 / 入池 / CLIP分）、``流程-*``
   属于流程状态，留产线，不得被误写成低质量隔离。
5. **退回与锁定**：单条允许退回重跑 1 次（``return_limit``），同一条全产线最多 2 次
   （``max_retry_per_item``），第 3 次锁定样本隔离库且不再自动回流。
6. **不得复制隔离样本或建负样本工厂**：被隔离记录的媒体一律不搬运，台账只保留
   有字节上限的记录快照（``max_embedded_record_bytes``，超出只留计数与哈希）。
7. **人工平台未接入时**：需要人工确认/复核的记录只能显式 ``waiting_manual``（留产线、
   不隔离），并按配置决定是显式降级还是结构化失败；**绝不伪造人工确认结论**。

本模块**无模型、无外部服务**，不产生任何模型分数。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Dict, Optional, Tuple

OPERATOR = "mismatch_disposal"
FORMAL_OPERATOR_NUMBER = 21

#: 统一台账字段（六类 + 主键 + 重跑计数）
LEDGER_FIELDS: Tuple[str, ...] = (
    "sample_id",      # 样本主键
    "label",          # 标签（表1 质量标签）
    "operator",       # 工序（本算子）
    "stage",          # 工序所在阶段
    "action",         # 动作
    "destination",    # 去向
    "event_time",     # 时间
    "batch_id",       # 批次
    "retry_count",    # 重跑次数
)


class DependencyError(RuntimeError):
    """外部依赖（人工复核平台）不可用且禁止降级。"""


# ------------------------------------------------------------------ 分类常量
CATEGORY_ORIGIN = "origin"
CATEGORY_SAMPLE = "sample"
CATEGORY_PROCESS = "process"
CATEGORY_PASS = "pass"
CATEGORY_RETURN = "return"
CATEGORY_LOCK = "lock"
CATEGORY_NONE = "none"

DECISION_PASS = "pass"
DECISION_PROCESS = "process_kept"
DECISION_NOT_APPLICABLE = "not_applicable"
DECISION_WAITING_MANUAL = "waiting_manual"
DECISION_RETURN = "returned"
DECISION_LOCKED = "locked"
DECISION_SAMPLE_ISOLATED = "sample_isolated"
DECISION_ORIGIN_ISOLATED = "raw_isolated"
DECISION_REJECTED = "rejected"

ROUTE_RELEASED = "released"
ROUTE_ORIGIN = "origin"
ROUTE_SAMPLE = "sample"
ROUTE_RETURN = "return"
ROUTE_PENDING = "pending"

#: 流程状态子类（决定「放行」还是「记流程状态留在产线」）
TIER_PASS = "pass"
TIER_PENDING = "pending"
TIER_STATUS = "status"
TIER_ISOLATED = "isolated"
TIER_NONE = "none"

DESTINATION_NEXT_STAGE = "next_stage"
DESTINATION_RAW_ISOLATION = "raw_isolation"
DESTINATION_SAMPLE_ISOLATION = "sample_isolation"
DESTINATION_RETURN = "return_to_upstream"
DESTINATION_HUMAN = "human_queue"
DESTINATION_PIPELINE = "pipeline"

STATUS_SUCCESS = "success"
STATUS_ISOLATED = "isolated"
STATUS_LOCKED = "locked"
STATUS_RETURNED = "returned"
STATUS_WAITING_MANUAL = "waiting_manual"
STATUS_NOT_APPLICABLE = "not_applicable"

REASON_MANUAL_PLATFORM_UNAVAILABLE = "manual_platform_unavailable"

#: 标签前缀（表1 质量三场景）
LABEL_ORIGIN_PREFIX = "原始文件-"
LABEL_SAMPLE_PREFIX = "样本-"
LABEL_FLOW_PREFIXES = ("流程-", "流程状态-")
LABEL_UNPARSEABLE = "原始文件-无法解析"
LABEL_SEMANTIC_MISMATCH = "样本-语义不通"

#: 评分分层标记（表1：评分分层不进隔离区）
SCORING_TIER_MARKERS = ("评分：", "评分:")

#: 原始文件问题标签 → 原因码
ORIGIN_LABEL_REASON = {
    "原始文件-损坏": "unreadable_file",
    "原始文件-编码异常": "encoding_error",
    "原始文件-空": "empty_file",
    "原始文件-无法解析": "unparseable",
}

#: 业务问题标签 → 原因码
SAMPLE_LABEL_REASON = {
    "样本-敏感": "sensitive",
    "样本-语义不通": "semantic_mismatch",
    "样本-评分低": "low_score",
    "样本-错配": "mismatch",
    "样本-人工不对齐": "human_not_aligned",
    "样本-标注不完整": "incomplete_annotation",
}

#: 上游质检/对齐结论为通过的标签（M20 主产物：``样本-质检通过``）。
#: 表1 #21：「通过样本才可进入 M22」——这类标签必须放行，不能被「其他 样本-*」
#: 的保守隔离分支误伤（真机实测 668 条会被误隔离）。
PASS_LABEL_TOKENS = ("质检通过", "对齐通过", "校验通过", "审核通过", "复核通过")
PASS_LABEL_NEGATIVE_TOKENS = ("未通过", "不通过", "不通", "待通过", "待复核")

#: 上游结论码（M20 ``decision``）→ 放行/待人工兜底（标签缺失时才用）
PASS_DECISIONS = {"accepted", "accept", "passed", "pass", "ok"}
PENDING_DECISIONS = {"flagged", "pending_review", "review", "pending"}

#: 流程标签内容 → 原因码
FLOW_REASON_TOKENS = (
    ("待补描述", "missing_description"),
    ("补描述", "missing_description"),
    ("未配对", "unpaired"),
    ("待人工", "pending_manual"),
    ("人工", "pending_manual"),
    ("不适用", "temporal_not_applicable"),
    ("关闭", "route_disabled"),
)

#: 需要人工参与（进人工队列）的标签关键词；「待补描述/未配对」属流程推进，不算人工
HUMAN_KEYWORDS = ("待人工", "人工", "复核", "确认")

_ACTION_SYNONYMS = {
    "isolate": "isolate", "隔离": "isolate", "quarantine": "isolate",
    "return": "return", "reflow": "return", "退回": "return", "回流": "return",
    "rerun": "return", "重跑": "return",
    "lock": "lock", "锁定": "lock",
    "keep": "keep", "pass": "keep", "通过": "keep", "admit": "keep",
    "release": "keep", "放行": "keep", "保留": "keep",
    "waiting_manual": "waiting_manual", "manual": "waiting_manual",
    "待人工": "waiting_manual",
}

_RAW_DESTINATIONS = {"raw_isolation", "origin_isolation", "raw", "原始隔离区", "原始隔离"}
_SAMPLE_DESTINATIONS = {"sample_isolation", "sample", "样本隔离库", "样本隔离"}
_HUMAN_DESTINATIONS = {"human_queue", "manual", "human", "人工队列", "人工", "人工复核",
                       "人工复核队列", "复核队列", "待人工"}
_PIPELINE_DESTINATIONS = {"pipeline", "next_stage", "样本池", "产线", "留产线", "下游",
                          "质量报告待回写", "主清单", "下游样本池"}

_UPSTREAM_OPERATOR_RE = re.compile(r"^\d{2}_")


# ------------------------------------------------------------------ 通用工具
def bump(counter: Dict[str, Any], key: str, amount: int = 1) -> None:
    """对统计字典的某个键累加。"""
    counter[key] = counter.get(key, 0) + amount


def initial_statistics() -> Dict[str, Any]:
    """统计初值（全部字段都会在真实处理中被覆盖为真实计数）。"""
    return {
        "input_lines": 0, "blank_lines": 0, "input_records": 0,
        "valid_records": 0, "bad_lines": 0,
        "released": 0, "passed": 0, "process_kept": 0, "not_applicable": 0,
        "waiting_manual": 0, "pending_process": 0,
        "origin_isolated": 0, "sample_isolated": 0, "returned": 0, "locked": 0,
        "main_entries": 0, "ledger_entries": 0, "origin_entries": 0,
        "sample_entries": 0, "return_entries": 0, "pending_entries": 0,
        "reject_entries": 0,
        "duplicates": 0, "media_carried": 0, "media_missing": 0,
        "isolated_media_carried": 0, "media_bytes": 0,
        "category_counts": {}, "reason_counts": {}, "label_counts": {},
        "decision_counts": {}, "destination_counts": {}, "degraded_reasons": {},
        "manual_platform_available": False, "degraded_mode": False,
        "empty_input": False, "conservation_ok": True,
        "entrypoint": "21_passed.jsonl",
    }


def _first_str(*values) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _nested(record: Any, *path) -> Any:
    current = record
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def normalize_action(value: Any) -> Optional[str]:
    """把上游动作归一为 isolate/return/lock/keep/waiting_manual。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    lowered = text.lower()
    return _ACTION_SYNONYMS.get(lowered) or _ACTION_SYNONYMS.get(text)


def normalize_destination(value: Any) -> Optional[str]:
    """把上游去向归一为 DESTINATION_* 常量。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    lowered = text.lower()
    if lowered in _RAW_DESTINATIONS or text in _RAW_DESTINATIONS:
        return DESTINATION_RAW_ISOLATION
    if lowered in _SAMPLE_DESTINATIONS or text in _SAMPLE_DESTINATIONS:
        return DESTINATION_SAMPLE_ISOLATION
    if lowered in _HUMAN_DESTINATIONS or text in _HUMAN_DESTINATIONS:
        return DESTINATION_HUMAN
    if lowered in _PIPELINE_DESTINATIONS or text in _PIPELINE_DESTINATIONS:
        return DESTINATION_PIPELINE
    if _UPSTREAM_OPERATOR_RE.match(text):
        return DESTINATION_RETURN
    return None


def is_scoring_tier(label: Any) -> bool:
    """是否表1 的“评分分层”标签（流程状态，不进隔离区）。"""
    if not isinstance(label, str) or not label.strip():
        return False
    text = label.strip()
    if any(marker in text for marker in SCORING_TIER_MARKERS):
        return True
    return text in {"评分分层", "样本-评分", "评分：通过", "评分：待人工"}


def _is_flow_label(label: str) -> bool:
    return label.startswith(LABEL_FLOW_PREFIXES)


def _is_pass_label(label: Any) -> bool:
    """上游是否已判定「通过」（``样本-质检通过`` / ``样本-对齐通过`` 等）。"""
    if not isinstance(label, str):
        return False
    text = label.strip()
    if not text or any(token in text for token in PASS_LABEL_NEGATIVE_TOKENS):
        return False
    if any(token in text for token in PASS_LABEL_TOKENS):
        return True
    return text.startswith(LABEL_SAMPLE_PREFIX) and text.endswith("通过")


def _origin_reason(label: str) -> str:
    return ORIGIN_LABEL_REASON.get(label, "unparseable")


def _sample_reason(label: str) -> str:
    return SAMPLE_LABEL_REASON.get(label, "business_quality_issue")


def _flow_reason(label: str) -> str:
    for token, reason in FLOW_REASON_TOKENS:
        if token in label:
            return reason
    return "flow_status"


def _needs_human(label: str, needs_human: bool) -> bool:
    """标签是否要求人工参与；上游显式声明的需人工确认优先。"""
    if needs_human:
        return True
    return any(token in label for token in HUMAN_KEYWORDS)


# ------------------------------------------------------------------ 证据抽取
def upstream_report(record: Dict[str, Any]) -> Dict[str, Any]:
    """取上游质检报告节点（规范 §4.21 输入「M20 质量报告」）。

    M20 主产物把结论同时写在 ``processing["20"]``（权威节点）与
    ``operator_20``（扁平摘要）里，两种位置都认；后者优先，便于直接回放
    M20 主产物而不依赖 ``processing`` 是否被上游改写。
    """
    for value in (record.get("operator_20"), _nested(record, "processing", "20")):
        if isinstance(value, dict) and value:
            return value
    return {}


def extract_evidence(record: Dict[str, Any]) -> Dict[str, Any]:
    """从上游台账条目或完整记录中抽取处置证据（多位置兜底，不修改原记录）。"""
    report = upstream_report(record)
    label = _first_str(
        record.get("label"),
        _nested(record, "disposition", "label"),
        report.get("label"),
        _nested(record, "quality_report", "label"),
        _nested(record, "qc", "label"),
        _nested(record, "record", "label"),
        _nested(record, "record", "disposition", "label"),
        _nested(record, "record", "metadata", "disposition", "label"),
        _nested(record, "metadata", "disposition", "label"),
        _nested(record, "metadata", "label"),
    )
    action_raw = _first_str(
        record.get("action"),
        _nested(record, "disposition", "action"),
        _nested(record, "quality_report", "action"),
        _nested(record, "record", "action"),
        _nested(record, "record", "disposition", "action"),
        _nested(record, "metadata", "disposition", "action"),
    )
    destination_raw = _first_str(
        record.get("destination"),
        _nested(record, "disposition", "destination"),
        report.get("destination"),
        _nested(record, "quality_report", "destination"),
        _nested(record, "record", "destination"),
        _nested(record, "record", "disposition", "destination"),
        _nested(record, "metadata", "disposition", "destination"),
    )
    batch_id = _first_str(
        record.get("batch_id"),
        _nested(record, "metadata", "batch_id"),
        _nested(record, "record", "batch_id"),
        _nested(record, "record", "metadata", "batch_id"),
    )
    stage = _first_str(record.get("stage"),
                       report.get("stage_id"),
                       _nested(record, "record", "stage"),
                       _nested(record, "metadata", "stage"))
    upstream_operator = _first_str(record.get("operator"),
                                   report.get("operator"),
                                   _nested(record, "record", "operator"),
                                   _nested(record, "metadata", "operator"))
    event_time = _first_str(record.get("event_time"),
                            report.get("evaluated_at"),
                            _nested(record, "record", "event_time"))
    upstream_decision = _first_str(report.get("decision"), record.get("decision"))
    retry_count = _first_int(
        record.get("retry_count"),
        _nested(record, "metadata", "retry_count"),
        _nested(record, "disposition", "retry_count"),
        _nested(record, "record", "retry_count"),
        _nested(record, "record", "metadata", "retry_count"),
        _nested(record, "record", "disposition", "retry_count"),
    )
    return_count = _first_int(
        _nested(record, "operator_21", "return_count"),
        _nested(record, "record", "operator_21", "return_count"),
        record.get("return_count"),
    )
    needs_human = bool(
        record.get("requires_human_confirmation") is True
        or record.get("human_review_required") is True
        or record.get("review_required") is True
        or _nested(record, "metadata", "human_review_required") is True
        or _nested(record, "metadata", "requires_human_confirmation") is True
        or _nested(record, "record", "metadata", "human_review_required") is True
        or _nested(record, "record", "requires_human_confirmation") is True
    )
    return {
        "label": label,
        "action": normalize_action(action_raw),
        "action_raw": action_raw,
        "destination": normalize_destination(destination_raw),
        "destination_raw": destination_raw,
        "stage": stage,
        "upstream_operator": upstream_operator,
        "upstream_decision": upstream_decision,
        "event_time": event_time,
        "batch_id": batch_id,
        "retry_count": retry_count,
        "return_count": return_count,
        "requires_human_confirmation": needs_human,
    }


def _first_int(*values) -> int:
    """取第一个合法的非负整数；布尔、字符串、负数一律按 0（不猜语义）。"""
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def media_reference(record: Dict[str, Any], name: str) -> Optional[str]:
    """取媒体相对路径：优先嵌入的 ``record``，其次条目顶层。"""
    candidates = (
        _nested(record, "record", name, "path"),
        _nested(record, name, "path"),
    )
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def record_size(record: Any) -> int:
    """记录 JSON（UTF-8，不转义中文）的字节数，用于台账快照上限判断。"""
    try:
        return len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ 分类
def _make_category(category: str, label: Optional[str], action: str, destination: str,
                   reason: str, tier: str = "status") -> Dict[str, Any]:
    return {
        "category": category,
        "label": label,
        "action": action,
        "destination": destination,
        "reason_code": reason,
        "tier": tier,
        "review_required": destination == DESTINATION_HUMAN,
    }


def classify(evidence: Dict[str, Any], business: Dict[str, Any]) -> Dict[str, Any]:
    """按表1 固定顺序判定处置分类。

    顺序：原始文件问题 → 评分分层（流程状态，表1 明令不进隔离区）→ 上游已通过 →
    业务问题 → 显式锁定/回流 → 流程标签 → 显式隔离 → 显式放行/待人工 → 显式去向 →
    上游结论码 → 无证据。

    ``tier`` 说明流程状态子类，供 ``evaluate_record`` 决定放行还是记 ``process_kept``：
    ``pass``（评分分层：通过/入池/CLIP分）、``pending``（待人工）、
    ``status``（其他流程状态）、``isolated``/``none``。
    """
    label = evidence.get("label")
    action = evidence.get("action")
    destination = evidence.get("destination")
    needs_human = bool(evidence.get("requires_human_confirmation"))
    scoring_not_isolated = bool(business.get("scoring_tiers_not_isolated", True))

    # 1. 原始文件问题：无论上游动作写什么，一律原始隔离区
    if isinstance(label, str) and label.startswith(LABEL_ORIGIN_PREFIX):
        return _make_category(CATEGORY_ORIGIN, label, "isolate",
                              DESTINATION_RAW_ISOLATION, _origin_reason(label), TIER_ISOLATED)

    # 2. 评分分层：流程状态，不进隔离区（表1 明令）
    if isinstance(label, str) and is_scoring_tier(label) and scoring_not_isolated:
        if _needs_human(label, needs_human):
            return _make_category(CATEGORY_PROCESS, label, "keep",
                                  DESTINATION_HUMAN, "scoring_tier", TIER_PENDING)
        return _make_category(CATEGORY_PROCESS, label, "keep",
                              DESTINATION_PIPELINE, "scoring_tier", TIER_PASS)

    # 3. 上游质检/对齐已通过（M20「样本-质检通过」）：表1「通过样本才可进入 M22」
    if _is_pass_label(label) and not _needs_human(label, needs_human):
        return _make_category(CATEGORY_PASS, label, "keep",
                              DESTINATION_NEXT_STAGE, "upstream_passed", TIER_PASS)

    # 4. 业务问题标签（敏感 / 语义不通 / 评分低 / 错配 / 人工不对齐 / 标注不完整）
    if isinstance(label, str) and (label in SAMPLE_LABEL_REASON or "评分低" in label):
        return _make_category(CATEGORY_SAMPLE, label, "isolate",
                              DESTINATION_SAMPLE_ISOLATION, _sample_reason(label),
                              TIER_ISOLATED)

    # 5. 显式锁定（第 3 次 → 样本隔离库；优先于流程标签，不被流程状态覆盖）
    if action == "lock":
        return _make_category(CATEGORY_LOCK, label or LABEL_SEMANTIC_MISMATCH, "lock",
                              DESTINATION_SAMPLE_ISOLATION, "explicit_lock", TIER_ISOLATED)

    # 6. 显式回流（优先于流程标签：同一条可能是「流程-待补描述 + 退回上游」）
    if action == "return":
        return _make_category(CATEGORY_RETURN, label, "return",
                              DESTINATION_RETURN, "return_to_upstream", TIER_STATUS)

    # 7. 流程标签：流程状态，留产线
    if isinstance(label, str) and _is_flow_label(label):
        if _needs_human(label, needs_human):
            return _make_category(CATEGORY_PROCESS, label, "keep",
                                  DESTINATION_HUMAN, _flow_reason(label), TIER_PENDING)
        return _make_category(CATEGORY_PROCESS, label, "keep",
                              DESTINATION_PIPELINE, _flow_reason(label), TIER_STATUS)

    # 8. 其他样本-* 标签：按业务问题处理（保守口径，交人工复核）
    if isinstance(label, str) and label.startswith(LABEL_SAMPLE_PREFIX):
        return _make_category(CATEGORY_SAMPLE, label, "isolate",
                              DESTINATION_SAMPLE_ISOLATION, _sample_reason(label),
                              TIER_ISOLATED)

    # 9. 显式隔离
    if action == "isolate":
        if destination == DESTINATION_RAW_ISOLATION:
            return _make_category(CATEGORY_ORIGIN, label, "isolate",
                                  DESTINATION_RAW_ISOLATION,
                                  _origin_reason(label) if label else "unparseable",
                                  TIER_ISOLATED)
        return _make_category(CATEGORY_SAMPLE, label, "isolate",
                              DESTINATION_SAMPLE_ISOLATION,
                              _sample_reason(label) if label else "business_quality_issue",
                              TIER_ISOLATED)

    # 10. 显式待人工 / 显式放行
    if action == "waiting_manual" or (action == "keep" and destination == DESTINATION_HUMAN):
        return _make_category(CATEGORY_PROCESS, label, "keep",
                              DESTINATION_HUMAN, "pending_manual", TIER_PENDING)
    if action == "keep":
        return _make_category(CATEGORY_PASS, label, "keep",
                              DESTINATION_NEXT_STAGE, "upstream_passed", TIER_PASS)

    # 11. 只有上游结论码（无标签/动作）：按 M20 ``decision`` 兜底
    upstream_decision = str(evidence.get("upstream_decision") or "").strip().lower()
    if upstream_decision in PASS_DECISIONS:
        return _make_category(CATEGORY_PASS, label, "keep",
                              DESTINATION_NEXT_STAGE, "upstream_passed", TIER_PASS)
    if upstream_decision in PENDING_DECISIONS:
        return _make_category(CATEGORY_PROCESS, label, "keep",
                              DESTINATION_HUMAN, "pending_manual", TIER_PENDING)

    # 12. 显式去向
    if destination == DESTINATION_RAW_ISOLATION:
        return _make_category(CATEGORY_ORIGIN, label, "isolate",
                              DESTINATION_RAW_ISOLATION,
                              _origin_reason(label) if label else "unparseable",
                              TIER_ISOLATED)
    if destination == DESTINATION_SAMPLE_ISOLATION:
        return _make_category(CATEGORY_SAMPLE, label, "isolate",
                              DESTINATION_SAMPLE_ISOLATION,
                              _sample_reason(label) if label else "business_quality_issue",
                              TIER_ISOLATED)
    if destination == DESTINATION_RETURN:
        return _make_category(CATEGORY_RETURN, label, "return",
                              DESTINATION_RETURN, "return_to_upstream", TIER_STATUS)
    if destination == DESTINATION_HUMAN:
        return _make_category(CATEGORY_PROCESS, label, "keep",
                              DESTINATION_HUMAN, "pending_manual", TIER_PENDING)
    if destination == DESTINATION_PIPELINE:
        return _make_category(CATEGORY_PROCESS, label, "keep",
                              DESTINATION_PIPELINE, "flow_status", TIER_STATUS)

    # 13. 无处置证据：留产线，记不适用
    return _make_category(CATEGORY_NONE, label, "keep",
                          DESTINATION_PIPELINE, "no_disposition_evidence", TIER_NONE)


# ------------------------------------------------------------------ 退回/锁定预算
def decide_retry(retry_count: int, return_count: int, business: Dict[str, Any]) -> Dict[str, Any]:
    """按表1 退回/锁定预算判定：允许回流还是锁定样本隔离库。"""
    return_limit = int(business.get("return_limit", 1))
    max_retry = int(business.get("max_retry_per_item", 2))
    next_return = return_count + 1
    next_retry = retry_count + 1
    if next_return > return_limit:
        return {"decision": DECISION_LOCKED, "locked": True, "retry_count": retry_count,
                "return_count": return_count, "reason_code": "return_limit_exhausted"}
    if next_retry > max_retry:
        return {"decision": DECISION_LOCKED, "locked": True, "retry_count": retry_count,
                "return_count": return_count, "reason_code": "retry_budget_exhausted"}
    return {"decision": DECISION_RETURN, "locked": False, "retry_count": next_retry,
            "return_count": next_return, "reason_code": "return_to_upstream"}


# ------------------------------------------------------------------ 台账条目
def _source_reference(evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "upstream_operator": evidence.get("upstream_operator"),
        "upstream_stage": evidence.get("stage"),
        "upstream_label": evidence.get("label"),
        "upstream_decision": evidence.get("upstream_decision"),
        "upstream_action": evidence.get("action_raw"),
        "upstream_destination": evidence.get("destination_raw"),
        "upstream_event_time": evidence.get("event_time"),
    }


def _snapshot(record: Any, limit: int) -> Tuple[Any, bool, int]:
    """返回 (快照, 是否截断, 字节数)；超限只保留哈希与大小（不复制隔离样本）。"""
    if record is None:
        return None, False, 0
    size = record_size(record)
    if size <= limit:
        return copy.deepcopy(record), False, size
    digest = hashlib.sha256(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return None, True, size


def build_ledger_entry(*, sample_id: Optional[str], line: Optional[int], label: Optional[str],
                       action: str, destination: str, reason_code: str,
                       operator: str, stage_id: str, event_time: str,
                       batch_id: Optional[str], retry_count: int,
                       decision: str, route: str, evidence: Dict[str, Any],
                       record: Any = None, limit: int = 32768,
                       duplicate: bool = False, locked: bool = False,
                       return_count: int = 0, degraded: bool = False,
                       degraded_reason: Optional[str] = None) -> Dict[str, Any]:
    """构造一条统一台账条目（六类字段 + 决策 + 证据 + 有限快照）。"""
    snapshot, truncated, size = _snapshot(record, limit)
    entry: Dict[str, Any] = {
        "sample_id": sample_id,
        "line": line,
        "label": label,
        "operator": operator,
        "stage": stage_id,
        "action": action,
        "destination": destination,
        "event_time": event_time,
        "batch_id": batch_id,
        "retry_count": int(retry_count),
        "return_count": int(return_count),
        "decision": decision,
        "route": route,
        "reason_code": reason_code,
        "locked": bool(locked),
        "duplicate": bool(duplicate),
        "degraded": bool(degraded),
        "degraded_reason": degraded_reason,
        "human_verified": False,       # 未接入人工复核平台，绝不伪造人工结论
        "review_required": destination == DESTINATION_HUMAN,
        "record_truncated": truncated,
        "record_bytes": size,
        "upstream_destination": evidence.get("destination_raw"),
        "source_reference": _source_reference(evidence),
        "evidence": {
            "upstream_label": evidence.get("label"),
            "upstream_action": evidence.get("action_raw"),
            "upstream_destination": evidence.get("destination_raw"),
            "upstream_operator": evidence.get("upstream_operator"),
            "upstream_return_count": evidence.get("return_count"),
        },
        # 有界快照：超限时为 None（只保留 record_bytes），绝不搬运隔离样本本体
        "record": snapshot,
    }
    return entry


def busy_line_entry(*, operator: str, stage_id: str, line: int, reason: str,
                    message: str, event_time: str,
                    sample_id: Optional[str] = None) -> Dict[str, Any]:
    """坏行/非法记录的拒绝台账条目（无法解析 → 原始隔离区）。"""
    return {
        "sample_id": sample_id,
        "line": line,
        "label": LABEL_UNPARSEABLE,
        "operator": operator,
        "stage": stage_id,
        "action": "isolate",
        "destination": DESTINATION_RAW_ISOLATION,
        "event_time": event_time,
        "batch_id": None,
        "retry_count": 0,
        "return_count": 0,
        "decision": DECISION_REJECTED,
        "route": ROUTE_ORIGIN,
        "reason_code": reason,
        "message": message,
        "locked": False,
        "duplicate": False,
        "degraded": False,
        "degraded_reason": None,
        "human_verified": False,
        "review_required": False,
        "record_truncated": False,
        "record_bytes": 0,
        "upstream_destination": None,
        "source_reference": {"upstream_operator": None, "upstream_stage": None,
                             "upstream_label": None, "upstream_action": None,
                             "upstream_destination": None, "upstream_event_time": None},
        "evidence": {},
    }


# ------------------------------------------------------------------ 单条处置
# [v4.2] status 双字段（规则 §2.10）：输出记录 status 为 int（1=成功/通过/不过滤，
# 0=失败/过滤），原字符串流程态迁入 process_status。
def _status_for(decision: str) -> tuple:
    """返回 ``(status:int, process_status:str)``；隔离/锁定为过滤（0），其余留产线（1）。"""
    if decision == DECISION_PASS or decision == DECISION_PROCESS:
        return 1, STATUS_SUCCESS
    if decision == DECISION_NOT_APPLICABLE:
        return 1, STATUS_NOT_APPLICABLE
    if decision == DECISION_WAITING_MANUAL:
        return 1, STATUS_WAITING_MANUAL
    if decision == DECISION_RETURN:
        return 1, STATUS_RETURNED
    if decision == DECISION_LOCKED:
        return 0, STATUS_LOCKED
    return 0, STATUS_ISOLATED


def _augment_record(record: Dict[str, Any], *, decision: str, label: Optional[str],
                    action: str, destination: str, reason_code: str, retry_count: int,
                    return_count: int, locked: bool, duplicate: bool,
                    business: Dict[str, Any],
                    evidence: Dict[str, Any]) -> Dict[str, Any]:
    """保留上游字段，只追加本算子命名空间与 ``processing["21"]``。

    主产物刻意**不写处理时刻**：同一输入必须产出逐字节相同的主清单（可回归比对），
    处置时刻记录在台账（六类字段的 ``event_time``）与 ``run.log`` 中。
    """
    result = copy.deepcopy(record)
    status, process_status = _status_for(decision)  # [v4.2] status 双字段
    namespace = {
        "operator": OPERATOR,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": business.get("operator_version"),
        "decision": decision,
        "status": status,
        "process_status": process_status,
        "label": label,
        "action": action,
        "destination": destination,
        "reason_code": reason_code,
        "retry_count": int(retry_count),
        "return_count": int(return_count),
        "locked": bool(locked),
        "duplicate": bool(duplicate),
        "human_verified": False,
        "review_required": destination == DESTINATION_HUMAN,
        "source_reference": _source_reference(evidence),
    }
    result["operator_21"] = namespace
    processing = dict(result.get("processing") or {})
    processing["21"] = {
        "status": status,
        "process_status": process_status,
        "action": action,
        "decision": decision,
        "destination": destination,
        "label": label,
        "reason_code": reason_code,
        "retry_count": int(retry_count),
        "locked": bool(locked),
    }
    result["processing"] = processing
    return result


def evaluate_record(record: Dict[str, Any], lineno: int, business: Dict[str, Any],
                    stats: Dict[str, Any], seen_ids: set,
                    event_time: str) -> Dict[str, Any]:
    """处置一条有效记录，返回 ``{route, decision, entry, record}``。

    ``route`` ∈ released / origin / sample / return / pending；``record`` 仅在
    ``released`` / ``pending`` 时非空（这两种要写入主清单）。
    """
    sample_id = record.get("sample_id")
    evidence = extract_evidence(record)
    decision = classify(evidence, business)
    category = decision["category"]
    limit = int(business.get("max_embedded_record_bytes", 32768))

    duplicate = sample_id in seen_ids
    if duplicate:
        bump(stats, "duplicates")
    seen_ids.add(sample_id)

    label = decision["label"]
    action = decision["action"]
    destination = decision["destination"]
    reason = decision["reason_code"]
    tier = decision.get("tier")
    retry_count = evidence["retry_count"]
    return_count = evidence["return_count"]
    locked = False
    degraded = False
    degraded_reason = None

    def _finish(route: str, decision_name: str, *, out_label=None, out_action=None,
                out_destination=None, out_reason=None, record_out=None,
                stat_keys=(), bump_reason=True) -> Dict[str, Any]:
        final_label = label if out_label is None else out_label
        final_action = action if out_action is None else out_action
        final_destination = destination if out_destination is None else out_destination
        final_reason = reason if out_reason is None else out_reason
        bump(stats["category_counts"], category)
        bump(stats["decision_counts"], decision_name)
        bump(stats["destination_counts"], final_destination)
        bump(stats["label_counts"], final_label or "<none>")
        if bump_reason:
            bump(stats["reason_counts"], final_reason)
        for key in stat_keys:
            bump(stats, key)
        entry = build_ledger_entry(
            sample_id=sample_id, line=lineno, label=final_label, action=final_action,
            destination=final_destination, reason_code=final_reason, operator=OPERATOR,
            stage_id=business.get("stage_id"), event_time=event_time,
            batch_id=evidence["batch_id"], retry_count=retry_count, decision=decision_name,
            route=route, evidence=evidence,
            record=evidence_snapshot if route != ROUTE_RELEASED and route != ROUTE_PENDING
            else None,
            limit=limit, duplicate=duplicate, locked=locked,
            return_count=return_count, degraded=degraded, degraded_reason=degraded_reason)
        return {"route": route, "decision": decision_name, "entry": entry,
                "record": record_out}

    # 原始快照来源：优先嵌入的 record，否则条目本身
    evidence_snapshot = record.get("record") if isinstance(record.get("record"), dict) else record

    if category == CATEGORY_NONE:
        out = _augment_record(record, decision=DECISION_NOT_APPLICABLE, label=label,
                              action="keep", destination=DESTINATION_PIPELINE,
                              reason_code="no_disposition_evidence", retry_count=retry_count,
                              return_count=return_count, locked=False, duplicate=duplicate,
                              business=business, evidence=evidence)
        return _finish(ROUTE_RELEASED, DECISION_NOT_APPLICABLE, out_action="keep",
                       out_destination=DESTINATION_PIPELINE,
                       out_reason="no_disposition_evidence", record_out=out,
                       stat_keys=("released", "not_applicable"), bump_reason=False)

    if category == CATEGORY_PASS:
        out = _augment_record(record, decision=DECISION_PASS, label=label,
                              action="keep", destination=DESTINATION_NEXT_STAGE,
                              reason_code=reason, retry_count=retry_count,
                              return_count=return_count, locked=False, duplicate=duplicate,
                              business=business, evidence=evidence)
        return _finish(ROUTE_RELEASED, DECISION_PASS, out_action="keep",
                       out_destination=DESTINATION_NEXT_STAGE, record_out=out,
                       stat_keys=("released", "passed"), bump_reason=False)

    if category == CATEGORY_PROCESS:
        # 需要人工参与：平台未接入时只能显式 waiting_manual 或结构化失败，绝不伪造结论
        if destination == DESTINATION_HUMAN:
            if not business.get("manual_platform_available", False):
                if not business.get("allow_degraded", False):
                    raise DependencyError(
                        "人工复核平台未接入，且未允许降级：需人工确认的样本 %s 无法处置"
                        % (sample_id or "<unknown>"))
                degraded = True
                degraded_reason = REASON_MANUAL_PLATFORM_UNAVAILABLE
                stats["degraded_mode"] = True
                bump(stats["degraded_reasons"], REASON_MANUAL_PLATFORM_UNAVAILABLE)
                out = _augment_record(record, decision=DECISION_WAITING_MANUAL, label=label,
                                      action="keep", destination=DESTINATION_HUMAN,
                                      reason_code=reason, retry_count=retry_count,
                                      return_count=return_count, locked=False,
                                      duplicate=duplicate, business=business,
                                      evidence=evidence)
                return _finish(ROUTE_PENDING, DECISION_WAITING_MANUAL, out_action="keep",
                               out_destination=DESTINATION_HUMAN, record_out=out,
                               stat_keys=("released", "waiting_manual"))
            out = _augment_record(record, decision=DECISION_PROCESS, label=label,
                                  action="keep", destination=DESTINATION_HUMAN,
                                  reason_code=reason, retry_count=retry_count,
                                  return_count=return_count, locked=False,
                                  duplicate=duplicate, business=business, evidence=evidence)
            return _finish(ROUTE_PENDING, DECISION_PROCESS, out_action="keep",
                           out_destination=DESTINATION_HUMAN, record_out=out,
                           stat_keys=("released", "process_kept", "pending_process"),
                           bump_reason=False)

        # 评分分层（通过/入池/CLIP分）：放行给下游，不进隔离区
        if tier == TIER_PASS:
            out = _augment_record(record, decision=DECISION_PASS, label=label,
                                  action="keep", destination=DESTINATION_NEXT_STAGE,
                                  reason_code=reason, retry_count=retry_count,
                                  return_count=return_count, locked=False,
                                  duplicate=duplicate, business=business, evidence=evidence)
            return _finish(ROUTE_RELEASED, DECISION_PASS, out_action="keep",
                           out_destination=DESTINATION_NEXT_STAGE, record_out=out,
                           stat_keys=("released", "passed"), bump_reason=False)

        # 其他流程状态（待补描述 / 未配对 / 路线关闭）：留产线，记流程状态
        out = _augment_record(record, decision=DECISION_PROCESS, label=label,
                              action="keep", destination=DESTINATION_PIPELINE,
                              reason_code=reason, retry_count=retry_count,
                              return_count=return_count, locked=False, duplicate=duplicate,
                              business=business, evidence=evidence)
        return _finish(ROUTE_RELEASED, DECISION_PROCESS, out_action="keep",
                       out_destination=DESTINATION_PIPELINE, record_out=out,
                       stat_keys=("released", "process_kept"), bump_reason=False)

    if category == CATEGORY_ORIGIN:
        return _finish(ROUTE_ORIGIN, DECISION_ORIGIN_ISOLATED, out_action="isolate",
                       out_destination=DESTINATION_RAW_ISOLATION,
                       stat_keys=("origin_isolated",))

    if category == CATEGORY_SAMPLE:
        return _finish(ROUTE_SAMPLE, DECISION_SAMPLE_ISOLATED, out_action="isolate",
                       out_destination=DESTINATION_SAMPLE_ISOLATION,
                       stat_keys=("sample_isolated",))

    # CATEGORY_RETURN / CATEGORY_LOCK
    if category == CATEGORY_LOCK:
        retry_decision = {"decision": DECISION_LOCKED, "locked": True,
                          "retry_count": retry_count, "return_count": return_count,
                          "reason_code": "explicit_lock"}
    else:
        retry_decision = decide_retry(retry_count, return_count, business)

    if retry_decision["decision"] == DECISION_LOCKED:
        locked = True
        return _finish(ROUTE_SAMPLE, DECISION_LOCKED, out_label=LABEL_SEMANTIC_MISMATCH,
                       out_action="lock", out_destination=DESTINATION_SAMPLE_ISOLATION,
                       out_reason=retry_decision["reason_code"],
                       stat_keys=("sample_isolated", "locked"))

    retry_count = retry_decision["retry_count"]
    return_count = retry_decision["return_count"]
    return _finish(ROUTE_RETURN, DECISION_RETURN, out_action="return",
                   out_destination=DESTINATION_RETURN,
                   out_reason=retry_decision["reason_code"],
                   stat_keys=("returned",))
