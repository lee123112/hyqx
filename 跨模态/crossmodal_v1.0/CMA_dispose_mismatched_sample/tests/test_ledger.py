"""业务模块 ``utils.ledger`` 单元测试：证据抽取、分类、退回/锁定预算、台账六字段。"""
from __future__ import annotations

import pytest

from tests.conftest import make_entry, make_full_record
from utils import ledger


BUSINESS = {
    "operator": ledger.OPERATOR,
    "operator_version": "v1.0.0",
    "stage_id": "S3",
    "return_limit": 1,
    "max_retry_per_item": 2,
    "raw_issue_destination": "raw_isolation",
    "quality_issue_destination": "sample_isolation",
    "scoring_tiers_not_isolated": True,
    "manual_platform_available": False,
    "allow_degraded": True,
    "max_embedded_record_bytes": 32768,
}


def _evidence(entry):
    return ledger.extract_evidence(entry)


def _classify(entry, business=None):
    return ledger.classify(_evidence(entry), business or BUSINESS)


# ------------------------------------------------------------------ 六字段台账
def test_ledger_fields_cover_unified_six_categories():
    for name in ("sample_id", "label", "operator", "stage", "action", "destination",
                 "event_time", "batch_id", "retry_count"):
        assert name in ledger.LEDGER_FIELDS


def test_formal_operator_number_is_21():
    assert ledger.FORMAL_OPERATOR_NUMBER == 21
    assert ledger.OPERATOR == "mismatch_disposal"


# ------------------------------------------------------------------ 证据抽取
def test_extract_evidence_flat_entry():
    entry = make_entry("s-1", label="样本-语义不通", action="isolate",
                       destination="样本隔离库", retry_count=1, stage="S3",
                       upstream_operator="image_text_scoring", event_time="2026-10-07T10:00:00+08:00",
                       batch_id="B-1")
    evidence = _evidence(entry)
    assert evidence["label"] == "样本-语义不通"
    assert evidence["action"] == "isolate"
    assert evidence["destination"] == "sample_isolation"
    assert evidence["retry_count"] == 1
    assert evidence["batch_id"] == "B-1"
    assert evidence["stage"] == "S3"
    assert evidence["upstream_operator"] == "image_text_scoring"


def test_extract_evidence_nested_full_record_fallback():
    record = make_full_record("s-2", label="原始文件-损坏", action="isolate",
                              destination="原始隔离区", retry_count=2)
    evidence = _evidence(record)
    assert evidence["label"] == "原始文件-损坏"
    assert evidence["action"] == "isolate"
    assert evidence["destination"] == "raw_isolation"
    assert evidence["retry_count"] == 2
    assert evidence["batch_id"] == "BATCH-21"


def test_extract_evidence_retry_count_defaults_to_zero_and_rejects_bad_values():
    assert _evidence({"sample_id": "s-3"})["retry_count"] == 0
    assert _evidence(make_entry("s-4", retry_count=-5))["retry_count"] == 0
    assert _evidence(make_entry("s-5", retry_count="2"))["retry_count"] == 0
    assert _evidence(make_entry("s-6", retry_count=True))["retry_count"] == 0


def test_extract_evidence_reads_previous_operator_return_count():
    entry = make_entry("s-7", action="return", upstream_return_count=2)
    assert _evidence(entry)["return_count"] == 2
    assert _evidence({"sample_id": "s-8"})["return_count"] == 0


def test_extract_evidence_requires_human_confirmation_chain():
    assert _evidence(make_entry("s-9", requires_human=True))["requires_human_confirmation"] is True
    assert _evidence(make_full_record("s-10", label="x", action="keep",
                                      requires_human=True))["requires_human_confirmation"] is True
    assert _evidence({"sample_id": "s-11"})["requires_human_confirmation"] is False


# ------------------------------------------------------------------ 分类
@pytest.mark.parametrize("label", ["原始文件-损坏", "原始文件-编码异常", "原始文件-空",
                                   "原始文件-无法解析"])
