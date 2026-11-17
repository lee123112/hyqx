"""中文 ITN（utils/itn.py）与评分链路集成测试。

覆盖 round9 隔离复核确认的「同一数值、两种写法」误判场景：
百分比、亿/万数量、日期月份、小数；以及防过度归一的负例。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from utils import itn  # noqa: E402
from utils import scoring  # noqa: E402


# ------------------------------------------------------------------ 解析与格式化

@pytest.mark.parametrize("text,expected", [
    ("三十八", 38),
    ("十五", 15),
    ("十", 10),
    ("三千", 3000),
    ("一万", 10000),
    ("两万", 20000),
    ("十万", 100000),
    ("两亿", 200000000),
    ("一一五", 115),
    ("三点八", 3.8),
    ("一点一一五", 1.115),
    ("二点七七一", 2.771),
])
def test_parse_number_valid(text, expected):
    assert itn.parse_number(text) == expected


@pytest.mark.parametrize("text", ["", "三亿点", "ABC", "三点八五亿万", None])
def test_parse_number_invalid_returns_none(text):
    assert itn.parse_number(text) is None


def test_format_number_strips_trailing_zeros():
    assert itn.format_number(3.8) == "3.8"
    assert itn.format_number(30000) == "30000"
    assert itn.format_number(0.5) == "0.5"


# ------------------------------------------------------------------ 文本归一化

@pytest.mark.parametrize("source,expected,count", [
    # round9 隔离复核确认的数字写法误判场景
    ("三点八亿", "3.8亿", 1),
    ("起拍总价一点一一五亿元", "起拍总价1.115亿元", 1),
    ("总价约二点七七一亿元", "总价约2.771亿元", 1),
    ("环比六月下降百分之六点八", "环比6月下降6.8%", 2),
    ("同比增长百分之二点五", "同比增长2.5%", 1),
    ("千分之五", "0.5%", 1),
    ("万分之二点五", "0.025%", 1),
    ("起拍楼面价约一万元", "起拍楼面价约10000元", 1),
    ("起始价三千元", "起始价3000元", 1),
    ("而六月居冠", "而6月居冠", 1),
    ("十月五号", "10月5号", 2),
    # 已是阿拉伯数字 / 无数字：原样保留
    ("总价高达38亿元", "总价高达38亿元", 0),
    ("还比6月下降68", "还比6月下降68", 0),
    ("丈夫死了", "丈夫死了", 0),
    # 防过度归一：普通文字中的数字字不得改写
    ("十字路口", "十字路口", 0),
    ("一个人在家", "一个人在家", 0),
    ("万历年间", "万历年间", 0),
    ("五星红旗", "五星红旗", 0),
])
def test_to_arabic_numbers(source, expected, count):
    text, converted = itn.to_arabic_numbers(source)
    assert text == expected
    assert converted == count


def test_to_arabic_numbers_none_input():
    assert itn.to_arabic_numbers(None) == ("", 0)


# ------------------------------------------------------------------ 评分链路集成

def test_itn_eliminates_number_format_mismatch():
    """round9 实案：ASR「1.115亿」vs 标注「一点一一五亿」，ITN 后 CER=0。"""
    ratio = scoring.edit_distance_ratio("起拍总价1.115亿元", "起拍总价一点一一五亿元")
    assert ratio == 0.0
    # 显式关闭 ITN 恢复旧口径：写法差异计入 CER
    legacy = scoring.edit_distance_ratio("起拍总价1.115亿元", "起拍总价一点一一五亿元",
                                         inverse_text_normalization=False)
    assert legacy > 0.0


def test_itn_reduces_percent_format_cer_to_real_error_only():
    """round9 实案 BAC0148：数字写法归一后只剩真实同音错字（还/环）。"""
    ratio = scoring.edit_distance_ratio("还比6月下降68", "环比六月下降百分之六点八")
    assert ratio == pytest.approx(1.0 / 8.0)  # 归一后两侧均为「x比6月下降68」
    assert ratio <= 0.30  # 可通过 edit_distance_ratio_max=0.30 门禁


def test_itn_does_not_change_semantic_mismatch():
    """真实语义不符的文本，ITN 不影响门禁结论。"""
    ratio = scoring.edit_distance_ratio("今天天气晴朗", "丈夫死了")
    assert ratio == 1.0


def test_itn_never_raises_cer():
    """ITN 只豁免不抬分：含百分数短句取两口径较小值（本例非 ITN 更低 4/15<4/12）。"""
    left = "但让一百分之四占局市场主立位置"
    right = "但仍以百分之四占据市场主力位置"
    with_itn = scoring.edit_distance_ratio(left, right)
    without_itn = scoring.edit_distance_ratio(left, right, inverse_text_normalization=False)
    assert with_itn == without_itn
    assert with_itn == pytest.approx(4.0 / 15.0)


def test_text_error_metrics_audit_fields():
    metrics = scoring.text_error_metrics("起拍总价1.115亿元", "起拍总价一点一一五亿元")
    assert metrics["itn_applied"] is True
    assert metrics["itn_lowered_cer"] is True
    assert metrics["cer_without_itn"] > 0.0
    assert metrics["number_tokens_normalized"] >= 1
    assert metrics["cer"] == 0.0
    # 关闭 ITN：审计字段如实记录，CER 恢复旧口径
    legacy = scoring.text_error_metrics("起拍总价1.115亿元", "起拍总价一点一一五亿元",
                                        inverse_text_normalization=False)
    assert legacy["itn_applied"] is False
    assert legacy["itn_lowered_cer"] is False
    assert legacy["number_tokens_normalized"] == 0
    assert legacy["cer"] > 0.0


def test_wer_tokens_apply_itn():
    tokens = scoring._wer_tokens("环比增长百分之六点八")
    assert "6" in tokens or "6.8" in tokens or "68" in "".join(tokens)
