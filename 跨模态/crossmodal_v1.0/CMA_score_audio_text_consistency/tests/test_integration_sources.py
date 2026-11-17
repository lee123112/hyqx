"""Integration regressions for verified transcripts and M09/M11 records.

These tests use generated audio only; they do not load speech or CLIP models.
"""
import pytest

import inference
from tests import fixtures
from utils import audio_text


def official_record(reason="transcription_disabled"):
    return {
        "sample_id": "AISHELL-3_SSB00050001",
        "audio": {"path": "media/whole.wav", "duration": 2.0},
        "metadata": {
            "dataset_id": "kaggle_aishell3_01",
            "official_text": "在这个迎新的日子里",
            "transcription_producer": "AISHELL-3 official metadata",
            "language": "zh",
        },
        "transcription_gate": {"direct_scoring_allowed": False, "reason": reason},
    }


@pytest.mark.parametrize("reason", ["transcription_disabled", "master_switch_off"])
def test_verified_official_text_replaces_only_inactive_asr(reason):
    transcript = audio_text.resolve_transcript(official_record(reason))
    assert transcript["value"] == "在这个迎新的日子里"
    assert transcript["source"] == "metadata.official_text"
    assert transcript["model"] == "dataset_official_transcript"
    assert transcript["scope"] == "whole_audio"
    assert transcript["upstream_gate_reason"] == reason


@pytest.mark.parametrize("reason", ["audio_decode_failed", "low_snr", "no_audio_track",
                                   "review_required", "unknown_failure", None])
def test_official_text_never_bypasses_a_real_or_unknown_gate(reason):
    record = official_record(reason)
    record["audio_transcript"] = "不能绕过门禁"
    assert audio_text.resolve_transcript(record) == {
        "value": None, "source": "transcription_gate"}


@pytest.mark.parametrize("field,value", [
    ("dataset_id", "unverified_dataset"),
    ("transcription_producer", "unknown producer"),
    ("official_text", "  "),
])
def test_unverified_official_metadata_remains_pending(field, value):
    record = official_record()
    record["metadata"][field] = value
    assert audio_text.resolve_transcript(record)["value"] is None


def test_official_text_requires_matching_sample_and_whole_audio():
    record = official_record()
    record["sample_id"] = "unverified_001"
    assert audio_text.resolve_transcript(record)["value"] is None
    record = official_record()
    record["audio"] = {"segments": [{"path": "media/part.wav"}]}
    assert audio_text.resolve_transcript(record)["value"] is None


def test_upstream_asr_keeps_priority_when_gate_allows_scoring():
    record = official_record()
    record["transcription_gate"] = {"direct_scoring_allowed": True}
    record["transcription"] = {"text": "真实转写优先", "model": "whisper-base"}
    transcript = audio_text.resolve_transcript(record)
    assert transcript["value"] == "真实转写优先"
    assert transcript["source"] == "transcription.text"


def test_whole_official_text_is_not_compared_with_first_audio_segment():
    record = official_record()
    record["audio"]["segments"] = [
        {"path": "media/part.wav", "start": 0.0, "end": 0.5}]
    ref = audio_text.resolve_audio_reference(record)
    assert ref["path"] == "media/whole.wav"
    assert ref["end"] == 2.0


def test_segment_asr_keeps_priority_and_its_segment_audio_reference():
    record = official_record()
    record["transcription_gate"] = {"direct_scoring_allowed": True}
    record["audio"]["segments"] = [
        {"path": "media/part.wav", "start": 0.0, "end": 0.5, "transcript": "段落转写"}]
    assert audio_text.resolve_transcript(record)["source"] == "audio.segments[0].transcript"
    assert audio_text.resolve_audio_reference(record)["path"] == "media/part.wav"


@pytest.mark.parametrize("audio_side", ["left", "right"])
def test_m11_nested_record_reads_m09_selected_description(audio_side):
    text_record = {"sample_id": "text-01", "description": "在这个迎新的日子里",
                   "description_source": "metadata.official_text",
                   "text": {"content": "旧的原始文本"}}
    sides = [{"record": official_record()}, {"record": text_record}]
    if audio_side == "right":
        sides.reverse()
    spec = audio_text.resolve_pair({"pair_id": "pair-01", "left": sides[0], "right": sides[1]})
    assert spec["description"] == {"value": "在这个迎新的日子里", "source": "description",
                                   "origin": "metadata.official_text"}
    assert spec["transcript"]["source"] == "metadata.official_text"


def test_blocked_audio_side_cannot_borrow_other_side_transcript():
    record = {"pair_id": "blocked-pair", "left": {"record": official_record("low_snr")},
              "right": {"record": {"sample_id": "text-side", "audio_transcript": "不能借用",
                                    "description": "不能借用"}}}
    assert audio_text.resolve_pair(record)["transcript"] == {
        "value": None, "source": "transcription_gate"}


def test_m09_selected_text_supports_legacy_string_text():
    assert audio_text.resolve_description({"text": "传统文本字段"}) == {
        "value": "传统文本字段", "source": "text"}


def test_official_m09_pair_scores_with_source_audit_and_existing_metrics(tmp_path):
    record = official_record()
    record["description"] = record["metadata"]["official_text"]
    record["description_source"] = "metadata.official_text"
    base = fixtures.build_input_tree(tmp_path / "input", [record],
                                    media={"media/whole.wav": fixtures.wav_bytes(duration=2.0)})
    result = inference.run(str(base / "demo.jsonl"), "official_audit",
                           output_root=tmp_path / "out", overrides={"enabled": True},
                           allow_external_input=True)
    node = fixtures.read_jsonl(result["outputFile"])[0]["processing"]["13"]
    assert node["passed"] is True
    assert node["transcript_source"] == "metadata.official_text"
    assert node["transcript_model"] == "dataset_official_transcript"
    assert node["transcript_source_type"] == "dataset_official"
    assert node["transcript_producer"] == record["metadata"]["transcription_producer"]
    assert node["transcript_upstream_gate_reason"] == "transcription_disabled"
    assert node["description_source"] == "description"
    assert node["description_origin"] == "metadata.official_text"
    assert node["metric"] == "CER"
    assert node["cer"] == 0.0
    assert node["edit_distance_ratio_max"] == 0.30
    assert node["itn_applied"] is True
    assert node["traditional_to_simplified"] is True
    assert node["homophone_tolerance"] is True
    assert node["edit_distance_ratio_homophone"] == 0.0
    assert result["statistics"]["conservation_ok"] is True


def test_no_audio_has_no_score_or_passed_conclusion():
    config = inference.load_config(overrides={"enabled": True})
    spec = audio_text.resolve_pair({"sample_id": "image-only", "text": {"content": "图片描述"}})
    spec["record"] = {"sample_id": "image-only"}
    result = audio_text.evaluate_record(spec, 1, media_roots=[], config=config,
                                        scorer=None, event_time="2026-11-09T12:00:00+08:00")
    assert result["reason"] == "no_audio_text_pair"
    assert result["node"]["process_status"] == "pending"
    assert result["node"]["passed"] is None
    assert result["node"]["cer"] is None
    assert result["node"]["candidate_pool_eligible"] is False
