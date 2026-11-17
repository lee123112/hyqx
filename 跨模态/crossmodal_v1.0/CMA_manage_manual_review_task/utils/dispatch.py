"""算子 25 核心业务：人工复核任务的类别判定、派单、结论回写与样本隔离。

严格对应表1 #25「管理人工复核任务」（第 7 环节「人工抽检复核」，``stageId=S7``）：

| 表1 要求 | 本模块实现 |
| --- | --- |
| 手动触发；是（可选）；优先级 P1 | ``enabled`` 默认 false，必须显式开启；``trigger`` 冻结为 ``manual`` |
| 类型=系统功能；模型 ``--`` | 不做任何模型打分，不产生固定/随机分数 |
| 输入：存疑、抽检异议、加工退回样本 | ``resolve_category`` 只认这三类显式证据 |
| 处理边界：手动触发派单并回写 | ``enabled`` 为真时才生成派单任务；``manual_platform_available`` 决定派单状态 |
| 通过回候选池 / 不通过进样本隔离库 | ``DECISION_PASSED`` → 候选池；``DECISION_REJECTED`` → 样本隔离库（#21） |
| 同一条最多复核 2 次 | ``max_review_count=2``；已有 2 轮则 ``DECISION_MAX_REVIEW`` 且锁定不再派单 |
| 不重复 #24 抽样 | 不重新抽样；``repeat_spot_check`` 冻结为 false |
| 自动对齐且未被抽中的样本不进队列 | ``DECISION_AUTO_ALIGNED_NOT_SAMPLED``（只读判定，不改写上游） |
| 低置信但尚未判错 | 留在人工队列（``DECISION_PENDING``），不拦其余样本 |
| 无三类样本不生成任务 | 类别取不到值时 ``DECISION_NOT_ELIGIBLE``，任务数 0 |

**不得伪造**：人工结论只来自输入证据；人工平台未接入时派单状态只能是
``waiting_manual``，队列记录保持待人工，绝不假定「平台已受理」或「人工已确认」。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import review as review_mod
from . import schemas

_TZ = timezone(timedelta(hours=8))

# -- 身份 ------------------------------------------------------------------ #
OPERATOR_NAME = schemas.OPERATOR_NAME
OPERATOR_LABEL = schemas.OPERATOR_LABEL
OPERATOR_VERSION = schemas.OPERATOR_VERSION
STAGE_ID = schemas.STAGE_ID
STAGE_LABEL = schemas.STAGE_LABEL
RUN_STAGE = schemas.RUN_STAGE
FORMAL_OPERATOR_NUMBER = schemas.FORMAL_OPERATOR_NUMBER
DEFAULT_PORT = schemas.DEFAULT_PORT
MAX_REVIEW_COUNT = schemas.MAX_REVIEW_COUNT_FROZEN
ELIGIBLE_CATEGORIES = schemas.ELIGIBLE_CATEGORIES

# -- 产物 ------------------------------------------------------------------ #
MAIN_NAME = "25_review_candidates.jsonl"
TASKS_NAME = "25_review_tasks.jsonl"
CONCLUSIONS_NAME = "25_review_conclusions.jsonl"
RETURN_NAME = "25_sample_pool_return.jsonl"
SAMPLE_QUARANTINE_NAME = "25_sample_quarantine.jsonl"
PENDING_NAME = "25_pending_manual.jsonl"
NOT_DISPATCHED_NAME = "25_not_dispatched.jsonl"
ORIGIN_QUARANTINE_NAME = "25_original_quarantine.jsonl"
REJECTS_NAME = "25_rejects.jsonl"
REPORT_NAME = "25_review_report.json"
LOG_NAME = "run.log"
MANIFEST_NAME = "manifest.json"

OUTPUT_NAMES: Tuple[str, ...] = (
    MAIN_NAME, TASKS_NAME, CONCLUSIONS_NAME, RETURN_NAME, SAMPLE_QUARANTINE_NAME,
    PENDING_NAME, NOT_DISPATCHED_NAME, ORIGIN_QUARANTINE_NAME, REJECTS_NAME, REPORT_NAME,
)

# -- 类别（表1 #25 三类样本） ---------------------------------------------- #
CATEGORY_UNCERTAIN = "uncertain"
CATEGORY_DISPUTE = "sampling_dispute"
CATEGORY_RETURNED = "returned"

# -- 判定 ------------------------------------------------------------------ #
DECISION_NOT_APPLICABLE = "not_applicable"
DECISION_NOT_ELIGIBLE = "not_eligible"
DECISION_NOT_TRIGGERED = "not_triggered"
DECISION_AUTO_ALIGNED_NOT_SAMPLED = "auto_aligned_not_sampled"
DECISION_MAX_REVIEW = "max_review_reached"
DECISION_PASSED = "passed"
DECISION_REJECTED = "rejected"
DECISION_PENDING = "pending_manual"
DECISION_UNCERTAIN = "uncertain"

# -- 统一台账字段（背景规则 §2.3） ---------------------------------------- #
LABEL_PASSED = "流程-复核通过"
LABEL_PENDING = "流程-复核待人工"
LABEL_UNCERTAIN = "流程-复核存疑"
LABEL_MAX_REVIEW = "流程-复核次数超限"
LABEL_NOT_ELIGIBLE = "流程-无复核任务"
LABEL_NOT_TRIGGERED = "流程-未触发复核"
LABEL_AUTO_ALIGNED = "流程-自动对齐未抽中"
LABEL_UPSTREAM_ISOLATED = "流程-上游已隔离"
LABEL_SAMPLE_SEMANTIC = "样本-语义不通"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_ORIGIN_DUPLICATE = "原始文件-重复标识"
LABEL_ORIGIN_BAD_LINE = "原始文件-坏行"

DESTINATION_POOL = "候选池"
DESTINATION_REVIEW = "人工复核队列"
DESTINATION_SAMPLE = "样本隔离库"
DESTINATION_ORIGIN = "原始隔离区"
DESTINATION_PIPELINE = "产线保留"

ACTION_KEEP = "keep"
ACTION_ISOLATE = "isolate"
ACTION_LOCK = "lock"
ACTION_WAITING = "waiting_manual"

#: 旁路台账角色 → 交接算子（表1 #25「通过回 #19/#20；不通过交 #21」）
HANDOVER = {
    "passed": ["operator_19", "operator_20"],
    "failed": ["operator_21"],
    "pending": ["operator_25_platform"],
    "locked": ["operator_21"],
}

# [v4.2] status 双字段（规则 §2.10）：输出记录写 int status（本算子处理型：
# 1=成功/正常流程态，0=失败/过滤——人工不通过进样本隔离库、复核超限锁定），
# 原字符串流程态一律迁入 process_status。``run()`` 顶层 status 仍保留字符串契约。
STATUS_AS_INT: Dict[str, int] = {
    "not_applicable": 1,
    "not_eligible": 1,
    "not_triggered": 1,
    "not_dispatched": 1,
    "waiting_manual": 1,
    "waiting_dispatch": 1,
    "dispatched": 1,
    "passed": 1,
    "isolated": 0,
    "locked": 0,
    "unknown": 1,
}

#: 判定结论 → 记录级流程态（写入 process_status）
_DECISION_STATE: Dict[str, str] = {
    DECISION_NOT_APPLICABLE: "not_applicable",
    DECISION_NOT_ELIGIBLE: "not_eligible",
    DECISION_NOT_TRIGGERED: "not_triggered",
    DECISION_AUTO_ALIGNED_NOT_SAMPLED: "not_dispatched",
    DECISION_MAX_REVIEW: "locked",
    DECISION_PASSED: "passed",
    DECISION_REJECTED: "isolated",
    DECISION_PENDING: "waiting_manual",
    DECISION_UNCERTAIN: "waiting_manual",
}


def status_int(value: Optional[str]) -> int:
    """[v4.2] 记录级字符串流程态 → int status；未登记的一律按正常流程态 1。"""
    return STATUS_AS_INT.get(value, 1)


REQUIRED_LEDGER_FIELDS = ("label", "stage", "action", "destination")
REQUIRED_LINEAGE_FIELDS = ("operator", "operator_version", "uniqueId", "stageId",
                           "inputFile", "status", "startedAt", "finishedAt",
                           "config", "statistics", "runId", "artifacts")
RAW_PREVIEW_LIMIT = 200

_AUTO_ALIGNMENT_VALUES = {"auto_aligned", "automatic", "auto", "aligned"}
_UNCERTAIN_ALIGNMENT_VALUES = {"uncertain", "doubt", "pending_manual",
                               "waiting_manual", "manual", "manual_review"}
_RETURN_ACTIONS = {"return", "rework", "retry", "reflow", "rerun", "returned"}
_RETURN_DECISIONS = {"returned", "return", "rework"}
_RETURN_LABELS = {"流程-退回重跑", "流程-退回", "流程-回流"}
_DISPUTE_DECISIONS = {"rejected", "reject", "disputed", "dispute"}
_DISPUTE_LABEL = "流程-抽检存疑"
_MANUAL_SCORE_LABEL = "样本-评分：待人工"
#: 表1 #12 的「疑似中间分」区间：0.25 到 0.32 属于待人工，不隔离
LOW_CONFIDENCE_MIN = 0.25
LOW_CONFIDENCE_MAX = 0.32


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def default_business(**overrides: Any) -> Dict[str, Any]:
    """与部署配置同源的业务参数字典。"""
    return schemas.default_business(**overrides)


# --------------------------------------------------------------------------- #
# 上游证据解析
# --------------------------------------------------------------------------- #

def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _lower(value: Any) -> str:
    text = _text(value)
    return text.lower() if text else ""


def _node(record: Mapping[str, Any], namespace: str) -> Optional[Dict[str, Any]]:
    processing = record.get("processing")
    if not isinstance(processing, dict):
        return None
    for key in (namespace, int(namespace) if str(namespace).isdigit() else namespace):
        node = processing.get(key)
        if isinstance(node, dict):
            return node
    return None


def upstream_isolated(record: Mapping[str, Any]) -> bool:
    """上游（#20/#21）已判定隔离的记录不再进入本算子队列。"""
    if record.get("isolated") is True:
        return True
    for namespace in ("21", "20"):
        node = _node(record, namespace)
        if not isinstance(node, dict):
            continue
        if node.get("isolated") is True:
            return True
        if _lower(node.get("decision")) in ("isolated", "isolate"):
            return True
        if _lower(node.get("action")) == ACTION_ISOLATE:
            return True
        if (node.get("label") == LABEL_SAMPLE_SEMANTIC
                and node.get("destination") == DESTINATION_SAMPLE):
            return True
    return False


