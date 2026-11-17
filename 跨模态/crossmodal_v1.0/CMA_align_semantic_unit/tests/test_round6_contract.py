from __future__ import annotations

import types
from pathlib import Path

import numpy as np
import torch

from utils import transcription as tr


def test_transcribe_retries_uncovered_tail_and_preserves_offsets(monkeypatch, tmp_path):
    """A long silent gap must not make Whisper output only the first paragraph."""
    settings = tr.TranscriptionSettings(
        master_enabled=True, switch_enabled=True, model="whisper-base", language="zh",
        model_root="weight", model_dir="whisper-base", device="cpu",
        max_segment_seconds=30.0, paragraph_silence_seconds=0.6, beam_size=1,
        # 本用例验证「尾部重试」逻辑，音轨用零波形（静音）构造，
        # 关闭预检以免在进入解码前被判 audio_silent。
        precheck=False,
    )
    transcriber = tr.WhisperTranscriber(settings)
    transcriber.loaded = True
    transcriber.device = "cpu"
    calls = []

    class FakeProcessor:
        def __call__(self, audio, *_args, **_kwargs):
            calls.append(len(audio))
            return types.SimpleNamespace(input_features=torch.zeros((1, 80, 20)))

    transcriber._processor = FakeProcessor()

    def fake_generate(_features, prompt_ids=None):
        if len(calls) == 1:
            return [{"start": 0.0, "end": 4.0, "text": "第一段"}]
        return [{"start": 0.0, "end": 3.0, "text": "第二段"}]

    transcriber._generate_segments = fake_generate  # type: ignore[method-assign]
    waveform = np.zeros(10 * tr.WHISPER_SAMPLE_RATE, dtype="float32")
    monkeypatch.setattr(tr, "read_audio", lambda _path: (waveform, tr.WHISPER_SAMPLE_RATE))

    result = transcriber.transcribe(Path(tmp_path) / "two_paragraphs.wav")
    assert result["status"] == tr.STATUS_OK
    assert len(result["segments"]) >= 2
    assert result["segments"][-1]["start"] >= 4.5
    assert len(calls) >= 2

