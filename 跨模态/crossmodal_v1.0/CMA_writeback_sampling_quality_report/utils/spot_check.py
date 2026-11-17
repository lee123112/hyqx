"""算子 24 核心业务：分层抽检、人工复核判定、样本隔离与质量报告回写。

严格对应表1 #24「人工抽检复核」：

| 表1 要求 | 本模块实现 |
| --- | --- |
| 输入：候选池（含自动对齐样本）、场景/模态/对齐类型分层信息 | ``strata.py`` 解析三个维度并分层 |
| 抽多少：默认 10%，可选 10%–20%，按层分开抽 | ``strata.select_samples`` 比例+最大余数，总量夹在区间内 |
| 自动对齐也进入抽样框 | 抽样框不因 ``alignment_type`` 排除任何层（``include_auto_aligned=true``） |
| 判对：结论写回报告 | ``DECISION_PASSED`` → 报告回写计数 |
| 判错：标签【样本-语义不通】、隔离、取消自动对齐 | ``DECISION_REJECTED`` → 样本隔离台账 + 改写 ``alignment`` |
| 存疑：异议交 #21/#25 | ``DECISION_UNCERTAIN`` → 异议台账（保留在主输出，不拦其余样本） |
| 回写未完成：不拦其余样本，版本保持待发布 | 待人工样本留在主输出，报告 ``status=waiting_writeback``、``version_state=pending_release`` |

**不得伪造**：人工结论只来自输入证据；未接入人工平台时全部判「待人工」，
绝不生成固定分数、随机分数或假定「模型可用」的结论。
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
from . import strata as strata_mod

_TZ = timezone(timedelta(hours=8))

# -- 身份 ------------------------------------------------------------------ #
OPERATOR_NAME = schemas.OPERATOR_NAME
OPERATOR_VERSION = schemas.OPERATOR_VERSION
STAGE_ID = schemas.STAGE_ID
FORMAL_OPERATOR_NUMBER = schemas.FORMAL_OPERATOR_NUMBER
DEFAULT_PORT = schemas.DEFAULT_PORT
OPERATOR_LABEL = "抽检并回写质量报告"
RUN_STAGE = "manual_spot_check_review"
STAGE_LABEL = "人工抽检复核"

# -- 产物 ------------------------------------------------------------------ #
MAIN_NAME = "24_spot_check.jsonl"
SAMPLED_NAME = "24_sampled.jsonl"
DISPUTES_NAME = "24_disputes.jsonl"
SAMPLE_QUARANTINE_NAME = "24_sample_quarantine.jsonl"
PENDING_NAME = "24_pending_manual.jsonl"
ORIGIN_QUARANTINE_NAME = "24_original_quarantine.jsonl"
REJECTS_NAME = "24_rejects.jsonl"
REPORT_NAME = "24_quality_report.json"
#: 表1 #24 输出「抽检结论」：与质量报告同级的独立材料，#23 封版门禁需要两份齐备
CONCLUSION_NAME = "24_sampling_conclusion.json"
LOG_NAME = "run.log"
MANIFEST_NAME = "manifest.json"

OUTPUT_NAMES: Tuple[str, ...] = (
    MAIN_NAME, SAMPLED_NAME, DISPUTES_NAME, SAMPLE_QUARANTINE_NAME, PENDING_NAME,
    ORIGIN_QUARANTINE_NAME, REJECTS_NAME, REPORT_NAME, CONCLUSION_NAME,
)

# -- 判定 ------------------------------------------------------------------ #
DECISION_PASSED = "passed"
DECISION_REJECTED = "rejected"
DECISION_UNCERTAIN = "uncertain"
DECISION_PENDING = "pending_manual"
DECISION_NOT_SAMPLED = "not_sampled"
DECISION_SKIPPED = "skipped"

# -- 统一台账字段（背景规则 §6 / #19 §5） --------------------------------- #
LABEL_PASSED = "流程-抽检通过"
LABEL_UNCERTAIN = "流程-抽检存疑"
LABEL_PENDING = "流程-抽检待人工"
LABEL_SAMPLE_SEMANTIC = "样本-语义不通"
LABEL_UPSTREAM_ISOLATED = "流程-上游已隔离"
LABEL_ORIGIN_MISSING_FIELD = "原始文件-缺字段"
LABEL_ORIGIN_DUPLICATE = "原始文件-重复标识"

DESTINATION_DELIVERY = "交付候选"
DESTINATION_SAMPLE = "样本隔离库"
DESTINATION_REVIEW = "人工复核队列"
DESTINATION_ORIGIN = "原始隔离区"

ACTION_KEEP = "keep"
ACTION_ISOLATE = "isolate"
ACTION_WAITING = "waiting_manual"

# -- 对齐类型 / 报告状态 ---------------------------------------------------- #
ALIGNMENT_AUTO = strata_mod.ALIGNMENT_AUTO
ALIGNMENT_NOT = strata_mod.ALIGNMENT_NOT
ALIGNMENT_CANCELLED = strata_mod.ALIGNMENT_CANCELLED

REPORT_STATUS_WAITING = schemas.REPORT_STATUS_WAITING
REPORT_STATUS_WRITTEN = schemas.REPORT_STATUS_WRITTEN
VERSION_STATE_PENDING = schemas.VERSION_STATE_PENDING
VERSION_STATE_DELIVERABLE = schemas.VERSION_STATE_DELIVERABLE

#: 旁路台账角色 → 交接算子（表1：异议交 #21/#25；判错入样本隔离库）
HANDOVER = {
    "sample_quarantine": ["operator_21"],
    "dispute": ["operator_21", "operator_25"],
    "pending_manual": ["operator_25"],
}

REQUIRED_LEDGER_FIELDS = ("label", "stage", "action", "destination")
_RAW_PREVIEW_LIMIT = 200


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def default_business(**overrides: Any) -> Dict[str, Any]:
    """与部署配置同源的业务参数字典。"""
    return schemas.default_business(**overrides)


# --------------------------------------------------------------------------- #
# 抽样框判定
# --------------------------------------------------------------------------- #

def upstream_isolated(record: Mapping[str, Any]) -> bool:
    """上游（#20/#21）已判定隔离的记录不再进入本次抽样框。"""
    if record.get("isolated") is True:
        return True
    processing = record.get("processing")
    if isinstance(processing, dict):
        for namespace in ("21", 21, "20", 20):
            node = processing.get(namespace)
            if not isinstance(node, dict):
                continue
            if node.get("isolated") is True:
                return True
            if str(node.get("decision") or "").strip().lower() in ("isolated", "isolate"):
                return True
            if str(node.get("action") or "").strip().lower() == ACTION_ISOLATE:
                return True
            if (node.get("label") == LABEL_SAMPLE_SEMANTIC
                    and node.get("destination") == DESTINATION_SAMPLE):
                return True
    return False


