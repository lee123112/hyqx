"""纯算法测试：余弦相似度、均值、阈值判定与一对一贪心匹配。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from utils import scoring  # noqa: E402


# --------------------------------------------------------------------------- #
# 余弦
# --------------------------------------------------------------------------- #

def test_cosine_identical_is_one():
    assert scoring.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert scoring.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_is_minus_one():
    assert scoring.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_zero_vector_is_zero_not_error():
    assert scoring.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_dimension_mismatch_raises():
    with pytest.raises(ValueError):
        scoring.cosine_similarity([1.0], [1.0, 2.0])


def test_cosine_is_clipped_into_range():
    value = scoring.cosine_similarity([1e12, 1e12], [1e12, -1e12])
    assert -1.0 <= value <= 1.0


# --------------------------------------------------------------------------- #
# 均值
# --------------------------------------------------------------------------- #

def test_mean_ignores_none_and_rounds():
    assert scoring.mean([0.1, None, 0.2]) == pytest.approx(0.15)
    assert scoring.mean([]) is None
    assert scoring.mean(None) is None


# --------------------------------------------------------------------------- #
# 阈值
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("score,expected", [
    (0.25, True), (0.2500000001, True), (0.2499, False), (0.0, False),
])
def test_meets_threshold_includes_boundary(score, expected):
    assert scoring.meets_threshold(score, 0.25) is expected


def test_meets_threshold_with_none_is_none():
    assert scoring.meets_threshold(None, 0.25) is None


# --------------------------------------------------------------------------- #
# 一对一贪心匹配
# --------------------------------------------------------------------------- #

def test_assign_one_to_one_picks_highest_and_respects_uniqueness():
    scores = [[0.9, 0.4],
              [0.8, 0.3]]
    assert scoring.assign_one_to_one(scores) == [0, 1]


def test_assign_one_to_one_competes_for_single_box():
    scores = [[0.9],
              [0.8]]
    assert scoring.assign_one_to_one(scores) == [0, None]


def test_assign_one_to_one_leaves_word_without_box():
    scores = [[0.5, 0.6],
              [0.7, 0.4]]
    assigned = scoring.assign_one_to_one(scores)
    assert None not in assigned
    assert assigned[0] != assigned[1]


def test_assign_one_to_one_more_boxes_than_words():
    assert scoring.assign_one_to_one([[0.1, 0.9, 0.3]]) == [1]


def test_assign_one_to_one_none_scores_are_skipped():
    assert scoring.assign_one_to_one([[None, 0.5], [0.6, None]]) == [1, 0]


def test_assign_one_to_one_empty():
    assert scoring.assign_one_to_one([]) == []
    assert scoring.assign_one_to_one([[]]) == [None]


def test_best_score_and_summary():
    summary = scoring.summarize([0.40, 0.10, None], 0.25)
    assert summary["relations"] == 3
    assert summary["prefilled"] == 1
    assert summary["best_score"] == pytest.approx(0.40)
    assert summary["any_above"] is True


def test_summarize_all_below():
    summary = scoring.summarize([0.10, None], 0.25)
    assert summary["prefilled"] == 0
    assert summary["best_score"] == pytest.approx(0.10)
    assert summary["any_above"] is False


# --------------------------------------------------------------------------- #
# 按类别校准
# --------------------------------------------------------------------------- #

def test_class_calibrate_none_is_identity():
    scores = [[0.30, 0.20], [0.10, 0.40]]
    calibrated, stats = scoring.class_calibrate(scores, ["person", "dog"], "none")
    assert calibrated == scores
    assert calibrated is not scores          # 返回副本，调用方可安全改动
    assert stats == {}


def test_class_calibrate_znorm_uses_per_class_mean():
    # person 框对所有词都偏高，dog 框偏低；校准后应看「相对本类的突出程度」
    scores = [[0.40, 0.20], [0.30, 0.10]]
    calibrated, stats = scoring.class_calibrate(scores, ["person", "dog"], "znorm")
    assert stats["person"]["mean"] == pytest.approx(0.35)
    assert stats["dog"]["mean"] == pytest.approx(0.15)
    assert calibrated[0][0] == pytest.approx((0.40 - 0.35) / 0.05)
    assert calibrated[1][1] == pytest.approx((0.10 - 0.15) / 0.05)
    # 原始分数不变，校准只影响派生矩阵
    assert scores == [[0.40, 0.20], [0.30, 0.10]]


def test_class_calibrate_constant_class_degrades_to_centering():
    calibrated, stats = scoring.class_calibrate([[0.25, 0.50]], ["person", "dog"], "znorm")
    assert stats["person"]["std"] == 0.0
    assert calibrated[0][0] == pytest.approx(0.0)


def test_class_calibrate_keeps_none_and_unknown_method_raises():
    calibrated, _stats = scoring.class_calibrate([[None, 0.3]], ["person", "dog"], "znorm")
    assert calibrated[0][0] is None
    with pytest.raises(ValueError):
        scoring.class_calibrate([[0.1]], ["person"], "isotonic")


def test_class_calibrate_empty_matrix():
    assert scoring.class_calibrate([], []) == ([], {})


def test_class_prior_boosts_texts_matching_the_box_class():
    # 词「冲浪板」对 person 框 0.34、对 surfboard 框 0.31；
    # 类别名先验把「冲浪板 vs surfboard」的相近度加回去，指派随之翻到正确框。
    scores = [[0.34, 0.31]]
    winners = [["冲浪板", "冲浪板"]]
    text_vectors = {"冲浪板": [1.0, 0.0]}
    class_vectors = {"person": [0.0, 1.0], "surfboard": [0.9, 0.1]}
    calibrated, info = scoring.class_prior(
        scores, text_vectors, winners, ["person", "surfboard"], class_vectors,
        weight=0.2)
    assert info["method"] == "prior"
    assert info["weight"] == pytest.approx(0.2)
    assert calibrated[0][1] > calibrated[0][0]
    assert calibrated[0][0] == pytest.approx(0.34)
    assert calibrated[0][1] == pytest.approx(0.31 + 0.2 * scoring.cosine_similarity(
        [1.0, 0.0], [0.9, 0.1]))


def test_class_prior_leaves_unknown_class_or_text_untouched():
    scores = [[0.30, 0.20], [None, 0.25]]
    winners = [["cat", None], [None, "dog"]]
    text_vectors = {"cat": [1.0, 0.0], "dog": [0.0, 1.0]}
    calibrated, _info = scoring.class_prior(
        scores, text_vectors, winners, [None, "dog"], {"dog": [0.0, 1.0]},
        weight=0.2)
    assert calibrated[0][0] == pytest.approx(0.30)     # 框没有类别名 → 不加先验
    assert calibrated[0][1] == pytest.approx(0.20)     # 该词没有匹配文本 → 不加先验
    assert calibrated[1][0] is None                     # None 原样保留
    assert calibrated[1][1] == pytest.approx(0.45)      # dog 词 + dog 框 → 完全命中
