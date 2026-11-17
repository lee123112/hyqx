"""同音容错编辑距离（`utils/pinyin.py`）专项测试。

覆盖：
1. 同音替换代价为 0、异音替换/插入/删除仍为 1；
2. 表外字符不猜读音（按普通替换处理）；
3. 比例口径：分母为描述侧字符数，任一侧为空返回 None；
4. 词典加载：包内表可用；`$MODEL_ROOT` 覆盖；表不可用时抛 PinyinTableError。
"""
from __future__ import annotations

import pytest

from utils import pinyin


@pytest.fixture()
def tiny_table():
    return {"张": "zhang", "章": "zhang", "工": "gong", "宫": "gong",
            "程": "cheng", "城": "cheng", "师": "shi", "诗": "shi",
            "完": "wan", "晚": "wan", "A": "a"}


def test_homophone_substitution_costs_nothing(tiny_table):
    # 张工程师 / 张宫城诗：全部同音替换 → 距离 0
    assert pinyin.homophone_distance("张工程师", "张宫城诗", tiny_table) == 0
    # 完全相同的串 → 0
    assert pinyin.homophone_distance("张工程师", "张工程师", tiny_table) == 0


def test_different_sounds_still_count(tiny_table):
    # 师(shi)→完(wan) 异音；其余同音 → 1
    assert pinyin.homophone_distance("张工程师", "张工程完", tiny_table) == 1
    # 插入/删除仍按 1 计（同音豁免只作用于替换）
    assert pinyin.homophone_distance("张工程师", "张工程", tiny_table) == 1
    assert pinyin.homophone_distance("张工程", "张工程师", tiny_table) == 1


def test_unknown_chars_are_not_guessed(tiny_table):
    # 表外字符按普通替换处理，不猜读音
    assert pinyin.homophone_distance("张工", "张X", tiny_table) == 1
    # 两个表外字符恰好相同 → 0
    assert pinyin.homophone_distance("张X", "张X", tiny_table) == 0


def test_ratio_uses_description_length(tiny_table):
    # 1 个异音替换（师→完）/ 描述 4 字
    assert pinyin.homophone_edit_ratio("张工程完", "张工程师", tiny_table) == pytest.approx(0.25)
    assert pinyin.homophone_edit_ratio("", "张工程师", tiny_table) is None
    assert pinyin.homophone_edit_ratio("张工程师", "", tiny_table) is None
    assert pinyin.homophone_edit_ratio(None, None, tiny_table) is None


def test_packaged_table_is_available_and_consistent():
    """随包词典必须可用，且能复现真实 Whisper 同音误写（tts-0007）。"""
    table, source, entries = pinyin.load_table()
    assert entries >= pinyin.MIN_TABLE_ENTRIES
    assert source.endswith("PinyinTable.txt")
    # 「傍晚护士在地铁临时排队」→「棒完互使在地铁临时排队」全部同音
    assert pinyin.homophone_distance("棒完互使在地铁临时排队",
                                     "傍晚护士在地铁临时排队", table) == 0
    # 「张工程师」→「张宫城师」全部同音
    assert pinyin.homophone_distance("张宫城师", "张工程师", table) == 0


def test_table_falls_back_to_model_root(tmp_path, monkeypatch):
    rows = "".join("%s\t%s\n" % (chr(0x4E00 + i), "aaa") for i in range(pinyin.MIN_TABLE_ENTRIES))
    model_root = tmp_path / "model"
    (model_root / "pinyin").mkdir(parents=True)
    (model_root / "pinyin" / "PinyinTable.txt").write_text(rows, encoding="utf-8")
    monkeypatch.setenv("MODEL_ROOT", str(model_root))
    monkeypatch.setattr(pinyin, "DEFAULT_TABLE_PATH", tmp_path / "missing.txt")
    pinyin._TABLE_CACHE.clear()
    try:
        table, source, entries = pinyin.load_table()
        assert entries == pinyin.MIN_TABLE_ENTRIES
        assert str(model_root) in source
    finally:
        monkeypatch.undo()
        pinyin._TABLE_CACHE.clear()


def test_unavailable_table_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pinyin, "DEFAULT_TABLE_PATH", tmp_path / "missing.txt")
    monkeypatch.delenv("MODEL_ROOT", raising=False)
    pinyin._TABLE_CACHE.clear()
    try:
        with pytest.raises(pinyin.PinyinTableError):
            pinyin.load_table()
        assert pinyin.table_available() is False
    finally:
        monkeypatch.undo()
        pinyin._TABLE_CACHE.clear()


def test_small_table_is_rejected(tmp_path, monkeypatch):
    """条目数低于下限（截断/损坏）视为不可用，不用于判定。"""
    path = tmp_path / "PinyinTable.txt"
    path.write_text("张\tzhang\n章\tzhang\n", encoding="utf-8")
    monkeypatch.setattr(pinyin, "DEFAULT_TABLE_PATH", path)
    monkeypatch.delenv("MODEL_ROOT", raising=False)
    pinyin._TABLE_CACHE.clear()
    try:
        with pytest.raises(pinyin.PinyinTableError):
            pinyin.load_table()
    finally:
        monkeypatch.undo()
        pinyin._TABLE_CACHE.clear()
