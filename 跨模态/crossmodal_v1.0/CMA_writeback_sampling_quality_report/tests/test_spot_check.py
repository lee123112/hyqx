"""业务判定、台账、回写报告与统计守恒的单元测试（表1 #24 冻结口径）。"""
from __future__ import annotations

import json

import pytest

from tests.conftest import make_record, write_quality_report
from utils import spot_check as sc


def business(**overrides):
    return sc.default_business(**overrides)


def _evaluate(record, *, sampled, stratum=("scene-A", "image+text", "auto_aligned"),
              biz=None, lineno=1):
    return sc.evaluate_record(record, sample_id=record.get("sample_id"),
                              sampled=sampled, stratum=stratum,
                              business=biz or business(), lineno=lineno)


# --------------------------------------------------------------------------- #
# 判定分支
# --------------------------------------------------------------------------- #

def test_not_sampled_record_keeps_flow_state_without_quality_label():
    record = make_record("ns-1")
    outcome = _evaluate(record, sampled=False)
    assert outcome.decision == sc.DECISION_NOT_SAMPLED
    assert outcome.sampled is False
    assert outcome.in_main_output is True
    assert outcome.label is None
    assert outcome.action == "keep"
    assert outcome.destination == sc.DESTINATION_DELIVERY
    assert outcome.review_required is False
    assert outcome.human_verified is False


def test_sampled_without_human_conclusion_is_pending_manual():
    record = make_record("pd-1")
    outcome = _evaluate(record, sampled=True)
    assert outcome.decision == sc.DECISION_PENDING
    assert outcome.label == sc.LABEL_PENDING
    assert outcome.destination == sc.DESTINATION_REVIEW
    assert outcome.action == "waiting_manual"
    assert outcome.in_main_output is True
    assert outcome.review_required is True
    assert outcome.human_verified is False
    assert outcome.reason_code == "no_human_conclusion"


def test_sampled_pass_is_recorded_as_human_verified():
    record = make_record("ps-1", verdict="pass")
    outcome = _evaluate(record, sampled=True)
    assert outcome.decision == sc.DECISION_PASSED
    assert outcome.label == sc.LABEL_PASSED
    assert outcome.destination == sc.DESTINATION_DELIVERY
    assert outcome.human_verified is True
    assert outcome.review_required is False
    assert outcome.in_main_output is True


def test_sampled_fail_cancels_auto_alignment_and_quarantines():
    record = make_record("fl-1", alignment="auto_aligned", verdict="not_aligned")
    outcome = _evaluate(record, sampled=True)
    assert outcome.decision == sc.DECISION_REJECTED
    assert outcome.label == sc.LABEL_SAMPLE_SEMANTIC
    assert outcome.destination == sc.DESTINATION_SAMPLE
    assert outcome.action == "isolate"
    assert outcome.in_main_output is False
    assert outcome.cancelled_auto_alignment is True
    # 判错已有最终结论：后续动作由 #21 承接，不重复计入「待人工复核」
    assert outcome.review_required is False
    assert outcome.human_verified is True
    alignment = outcome.record["alignment"]
    assert alignment["status"] == sc.ALIGNMENT_CANCELLED
    assert alignment["auto_aligned"] is False
    assert alignment["cancelled_by"] == "operator_24"
    assert alignment["previous_status"] == sc.ALIGNMENT_AUTO


def test_sampled_fail_without_auto_alignment_still_quarantines():
    record = make_record("fl-2", alignment="manual_aligned", verdict="fail")
    outcome = _evaluate(record, sampled=True)
    assert outcome.decision == sc.DECISION_REJECTED
    assert outcome.cancelled_auto_alignment is False
    assert outcome.in_main_output is False
    # 没有自动对齐可取消时**不改写**上游对齐字段，只在本次命名空间登记人工判定
    assert "cancelled_by" not in outcome.record["alignment"]
    assert outcome.record["alignment"]["status"] == "manual_aligned"
    assert outcome.record["processing"]["24"]["human_alignment_judgement"] == \
        sc.ALIGNMENT_NOT