def sampled_by_24(record: Mapping[str, Any]) -> Optional[bool]:
    """是否被 #24 抽中过（``None`` 表示上游没有给出该证据）。"""
    node = _node(record, "24")
    if isinstance(node, dict):
        if isinstance(node.get("sampled"), bool):
            return bool(node["sampled"])
        if _text(node.get("role")) in ("spot_check_sample", "dispute"):
            return True
    if isinstance(record.get("sampled"), bool):
        return bool(record["sampled"])
    return None


def is_auto_aligned(record: Mapping[str, Any]) -> bool:
    """上游对齐结论是否为「自动对齐」。"""
    alignment = record.get("alignment")
    if isinstance(alignment, dict):
        if alignment.get("auto_aligned") is True:
            return True
        for key in ("status", "type", "decision", "alignment_type"):
            if _lower(alignment.get(key)) in _AUTO_ALIGNMENT_VALUES:
                return True
    elif isinstance(alignment, str) and _lower(alignment) in _AUTO_ALIGNMENT_VALUES:
        return True
    node = _node(record, "16")
    if isinstance(node, dict):
        if node.get("auto_aligned") is True:
            return True
        if _lower(node.get("status")) in _AUTO_ALIGNMENT_VALUES:
            return True
    return False


def _returned_evidence(record: Mapping[str, Any]) -> Optional[str]:
    node = _node(record, "21")
    if isinstance(node, dict):
        action = _lower(node.get("action"))
        decision = _lower(node.get("decision"))
        if action in _RETURN_ACTIONS:
            return "processing_21.action=%s" % action
        if decision in _RETURN_DECISIONS:
            return "processing_21.decision=%s" % decision
        if _lower(node.get("status")) in _RETURN_DECISIONS:
            return "processing_21.status"
        if node.get("returned") is True:
            return "processing_21.returned"
        label = _text(node.get("label"))
        if label in _RETURN_LABELS:
            return "processing_21.label=%s" % label
    if record.get("returned") is True:
        return "record.returned"
    if _text(record.get("return_reason")):
        return "record.return_reason"
    return None


