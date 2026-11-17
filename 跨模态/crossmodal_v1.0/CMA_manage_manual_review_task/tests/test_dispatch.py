"""核心业务单测：类别判定、轮次预算、派单、结论回写与统计守恒。

覆盖表1 #25 研发测边界：

- 无三类样本不生成任务；
- 不重复 #24 抽样（上游抽检通过/未抽中的样本不进队列）；
- 自动对齐且未被抽中的样本不进队列；
- 同一条超过 2 次不再派单。
"""
from __future__ import annotations

import pytest

from tests.conftest import (auto_aligned_not_sampled_record, dispute_record,
                            isolated_record, passed_record, returned_record,
                            set_processing, uncertain_record, with_review,
                            with_rounds)
from utils import dispatch as dp
from utils import review as review_mod


# --------------------------------------------------------------------------- #
# 类别判定
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("factory,expected", [
    (lambda sid: uncertain_record(sid), dp.CATEGORY_UNCERTAIN),
    (lambda sid: uncertain_record(sid, source="16"), dp.CATEGORY_UNCERTAIN),
    (lambda sid: uncertain_record(sid, source="low_confidence"), dp.CATEGORY_UNCERTAIN),
    (lambda sid: dispute_record(sid, decision="uncertain"), dp.CATEGORY_DISPUTE),
    (lambda sid: dispute_record(sid, decision="rejected"), dp.CATEGORY_DISPUTE),
    (lambda sid: returned_record(sid), dp.CATEGORY_RETURNED),
])
def test_resolve_category_maps_three_sources(factory, expected):
    record = factory("cat-01")
    category, evidence = dp.resolve_category(record)
    assert category == expected
    assert evidence


def test_resolve_category_is_none_for_passed_and_isolated():
    assert dp.resolve_category(passed_record("cat-pass"))[0] is None
    assert dp.resolve_category(isolated_record("cat-iso"))[0] is None


def test_resolve_category_prefers_returned_then_dispute():
    record = returned_record("cat-order")
    set_processing(record, "24", {"decision": "uncertain", "role": "dispute",
                                  "sampled": True})
    assert dp.resolve_category(record)[0] == dp.CATEGORY_RETURNED


def test_eligible_categories_are_frozen():
    assert dp.ELIGIBLE_CATEGORIES == ("uncertain", "sampling_dispute", "returned")


# --------------------------------------------------------------------------- #
# 轮次预算
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rounds,expected", [(0, 0), (1, 1), (2, 2), (5, 5)])
def test_resolve_review_rounds_reads_review_history(rounds, expected):
    record = with_rounds(uncertain_record("round-01"), rounds)
    value, _source = dp.resolve_review_rounds(record)
    assert value == expected


def test_resolve_review_rounds_reads_operator_25_namespace():
    record = uncertain_record("round-02")
    record["operator_25"] = {"review_round": 2}
    record["processing"] = {"25": {"review_round": 2}}
    assert dp.resolve_review_rounds(record)[0] == 2


def test_resolve_review_rounds_ignores_non_numeric_noise():
    record = uncertain_record("round-03")
    record["review_count"] = "many"
    record["review_history"] = "broken"
    assert dp.resolve_review_rounds(record)[0] == 0


# --------------------------------------------------------------------------- #
# 派单与结论
# --------------------------------------------------------------------------- #

def test_not_triggered_generates_no_task(enabled_biz):
    record = uncertain_record("nt-01")
    outcome = dp.evaluate_record(record, sample_id="nt-01", business=enabled_biz)
    assert outcome.dispatched is True

    not_triggered = dict(enabled_biz, enabled=False)
    outcome = dp.evaluate_record(record, sample_id="nt-01", business=not_triggered)
    assert outcome.decision == dp.DECISION_NOT_TRIGGERED
    assert outcome.dispatched is False
    assert outcome.in_main_output is True