def test_sampled_uncertain_goes_to_manual_queue_not_isolation():
    record = make_record("uc-1", verdict="uncertain")
    outcome = _evaluate(record, sampled=True)
    assert outcome.decision == sc.DECISION_UNCERTAIN
    assert outcome.label == sc.LABEL_UNCERTAIN
    assert outcome.destination == sc.DESTINATION_REVIEW
    assert outcome.in_main_output is True
    assert outcome.review_required is True
    assert outcome.human_verified is False


def test_invalid_review_evidence_is_pending_with_reason():
    record = make_record("iv-1", verdict="pass", reviewer=None)
    outcome = _evaluate(record, sampled=True)
    assert outcome.decision == sc.DECISION_PENDING
    assert outcome.reason_code == "review_invalid:missing_reviewer"


def test_annotation_keeps_original_fields_and_adds_namespace():
    record = make_record("an-1", verdict="pass")
    record["unknown_extension"] = {"keep": True}
    outcome = _evaluate(record, sampled=True, stratum=("s-1", "image+text", "auto_aligned"))
    node = outcome.record["processing"]["24"]
    assert node["operator"] == sc.OPERATOR_NAME
    assert node["operator_number"] == 24
    assert node["stage_id"] == sc.STAGE_ID
    assert node["sampled"] is True
    assert node["stratum"] == {"scene": "s-1", "modality": "image+text",
                               "alignment_type": "auto_aligned"}
    assert node["label"] == sc.LABEL_PASSED
    assert node["human_verified"] is True
    assert node["writeback"]["report"] == sc.REPORT_NAME
    assert node["source_reference"] == "operator_24"
    assert outcome.record["unknown_extension"] == {"keep": True}
    assert outcome.record["operator_24"]["decision"] == sc.DECISION_PASSED
    assert outcome.record["text"] == record["text"]


# --------------------------------------------------------------------------- #
# 台账
# --------------------------------------------------------------------------- #

def test_reject_entry_has_ledger_fields_without_dumping_payload():
    entry = sc.build_reject_entry(7, "json_parse_error", "not-json", business(),
                                  "task_1", "run001")
    assert entry["line"] == 7
    assert entry["reason"] == "json_parse_error"
    assert entry["destination"] == sc.DESTINATION_ORIGIN
    assert entry["action"] == "isolate"
    assert entry["uniqueId"] == "task_1"
    assert entry["runId"] == "run001"
    assert entry["operator_number"] == 24
    assert entry["raw_preview"] == "not-json"


def test_origin_ledger_keeps_the_original_record():
    record = make_record("oq-1")
    ledger = sc.build_origin_ledger(record, sample_id="oq-1", reason="duplicate_sample_id",
                                    label=sc.LABEL_ORIGIN_DUPLICATE, lineno=3,
                                    business=business(), unique_id="t_f1", run_id="run001")
    assert ledger["sample_id"] == "oq-1"
    assert ledger["label"] == sc.LABEL_ORIGIN_DUPLICATE
    assert ledger["record"] == record


def test_sample_quarantine_ledger_contains_decision_and_record():
    outcome = _evaluate(make_record("sq-1", verdict="fail"), sampled=True)
    ledger = sc.build_ledger(outcome, business(), "t", "run001", role="sample_quarantine")
    assert ledger["decision"] == sc.DECISION_REJECTED
    assert ledger["label"] == sc.LABEL_SAMPLE_SEMANTIC
    assert ledger["destination"] == sc.DESTINATION_SAMPLE
    assert ledger["human_verified"] is True
    assert ledger["record"]["alignment"]["cancelled_by"] == "operator_24"


def test_dispute_and_pending_ledgers_are_marked_with_roles():
    dispute = sc.build_ledger(_evaluate(make_record("dp-1", verdict="uncertain"), sampled=True),
                              business(), "t", "run001", role="dispute")
    pending = sc.build_ledger(_evaluate(make_record("pd-9"), sampled=True),
                              business(), "t", "run001", role="pending_manual")
    assert dispute["role"] == "dispute"
    assert pending["role"] == "pending_manual"
    assert dispute["handover"] == ["operator_21", "operator_25"]


# --------------------------------------------------------------------------- #
# 回写报告
# --------------------------------------------------------------------------- #

