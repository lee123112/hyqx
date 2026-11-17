from utils.audio_text import resolve_transcript


def test_consumes_operator_08_chinese_transcript():
    record = {"audio": {"path": "a.wav"}, "audio_transcript": "今天学习中文",
              "transcription": {"status": "ok", "text": "今天学习中文"},
              "transcription_gate": {"direct_scoring_allowed": True}}
    assert resolve_transcript(record)["value"] == "今天学习中文"


def test_failed_operator_08_gate_blocks_stale_metadata_transcript():
    record = {"audio": {"path": "a.wav", "transcript": "旧转写"},
              "metadata": {"transcript": "旧转写"},
              "transcription": {"status": "failed", "text": None},
              "transcription_gate": {"direct_scoring_allowed": False}}
    assert resolve_transcript(record)["value"] is None