def test_no_eligible_category_generates_no_task(enabled_biz):
    outcome = dp.evaluate_record(passed_record("ne-01"), sample_id="ne-01",
                                 business=enabled_biz)
    assert outcome.decision == dp.DECISION_NOT_ELIGIBLE
    assert outcome.dispatched is False
    assert outcome.in_main_output is True


def test_upstream_isolated_is_not_applicable(enabled_biz):
    outcome = dp.evaluate_record(isolated_record("iso-01"), sample_id="iso-01",
                                 business=enabled_biz)
    assert outcome.not_applicable is True
    assert outcome.decision == dp.DECISION_NOT_APPLICABLE
    assert outcome.dispatched is False
    assert outcome.in_main_output is True


def test_auto_aligned_and_not_sampled_never_enters_queue(enabled_biz):
    record = auto_aligned_not_sampled_record("aa-01")
    outcome = dp.evaluate_record(record, sample_id="aa-01", business=enabled_biz)
    assert outcome.decision == dp.DECISION_AUTO_ALIGNED_NOT_SAMPLED
    assert outcome.dispatched is False
    assert outcome.in_main_output is True


def test_max_review_count_stops_dispatch(enabled_biz):
    record = with_rounds(uncertain_record("max-01"), 2)
    outcome = dp.evaluate_record(record, sample_id="max-01", business=enabled_biz)
    assert outcome.decision == dp.DECISION_MAX_REVIEW
    assert outcome.dispatched is False
    assert outcome.action == dp.ACTION_LOCK
    assert outcome.destination == dp.DESTINATION_SAMPLE
    assert outcome.in_main_output is False


def test_second_review_is_still_dispatched(enabled_biz):
    record = with_rounds(uncertain_record("max-02"), 1)
    outcome = dp.evaluate_record(record, sample_id="max-02", business=enabled_biz)
    assert outcome.dispatched is True
    assert outcome.next_round == 2


def test_missing_conclusion_stays_pending_manual(enabled_biz):
    outcome = dp.evaluate_record(uncertain_record("pend-01"), sample_id="pend-01",
                                 business=enabled_biz)
    assert outcome.decision == dp.DECISION_PENDING
    assert outcome.destination == dp.DESTINATION_REVIEW
    assert outcome.action == dp.ACTION_WAITING
    assert outcome.in_main_output is True
    assert outcome.review_required is True
    assert outcome.human_verified is False


def test_pass_conclusion_returns_to_pool(enabled_biz):
    record = with_review(uncertain_record("pass-01"), "pass", round_no=1)
    outcome = dp.evaluate_record(record, sample_id="pass-01", business=enabled_biz)
    assert outcome.decision == dp.DECISION_PASSED
    assert outcome.destination == dp.DESTINATION_POOL
    assert outcome.in_main_output is True
    assert outcome.human_verified is True
    assert outcome.review_required is False
    assert outcome.next_round == 1


def test_fail_conclusion_isolates_record(enabled_biz):
    record = with_review(dispute_record("fail-01", decision="uncertain"), "fail",
                         dimensions={"semantics": "fail"})
    outcome = dp.evaluate_record(record, sample_id="fail-01", business=enabled_biz)
    assert outcome.decision == dp.DECISION_REJECTED
    assert outcome.label == dp.LABEL_SAMPLE_SEMANTIC
    assert outcome.destination == dp.DESTINATION_SAMPLE
    assert outcome.action == dp.ACTION_ISOLATE
    assert outcome.in_main_output is False
    assert outcome.human_verified is True


def test_uncertain_conclusion_keeps_waiting_manual(enabled_biz):
    record = with_review(uncertain_record("unc-01"), "uncertain")
    outcome = dp.evaluate_record(record, sample_id="unc-01", business=enabled_biz)
    assert outcome.decision == dp.DECISION_UNCERTAIN
    assert outcome.destination == dp.DESTINATION_REVIEW
    assert outcome.in_main_output is True
    assert outcome.human_verified is False


def test_conclusion_without_reviewer_is_invalid_and_pending(enabled_biz):
    record = with_review(uncertain_record("no-rev"), "pass", reviewer=None)
    outcome = dp.evaluate_record(record, sample_id="no-rev", business=enabled_biz)
    assert outcome.decision == dp.DECISION_PENDING
    assert outcome.reason_code == "review_invalid:missing_reviewer"