def _sampling(target=4):
    return {"sample_rate": 0.10, "salt": "operator24", "strata": ["scene", "modality",
                                                                  "alignment_type"],
            "target_total": target, "achieved_rate": 0.10,
            "strata_detail": {"scene=scene-A|modality=image+text|alignment_type=auto_aligned":
                              {"scene": "scene-A", "modality": "image+text",
                               "alignment_type": "auto_aligned", "size": 40,
                               "target": target, "sampled": target}}}


def _statistics(**overrides):
    stats = {
        "input_lines": 46, "blank_lines": 1, "input": 45, "rejected": 3, "valid": 42,
        "output": 40, "origin_quarantined": 2, "sample_quarantined": 0,
        "duplicates": 1, "sampled": 4, "sampled_in_output": 4, "not_sampled": 36,
        "sampled_passed": 4, "sampled_rejected": 0, "sampled_uncertain": 0,
        "sampled_pending": 0, "disputes": 0, "pending_manual": 0,
        "auto_alignment_cancelled": 0, "human_verified_records": 4,
        "review_required_records": 0, "not_applicable_records": 0,
    }
    stats.update(overrides)
    return stats


def test_writeback_report_marks_written_back_when_all_conclusions_present(tmp_path):
    source_path = write_quality_report(tmp_path / "20_quality_report.json", total=42)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    report = sc.build_writeback_report(
        source, _statistics(), business(), unique_id="task_1", run_id="run001",
        sampling=_sampling(), conclusions=sc.conclusion_counts(_statistics()),
        cancelled_ids=[], disputes=0, pending_manual=0, writeback_complete=True,
        generated_at="2026-10-08T09:00:00+08:00",
        source_name="20_quality_report.json", source_available=True)
    assert report["status"] == sc.REPORT_STATUS_WRITTEN
    assert report["writeback"]["writeback_completed"] is True
    assert report["writeback"]["version_state"] == sc.VERSION_STATE_DELIVERABLE
    assert report["writeback"]["writeback_by"] == "operator_24"
    assert report["original_status"] == "waiting_writeback"
    # 上游结论不得被覆盖
    assert report["checks"] == source["checks"]
    assert report["metrics"] == source["metrics"]
    assert report["statistics"] == source["statistics"]
    assert report["spot_check"]["sampling"]["target_total"] == 4
    assert report["operator_24"]["statistics"]["sampled"] == 4


def test_writeback_report_stays_pending_when_conclusions_missing(tmp_path):
    source_path = write_quality_report(tmp_path / "20_quality_report.json")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    stats = _statistics(sampled=4, sampled_in_output=3, sampled_passed=1,
                        sampled_rejected=1, sampled_uncertain=1, sampled_pending=1,
                        sample_quarantined=1, output=39, disputes=2, pending_manual=1,
                        human_verified_records=2, review_required_records=2)
    report = sc.build_writeback_report(
        source, stats, business(), unique_id="task_1", run_id="run002",
        sampling=_sampling(), conclusions=sc.conclusion_counts(stats),
        cancelled_ids=["sq-1"], disputes=2, pending_manual=1, writeback_complete=False,
        generated_at="2026-10-08T09:00:00+08:00",
        source_name="20_quality_report.json", source_available=True)
    assert report["status"] == sc.REPORT_STATUS_WAITING
    assert report["writeback"]["writeback_completed"] is False
    assert report["writeback"]["version_state"] == sc.VERSION_STATE_PENDING
    assert report["writeback"]["cancelled_auto_alignment"] == ["sq-1"]
    assert report["writeback"]["gate"]["all_sampled_concluded"] is False


def test_writeback_report_works_without_source_report():
    stats = _statistics(sampled=0, sampled_in_output=0, not_sampled=40,
                        sampled_passed=0, human_verified_records=0)
    assert sc.conservation_failures(stats) == []
    report = sc.build_writeback_report(
        None, stats, business(), unique_id="task_1", run_id="run001",
        sampling=_sampling(target=0), conclusions=sc.conclusion_counts(stats),
        cancelled_ids=[], disputes=0, pending_manual=0, writeback_complete=True,
        generated_at="2026-10-08T09:00:00+08:00",
        source_name=None, source_available=False)
    assert report["status"] == sc.REPORT_STATUS_WRITTEN
    assert report["source_report"]["available"] is False
    assert report["source_report"]["name"] is None
    assert report["writeback"]["version_state"] == sc.VERSION_STATE_DELIVERABLE


