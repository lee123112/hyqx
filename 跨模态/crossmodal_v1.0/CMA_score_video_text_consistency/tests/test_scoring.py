"""``utils.scoring`` 纯算法测试：余弦相似度、均值与「帧分 → 段分 → 视频分」判定。

2026-10-23 口径：``video_text_score`` = 全部被选中帧的均值（业务路径传 ``frame_scores``）；
未传帧分时仅保留纯段均值聚合的兼容回退。

本文件不加载任何模型。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from utils import scoring  # noqa: E402


# --------------------------------------------------------------------------- #
# 余弦相似度
# --------------------------------------------------------------------------- #

def test_cosine_identical_vectors_is_one():
    assert scoring.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    assert scoring.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors_is_minus_one():
    assert scoring.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_is_scale_invariant():
    left = [0.2, 0.4, 0.6]
    right = [0.6, 0.4, 0.2]
    scaled = [value * 12.0 for value in right]
    assert scoring.cosine_similarity(left, right) == pytest.approx(
        scoring.cosine_similarity(left, scaled), abs=1e-9)


def test_cosine_dimension_mismatch_raises():
    with pytest.raises(ValueError):
        scoring.cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_cosine_zero_vector_is_zero_not_crash():
    """零向量返回 0.0（可解释而非抛异常），保证坏向量不会中断整批。"""
    assert scoring.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_result_is_clamped_to_valid_range():
    value = scoring.cosine_similarity([1.0, 1.0], [1.0, 1.0])
    assert -1.0 <= value <= 1.0


# --------------------------------------------------------------------------- #
# 均值与判定
# --------------------------------------------------------------------------- #

def test_mean_of_empty_is_none():
    assert scoring.mean([]) is None
    assert scoring.mean(None) is None


def test_mean_averages_values():
    assert scoring.mean([0.2, 0.4]) == pytest.approx(0.3)
    assert scoring.mean([0.5]) == pytest.approx(0.5)


def test_segment_passed_uses_inclusive_threshold():
    """表1：段均值不低于阈值即通过（``>= 0.25``）。"""
    assert scoring.segment_passed(0.25, 0.25) is True
    assert scoring.segment_passed(0.2499, 0.25) is False


# --------------------------------------------------------------------------- #
# aggregate：段均值 → 视文分数 / 最低段分 / 通过结论
# --------------------------------------------------------------------------- #

def test_aggregate_all_segments_pass():
    result = scoring.aggregate([0.40, 0.30, 0.50], 0.25)
    assert result["passed"] is True
    assert result["segment_count"] == 3
    assert result["video_text_score"] == pytest.approx((0.40 + 0.30 + 0.50) / 3.0)
    assert result["min_segment_score"] == pytest.approx(0.30)
    assert result["failing_segments"] == []


def test_aggregate_single_failing_segment_fails_whole_sample():
    result = scoring.aggregate([0.60, 0.10, 0.55], 0.25)
    assert result["passed"] is False
    assert result["video_text_score"] == pytest.approx((0.60 + 0.10 + 0.55) / 3.0)
    assert result["min_segment_score"] == pytest.approx(0.10)
    assert result["failing_segments"] == [1]


def test_aggregate_boundary_segment_exactly_at_threshold_passes():
    result = scoring.aggregate([0.25, 0.25], 0.25)
    assert result["passed"] is True
    assert result["min_segment_score"] == pytest.approx(0.25)


def test_aggregate_reports_all_failing_segments():
    result = scoring.aggregate([0.10, 0.20, 0.05], 0.25)
    assert result["passed"] is False
    assert result["failing_segments"] == [0, 1, 2]


def test_aggregate_without_segments_returns_none_conclusion():
    result = scoring.aggregate([], 0.25)
    assert result["passed"] is None
    assert result["video_text_score"] is None
    assert result["min_segment_score"] is None
    assert result["segment_count"] == 0
    assert result["failing_segments"] == []


def test_aggregate_rounds_scores_to_six_decimals():
    result = scoring.aggregate([1.0 / 3.0], 0.25)
    assert result["video_text_score"] == round(1.0 / 3.0, 6)


def test_aggregate_video_score_is_mean_of_all_scored_frames():
    """整段视频分 = 全部被选中帧的均值，而不是段均值的均值。"""
    result = scoring.aggregate([0.80, 0.10], 0.25, [0.80, 0.80, 0.10])
    assert result["video_text_score"] == pytest.approx((0.80 + 0.80 + 0.10) / 3.0)
    assert result["video_text_score"] != pytest.approx((0.80 + 0.10) / 2.0)
    assert result["segment_mean"] == result["video_text_score"]
    assert result["min_segment_score"] == pytest.approx(0.10)
    assert result["passed"] is False
    assert result["failing_segments"] == [1]


def test_aggregate_ignores_segments_without_evidence():
    """0 帧段（段分为 None）不参与判定，也不计入失败段。"""
    result = scoring.aggregate([0.50, None, 0.50], 0.25, [0.50, 0.50])
    assert result["passed"] is True
    assert result["segment_count"] == 2
    assert result["video_text_score"] == pytest.approx(0.50)
    assert result["failing_segments"] == []


def test_aggregate_with_frame_scores_is_deterministic():
    first = scoring.aggregate([0.3, 0.4], 0.25, [0.3, 0.3, 0.4])
    second = scoring.aggregate([0.3, 0.4], 0.25, [0.3, 0.3, 0.4])
    assert first == second
    assert first["video_text_score"] == round(1.0 / 3.0, 6)


def test_aggregate_is_deterministic():
    first = scoring.aggregate([0.31, 0.29, 0.7], 0.25)
    second = scoring.aggregate([0.31, 0.29, 0.7], 0.25)
    assert first == second


def test_cosine_matches_reference_math():
    left = [0.3, -0.4, 0.5, 0.7]
    right = [-0.2, 0.6, 0.1, 0.3]
    expected = (sum(a * b for a, b in zip(left, right))
                / (math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))))
    assert scoring.cosine_similarity(left, right) == pytest.approx(expected, abs=1e-12)