def test_classify_origin_file_labels(label):
    result = _classify(make_entry("s-x", label=label))
    assert result["category"] == ledger.CATEGORY_ORIGIN
    assert result["destination"] == "raw_isolation"
    assert result["action"] == "isolate"


@pytest.mark.parametrize("label", ["样本-敏感", "样本-语义不通", "样本-评分低", "样本-错配"])
def test_classify_business_problem_labels(label):
    result = _classify(make_entry("s-x", label=label))
    assert result["category"] == ledger.CATEGORY_SAMPLE
    assert result["destination"] == "sample_isolation"
    assert result["action"] == "isolate"


@pytest.mark.parametrize("label", ["样本-评分：通过", "样本-评分：待人工", "样本-评分：入池",
                                   "样本-评分：CLIP分"])
def test_scoring_tiers_never_go_into_isolation(label):
    """表1：评分分层属于流程状态，不进隔离区（即使上游写了 action=isolate）。"""
    result = _classify(make_entry("s-x", label=label, action="isolate",
                                  destination="样本隔离库"))
    assert result["category"] == ledger.CATEGORY_PROCESS
    assert result["destination"] in ("pipeline", "human_queue")
    assert result["action"] != "isolate"


@pytest.mark.parametrize("label", ["流程-待补描述", "流程-未配对", "流程状态-待人工"])
def test_flow_statuses_are_process_not_isolation(label):
    result = _classify(make_entry("s-x", label=label))
    assert result["category"] == ledger.CATEGORY_PROCESS


def test_scoring_tier_not_isolated_flag_can_be_turned_off_explicitly():
    business = dict(BUSINESS, scoring_tiers_not_isolated=False)
    result = _classify(make_entry("s-x", label="样本-评分：待人工", action="isolate",
                                  destination="样本隔离库"), business)
    assert result["category"] == ledger.CATEGORY_SAMPLE


def test_classify_falls_back_to_action_and_destination_when_label_unknown():
    origin = _classify(make_entry("s-x", action="isolate", destination="raw_isolation"))
    assert origin["category"] == ledger.CATEGORY_ORIGIN
    sample = _classify(make_entry("s-y", action="isolate", destination="sample_isolation"))
    assert sample["category"] == ledger.CATEGORY_SAMPLE
    returned = _classify(make_entry("s-z", action="return", destination="09_准备候选描述"))
    assert returned["category"] == ledger.CATEGORY_RETURN
    locked = _classify(make_entry("s-w", action="lock"))
    assert locked["category"] == ledger.CATEGORY_LOCK


def test_classify_without_any_disposition_evidence_is_none():
    result = _classify({"sample_id": "s-none"})
    assert result["category"] == ledger.CATEGORY_NONE
    assert result["destination"] == "pipeline"


def test_classify_keep_action_is_pass():
    result = _classify(make_entry("s-x", action="keep", destination="样本池"))
    assert result["category"] == ledger.CATEGORY_PASS


# ------------------------------------------------------------------ 退回/锁定预算
def test_decide_retry_first_return_is_allowed():
    decision = ledger.decide_retry(0, 0, BUSINESS)
    assert decision["decision"] == ledger.DECISION_RETURN
    assert decision["retry_count"] == 1
    assert decision["return_count"] == 1
    assert decision["locked"] is False


def test_decide_retry_second_pipeline_retry_still_allowed_when_return_budget_remains():
    business = dict(BUSINESS, return_limit=2)
    decision = ledger.decide_retry(1, 1, business)
    assert decision["decision"] == ledger.DECISION_RETURN
    assert decision["retry_count"] == 2


def test_decide_retry_locks_on_third_pipeline_attempt():
    """全产线最多 2 次；retry_count=2 时的下一次即第 3 次 → 锁定且不再回流。"""
    decision = ledger.decide_retry(2, 0, BUSINESS)
    assert decision["decision"] == ledger.DECISION_LOCKED
    assert decision["locked"] is True
    assert decision["retry_count"] == 2
    assert decision["reason_code"] == "retry_budget_exhausted"


