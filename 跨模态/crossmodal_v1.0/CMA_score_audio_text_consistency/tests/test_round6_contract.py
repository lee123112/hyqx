from __future__ import annotations

from pathlib import Path

import pytest

import inference
from tests import fixtures


class StubAuxiliaryScorer:
    model_id = "whisper-base"
    model_version = "test-whisper"
    device = "cpu"

    def __init__(self, value: float):
        self.value = float(value)
        self.calls = 0

    def score(self, waveform, sample_rate: int, text: str):
        self.calls += 1
        return {
            "similarity": self.value,
            "model": self.model_id,
            "model_version": self.model_version,
            "device": self.device,
        }


@pytest.mark.skip(reason="v4.2 §4.13 反转：裸余弦降为诊断字段（diag_cosine），不再作为正式门禁")
def test_raw_cosine_remains_formal_gate_when_whisper_is_high(tmp_path, degraded_config_path):
    """Whisper may be evidence, but it cannot replace the frozen raw cosine gate."""
    entry = fixtures.build_input_tree(
        tmp_path / "in",
        [fixtures.make_flat_record(
            "raw-gate", transcript="今天天气很好", description="今天天气很好")],
        media=fixtures.default_media(),
    )
    aux = StubAuxiliaryScorer(0.80)
    result = inference.run(
        str(entry / "demo.jsonl"), "r6_raw_gate", config_path=str(degraded_config_path),
        output_root=tmp_path / "out", allow_external_input=True,
        scorer=fixtures.StubScorer(cosine=0.01), auxiliary_scorer=aux,
    )
    rows = fixtures.read_jsonl(result["sampleQuarantineFile"])
    if not rows:
        rows = fixtures.read_jsonl(result["outputFile"])
        node = rows[0]["processing"]["13"]
    else:
        node = rows[0]["record"]["processing"]["13"]
    assert node["passed"] is False
    assert node["raw_cosine_similarity"] == 0.01
    assert node["cosine_similarity"] == 0.01
    assert node["cosine_source"] == "wav2vec2_bert"
    assert node["auxiliary_similarity"] == 0.80
    assert aux.calls == 1