# --------------------------------------------------------------------------- #
# 单条记录判定
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    """单条记录在本算子内的判定结果。"""

    record: Dict[str, Any]
    sample_id: str
    lineno: int
    decision: str
    label: Optional[str]
    destination: str
    action: str
    reason_code: str
    reason: str
    stratum: Dict[str, str]
    stratum_label: str
    sampled: bool
    review: Dict[str, Any]
    in_main_output: bool
    cancelled_auto_alignment: bool
    human_verified: bool
    review_required: bool
    not_applicable: bool = False
    node: Dict[str, Any] = field(default_factory=dict)


def _stratum_dict(key: Sequence[str]) -> Dict[str, str]:
    values = list(key) + [strata_mod.UNKNOWN] * (3 - len(key))
    return dict(zip(strata_mod.STRATUM_DIMENSIONS, values))


def _annotate(outcome: Outcome, business: Mapping[str, Any]) -> Dict[str, Any]:
    node = {
        "operator": OPERATOR_NAME,
        "operator_label": OPERATOR_LABEL,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "run_stage": RUN_STAGE,
        # [v4.2] status 双字段（规则 §2.10）：int status（1=成功/留产线，0=过滤/隔离）
        # + 字符串 process_status（原流程态）。
        "status": {
            DECISION_PASSED: 1,
            DECISION_REJECTED: 0,
            DECISION_UNCERTAIN: 1,
            DECISION_PENDING: 1,
            DECISION_NOT_SAMPLED: 1,
            DECISION_SKIPPED: 1,
        }.get(outcome.decision, 1),
        "process_status": {
            DECISION_PASSED: "success",
            DECISION_REJECTED: "isolated",
            DECISION_UNCERTAIN: "waiting_manual",
            DECISION_PENDING: "waiting_manual",
            DECISION_NOT_SAMPLED: "not_sampled",
            DECISION_SKIPPED: "skipped",
        }.get(outcome.decision, "unknown"),
        "action": outcome.action,
        "decision": outcome.decision,
        "label": outcome.label,
        "destination": outcome.destination,
        "reason_code": outcome.reason_code,
        "reason": outcome.reason,
        "sampled": outcome.sampled,
        "stratum": dict(outcome.stratum),
        "sample_rate": business.get("sample_rate"),
        "sampling_salt": business.get("sampling_salt"),
        "review": (review_mod.review_summary(outcome.review)
                   if outcome.review.get("present") else None),
        "review_required": outcome.review_required,
        "human_verified": outcome.human_verified,
        "human_alignment_judgement": None,
        "auto_alignment_cancelled": outcome.cancelled_auto_alignment,
        "writeback": {"report": REPORT_NAME, "written": False},
        "checked_at": now_iso(),
        "retry_count": 0,
        "source_reference": "operator_24",
    }
    return node


