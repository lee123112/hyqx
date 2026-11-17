"""[v4.2 §4.13] 表1 判定边界：编辑距离比例 <= 阈值通过，超过即不通过；裸余弦不参与。"""
from utils.scoring import decide_edit


def test_edit_ratio_equal_to_limit_passes():
    passed, reason = decide_edit(0.30, edit_ratio_max=0.30)
    assert passed is True and reason == "passed"


def test_edit_ratio_just_above_limit_fails():
    passed, reason = decide_edit(0.3001, edit_ratio_max=0.30)
    assert passed is False and reason == "edit_distance_exceeded"


def test_edit_ratio_below_limit_passes():
    assert decide_edit(0.299, edit_ratio_max=0.30) == (True, "passed")