def _dispute_evidence(record: Mapping[str, Any]) -> Optional[str]:
    node = _node(record, "24")
    if isinstance(node, dict):
        if _text(node.get("role")) == "dispute":
            return "processing_24.role=dispute"
        decision = _lower(node.get("decision"))
        if decision in _DISPUTE_DECISIONS:
            return "processing_24.decision=%s" % decision
        label = _text(node.get("label"))
        sampled = sampled_by_24(record)
        if label == _DISPUTE_LABEL:
            return "processing_24.label=%s" % label
        if label == LABEL_SAMPLE_SEMANTIC and sampled:
            return "processing_24.label=%s" % label
        if decision == "uncertain" and sampled:
            return "processing_24.decision=uncertain"
    if record.get("dispute") is True:
        return "record.dispute"
    return None


def _uncertain_evidence(record: Mapping[str, Any]) -> Optional[str]:
    alignment = record.get("alignment")
    if isinstance(alignment, dict):
        for key in ("status", "type", "decision"):
            value = _lower(alignment.get(key))
            if value in _UNCERTAIN_ALIGNMENT_VALUES:
                return "alignment.%s=%s" % (key, value)
    elif isinstance(alignment, str) and _lower(alignment) in _UNCERTAIN_ALIGNMENT_VALUES:
        return "alignment=%s" % _lower(alignment)

    node16 = _node(record, "16")
    if isinstance(node16, dict):
        for key in ("status", "decision", "alignment_status"):
            value = _lower(node16.get(key))
            if value in _UNCERTAIN_ALIGNMENT_VALUES:
                return "processing_16.%s=%s" % (key, value)

    for namespace in ("12", "13", "14", "15"):
        node = _node(record, namespace)
        if not isinstance(node, dict):
            continue
        label = _text(node.get("label"))
        if label == _MANUAL_SCORE_LABEL:
            return "processing_%s.label=%s" % (namespace, label)
        for key in ("decision", "status", "band"):
            value = _lower(node.get(key))
            if value in _UNCERTAIN_ALIGNMENT_VALUES:
                return "processing_%s.%s=%s" % (namespace, key, value)
        score = node.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            if LOW_CONFIDENCE_MIN <= float(score) < LOW_CONFIDENCE_MAX:
                return "processing_%s.score=%s" % (namespace, score)
    if record.get("review_required") is True:
        return "record.review_required"
    return None


def resolve_category(record: Mapping[str, Any],
                     business: Optional[Mapping[str, Any]] = None
                     ) -> Tuple[Optional[str], str]:
    """按「退回 → 抽检异议 → 存疑」顺序解析样本类别。

    返回 ``(类别 或 None, 证据串)``；类别取不到值表示该记录不属于表1 #25
    的三类样本（应保持不适用/不生成任务）。
    """
    del business  # 类别判定只看输入证据，配置不参与（表1 冻结）
    if not isinstance(record, Mapping):
        return None, ""
    for category, evidence in (
            (CATEGORY_RETURNED, _returned_evidence(record)),
            (CATEGORY_DISPUTE, _dispute_evidence(record)),
            (CATEGORY_UNCERTAIN, _uncertain_evidence(record)),
    ):
        if evidence:
            return category, evidence
    return None, ""