def test_decide_retry_locks_when_per_item_return_limit_exhausted():
    decision = ledger.decide_retry(1, 1, BUSINESS)
    assert decision["decision"] == ledger.DECISION_LOCKED
    assert decision["reason_code"] == "return_limit_exhausted"


def test_decide_retry_boundary_equal_is_allowed():
    business = dict(BUSINESS, return_limit=2, max_retry_per_item=2)
    assert ledger.decide_retry(0, 1, business)["decision"] == ledger.DECISION_RETURN
    assert ledger.decide_retry(0, 2, business)["decision"] == ledger.DECISION_LOCKED


# ------------------------------------------------------------------ 媒体引用
def test_media_reference_prefers_embedded_record_then_top_level():
    assert ledger.media_reference(make_entry("s-1", image="media/a.png"), "image") == "media/a.png"
    assert ledger.media_reference({"sample_id": "s-2", "image": {"path": "media/b.png"}},
                                  "image") == "media/b.png"


def test_media_reference_none_when_absent_or_invalid():
    assert ledger.media_reference({"sample_id": "s-1"}, "image") is None
    assert ledger.media_reference({"sample_id": "s-2", "image": "media/c.png"}, "image") is None
    assert ledger.media_reference({"sample_id": "s-3", "image": {"path": ""}}, "image") is None
    assert ledger.media_reference({"sample_id": "s-4", "image": {"path": 5}}, "image") is None


# ------------------------------------------------------------------ 台账条目
def test_busy_line_entry_is_origin_isolation_with_six_fields():
    entry = ledger.busy_line_entry(operator=ledger.OPERATOR, stage_id="S3", line=7,
                                  reason="invalid_json", message="JSON 解析失败",
                                  event_time="2026-10-07T10:00:00+08:00")
    assert entry["sample_id"] is None
    assert entry["label"] == "原始文件-无法解析"
    assert entry["action"] == "isolate"
    assert entry["destination"] == "raw_isolation"
    assert entry["retry_count"] == 0
    for name in ledger.LEDGER_FIELDS:
        assert name in entry