# --------------------------------------------------------------------------- #
# 抽检结论（表1 #24「输出：抽检结论和报告状态」独立材料）
# --------------------------------------------------------------------------- #

def test_sampling_conclusion_written_back_when_all_conclusions_present():
    stats = _statistics()
    conclusion = sc.build_sampling_conclusion(
        stats, business(), unique_id="task_1", run_id="run001",
        sampling=_sampling(), conclusions=sc.conclusion_counts(stats),
        cancelled_ids=[], strata_detail=_sampling()["strata_detail"],
        writeback_complete=True, generated_at="2026-10-08T09:00:00+08:00",
        source_name="20_quality_report.json", source_available=True, status="success")
    assert conclusion["status"] == sc.REPORT_STATUS_WRITTEN
    assert conclusion["written_back"] is True
    assert conclusion["conclusion_written_back"] is True
    assert conclusion["sampling_written_back"] is True
    assert conclusion["version_state"] == sc.VERSION_STATE_DELIVERABLE
    assert conclusion["sampling_rate"] == pytest.approx(0.10)
    assert conclusion["operator_number"] == 24
    assert conclusion["writeback"]["completed"] is True
    assert conclusion["writeback"]["completed_at"] == "2026-10-08T09:00:00+08:00"
    assert conclusion["summary"]["sampled"] == 4
    assert conclusion["strata_dimensions"] == ["scene", "modality", "alignment_type"]


def test_sampling_conclusion_stays_pending_and_flags_cancelled_auto_alignment():
    """表1 #24 步骤 3)+4)：判错取消自动对齐，结论未回写时版本保持待发布。"""
    stats = _statistics(sampled=4, sampled_in_output=3, sampled_passed=1,
                        sampled_rejected=1, sampled_uncertain=1, sampled_pending=1,
                        sample_quarantined=1, output=39, disputes=2, pending_manual=1)
    conclusion = sc.build_sampling_conclusion(
        stats, business(), unique_id="task_1", run_id="run002",
        sampling=_sampling(), conclusions=sc.conclusion_counts(stats),
        cancelled_ids=["sq-1"], strata_detail=_sampling()["strata_detail"],
        writeback_complete=False, generated_at="2026-10-08T09:00:00+08:00",
        source_name="20_quality_report.json", source_available=True,
        status="waiting_manual")
    assert conclusion["status"] == sc.REPORT_STATUS_WAITING
    assert conclusion["written_back"] is False
    assert conclusion["version_state"] == sc.VERSION_STATE_PENDING
    assert conclusion["cancelled_auto_alignment"] == ["sq-1"]
    assert conclusion["handover"]["sample_quarantine"] == ["operator_21"]
    assert conclusion["writeback"]["completed"] is False
    assert conclusion["writeback"]["completed_at"] is None
    assert conclusion["summary"]["cancelled_auto_alignment"] == 1


def test_sampling_conclusion_is_a_separate_material_from_quality_report():
    """#23 封版门禁要求「质量报告已回写」与「抽检结论已回写」两份独立材料。"""
    assert sc.CONCLUSION_NAME != sc.REPORT_NAME
    assert sc.CONCLUSION_NAME in sc.OUTPUT_NAMES
    assert sc.REPORT_NAME in sc.OUTPUT_NAMES
    assert "sampling" in sc.CONCLUSION_NAME


# --------------------------------------------------------------------------- #
# 统计守恒
# --------------------------------------------------------------------------- #

def test_conservation_passes_on_consistent_statistics():
    assert sc.conservation_failures(_statistics()) == []


@pytest.mark.parametrize("patch", [
    {"output": 41},
    {"valid": 43},
    {"sampled": 5},
    {"input": 44},
])
def test_conservation_detects_inconsistent_statistics(patch):
    failures = sc.conservation_failures(_statistics(**patch))
    assert failures


def test_digest_sample_ids_is_order_sensitive_and_stable():
    assert sc.digest_sample_ids(["a", "b"]) == sc.digest_sample_ids(["a", "b"])
    assert sc.digest_sample_ids(["a", "b"]) != sc.digest_sample_ids(["b", "a"])
    assert sc.digest_sample_ids([]) == sc.digest_sample_ids([])
