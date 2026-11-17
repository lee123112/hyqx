"""人工复核结论解析的单元测试。

规则要点：**结论只来自输入证据**；缺少复核人身份或取值非法时，
只能判为「待人工」，绝不允许把它当成人工确认。
"""
from __future__ import annotations

import pytest

from tests.conftest import make_record
from utils import review as review_mod


def test_missing_review_is_not_present():
    result = review_mod.resolve_review({"sample_id": "r-1"})
    assert result["present"] is False
    assert result["valid"] is False
    assert result["verdict"] is None
    assert result["reason"] == "no_human_conclusion"


@pytest.mark.parametrize("raw,expected", [
    ("pass", review_mod.VERDICT_PASS),
    ("PASS", review_mod.VERDICT_PASS),
    ("aligned", review_mod.VERDICT_PASS),
    ("通过", review_mod.VERDICT_PASS),
    ("fail", review_mod.VERDICT_FAIL),
    ("not_aligned", review_mod.VERDICT_FAIL),
    ("不对齐", review_mod.VERDICT_FAIL),
    ("判错", review_mod.VERDICT_FAIL),
    ("uncertain", review_mod.VERDICT_UNCERTAIN),
    ("存疑", review_mod.VERDICT_UNCERTAIN),
])
def test_verdict_aliases(raw, expected):
    record = make_record("r-2", verdict=raw)
    result = review_mod.resolve_review(record)
    assert result["valid"] is True
    assert result["verdict"] == expected
    assert result["reviewer"] == "reviewer-01"


def test_unknown_verdict_is_invalid():
    record = make_record("r-3", verdict="maybe-later")
    result = review_mod.resolve_review(record)
    assert result["present"] is True
    assert result["valid"] is False
    assert result["reason"] == "invalid_verdict"


def test_missing_reviewer_is_invalid_when_required():
    record = make_record("r-4", verdict="pass", reviewer=None)
    result = review_mod.resolve_review(record, require_reviewer=True)
    assert result["valid"] is False
    assert result["reason"] == "missing_reviewer"
    # 关闭身份要求后同一份证据可被接受（默认开启，仅用于验证开关语义）
    assert review_mod.resolve_review(record, require_reviewer=False)["valid"] is True


def test_dimension_failure_overrides_pass():
    record = make_record("r-5", verdict="pass",
                         dimensions={"semantics": "pass", "temporal": "fail"})
    result = review_mod.resolve_review(record)
    assert result["valid"] is True
    assert result["verdict"] == review_mod.VERDICT_FAIL
    assert result["dimensions"]["temporal"] == review_mod.VERDICT_FAIL
    assert result["overridden_by_dimension"] is True


def test_dimensions_alone_derive_overall_verdict():
    record = {"sample_id": "r-6", "review": {
        "reviewer": "reviewer-09",
        "dimensions": {"semantics": "pass", "temporal": "pass", "scene": "uncertain"}}}
    result = review_mod.resolve_review(record)
    assert result["valid"] is True
    assert result["verdict"] == review_mod.VERDICT_UNCERTAIN


def test_unknown_dimension_is_invalid():
    record = make_record("r-7", verdict="pass",
                         dimensions={"semantics": "excellent"})
    result = review_mod.resolve_review(record)
    assert result["valid"] is False
    assert result["reason"] == "invalid_dimension"


def test_processing_24_review_wins_over_top_level_review():
    record = make_record("r-8", verdict="fail")
    record["processing"] = {"24": {"review": {"verdict": "pass",
                                              "reviewer": "reviewer-77"}}}
    result = review_mod.resolve_review(record)
    assert result["source"] == "processing_24"
    assert result["verdict"] == review_mod.VERDICT_PASS
    assert result["reviewer"] == "reviewer-77"

def test_metadata_human_review_is_accepted():
    record = {"sample_id": "r-9",
              "metadata": {"human_review": {"verdict": "not_aligned",
                                            "reviewer": "reviewer-05"}}}
    result = review_mod.resolve_review(record)
    assert result["valid"] is True
    assert result["verdict"] == review_mod.VERDICT_FAIL
    assert result["source"] == "metadata"


def test_non_mapping_review_is_invalid():
    record = {"sample_id": "r-10", "review": "pass"}
    result = review_mod.resolve_review(record)
    assert result["present"] is False
    assert result["valid"] is False


def test_review_summary_only_exposes_safe_fields():
    record = make_record("r-11", verdict="fail", dimensions={"scene": "fail"})
    summary = review_mod.review_summary(review_mod.resolve_review(record))
    assert set(summary) == {"verdict", "reviewer", "reviewed_at", "dimensions",
                            "source", "valid", "reason", "simulated_human"}
    assert summary["verdict"] == review_mod.VERDICT_FAIL