def evaluate_record(record: Mapping[str, Any], *, sample_id: Optional[str],
                    sampled: bool, stratum: Sequence[str],
                    business: Mapping[str, Any], lineno: int = 0) -> Outcome:
    """判定单条记录，并返回带命名空间注解的**新**记录（不改动输入对象）。"""
    annotated: Dict[str, Any] = copy.deepcopy(dict(record))
    key = tuple(stratum or ())
    stratum_dict = _stratum_dict(key)
    label_text = strata_mod.stratum_label(key)

    outcome = Outcome(
        record=annotated, sample_id=str(sample_id or ""), lineno=int(lineno),
        decision=DECISION_NOT_SAMPLED, label=None, destination=DESTINATION_DELIVERY,
        action=ACTION_KEEP, reason_code="not_sampled", reason="未命中本次抽检",
        stratum=stratum_dict, stratum_label=label_text, sampled=bool(sampled),
        review=review_mod.resolve_review(record, require_reviewer=bool(
            business.get("require_reviewer_identity", True))),
        in_main_output=True, cancelled_auto_alignment=False, human_verified=False,
        review_required=False,
    )

    if upstream_isolated(record):
        outcome.not_applicable = True
        outcome.decision = DECISION_SKIPPED
        outcome.label = LABEL_UPSTREAM_ISOLATED
        outcome.destination = DESTINATION_SAMPLE
        outcome.action = ACTION_KEEP
        outcome.reason_code = "upstream_isolated"
        outcome.reason = "上游已判定隔离，不进入本次抽样框"
    elif not sampled:
        outcome.reason_code = "not_sampled"
        outcome.reason = "未命中本次抽检"
    else:
        _decide_sampled(outcome, record, business)

    node = _annotate(outcome, business)
    node["not_applicable"] = outcome.not_applicable
    if outcome.decision in (DECISION_PASSED, DECISION_REJECTED):
        node["writeback"]["written"] = True
    if outcome.decision == DECISION_REJECTED:
        # 人工判定为「不对齐」：自动对齐已取消时同步改写上游 alignment 字段
        node["human_alignment_judgement"] = ALIGNMENT_CANCELLED
    annotated.setdefault("processing", {})
    if isinstance(annotated["processing"], dict):
        annotated["processing"]["24"] = node
    annotated["operator_24"] = {
        "operator": OPERATOR_NAME,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "decision": outcome.decision,
        "label": outcome.label,
        "destination": outcome.destination,
        "action": outcome.action,
        "sampled": outcome.sampled,
        "human_verified": outcome.human_verified,
        "review_required": outcome.review_required,
        "cancelled_auto_alignment": outcome.cancelled_auto_alignment,
        "checked_at": node["checked_at"],
        "source_reference": "operator_24",
    }
    outcome.node = node
    return outcome