def test_evaluate_record_released_adds_namespace_and_ledger_row():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    result = ledger.evaluate_record(make_entry("s-1", label="样本-评分：通过"),
                                    1, BUSINESS, stats, set(), "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_RELEASED
    assert result["decision"] == ledger.DECISION_PASS
    record = result["record"]
    assert record["operator_21"]["decision"] == ledger.DECISION_PASS
    # [v4.2] status 双字段：int status + 字符串 process_status
    assert record["processing"]["21"]["status"] == 1
    assert record["processing"]["21"]["process_status"] == "success"
    assert result["entry"]["destination"] == "next_stage"
    assert result["entry"]["review_required"] is False


def test_evaluate_record_isolate_keeps_bounded_record_snapshot_only():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    entry = make_entry("s-2", label="样本-语义不通", image="media/sample_bad.png")
    result = ledger.evaluate_record(entry, 2, BUSINESS, stats, set(),
                                   "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_SAMPLE
    assert result["decision"] == ledger.DECISION_SAMPLE_ISOLATED
    assert result["record"] is None
    assert result["entry"]["record"]["image"]["path"] == "media/sample_bad.png"
    assert result["entry"]["record_truncated"] is False


def test_evaluate_record_truncates_oversized_snapshot():
    business = dict(BUSINESS, max_embedded_record_bytes=64)
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    entry = make_entry("s-3", label="样本-语义不通",
                       record_extra={"payload": "x" * 500})
    result = ledger.evaluate_record(entry, 3, business, stats, set(),
                                   "2026-10-07T10:00:00+08:00")
    assert result["entry"]["record_truncated"] is True
    assert result["entry"]["record"] is None
    assert result["entry"]["record_bytes"] > 64


def test_evaluate_record_origin_route():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    result = ledger.evaluate_record(make_entry("s-4", label="原始文件-损坏"),
                                    4, BUSINESS, stats, set(), "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_ORIGIN
    assert result["decision"] == ledger.DECISION_ORIGIN_ISOLATED
    assert result["entry"]["destination"] == "raw_isolation"


def test_evaluate_record_return_within_budget_goes_to_upstream():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    result = ledger.evaluate_record(
        make_entry("s-5", action="return", destination="09_准备候选描述", retry_count=0),
        5, BUSINESS, stats, set(), "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_RETURN
    assert result["decision"] == ledger.DECISION_RETURN
    assert result["entry"]["retry_count"] == 1
    assert result["entry"]["destination"] == "return_to_upstream"


def test_evaluate_record_return_over_budget_is_locked_into_sample_isolation():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    result = ledger.evaluate_record(
        make_entry("s-6", action="return", destination="01_接入多模态批次", retry_count=2),
        6, BUSINESS, stats, set(), "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_SAMPLE
    assert result["decision"] == ledger.DECISION_LOCKED
    assert result["entry"]["locked"] is True
    assert result["entry"]["label"] == "样本-语义不通"


def test_evaluate_record_waiting_manual_when_platform_unavailable():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    result = ledger.evaluate_record(
        make_entry("s-7", label="样本-评分：待人工", requires_human=True),
        7, BUSINESS, stats, set(), "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_PENDING
    assert result["decision"] == ledger.DECISION_WAITING_MANUAL
    assert result["entry"]["destination"] == "human_queue"
    assert result["record"] is not None


def test_evaluate_record_pending_is_process_state_when_platform_available():
    business = dict(BUSINESS, manual_platform_available=True)
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    result = ledger.evaluate_record(
        make_entry("s-8", label="样本-评分：待人工", requires_human=True),
        8, business, stats, set(), "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_PENDING
    assert result["decision"] == ledger.DECISION_PROCESS
    assert result["entry"]["destination"] == "human_queue"


def test_evaluate_record_not_applicable_without_evidence():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    result = ledger.evaluate_record({"sample_id": "s-9"}, 9, BUSINESS, stats, set(),
                                   "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_RELEASED
    assert result["decision"] == ledger.DECISION_NOT_APPLICABLE
    assert result["entry"]["review_required"] is False


def test_evaluate_record_marks_duplicates_without_dropping_or_isolating():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    seen = {"s-dup"}
    result = ledger.evaluate_record(make_entry("s-dup", label="样本-评分：通过"),
                                    10, BUSINESS, stats, seen, "2026-10-07T10:00:00+08:00")
    assert result["route"] == ledger.ROUTE_RELEASED
    assert result["entry"]["duplicate"] is True
    assert result["record"]["operator_21"]["duplicate"] is True


def test_every_ledger_row_carries_all_six_categories():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    entries = [
        make_entry("a", label="样本-语义不通"),
        make_entry("b", label="原始文件-损坏"),
        make_entry("c", label="样本-评分：通过"),
        make_entry("d", action="return", destination="09_准备候选描述"),
    ]
    for index, entry in enumerate(entries, 1):
        result = ledger.evaluate_record(entry, index, BUSINESS, stats, set(),
                                        "2026-10-07T10:00:00+08:00")
        for name in ledger.LEDGER_FIELDS:
            assert name in result["entry"], name


# ------------------------------------------------------------------ M20 质量报告输入
def make_m20_record(sample_id="s-m20", label="样本-质检通过", decision="accepted",
                    destination="质量报告待回写", reason_code="quality_check_passed"):
    """M20 主产物的一行：结论在 ``operator_20`` 扁平摘要与 ``processing["20"]`` 各一份。"""
    node = {"operator": "alignment_qc", "operator_number": 20, "operator_version": "v1.0.0",
            "stage_id": "S4", "sample_id": sample_id, "decision": decision,
            "label": label, "destination": destination, "reason_code": reason_code,
            "evaluated_at": "2026-11-02T17:50:36+08:00"}
    return {"sample_id": sample_id, "batch_id": "B-20", "metadata": {"caption": "x"},
            "processing": {"20": dict(node)}, "operator_20": dict(node)}


def test_extract_evidence_reads_m20_report_summary():
    evidence = _evidence(make_m20_record())
    assert evidence["label"] == "样本-质检通过"
    assert evidence["destination"] == "pipeline"
    assert evidence["destination_raw"] == "质量报告待回写"
    assert evidence["stage"] == "S4"
    assert evidence["upstream_operator"] == "alignment_qc"
    assert evidence["upstream_decision"] == "accepted"
    assert evidence["event_time"] == "2026-11-02T17:50:36+08:00"
    assert evidence["batch_id"] == "B-20"


def test_extract_evidence_reads_processing_20_without_flat_summary():
    record = make_m20_record()
    record.pop("operator_20")
    evidence = _evidence(record)
    assert evidence["label"] == "样本-质检通过"


def test_classify_m20_passed_label_goes_downstream():
    """表1 #21「通过样本才可进入 M22」：不能落进「其他 样本-*」隔离分支。"""
    result = _classify(make_m20_record())
    assert result["category"] == ledger.CATEGORY_PASS
    assert result["destination"] == ledger.DESTINATION_NEXT_STAGE
    assert result["tier"] == ledger.TIER_PASS
    assert result["action"] == "keep"


def test_classify_m20_flagged_label_waits_for_human():
    record = make_m20_record(label="流程-待人工复核", decision="flagged",
                             destination="人工复核队列",
                             reason_code="reverse_inconsistent")
    result = _classify(record)
    assert result["category"] == ledger.CATEGORY_PROCESS
    assert result["destination"] == ledger.DESTINATION_HUMAN
    assert result["tier"] == ledger.TIER_PENDING
    assert result["reason_code"] == "pending_manual"


def test_classify_falls_back_to_m20_decision_without_label():
    record = make_m20_record()
    record["operator_20"].pop("label")
    record["processing"]["20"].pop("label")
    assert _classify(record)["category"] == ledger.CATEGORY_PASS
    record["operator_20"]["decision"] = "flagged"
    record["processing"]["20"]["decision"] = "flagged"
    assert _classify(record)["category"] == ledger.CATEGORY_PROCESS


def test_evaluate_record_m20_flagged_is_waiting_manual_when_degraded():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    record = make_m20_record(label="流程-待人工复核", decision="flagged",
                             destination="人工复核队列",
                             reason_code="reverse_inconsistent")
    result = ledger.evaluate_record(record, 1, BUSINESS, stats, set(),
                                    "2026-11-02T18:00:00+08:00")
    assert result["route"] == ledger.ROUTE_PENDING
    assert result["decision"] == ledger.DECISION_WAITING_MANUAL
    assert result["entry"]["destination"] == "human_queue"
    assert result["entry"]["degraded"] is True
    assert result["record"]["operator_21"]["decision"] == "waiting_manual"
    assert result["record"]["processing"]["21"]["destination"] == "human_queue"


def test_evaluate_record_m20_flagged_fails_without_degraded():
    business = dict(BUSINESS, allow_degraded=False)
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    record = make_m20_record(label="流程-待人工复核", decision="flagged",
                             destination="人工复核队列")
    with pytest.raises(ledger.DependencyError):
        ledger.evaluate_record(record, 1, business, stats, set(),
                               "2026-11-02T18:00:00+08:00")


def test_evaluate_record_m20_passed_releases_with_pass_decision():
    stats = ledger.initial_statistics()  # type: ignore[attr-defined]
    result = ledger.evaluate_record(make_m20_record(), 1, BUSINESS, stats, set(),
                                    "2026-11-02T18:00:00+08:00")
    assert result["route"] == ledger.ROUTE_RELEASED
    assert result["decision"] == ledger.DECISION_PASS
    assert stats["passed"] == 1