def test_reviewer_not_required_when_disabled(enabled_biz):
    relaxed = dict(enabled_biz, require_reviewer_identity=False)
    record = with_review(uncertain_record("no-rev-2"), "pass", reviewer=None)
    outcome = dp.evaluate_record(record, sample_id="no-rev-2", business=relaxed)
    assert outcome.decision == dp.DECISION_PASSED


def test_platform_unavailable_keeps_task_in_waiting_state(enabled_biz):
    outcome = dp.evaluate_record(uncertain_record("plat-01"), sample_id="plat-01",
                                 business=enabled_biz)
    task = dp.build_task_entry(outcome, enabled_biz, "t_1", "run001")
    assert task["dispatch"]["state"] == "waiting_manual"
    assert task["dispatch"]["platform_available"] is False
    assert task["dispatch"]["assignee"] is None
    assert task["round"] == 1
    assert task["max_review_count"] == 2


def test_platform_available_marks_task_dispatched():
    from utils import dispatch

    business = dispatch.default_business(enabled=True, manual_platform_available=True)
    outcome = dispatch.evaluate_record(uncertain_record("plat-02"), sample_id="plat-02",
                                      business=business)
    task = dispatch.build_task_entry(outcome, business, "t_1", "run001")
    assert task["dispatch"]["state"] == "dispatched"
    assert task["dispatch"]["platform_available"] is True


def test_task_id_is_deterministic_and_round_aware(enabled_biz):
    outcome = dp.evaluate_record(uncertain_record("tid-01"), sample_id="tid-01",
                                 business=enabled_biz)
    first = dp.build_task_entry(outcome, enabled_biz, "t_1", "run001")
    second = dp.build_task_entry(outcome, enabled_biz, "t_1", "run002")
    assert first["task_id"] == second["task_id"]
    assert first["task_id"].endswith("-r1")


# --------------------------------------------------------------------------- #
# 统计与守恒
# --------------------------------------------------------------------------- #

def test_statistics_conservation_on_full_matrix(mixed_rows, enabled_biz):
    business = enabled_biz
    stats = dp.empty_statistics()
    stats["input_lines"] = len(mixed_rows)
    stats["input"] = len(mixed_rows)
    stats["valid"] = len(mixed_rows)
    for record in mixed_rows:
        outcome = dp.evaluate_record(record, sample_id=record["sample_id"],
                                     business=business)
        dp.accumulate(stats, outcome)
    assert stats["conservation_failures"] == []
    assert stats["conservation_ok"] is True
    assert stats["tasks_generated"] == 6
    assert stats["not_eligible"] == 1
    assert stats["not_applicable_records"] == 1
    assert stats["queued"] == stats["tasks_generated"]
    assert stats["waiting_dispatch"] == 6
    assert stats["dispatched"] == 0


def test_conservation_failures_are_reported(mixed_rows, enabled_biz):
    stats = dp.empty_statistics()
    stats["input"] = 3
    stats["valid"] = 2
    stats["rejected"] = 0
    failures = dp.conservation_failures(stats)
    assert failures
    assert any("input == valid + rejected" in item for item in failures)


def test_not_triggered_statistics_are_consistent(mixed_rows, biz):
    stats = dp.empty_statistics()
    stats["input_lines"] = len(mixed_rows)
    stats["input"] = len(mixed_rows)
    stats["valid"] = len(mixed_rows)
    for record in mixed_rows:
        dp.accumulate(stats, dp.evaluate_record(record, sample_id=record["sample_id"],
                                                business=biz))
    assert stats["conservation_failures"] == []
    assert stats["tasks_generated"] == 0
    assert stats["not_triggered"] == 6
    assert stats["output"] == len(mixed_rows)