def _decide_sampled(outcome: Outcome, record: Mapping[str, Any],
                    business: Mapping[str, Any]) -> None:
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
            outcome.reason = "尚未收到资深标注员复核结论，保持待人工、不拦其余样本"
        else:
            outcome.reason = "复核结论证据不合法（%s），按待人工处理" % reason
        return

    verdict = resolved.get("verdict")
    outcome.human_verified = verdict in (review_mod.VERDICT_PASS, review_mod.VERDICT_FAIL)
    if verdict == review_mod.VERDICT_FAIL:
        outcome.decision = DECISION_REJECTED
        outcome.label = LABEL_SAMPLE_SEMANTIC
        outcome.destination = DESTINATION_SAMPLE
        outcome.action = ACTION_ISOLATE
        # 判错样本已有最终结论（隔离 + 取消自动对齐），不再挂"待人工"，
        # 后续动作由 destination/handover 指向 #21，避免与待人工统计重复计数
        outcome.review_required = False
        outcome.reason_code = "human_judged_mismatch"
        outcome.reason = "资深标注员判错：取消自动对齐并进入样本隔离库"
        outcome.in_main_output = False
        _cancel_auto_alignment(outcome, record)
        return
    if verdict == review_mod.VERDICT_UNCERTAIN:
        outcome.decision = DECISION_UNCERTAIN
        outcome.label = LABEL_UNCERTAIN
        outcome.destination = DESTINATION_REVIEW
        outcome.action = ACTION_WAITING
        outcome.review_required = True
        outcome.reason_code = "human_judged_uncertain"
        outcome.reason = "复核存疑：进入异议清单，交 #21/#25"
        return
    outcome.decision = DECISION_PASSED
    outcome.label = LABEL_PASSED
    outcome.destination = DESTINATION_DELIVERY
    outcome.action = ACTION_KEEP
    outcome.review_required = False
    outcome.reason_code = "human_judged_consistent"
    outcome.reason = "复核通过：结论写回质量报告"


def _cancel_auto_alignment(outcome: Outcome, record: Mapping[str, Any]) -> None:
    """判错时取消自动对齐：只在确为自动对齐时改写上游字段，否则仅登记人工判定。"""
    alignment_type, found, auto = strata_mod.resolve_alignment(record)
    if not (found and auto):
        return
    alignment = outcome.record.get("alignment")
    if not isinstance(alignment, dict):
        alignment = {"status": alignment}
        outcome.record["alignment"] = alignment
    alignment["previous_status"] = alignment.get("status") or alignment_type
    alignment["status"] = ALIGNMENT_CANCELLED
    alignment["auto_aligned"] = False
    alignment["cancelled"] = True
    alignment["cancelled_by"] = "operator_24"
    alignment["cancelled_at"] = now_iso()
    alignment["cancel_reason"] = "human_span_check_mismatch"
    outcome.cancelled_auto_alignment = True


# --------------------------------------------------------------------------- #
# 台账构造
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
        "source_reference": "operator_24",
        "sample_rate": business.get("sample_rate"),
    }


def _preview(raw: str, limit: int = _RAW_PREVIEW_LIMIT) -> str:
    text = "".join(ch if ord(ch) >= 32 or ch == "\t" else " " for ch in str(raw))
    return text[:limit]