def resolve_review_rounds(record: Mapping[str, Any]) -> Tuple[int, Optional[str]]:
    """已完成的复核轮数：取各处证据的最大值，非数字噪声一律忽略。"""
    candidates: List[Tuple[int, str]] = []

    def push(value: Any, source: str) -> None:
        number = _as_count(value)
        if number is not None:
            candidates.append((number, source))

    node = _node(record, "25")
    if isinstance(node, dict):
        push(node.get("review_round"), "processing_25.review_round")
        push(node.get("round"), "processing_25.round")
        push(node.get("review_count"), "processing_25.review_count")
    operator_ns = record.get("operator_25")
    if isinstance(operator_ns, dict):
        push(operator_ns.get("review_round"), "operator_25.review_round")
        push(operator_ns.get("review_count"), "operator_25.review_count")
    push(record.get("review_round"), "record.review_round")
    push(record.get("review_count"), "record.review_count")
    push(record.get("review_rounds"), "record.review_rounds")
    for key in ("review_history", "review_tasks", "manual_review_history"):
        value = record.get(key)
        if isinstance(value, (list, tuple)):
            candidates.append((len(value), "record.%s" % key))
    if not candidates:
        return 0, None
    best = max(candidates, key=lambda item: (item[0], item[1]))
    return best[0], best[1]


