"""表1 v4.2 / J15-P15 的生产契约回归测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference
from tests import fixtures
from utils import scoring
from utils.schemas import ConfigError


def _run(tmp_path, record, **overrides):
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    return inference.run(str(entry / "demo.jsonl"), "task_v2_contract",
                         output_root=tmp_path / "out", allow_external_input=True,
                         overrides={"enabled": True, **overrides})


def _first_row(result):
    return json.loads(Path(result["outputFile"]).read_text(encoding="utf-8").splitlines()[0])


def test_enabled_route_is_rule_only_and_raw_cosine_cannot_gate(tmp_path, monkeypatch):
    def fail_probe(*_args, **_kwargs):
        raise AssertionError("v2 production route must not probe Wav2Vec2/BERT")

    monkeypatch.setattr(scoring, "probe_models", fail_probe)
    result = _run(tmp_path,
                  fixtures.make_pair("pair-eq", transcript="今天天气很好",
                                     description="今天天气很好"),
                  edit_distance_ratio_max=0.10)
    row = _first_row(result)
    node = row["processing"]["13"]
    assert result["statistics"]["models_available"] is False
    assert node["passed"] is True
    assert node["candidate_pool_eligible"] is True
    assert node["scoring_method"] == "character_edit_distance_ratio"
    assert node["cosine_similarity"] is None


def test_missing_transcript_is_pending_not_low_score(tmp_path):
    result = _run(tmp_path,
                  fixtures.make_pair("pair-missing", transcript="",
                                     description="今天天气很好"))
    pending = [json.loads(line) for line in Path(result["pendingFile"]).read_text(encoding="utf-8").splitlines() if line]
    assert result["statistics"]["pending_records"] == 1
    assert pending[0]["reason"] == "missing_transcript"
    assert pending[0]["audio_text"]["passed"] is None


def test_cosine_is_not_a_request_config(tmp_path):
    with pytest.raises(ConfigError):
        inference.load_config(overrides={"cosine_threshold": 0.25})