def _batch_id(record: Mapping[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    for value in (metadata.get("batch_id"), record.get("batch_id")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def build_reject_entry(lineno: int, reason: str, raw_line: str,
                       business: Mapping[str, Any], unique_id: str, run_id: str
                       ) -> Dict[str, Any]:
    """坏行台账：只保留截断预览，绝不写入大段原文。"""
    entry = _ledger_base(business, unique_id, run_id)
    entry.update({
        "line": int(lineno),
        "sample_id": None,
        "batch_id": "unknown",
        "reason": reason,
        "reason_code": reason,
        "label": LABEL_ORIGIN_MISSING_FIELD if reason == "missing_sample_id"
        else "原始文件-坏行",
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
        "reason": reason,
        "reason_code": reason,
        "label": label,
        "action": ACTION_ISOLATE,
        "destination": DESTINATION_ORIGIN,
        "record": copy.deepcopy(dict(record)),
    })
    return entry


def build_ledger(outcome: Outcome, business: Mapping[str, Any], unique_id: str,
                 run_id: str, *, role: str) -> Dict[str, Any]:
    """旁路台账（抽检样本 / 判错隔离 / 异议 / 待人工）。"""
    entry = _ledger_base(business, unique_id, run_id)
    entry.update({
        "line": outcome.lineno,
        "sample_id": outcome.sample_id,
        "batch_id": _batch_id(outcome.record),
        "role": role,
        "decision": outcome.decision,
        "label": outcome.label,
        "action": outcome.action,
        "destination": outcome.destination,
        "reason_code": outcome.reason_code,
        "reason": outcome.reason,
        "stratum": dict(outcome.stratum),
        "stratum_label": outcome.stratum_label,
        "sampled": outcome.sampled,
        "human_verified": outcome.human_verified,
        "review_required": outcome.review_required,
        "review": review_mod.review_summary(outcome.review),
        "simulated_human": outcome.review.get("simulated_human") is True,
        "cancelled_auto_alignment": outcome.cancelled_auto_alignment,
        "handover": list(HANDOVER.get(role, [])),
        "record": copy.deepcopy(outcome.record),
    })
    return entry


def build_sampled_entry(outcome: Outcome, business: Mapping[str, Any], unique_id: str,
                        run_id: str) -> Dict[str, Any]:
    """抽检样本明细（含人工结论），即「分层抽检样本 / 抽检结论」产物。"""
    entry = build_ledger(outcome, business, unique_id, run_id, role="spot_check_sample")
    entry["record"] = copy.deepcopy(outcome.record)
    return entry


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

STATISTICS_KEYS: Tuple[str, ...] = (
    "input_lines", "blank_lines", "input", "rejected", "valid", "output",
    "origin_quarantined", "sample_quarantined", "duplicates", "sampled",
    "sampled_in_output", "not_sampled", "sampled_passed", "sampled_rejected",
    "sampled_uncertain", "sampled_pending", "disputes", "pending_manual",
    "auto_alignment_cancelled", "human_verified_records", "review_required_records",
    "not_applicable_records",
)


def empty_statistics() -> Dict[str, Any]:
    stats: Dict[str, Any] = {key: 0 for key in STATISTICS_KEYS}
    stats.update({
        "empty_input": True,
        "achieved_sample_rate": 0.0,
        "strata": {},
        "conservation_ok": True,
        "conservation_failures": [],
    })
    return stats


def conservation_failures(statistics: Mapping[str, Any]) -> List[str]:
    """统计守恒校验：主输出、旁路台账与抽样口径必须两两对得上。"""
    def value(key: str) -> int:
        raw = statistics.get(key, 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return -1

    checks = [
        ("input == valid + rejected",
         value("input"), value("valid") + value("rejected")),
        ("input_lines == input + blank_lines",
         value("input_lines"), value("input") + value("blank_lines")),
        ("valid == output + origin_quarantined + sample_quarantined",
         value("valid"), value("output") + value("origin_quarantined")
         + value("sample_quarantined")),
        ("output == sampled_in_output + not_sampled + not_applicable_records",
         value("output"), value("sampled_in_output") + value("not_sampled")
         + value("not_applicable_records")),
        ("sampled == sampled_in_output + sampled_rejected",
         value("sampled"), value("sampled_in_output") + value("sampled_rejected")),
        ("sampled == passed + rejected + uncertain + pending",
         value("sampled"), value("sampled_passed") + value("sampled_rejected")
         + value("sampled_uncertain") + value("sampled_pending")),
        ("sample_quarantined == sampled_rejected",
         value("sample_quarantined"), value("sampled_rejected")),
        ("disputes == sampled_rejected + sampled_uncertain",
         value("disputes"), value("sampled_rejected") + value("sampled_uncertain")),
        ("pending_manual == sampled_pending",
         value("pending_manual"), value("sampled_pending")),
        ("human_verified_records == sampled_passed + sampled_rejected",
         value("human_verified_records"),
         value("sampled_passed") + value("sampled_rejected")),
        ("review_required_records == sampled_uncertain + sampled_pending",
         value("review_required_records"),
         value("sampled_uncertain") + value("sampled_pending")),
    ]
    failures = ["%s（实际 %d）" % (name, actual)
                for name, actual, expected in checks if actual != expected]
    return failures


def conclusion_counts(statistics: Mapping[str, Any]) -> Dict[str, int]:
    return {
        "passed": int(statistics.get("sampled_passed", 0)),
        "rejected": int(statistics.get("sampled_rejected", 0)),
        "uncertain": int(statistics.get("sampled_uncertain", 0)),
        "pending_manual": int(statistics.get("sampled_pending", 0)),
        "not_sampled": int(statistics.get("not_sampled", 0)),
        "sampled_total": int(statistics.get("sampled", 0)),
    }


def digest_sample_ids(sample_ids: Sequence[str]) -> str:
    payload = "\n".join(str(item) for item in sample_ids).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


# --------------------------------------------------------------------------- #
# 质量报告回写
# --------------------------------------------------------------------------- #

def find_source_report(search_dir, business: Mapping[str, Any]
                       ) -> Tuple[Optional[Path], Optional[str]]:
    """在输入目录内按配置顺序查找「待回写质量报告」。"""
    directory = Path(search_dir)
    if not directory.is_dir():
        return None, None
    for name in business.get("report_source_names") or ():
        candidate = Path(name)
        if candidate.name != name or "/" in name or "\\" in name:
            continue
        target = directory / name
        if target.is_file():
            return target, name
    return None, None


def load_source_report(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def build_sampling_conclusion(statistics: Mapping[str, Any],
                              business: Mapping[str, Any], *,
                              unique_id: str, run_id: str,
                              sampling: Mapping[str, Any],
                              conclusions: Mapping[str, Any],
                              cancelled_ids: Sequence[str],
                              strata_detail: Mapping[str, Any],
                              writeback_complete: bool, generated_at: str,
                              source_name: Optional[str],
                              source_available: bool,
                              status: Optional[str] = None) -> Dict[str, Any]:
    """表1 #24「输出：抽检结论和报告状态」——独立材料。

    #23 的封版门禁要求「质量报告已回写」与「抽检结论已回写」两份材料齐备，
    因此本文件与 ``24_quality_report.json`` 同构但独立：未回写时
    ``status=waiting_writeback``、``version_state=pending_release``（待发布）。
    """
    report_status = REPORT_STATUS_WRITTEN if writeback_complete else REPORT_STATUS_WAITING
    version_state = VERSION_STATE_DELIVERABLE if writeback_complete else VERSION_STATE_PENDING
    cancelled = list(cancelled_ids)
    sampling_block = {
        "sample_rate": sampling.get("sample_rate"),
        "salt": sampling.get("salt"),
        "strata": list(sampling.get("strata") or []),
        "target_total": sampling.get("target_total"),
        "achieved_rate": sampling.get("achieved_rate"),
    }
    return {
        "format": "crossmodal-sampling-conclusion-v1",
        "simulated_human": statistics.get("simulated_human") is True,
        "report_id": "sampling-conclusion-%s-%s" % (unique_id, run_id),
        "operator": OPERATOR_NAME,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "uniqueId": unique_id,
        "runId": run_id,
        "generated_at": generated_at,
        "status": report_status,
        "written_back": bool(writeback_complete),
        "conclusion_written_back": bool(writeback_complete),
        "sampling_written_back": bool(writeback_complete),
        "version_state": version_state,
        "sampling_rate": sampling.get("sample_rate"),
        "target_total": sampling.get("target_total"),
        "achieved_rate": sampling.get("achieved_rate"),
        "salt": sampling.get("salt"),
        "strata_dimensions": list(sampling.get("strata") or []),
        "strata": copy.deepcopy(dict(strata_detail or {})),
        "conclusion": dict(conclusions),
        "summary": {
            "sampled": int(statistics.get("sampled", 0)),
            "passed": int(statistics.get("sampled_passed", 0)),
            "rejected": int(statistics.get("sampled_rejected", 0)),
            "uncertain": int(statistics.get("sampled_uncertain", 0)),
            "pending_manual": int(statistics.get("sampled_pending", 0)),
            "sample_quarantined": int(statistics.get("sample_quarantined", 0)),
            "cancelled_auto_alignment": len(cancelled),
        },
        #: 表1 #24 步骤 3)：单条判错 → 取消自动对齐 + 交隔离留痕算子
        "cancelled_auto_alignment": cancelled,
        "handover": {"sample_quarantine": list(HANDOVER["sample_quarantine"])},
        "source_report": {"name": source_name, "available": bool(source_available),
                          "operator_number": FORMAL_OPERATOR_NUMBER - 4},
        "writeback": {
            "required": True,
            "completed": bool(writeback_complete),
            "completed_at": generated_at if writeback_complete else None,
            "version_state": version_state,
            "operator": OPERATOR_NAME,
            "operator_number": FORMAL_OPERATOR_NUMBER,
            "operator_version": OPERATOR_VERSION,
            "uniqueId": unique_id,
            "runId": run_id,
            "sampling": sampling_block,
        },
        "operator_status": status,
    }


def build_writeback_report(source: Optional[Mapping[str, Any]],
                           statistics: Mapping[str, Any],
                           business: Mapping[str, Any], *,
                           unique_id: str, run_id: str,
                           sampling: Mapping[str, Any],
                           conclusions: Mapping[str, Any],
                           cancelled_ids: Sequence[str],
                           disputes: int, pending_manual: int,
                           writeback_complete: bool, generated_at: str,
                           source_name: Optional[str],
                           source_available: bool,
                           status: Optional[str] = None) -> Dict[str, Any]:
    """生成**已回写**的质量报告：保留上游全部字段，只增不改。"""
    if isinstance(source, Mapping):
        report = copy.deepcopy(dict(source))
    else:
        report = {
            "format": "crossmodal-quality-report-v1",
            "report_id": "quality-report-%s-%s" % (unique_id, run_id),
            "operator": None,
            "operator_number": None,
            "operator_version": None,
            "stage_id": None,
            "uniqueId": unique_id,
            "runId": None,
            "generated_at": generated_at,
            "status": REPORT_STATUS_WAITING,
            "checks": {},
            "metrics": {},
            "statistics": {},
        }
    report.setdefault("checks", {})
    report.setdefault("metrics", {})

    report["simulated_human"] = statistics.get("simulated_human") is True
    sampled_total = int(statistics.get("sampled", 0))
    report["original_status"] = (source or {}).get("status") if isinstance(source, Mapping) \
        else None
    report["status"] = REPORT_STATUS_WRITTEN if writeback_complete else REPORT_STATUS_WAITING
    #: #23 封版门禁读「已回写」的三种形式之一（顶层 written_back / writeback.completed /
    #: status=written_back）。本算子原只写 writeback.writeback_completed，与抽检结论
    #: 回执同构但门禁读不到，导致质量报告永远判未回写——这里补齐前两种写法。
    report["written_back"] = bool(writeback_complete)
    report["source_report"] = {
        "name": source_name,
        "available": bool(source_available),
        "status": report.get("original_status"),
        "operator": (source or {}).get("operator") if isinstance(source, Mapping) else None,
        "operator_version": (source or {}).get("operator_version")
        if isinstance(source, Mapping) else None,
        "runId": (source or {}).get("runId") if isinstance(source, Mapping) else None,
    }
    if not writeback_complete:
        gate_reason = "sampled_without_final_conclusion"
    elif sampled_total == 0:
        gate_reason = "no_sample"
    else:
        gate_reason = None
    report["writeback"] = {
        "required": True,
        "writeback_by": "operator_24",
        "writeback_completed": bool(writeback_complete),
        "completed": bool(writeback_complete),
        "version_state": VERSION_STATE_DELIVERABLE if writeback_complete
        else VERSION_STATE_PENDING,
        "writeback_at": generated_at,
        "writeback_operator": OPERATOR_NAME,
        "writeback_operator_label": OPERATOR_LABEL,
        "writeback_operator_number": FORMAL_OPERATOR_NUMBER,
        "writeback_operator_version": OPERATOR_VERSION,
        "uniqueId": unique_id,
        "runId": run_id,
        "source_report_name": source_name,
        "source_report_available": bool(source_available),
        "sampling": {
            "sample_rate": sampling.get("sample_rate"),
            "salt": sampling.get("salt"),
            "strata": sampling.get("strata"),
            "target_total": sampling.get("target_total"),
            "achieved_rate": sampling.get("achieved_rate"),
        },
        "conclusions": dict(conclusions),
        "cancelled_auto_alignment": list(cancelled_ids),
        "disputes": int(disputes),
        "pending_manual": int(pending_manual),
        "gate": {
            "all_sampled_concluded": bool(writeback_complete),
            "sampled_total": sampled_total,
            "no_sample": sampled_total == 0,
            "reason": gate_reason,
        },
    }
    report["spot_check"] = {
        "runId": run_id,
        "generated_at": generated_at,
        "sampling": {
            "sample_rate": sampling.get("sample_rate"),
            "salt": sampling.get("salt"),
            "strata": sampling.get("strata"),
            "target_total": sampling.get("target_total"),
            "achieved_rate": sampling.get("achieved_rate"),
        },
        "strata": copy.deepcopy(dict(sampling.get("strata_detail") or {})),
        "conclusions": dict(conclusions),
        "summary": {
            "sampled": sampled_total,
            "passed": int(statistics.get("sampled_passed", 0)),
            "rejected": int(statistics.get("sampled_rejected", 0)),
            "uncertain": int(statistics.get("sampled_uncertain", 0)),
            "pending_manual": int(statistics.get("sampled_pending", 0)),
            "sample_quarantined": int(statistics.get("sample_quarantined", 0)),
            "disputes": int(disputes),
            "auto_alignment_cancelled": int(
                statistics.get("auto_alignment_cancelled", 0)),
        },
    }
    report["operator_24"] = {
        "operator": OPERATOR_NAME,
        "operator_label": OPERATOR_LABEL,
        "operator_number": FORMAL_OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": STAGE_ID,
        "run_stage": RUN_STAGE,
        "uniqueId": unique_id,
        "runId": run_id,
        "generated_at": generated_at,
        "artifacts": {
            "main": MAIN_NAME,
            "sampled": SAMPLED_NAME,
            "disputes": DISPUTES_NAME,
            "sample_quarantine": SAMPLE_QUARANTINE_NAME,
            "pending_manual": PENDING_NAME,
            "origin_quarantine": ORIGIN_QUARANTINE_NAME,
            "rejects": REJECTS_NAME,
        },
        "statistics": copy.deepcopy(dict(statistics)),
    }
    if status is not None:
        report["run_status"] = status
    return report


def flat_summary(node: Mapping[str, Any]) -> Dict[str, Any]:
    """阶段末 lineage 里的紧凑摘要（供工程侧快速判断）。"""
    statistics = node.get("statistics") or {}
    return {
        "operator": OPERATOR_NAME,
        "runId": node.get("runId"),
        "status": node.get("status"),
        "reportStatus": statistics.get("writeback", {}).get("status"),
        "sampled": statistics.get("sampled"),
        "output": statistics.get("output"),
    }
