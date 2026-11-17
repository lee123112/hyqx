from types import SimpleNamespace
import inference
import pytest
from utils import alignment


def test_rescore_logged_once_when_pair_occurs_in_main_and_review():
    pair = {"pair_id": "edited-pair", "alignment": {"whole": {
        "bucketed_by": "rescore", "clip_score": 0.28,
        "decision": "pending_manual_review",
        "description_edit": {"score_before": 0.27}}}}
    result = SimpleNamespace(main=[pair], isolation=[],
                             review=[{"pair_id": "edited-pair", "record": pair}],
                             not_applicable=[])
    assert inference.rescore_log_evidence(result) == [
        ("edited-pair", 0.27, 0.28, "pending_manual_review")]


@pytest.mark.parametrize("decision", ["aligned", "not_aligned", "uncertain"])
def test_simulated_conclusion_remains_marked_in_sample_and_audit(plan, build_pair, decision):
    events = alignment.load_human_events([(1, {
        "pair_id": "pair-t11--i11", "decision": decision,
        "reviewer_id": "simulated-reviewer", "reviewed_at": "2026-10-10T09:00:00+08:00",
        "simulated_human": True}, None)])
    result = plan([build_pair(11, 0.28)], events=events)
    records = result.main + [r["record"] for r in result.isolation + result.review]
    assert records
    assert all(r["alignment"]["whole"]["simulated_human"] is True for r in records)
    assert all(r["simulated_human"] is True for r in result.audit if r["kind"] == "human_event")
