"""转写可靠性门：ASR 故障（解码循环/语速超物理上限）不得被判成「语义不通」。

背景：上游 #8 的 Whisper-base 在音乐、噪声、低信噪比片段上会出现解码重复循环。
实测时序窗口用 4.88s 音频解码出 453 字（93 字/秒），另一条 5.52s 音频解码出
"你不想要我" 重复 28 次。这类转写与描述做字符级 CER 必然虚高，但度量的是
ASR 故障程度，不是音文语义差异，因此按流程态判 `not_applicable`（不隔离、
留产线，由 #15 时序等下游证据继续判定）。
"""
from __future__ import annotations

from pathlib import Path

import inference
from tests import fixtures
from utils import audio_text


def test_detect_repetition_loop_helpers():
    loop = audio_text.detect_repetition_loop("你不想要我 你不想要我 你不想要我 "
                                             "你不想要我 你不想要我 你不想要我")
    assert loop is not None
    assert loop["unit"] == "你不想要我"
    assert loop["times"] == 6
    # 5 次（不足 6 次）不算循环；正常语句不误报
    assert audio_text.detect_repetition_loop("你不想要我 你不想要我 你不想要我 你不想要我 你不想要我") is None
    assert audio_text.detect_repetition_loop("夏天，同事在实验室提前采集样本。") is None
    assert audio_text.detect_repetition_loop("") is None
    assert audio_text.detect_repetition_loop(None) is None


def test_transcript_reliability_metrics():
    # 1.2 秒音频里 43 个字 → 35.8 字/秒，超出物理上限
    long_text = "今天天气很好我们一起去公园散步吧大家都很开心这是美好的一天阳光明媚空气清新鸟儿在歌唱"
    result = audio_text.transcript_reliability(long_text, 1.2)
    assert result["reliable"] is False
    assert result["reasons"] == ["speech_rate_exceeded"]
    assert result["chars_per_second"] > 30.0

    # 时长未知时不按语速判，也不误伤
    assert audio_text.transcript_reliability("今天天气很好", None)["reliable"] is True
    assert audio_text.transcript_reliability("今天天气很好", 1.2)["reliable"] is True


def test_repetition_loop_not_isolated(tmp_path, degraded_config_path):
    """解码循环 → not_applicable：不进隔离库、留在主产物。"""
    media = fixtures.default_media()
    records = [fixtures.make_pair("pair-loop", transcript="好好好好好好",
                                  description="今天天气很好")]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_loop_gate",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)

    stats = result["statistics"]
    assert result["status"] == "success"
    assert stats["transcript_unreliable"] == 1
    assert stats["skipped_records"] == 1
    assert stats["sample_isolated"] == 0
    assert stats["edit_distance_exceeded"] == 0
    assert stats["passed_records"] == 0
    assert stats["conservation_ok"] is True

    rows = fixtures.read_jsonl(result["outputFile"])
    assert len(rows) == 1
    node = rows[0]["processing"]["13"]
    assert node["status"] == 1
    assert node["process_status"] == "not_applicable"
    assert node["passed"] is None
    assert node["reason"] == audio_text.REASON_TRANSCRIPT_UNRELIABLE
    assert node["label"] == audio_text.LABEL_FLOW_TRANSCRIPT_UNRELIABLE
    assert node["destination"] == audio_text.DEST_PIPELINE
    assert node["transcript_reliable"] is False
    assert node["transcript_repetition"]["unit"] == "好"
    assert "repetition_loop" in node["transcript_unreliable_reasons"]
    assert fixtures.read_jsonl(Path(result["runDir"]) / audio_text.SAMPLE_QUARANTINE_NAME) == []


def test_speech_rate_exceeded_not_isolated(tmp_path, degraded_config_path):
    """语速超出物理上限（无重复）→ 即使 CER=0 也不判通过，判 not_applicable。"""
    media = fixtures.default_media()
    long_text = "今天天气很好我们一起去公园散步吧大家都很开心这是美好的一天阳光明媚空气清新鸟儿在歌唱"
    records = [fixtures.make_pair("pair-rate", transcript=long_text, description=long_text)]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_rate_gate",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)

    stats = result["statistics"]
    assert stats["transcript_unreliable"] == 1
    assert stats["passed_records"] == 0
    assert stats["sample_isolated"] == 0
    assert stats["conservation_ok"] is True
    rows = fixtures.read_jsonl(result["outputFile"])
    node = rows[0]["processing"]["13"]
    assert node["reason"] == audio_text.REASON_TRANSCRIPT_UNRELIABLE
    assert node["transcript_unreliable_reasons"] == ["speech_rate_exceeded"]
    assert node["transcript_repetition"] is None


def test_reliability_gate_can_be_disabled_for_comparison(tmp_path):
    """transcript_reliability_gate=false 回退纯 CER 口径：同一解码循环会被隔离。"""
    media = fixtures.default_media()
    records = [fixtures.make_pair("pair-loop", transcript="好好好好好好",
                                  description="今天天气很好")]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_loop_off",
                           output_root=tmp_path / "out", allow_external_input=True,
                           overrides={"enabled": True, "transcript_reliability_gate": False})
    stats = result["statistics"]
    assert stats["transcript_unreliable"] == 0
    assert stats["sample_isolated"] == 1
    assert stats["edit_distance_exceeded"] == 1
    assert stats["conservation_ok"] is True
    # 关闭门只影响判定，不影响证据字段：转写仍被标记为不可靠
    entry_row = fixtures.read_jsonl(Path(result["runDir"])
                                    / audio_text.SAMPLE_QUARANTINE_NAME)[0]
    assert entry_row["audio_text"]["transcript_reliable"] is False


def test_reliable_transcript_still_scored(tmp_path, degraded_config_path):
    """正常转写不受影响：照常按 CER 判定并记录语速证据。"""
    media = fixtures.default_media()
    records = [fixtures.make_pair("pair-ok", transcript="今天天气很好",
                                  description="今天天气很好")]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_ok_gate",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    stats = result["statistics"]
    assert stats["passed_records"] == 1
    assert stats["transcript_unreliable"] == 0
    node = fixtures.read_jsonl(result["outputFile"])[0]["processing"]["13"]
    assert node["transcript_reliable"] is True
    assert node["transcript_chars_per_second"] == round(6 / 1.2, 3)


def test_reliability_gate_default_is_on():
    config = inference.load_config()
    assert config.transcript_reliability_gate is True
    assert config.max_chars_per_second == 30.0
    assert config.business_dict()["transcript_reliability_gate"] is True