def _as_count(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and float(value).is_integer():
        return max(0, int(value))
    return None


# --------------------------------------------------------------------------- #
# 单条记录判定
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    """单条记录在本算子内的判定结果。"""

    record: Dict[str, Any]
    sample_id: str
    lineno: int
    category: Optional[str]
    evidence: str
    decision: str
    label: Optional[str]
    destination: str
    action: str
    reason_code: str
    reason: str
    review: Dict[str, Any]
    review_round: int
    next_round: int
    dispatched: bool
    in_main_output: bool
    human_verified: bool
    review_required: bool
    locked: bool
    not_applicable: bool
    platform_available: bool = False
    task: Dict[str, Any] = field(default_factory=dict)
    node: Dict[str, Any] = field(default_factory=dict)


def _annotate(outcome: Outcome, business: Mapping[str, Any]) -> Dict[str, Any]:
    """构造主输出命名空间；**刻意不写处理时刻**，保证主产物逐字节可复现。"""
    state = _DECISION_STATE.get(outcome.decision, "unknown")
    return {
        "operator": OPERATOR_NAME,
        "operator_label": OPERATOR_LABEL,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "run_stage": RUN_STAGE,
        # [v4.2] status 双字段（§2.10）：int status + 原字符串迁入 process_status
        "status": status_int(state),
        "process_status": state,
        "category": outcome.category,
        "decision": outcome.decision,
        "label": outcome.label,
        "destination": outcome.destination,
        "action": outcome.action,
        "reason_code": outcome.reason_code,
        "reason": outcome.reason,
        "evidence": outcome.evidence or None,
        "review_round": outcome.review_round,
        "next_round": outcome.next_round if outcome.dispatched else None,
        "dispatched": outcome.dispatched,
        "max_review_count": int(business.get("max_review_count", MAX_REVIEW_COUNT)),
        "trigger": business.get("trigger"),
        "review": (review_mod.review_summary(outcome.review)
                   if outcome.review.get("present") else None),
        "review_required": outcome.review_required,
        "human_verified": outcome.human_verified,
        "locked": outcome.locked,
        "not_applicable": outcome.not_applicable,
        "source_reference": "operator_25",
    }


def evaluate_record(record: Mapping[str, Any], *, sample_id: Optional[str],
                    business: Mapping[str, Any], lineno: int = 0) -> Outcome:
    """判定单条记录，并返回带命名空间注解的**新**记录（不改动输入对象）。"""
    annotated: Dict[str, Any] = copy.deepcopy(dict(record))
    resolved_id = _text(sample_id) or _text(record.get("sample_id")) or ""
    rounds, _round_source = resolve_review_rounds(record)
    review = review_mod.resolve_review(
        record, require_reviewer=bool(business.get("require_reviewer_identity", True)))
    category, evidence = resolve_category(record, business)
    max_review_count = int(business.get("max_review_count", MAX_REVIEW_COUNT))

    outcome = Outcome(
        record=annotated, sample_id=resolved_id, lineno=int(lineno),
        category=category, evidence=evidence,
        decision=DECISION_NOT_ELIGIBLE, label=LABEL_NOT_ELIGIBLE,
        destination=DESTINATION_PIPELINE, action=ACTION_KEEP,
        reason_code="no_eligible_category", reason="不属于表1 #25 三类样本",
        review=review, review_round=rounds, next_round=rounds,
        dispatched=False, in_main_output=True, human_verified=False,
        review_required=False, locked=False, not_applicable=False,
    )

    if upstream_isolated(record):
        outcome.not_applicable = True
        outcome.decision = DECISION_NOT_APPLICABLE
        outcome.label = LABEL_UPSTREAM_ISOLATED
        outcome.destination = DESTINATION_SAMPLE
        outcome.action = ACTION_KEEP
        outcome.reason_code = "upstream_isolated"
        outcome.reason = "上游已判定隔离，本算子不重复定责"
    elif category is None:
        outcome.reason_code = "no_eligible_category"
        outcome.reason = "不属于存疑 / 抽检异议 / 加工退回三类样本，不生成复核任务"
    elif not business.get("enabled"):
        outcome.decision = DECISION_NOT_TRIGGERED
        outcome.label = LABEL_NOT_TRIGGERED
        outcome.destination = DESTINATION_PIPELINE
        outcome.action = ACTION_KEEP
        outcome.reason_code = "trigger_disabled"
        outcome.reason = "表1 #25 为手动触发，本次未开启，不生成派单任务"
    elif (category == CATEGORY_UNCERTAIN and is_auto_aligned(record)
          and sampled_by_24(record) is not True):
        outcome.decision = DECISION_AUTO_ALIGNED_NOT_SAMPLED
        outcome.label = LABEL_AUTO_ALIGNED
        outcome.destination = DESTINATION_PIPELINE
        outcome.action = ACTION_KEEP
        outcome.reason_code = "auto_aligned_not_sampled_by_24"
        outcome.reason = "自动对齐且未被 #24 抽中，按表1 不进人工复核队列"
    elif rounds >= max_review_count:
        outcome.decision = DECISION_MAX_REVIEW
        outcome.label = LABEL_MAX_REVIEW
        outcome.destination = DESTINATION_SAMPLE
        outcome.action = ACTION_LOCK
        outcome.reason_code = "max_review_count_reached"
        outcome.reason = ("同一条已完成 %d 次复核（上限 %d），不再派单并锁定"
                          % (rounds, max_review_count))
        outcome.locked = True
        outcome.in_main_output = False
    else:
        outcome.dispatched = bool(business.get("auto_dispatch", True))
        outcome.platform_available = bool(
            business.get("manual_platform_available", False))
        outcome.next_round = rounds + 1
        if not outcome.dispatched:
            outcome.decision = DECISION_NOT_TRIGGERED
            outcome.label = LABEL_NOT_TRIGGERED
            outcome.destination = DESTINATION_PIPELINE
            outcome.action = ACTION_KEEP
            outcome.reason_code = "auto_dispatch_disabled"
            outcome.reason = "auto_dispatch=false：只登记队列，不生成派单任务"
        else:
            _apply_review(outcome, business)

    node = _annotate(outcome, business)
    annotated.setdefault("processing", {})
    if isinstance(annotated["processing"], dict):
        annotated["processing"]["25"] = dict(node)
    annotated["operator_25"] = {
        "operator": OPERATOR_NAME,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "category": outcome.category,
        "decision": outcome.decision,
        "label": outcome.label,
        "destination": outcome.destination,
        "action": outcome.action,
        "review_round": outcome.review_round,
        "next_round": outcome.next_round if outcome.dispatched else None,
        "human_verified": outcome.human_verified,
        "review_required": outcome.review_required,
        "locked": outcome.locked,
        "source_reference": "operator_25",
    }
    outcome.node = node
    return outcome


def _apply_review(outcome: Outcome, business: Mapping[str, Any]) -> None:
    """把输入中的人工结论回写为流程结论（结论只来自输入证据）。"""
    resolved = outcome.review
    if not resolved.get("valid"):
        outcome.decision = DECISION_PENDING
        outcome.label = LABEL_PENDING
        outcome.destination = DESTINATION_REVIEW
        outcome.action = ACTION_WAITING
        outcome.review_required = True
        reason = resolved.get("reason") or "no_human_conclusion"
        outcome.reason_code = ("review_invalid:" + reason) if resolved.get("present") \
            else reason
        if reason == "no_human_conclusion":
            outcome.reason = "尚未收到人工复核结论，保持待人工队列，不拦其余样本"
        else:
            outcome.reason = "复核结论证据不合法（%s），按待人工处理" % reason
        return

    verdict = resolved.get("verdict")
    if verdict == review_mod.VERDICT_FAIL:
        outcome.decision = DECISION_REJECTED
        outcome.label = LABEL_SAMPLE_SEMANTIC
        outcome.destination = DESTINATION_SAMPLE
        outcome.action = ACTION_ISOLATE
        outcome.human_verified = True
        outcome.review_required = False
        outcome.reason_code = "human_judged_mismatch"
        outcome.reason = "人工复核不通过：进入样本隔离库（#21）"
        outcome.in_main_output = False
        return
    if verdict == review_mod.VERDICT_UNCERTAIN:
        outcome.decision = DECISION_UNCERTAIN
        outcome.label = LABEL_UNCERTAIN
        outcome.destination = DESTINATION_REVIEW
        outcome.action = ACTION_WAITING
        outcome.review_required = True
        outcome.reason_code = "human_judged_uncertain"
        outcome.reason = "人工复核存疑：留在人工队列继续复核"
        return
    outcome.decision = DECISION_PASSED
    outcome.label = LABEL_PASSED
    outcome.destination = DESTINATION_POOL
    outcome.action = ACTION_KEEP
    outcome.human_verified = True
    outcome.review_required = False
    outcome.reason_code = "human_judged_consistent"
    outcome.reason = "人工复核通过：样本回候选池（#19/#20）"


# --------------------------------------------------------------------------- #
# 台账与任务构造
# --------------------------------------------------------------------------- #

def _ledger_base(business: Mapping[str, Any], unique_id: str, run_id: str) -> Dict[str, Any]:
    return {
        "operator": OPERATOR_NAME,
        "operator_label": OPERATOR_LABEL,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage": STAGE_LABEL,
        "stage_id": STAGE_ID,
        "uniqueId": unique_id,
        "runId": run_id,
        "event_time": now_iso(),
        "retry_count": 0,
        "source_reference": "operator_25",
    }


def _preview(raw: str, limit: int = RAW_PREVIEW_LIMIT) -> str:
    text = "".join(ch if ord(ch) >= 32 or ch == "\t" else " " for ch in str(raw))
    return text[:limit]


def _batch_id(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for value in (metadata.get("batch_id"), record.get("batch_id")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def build_task_id(unique_id: str, sample_id: str, round_no: int) -> str:
    """任务号：同一 ``uniqueId`` + 样本 + 轮次下确定性可复现。"""
    return "task-%s-%s-r%d" % (unique_id, sample_id, int(round_no))


def build_task_entry(outcome: Outcome, business: Mapping[str, Any], unique_id: str,
                     run_id: str) -> Dict[str, Any]:
    """人工复核任务 + 派单状态（平台未接入时只能是 ``waiting_manual``）。"""
    entry = _ledger_base(business, unique_id, run_id)
    platform = bool(business.get("manual_platform_available", False))
    dispatch_state = "dispatched" if platform else "waiting_manual"
    entry.update({
        "task_id": build_task_id(unique_id, outcome.sample_id, outcome.next_round),
        "sample_id": outcome.sample_id,
        "batch_id": _batch_id(outcome.record),
        "category": outcome.category,
        "evidence": outcome.evidence,
        "round": outcome.next_round,
        "previous_rounds": outcome.review_round,
        "max_review_count": int(business.get("max_review_count", MAX_REVIEW_COUNT)),
        "priority": "P1",
        "trigger": business.get("trigger"),
        # [v4.2] status 双字段（§2.10）：任务创建成功=1；流程态（dispatched/waiting_manual）迁 process_status
        "status": status_int(dispatch_state),
        "process_status": dispatch_state,
        "review_required": True,
        "dispatch": {
            "state": dispatch_state,
            "platform_available": platform,
            "assignee": None,
            "dispatched_at": now_iso() if platform else None,
            "reason": None if platform else "人工复核平台未接入，任务停在待派单状态",
        },
        "handover": list(HANDOVER["pending"]),
        "label": outcome.label,
        "action": outcome.action,
        "destination": outcome.destination,
        "reason_code": outcome.reason_code,
        "reason": outcome.reason,
        "record": copy.deepcopy(outcome.record),
    })
    return entry


def build_conclusion_entry(outcome: Outcome, business: Mapping[str, Any],
                           unique_id: str, run_id: str) -> Dict[str, Any]:
    """结论回写台账：通过 / 不通过 / 存疑 / 待人工各一条。"""
    entry = _ledger_base(business, unique_id, run_id)
    handover_key = {
        DECISION_PASSED: "passed",
        DECISION_REJECTED: "failed",
        DECISION_UNCERTAIN: "pending",
        DECISION_PENDING: "pending",
    }.get(outcome.decision, "pending")
    entry.update({
        "task_id": build_task_id(unique_id, outcome.sample_id, outcome.next_round),
        "sample_id": outcome.sample_id,
        "batch_id": _batch_id(outcome.record),
        "category": outcome.category,
        "decision": outcome.decision,
        "conclusion": {
            "verdict": outcome.review.get("verdict"),
            "valid": bool(outcome.review.get("valid")),
            "reviewer": outcome.review.get("reviewer"),
            "reviewed_at": outcome.review.get("reviewed_at"),
            "dimensions": dict(outcome.review.get("dimensions") or {}),
            "source": outcome.review.get("source"),
            "reason": outcome.review.get("reason"),
        },
        "round": outcome.next_round,
        "max_review_count": int(business.get("max_review_count", MAX_REVIEW_COUNT)),
        "label": outcome.label,
        "action": outcome.action,
        "destination": outcome.destination,
        "reason_code": outcome.reason_code,
        "reason": outcome.reason,
        "human_verified": outcome.human_verified,
        "review_required": outcome.review_required,
        "handover": list(HANDOVER.get(handover_key, [])),
        "record": copy.deepcopy(outcome.record),
    })
    return entry


def build_ledger(outcome: Outcome, business: Mapping[str, Any], unique_id: str,
                 run_id: str, *, role: str) -> Dict[str, Any]:
    """旁路台账（未派单 / 待人工 / 通过回池 / 样本隔离）。"""
    entry = _ledger_base(business, unique_id, run_id)
    handover_key = {
        "sample_pool_return": "passed",
        "sample_quarantine": "locked" if outcome.locked else "failed",
        "pending_manual": "pending",
        "not_dispatched": None,
    }.get(role)
    entry.update({
        "line": outcome.lineno,
        "sample_id": outcome.sample_id,
        "batch_id": _batch_id(outcome.record),
        "role": role,
        "category": outcome.category,
        "evidence": outcome.evidence,
        "decision": outcome.decision,
        "label": outcome.label,
        "action": outcome.action,
        "destination": outcome.destination,
        "reason_code": outcome.reason_code,
        "reason": outcome.reason,
        "review_round": outcome.review_round,
        "next_round": outcome.next_round if outcome.dispatched else None,
        "dispatched": outcome.dispatched,
        "locked": outcome.locked,
        "human_verified": outcome.human_verified,
        "review_required": outcome.review_required,
        "handover": list(HANDOVER.get(handover_key, [])) if handover_key else [],
        "record": copy.deepcopy(outcome.record),
    })
    return entry


def build_reject_entry(lineno: int, reason: str, raw_line: str,
                       business: Mapping[str, Any], unique_id: str, run_id: str
                       ) -> Dict[str, Any]:
    """坏行台账：只保留截断预览，绝不写入大段原文。"""
    entry = _ledger_base(business, unique_id, run_id)
    entry.update({
        "line": int(lineno),
        "sample_id": None,
        "batch_id": "unknown",
        "role": "reject",
        "reason": reason,
        "reason_code": reason,
        "label": LABEL_ORIGIN_BAD_LINE,
        "action": ACTION_ISOLATE,
        "destination": DESTINATION_ORIGIN,
        "raw_preview": _preview(raw_line),
    })
    return entry


def build_origin_ledger(record: Mapping[str, Any], *, sample_id: Optional[str],
                        reason: str, label: str, lineno: int,
                        business: Mapping[str, Any], unique_id: str, run_id: str
                        ) -> Dict[str, Any]:
    """原始隔离台账：保留原记录以便上游修复。"""
    entry = _ledger_base(business, unique_id, run_id)
    entry.update({
        "line": int(lineno),
        "sample_id": sample_id,
        "batch_id": _batch_id(record),
        "role": "origin_quarantine",
        "reason": reason,
        "reason_code": reason,
        "label": label,
        "action": ACTION_ISOLATE,
        "destination": DESTINATION_ORIGIN,
        "record": copy.deepcopy(dict(record)),
    })
    return entry


# --------------------------------------------------------------------------- #
# 统计与守恒
# --------------------------------------------------------------------------- #

STATISTICS_KEYS: Tuple[str, ...] = (
    "input_lines", "blank_lines", "input", "rejected", "valid", "output",
    "origin_quarantined", "sample_quarantined", "duplicates", "not_dispatched",
    "not_applicable_records", "not_eligible", "not_triggered",
    "auto_aligned_not_sampled", "max_review_reached", "locked", "queued",
    "tasks_generated", "dispatched", "waiting_dispatch", "concluded_passed",
    "concluded_rejected", "concluded_pending", "concluded_uncertain",
    "conclusions_written", "returned_to_pool", "human_verified_records",
    "review_required_records",
)


def empty_statistics() -> Dict[str, Any]:
    stats: Dict[str, Any] = {key: 0 for key in STATISTICS_KEYS}
    stats.update({
        "empty_input": True,
        "rounds": {},
        "conservation_ok": True,
        "conservation_failures": [],
    })
    return stats


def accumulate(statistics: Dict[str, Any], outcome: Outcome) -> None:
    """把单条判定累加进统计；主输出与旁路清单**不重复加总**。"""
    if outcome.in_main_output:
        statistics["output"] = int(statistics.get("output", 0)) + 1
    decision = outcome.decision
    if decision == DECISION_NOT_APPLICABLE:
        statistics["not_applicable_records"] += 1
    elif decision == DECISION_NOT_ELIGIBLE:
        statistics["not_eligible"] += 1
    elif decision == DECISION_NOT_TRIGGERED:
        statistics["not_triggered"] += 1
    elif decision == DECISION_AUTO_ALIGNED_NOT_SAMPLED:
        statistics["auto_aligned_not_sampled"] += 1
    elif decision == DECISION_MAX_REVIEW:
        statistics["max_review_reached"] += 1
        statistics["locked"] += 1
        statistics["sample_quarantined"] += 1
    if not outcome.dispatched:
        statistics["not_dispatched"] += 1
        return

    statistics["tasks_generated"] += 1
    statistics["queued"] += 1
    if outcome.category:
        rounds = statistics.setdefault("rounds", {})
        key = "round_%d" % int(outcome.next_round)
        rounds[key] = int(rounds.get(key, 0)) + 1
    if outcome.platform_available:
        statistics["dispatched"] += 1
    else:
        statistics["waiting_dispatch"] += 1
    if decision == DECISION_PASSED:
        statistics["concluded_passed"] += 1
        statistics["conclusions_written"] += 1
        statistics["returned_to_pool"] += 1
        statistics["human_verified_records"] += 1
    elif decision == DECISION_REJECTED:
        statistics["concluded_rejected"] += 1
        statistics["conclusions_written"] += 1
        statistics["human_verified_records"] += 1
        statistics["sample_quarantined"] += 1
    elif decision == DECISION_UNCERTAIN:
        statistics["concluded_uncertain"] += 1
        statistics["review_required_records"] += 1
    else:  # DECISION_PENDING
        statistics["concluded_pending"] += 1
        statistics["review_required_records"] += 1


def conservation_failures(statistics: Mapping[str, Any]) -> List[str]:
    """统计守恒校验：主输出、旁路台账与队列口径必须两两对得上。"""
    def value(key: str) -> int:
        raw = statistics.get(key, 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return -1

    checks = [
        ("input_lines == input + blank_lines",
         value("input_lines"), value("input") + value("blank_lines")),
        ("input == valid + rejected",
         value("input"), value("valid") + value("rejected")),
        ("valid == output + origin_quarantined + sample_quarantined",
         value("valid"), value("output") + value("origin_quarantined")
         + value("sample_quarantined")),
        ("valid == not_dispatched + tasks_generated + origin_quarantined",
         value("valid"), value("not_dispatched") + value("tasks_generated")
         + value("origin_quarantined")),
        ("not_dispatched == 各类未派单原因之和",
         value("not_dispatched"),
         value("not_applicable_records") + value("not_eligible")
         + value("not_triggered") + value("auto_aligned_not_sampled")
         + value("max_review_reached")),
        ("sample_quarantined == concluded_rejected + max_review_reached",
         value("sample_quarantined"),
         value("concluded_rejected") + value("max_review_reached")),
        ("locked == max_review_reached",
         value("locked"), value("max_review_reached")),
        ("queued == tasks_generated",
         value("queued"), value("tasks_generated")),
        ("tasks_generated == concluded_* 之和",
         value("tasks_generated"), value("concluded_passed")
         + value("concluded_rejected") + value("concluded_pending")
         + value("concluded_uncertain")),
        ("tasks_generated == dispatched + waiting_dispatch",
         value("tasks_generated"), value("dispatched") + value("waiting_dispatch")),
        ("returned_to_pool == concluded_passed",
         value("returned_to_pool"), value("concluded_passed")),
        ("conclusions_written == concluded_passed + concluded_rejected",
         value("conclusions_written"),
         value("concluded_passed") + value("concluded_rejected")),
        ("human_verified_records == concluded_passed + concluded_rejected",
         value("human_verified_records"),
         value("concluded_passed") + value("concluded_rejected")),
        ("review_required_records == concluded_pending + concluded_uncertain",
         value("review_required_records"),
         value("concluded_pending") + value("concluded_uncertain")),
    ]
    return ["%s（实际 %d）" % (name, actual)
            for name, actual, expected in checks if actual != expected]


def digest_sample_ids(sample_ids: Sequence[str]) -> str:
    payload = "\n".join(str(item) for item in sample_ids).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


# --------------------------------------------------------------------------- #
# 管理报告
# --------------------------------------------------------------------------- #

def derive_status(statistics: Mapping[str, Any]) -> str:
    """运行状态：真实反映未触发 / 待人工 / 部分隔离 / 成功。"""
    if int(statistics.get("input", 0)) == 0:
        return "success"
    if int(statistics.get("not_triggered", 0)) and not int(
            statistics.get("tasks_generated", 0)):
        return "disabled"
    if int(statistics.get("review_required_records", 0)):
        return "waiting_manual"
    if (int(statistics.get("sample_quarantined", 0))
            or int(statistics.get("origin_quarantined", 0))
            or int(statistics.get("rejected", 0))
            or int(statistics.get("not_applicable_records", 0))):
        return "partial"
    return "success"


def build_management_report(statistics: Mapping[str, Any],
                            business: Mapping[str, Any], *,
                            unique_id: str, run_id: str, generated_at: str,
                            status: Optional[str] = None,
                            source_name: Optional[str] = None,
                            source_available: bool = False) -> Dict[str, Any]:
    """生成人工复核任务管理报告（全部数字来自真实统计）。"""
    resolved_status = status or derive_status(statistics)
    return {
        "format": "crossmodal-review-task-report-v1",
        "report_id": "review-task-report-%s-%s" % (unique_id, run_id),
        "operator": OPERATOR_NAME,
        "operator_label": OPERATOR_LABEL,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage": STAGE_LABEL,
        "stage_id": STAGE_ID,
        "uniqueId": unique_id,
        "runId": run_id,
        "generated_at": generated_at,
        # [v4.2] 运行级状态保留字符串：属 run() 顶层内部契约（app.py 判 failed、
        # 响应 result.status/reviewStatus），不是逐条输出记录的 status 双字段。
        "status": resolved_status,
        "source_reference": {
            "name": source_name,
            "available": bool(source_available),
            "upstream_queue": "operator_16/operator_24/operator_21",
        },
        "review": {
            "trigger": business.get("trigger"),
            "enabled": bool(business.get("enabled")),
            "auto_dispatch": bool(business.get("auto_dispatch", True)),
            "platform_available": bool(business.get("manual_platform_available", False)),
            "max_review_count": int(business.get("max_review_count", MAX_REVIEW_COUNT)),
            "eligible": list(business.get("eligible") or ELIGIBLE_CATEGORIES),
            "repeat_spot_check": bool(business.get("repeat_spot_check", False)),
            "require_reviewer_identity": bool(
                business.get("require_reviewer_identity", True)),
            "task_states": {
                "dispatched": int(statistics.get("dispatched", 0)),
                "waiting_manual": int(statistics.get("waiting_dispatch", 0)),
                "not_dispatched": int(statistics.get("not_dispatched", 0)),
            },
            "rounds": dict(statistics.get("rounds") or {}),
            "queue": {
                "tasks_generated": int(statistics.get("tasks_generated", 0)),
                "concluded_passed": int(statistics.get("concluded_passed", 0)),
                "concluded_rejected": int(statistics.get("concluded_rejected", 0)),
                "concluded_pending": int(statistics.get("concluded_pending", 0)),
                "concluded_uncertain": int(statistics.get("concluded_uncertain", 0)),
                "returned_to_pool": int(statistics.get("returned_to_pool", 0)),
                "sample_quarantined": int(statistics.get("sample_quarantined", 0)),
                "max_review_reached": int(statistics.get("max_review_reached", 0)),
            },
            "statistics": copy.deepcopy(dict(statistics)),
        },
        "handover": {
            "passed_to": list(HANDOVER["passed"]),
            "failed_to": list(HANDOVER["failed"]),
            "pending_to": list(HANDOVER["pending"]),
            "locked_to": list(HANDOVER["locked"]),
            "reject_to": list(HANDOVER["failed"]),
        },
        "artifacts": {
            "main": MAIN_NAME,
            "tasks": TASKS_NAME,
            "conclusions": CONCLUSIONS_NAME,
            "sample_pool_return": RETURN_NAME,
            "sample_quarantine": SAMPLE_QUARANTINE_NAME,
            "pending_manual": PENDING_NAME,
            "not_dispatched": NOT_DISPATCHED_NAME,
            "origin_quarantine": ORIGIN_QUARANTINE_NAME,
            "rejects": REJECTS_NAME,
        },
    }


def flat_summary(node: Mapping[str, Any]) -> Dict[str, Any]:
    """阶段末 lineage 里的紧凑摘要（供工程侧快速判断）。"""
    statistics = node.get("statistics") or {}
    return {
        "operator": OPERATOR_NAME,
        "runId": node.get("runId"),
        "status": node.get("status"),
        "tasksGenerated": statistics.get("tasks_generated"),
        "waitingManual": statistics.get("waiting_dispatch"),
        "output": statistics.get("output"),
    }
