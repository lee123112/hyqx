"""同音容错表缺失：不得静默降级为严格字符 CER。

背景：同音容错开启时，严格字符 CER 会把同音误写（张工程师→张宫城师）判成
「语义不通」，实测正样本最差 0.5714、默认阈值 0.30——拼音表一旦缺失，这类
正样本会被成批误隔离。因此：
1. 运行前快速失败（ConfigError），不写任何产物；
2. 若运行期表仍不可用，严格口径已超阈值的记录判 `not_applicable`
   （reason=`pinyin_table_unavailable`，留产线、不隔离），并写显式标记；
3. 严格口径本就在阈值内的记录照常通过，但同样带标记供审计。
"""
from __future__ import annotations

import pytest

import inference
from tests import fixtures
from utils import audio_text, pinyin

# 全部同音：严格字符 CER = 0.4（2/5），同音口径 = 0
TRANSCRIPT = "张宫城师"
DESCRIPTION = "张工程师"


def _break_table(monkeypatch, tmp_path):
    monkeypatch.setattr(pinyin, "DEFAULT_TABLE_PATH", tmp_path / "missing.txt")
    monkeypatch.delenv("MODEL_ROOT", raising=False)
    pinyin._TABLE_CACHE.clear()


def _run(tmp_path, records, unique_id, overrides=None):
    media = fixtures.default_media()
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    return inference.run(str(entry / "demo.jsonl"), unique_id,
                         output_root=tmp_path / "out", allow_external_input=True,
                         overrides=overrides or {"enabled": True})


def test_run_fails_fast_when_table_missing(tmp_path, monkeypatch):
    """开启同音容错但拼音表不可用：写产物前抛 ConfigError。"""
    _break_table(monkeypatch, tmp_path)
    records = [fixtures.make_pair("pair-1", transcript=TRANSCRIPT,
                                  description=DESCRIPTION)]
    with pytest.raises(inference.ConfigError, match="拼音表不可用"):
        _run(tmp_path, records, "task_table_missing")
    # 快速失败不应留下运行目录（未开始处理）
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").rglob("13_*"))


def test_table_missing_does_not_isolate_same_sound_pair(tmp_path, monkeypatch):
    """运行期表不可用：同音误写判 not_applicable，不隔离、不计隔离台账。"""
    monkeypatch.setattr(pinyin, "table_available", lambda: True)

    def _missing():
        raise pinyin.PinyinTableError("拼音表不可用：stub")

    monkeypatch.setattr(pinyin, "load_table", _missing)
    records = [fixtures.make_pair("pair-1", transcript=TRANSCRIPT,
                                  description=DESCRIPTION)]
    result = _run(tmp_path, records, "task_table_runtime")

    stats = result["statistics"]
    assert result["status"] == "success"
    assert stats["pinyin_table_unavailable"] == 1
    assert stats["skipped_records"] == 1
    assert stats["sample_isolated"] == 0
    assert stats["edit_distance_exceeded"] == 0
    assert stats["scored_records"] == 0
    assert stats["conservation_ok"] is True

    rows = fixtures.read_jsonl(result["outputFile"])
    node = rows[0]["processing"]["13"]
    assert node["status"] == 1
    assert node["process_status"] == "not_applicable"
    assert node["passed"] is None
    assert node["reason"] == audio_text.REASON_PINYIN_TABLE_UNAVAILABLE
    assert node["label"] == audio_text.LABEL_FLOW_PINYIN_TABLE_UNAVAILABLE
    assert node["destination"] == audio_text.DEST_PIPELINE
    assert node["pinyin_table_unavailable"] is True
    assert node["pinyin_table_error"].startswith("拼音表不可用")


def test_table_missing_clean_pair_still_passes_with_flag(tmp_path, monkeypatch):
    """严格口径本就在阈值内：照常通过，但显式写表缺失标记，便于审计。"""
    monkeypatch.setattr(pinyin, "table_available", lambda: True)

    def _missing():
        raise pinyin.PinyinTableError("拼音表不可用：stub")

    monkeypatch.setattr(pinyin, "load_table", _missing)
    records = [fixtures.make_pair("pair-1", transcript="今天天气很好",
                                  description="今天天气很好")]
    result = _run(tmp_path, records, "task_table_clean")

    stats = result["statistics"]
    assert stats["passed_records"] == 1
    assert stats["pinyin_table_unavailable"] == 0
    assert stats["sample_isolated"] == 0
    rows = fixtures.read_jsonl(result["outputFile"])
    node = rows[0]["processing"]["13"]
    assert node["passed"] is True
    assert node["pinyin_table_unavailable"] is True


def test_table_missing_falls_back_to_strict_when_tolerance_disabled(tmp_path, monkeypatch):
    """显式关闭同音容错：不检查表，按严格 CER 判定（既有口径不变）。"""
    _break_table(monkeypatch, tmp_path)
    records = [fixtures.make_pair("pair-1", transcript=TRANSCRIPT,
                                  description=DESCRIPTION)]
    result = _run(tmp_path, records, "task_table_off",
                  overrides={"enabled": True, "homophone_tolerance": False})

    stats = result["statistics"]
    assert stats["sample_isolated"] == 1
    assert stats["edit_distance_exceeded"] == 1
    assert stats["pinyin_table_unavailable"] == 0
