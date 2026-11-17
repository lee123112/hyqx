"""语言适用性门：非中文音文对不得按中文 CER 判「语义不通」。

背景：上游 #8 固定按中文解码（表1），英文素材的 Whisper 转写会输出中文/混排文本，
与英文字幕描述做字符级 CER 必然虚高（实测时序窗口 CER 0.29–1.00），会被 #13 误隔离。
#13 的 `edit_distance_ratio_max` 是中文 CER 口径，对没有中文的描述没有语义，
因此按流程态判 `not_applicable`（不隔离、留产线）。
"""
from __future__ import annotations

from pathlib import Path

import inference
from tests import fixtures
from utils import audio_text


def test_cjk_detection_helpers():
    assert audio_text.cjk_char_count("今天天气很好") == 6
    assert audio_text.cjk_char_count("你好 Look Celia") == 2
    assert audio_text.cjk_char_count("You're a jerk, Thom.") == 0
    assert audio_text.cjk_char_count("") == 0
    assert audio_text.cjk_char_count(None) == 0
    assert audio_text.language_applicable("今天天气很好") is True
    assert audio_text.language_applicable("You're a jerk, Thom.") is False


def test_english_pair_not_applicable_not_isolated(tmp_path, degraded_config_path):
    """英文描述 + 中文/混排转写 → not_applicable，样本留在主产物、不进隔离库。"""
    media = fixtures.default_media()
    records = [
        fixtures.make_pair(
            "pair-en",
            transcript="你很聪明 Look Celia, we have to follow our passions",
            description="You're a jerk, Thom. Look Celia, we have to follow our passions;"),
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_lang_gate",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)

    stats = result["statistics"]
    assert result["status"] == "success"
    assert stats["language_not_applicable"] == 1
    assert stats["skipped_records"] == 1
    assert stats["passed_records"] == 0
    assert stats["sample_isolated"] == 0
    assert stats["edit_distance_exceeded"] == 0
    assert stats["scored_records"] == 0
    assert stats["conservation_ok"] is True

    rows = fixtures.read_jsonl(result["outputFile"])
    assert len(rows) == 1
    node = rows[0]["processing"]["13"]
    assert node["status"] == 1
    assert node["process_status"] == "not_applicable"
    assert node["passed"] is None
    assert node["reason"] == audio_text.REASON_LANGUAGE_NOT_APPLICABLE
    assert node["label"] == audio_text.LABEL_FLOW_LANGUAGE_NOT_APPLICABLE
    assert node["destination"] == audio_text.DEST_PIPELINE
    assert node["language_applicable"] is False
    assert node["description_cjk_chars"] == 0
    assert fixtures.read_jsonl(Path(result["runDir"]) / audio_text.SAMPLE_QUARANTINE_NAME) == []


def test_chinese_pair_still_scored(tmp_path, degraded_config_path):
    """中文描述不受语言门影响，仍按 CER 判定（通过）。"""
    media = fixtures.default_media()
    records = [fixtures.make_pair("pair-zh", transcript="今天天气很好", description="今天天气很好")]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_lang_zh",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    stats = result["statistics"]
    assert stats["language_not_applicable"] == 0
    assert stats["passed_records"] == 1
    assert stats["conservation_ok"] is True


def test_language_gate_can_be_disabled_for_comparison(tmp_path):
    """language_gate=false 回退纯 CER 口径：同一英文对会被判 edit_distance_exceeded。"""
    media = fixtures.default_media()
    records = [
        fixtures.make_pair(
            "pair-en",
            transcript="你很聪明",
            description="You're a jerk, Thom."),
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_lang_off",
                           output_root=tmp_path / "out", allow_external_input=True,
                           overrides={"enabled": True, "language_gate": False})
    stats = result["statistics"]
    assert stats["language_not_applicable"] == 0
    assert stats["sample_isolated"] == 1
    assert stats["edit_distance_exceeded"] == 1
    assert stats["conservation_ok"] is True


def test_language_gate_default_is_on():
    config = inference.load_config()
    assert config.language_gate is True
    assert config.business_dict()["language_gate"] is True