def test_flags_do_not_double_count(mixed_rows, enabled_biz):
    """flagged 类计数（locked/等）不得与主记录或隔离重复加总。"""
    stats = dp.empty_statistics()
    stats["input_lines"] = len(mixed_rows)
    stats["input"] = len(mixed_rows)
    stats["valid"] = len(mixed_rows)
    for record in mixed_rows:
        dp.accumulate(stats, dp.evaluate_record(record, sample_id=record["sample_id"],
                                                business=enabled_biz))
    assert stats["output"] == stats["valid"] - stats["origin_quarantined"] \
        - stats["sample_quarantined"]
    assert stats["sample_quarantined"] == (stats["concluded_rejected"]
                                           + stats["max_review_reached"])


def test_management_report_reflects_real_statistics(enabled_biz):
    stats = dp.empty_statistics()
    stats["input"] = 2
    stats["valid"] = 2
    stats["tasks_generated"] = 2
    stats["waiting_dispatch"] = 2
    stats["queued"] = 2
    report = dp.build_management_report(stats, enabled_biz, unique_id="t_1",
                                       run_id="run001", generated_at="2026-10-08T10:00:00+08:00",
                                       status="waiting_manual")
    assert report["status"] == "waiting_manual"
    assert report["review"]["trigger"] == "manual"
    assert report["review"]["statistics"]["tasks_generated"] == 2
    assert report["operator_number"] == dp.FORMAL_OPERATOR_NUMBER


def test_review_conclusion_counts_only_final_verdicts():
    stats = dp.empty_statistics()
    stats.update({
        "input_lines": 5, "input": 5, "valid": 5, "output": 4,
        "not_dispatched": 1, "not_eligible": 1,
        "tasks_generated": 4, "queued": 4, "dispatched": 4, "waiting_dispatch": 0,
        "concluded_passed": 1, "concluded_rejected": 1,
        "concluded_pending": 1, "concluded_uncertain": 1,
        "conclusions_written": 2, "human_verified_records": 2,
        "review_required_records": 2, "returned_to_pool": 1,
        "sample_quarantined": 1,
    })
    assert dp.conservation_failures(stats) == []


def test_review_verdict_aliases_are_normalised():
    assert review_mod.normalise_verdict("通过") == review_mod.VERDICT_PASS
    assert review_mod.normalise_verdict("fail") == review_mod.VERDICT_FAIL
    assert review_mod.normalise_verdict("存疑") == review_mod.VERDICT_UNCERTAIN
    assert review_mod.normalise_verdict("nonsense") is None


def test_dimension_fail_overrides_pass_verdict():
    record = uncertain_record("dim-01")
    record["review"] = {"verdict": "pass", "reviewer": "reviewer-01",
                        "dimensions": {"semantics": "fail"}}
    resolved = review_mod.resolve_review(record)
    assert resolved["valid"] is True
    assert resolved["verdict"] == review_mod.VERDICT_FAIL
    assert resolved["overridden_by_dimension"] is True


def test_unknown_dimension_is_invalid_evidence():
    record = uncertain_record("dim-02")
    record["review"] = {"verdict": "pass", "reviewer": "reviewer-01",
                        "dimensions": {"colour": "fail"}}
    resolved = review_mod.resolve_review(record)
    assert resolved["valid"] is False
    assert resolved["reason"] == "invalid_dimension"


def test_processing_25_review_takes_priority_for_idempotency():
    record = uncertain_record("idem-01")
    record["review"] = {"verdict": "pass", "reviewer": "reviewer-01"}
    record["processing"] = {"25": {"review": {"verdict": "fail",
                                              "reviewer": "reviewer-09"}}}
    resolved = review_mod.resolve_review(record)
    assert resolved["verdict"] == review_mod.VERDICT_FAIL
    assert resolved["source"] == "processing_25"


def test_review_summary_hides_notes_text():
    record = uncertain_record("sum-01")
    record["review"] = {"verdict": "pass", "reviewer": "reviewer-01",
                        "notes": "敏感长文本" * 50}
    summary = review_mod.review_summary(review_mod.resolve_review(record))
    assert "notes" not in summary
    assert summary["verdict"] == "pass"
