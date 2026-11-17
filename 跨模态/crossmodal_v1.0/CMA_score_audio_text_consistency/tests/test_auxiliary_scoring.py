"""#13 auxiliary Whisper compatibility score contract tests."""
from __future__ import annotations

from tests import fixtures
from utils import scoring
from utils.schemas import OperatorConfig
import inference


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


def test_auxiliary_score_is_diagnostic_only(tmp_path, degraded_config_path):
    """[v4.2 §4.13] 判定只看 CER/编辑距离；裸余弦与辅助相似度均为诊断字段。"""
    media = fixtures.default_media()
    records = [fixtures.make_flat_record(
        "aux-pass", transcript="今天天气很好", description="今天天气很好")]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    aux = StubAuxiliaryScorer(0.80)
    result = __import__("inference").run(
        str(entry / "demo.jsonl"), "aux-task_f1", config_path=str(degraded_config_path),
        output_root=tmp_path / "out", allow_external_input=True,
        scorer=fixtures.StubScorer(cosine=0.01), auxiliary_scorer=aux)
    # 编辑距离 0 → 通过进主产物；裸余弦 0.01 不参与判定
    rows = fixtures.read_jsonl(result["outputFile"])
    node = rows[0]["processing"]["13"]
    assert node["passed"] is True
    assert node["raw_cosine_similarity"] == 0.01
    assert node["diag_cosine"] == 0.01
    assert node["cosine_source"] == "diagnostic_only"
    # [v4.2 §4.13] Whisper 转写复用上游产物，本算子不加载辅助模型
    assert node["auxiliary_similarity"] is None
    assert aux.calls == 0


def test_auxiliary_score_below_threshold_keeps_raw_failure(tmp_path, degraded_config_path):
    """辅助分低也不能改变 CER 结论；真正的失败来源是编辑距离超阈值。"""
    media = fixtures.default_media()
    records = [fixtures.make_flat_record(
        "aux-fail", transcript="完全不同的一段无关内容", description="今天天气很好")]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    aux = StubAuxiliaryScorer(0.20)
    result = __import__("inference").run(
        str(entry / "demo.jsonl"), "aux-task_f1", config_path=str(degraded_config_path),
        output_root=tmp_path / "out", allow_external_input=True,
        scorer=fixtures.StubScorer(cosine=0.01), auxiliary_scorer=aux)
    rows = fixtures.read_jsonl(result["sampleQuarantineFile"])
    node = rows[0]["record"]["processing"]["13"]
    assert node["passed"] is False
    assert node["reason"] == "edit_distance_exceeded"
    assert node["raw_cosine_similarity"] == 0.01
    assert node["diag_cosine"] == 0.01
    assert node["cosine_source"] == "diagnostic_only"


def test_whisper_similarity_rejects_invalid_value():
    assert scoring.validate_auxiliary_similarity(0.25) == 0.25
    try:
        scoring.validate_auxiliary_similarity(1.1)
    except scoring.ScoreError:
        pass
    else:
        raise AssertionError("out-of-range auxiliary score must fail explicitly")


def test_master_switch_off_never_loads_auxiliary(monkeypatch):
    config = OperatorConfig(enabled=False, auxiliary_enabled=True)

    def fail_if_loaded(_config):
        raise AssertionError("auxiliary model must not load while route is disabled")

    monkeypatch.setattr(scoring, "load_whisper_compatibility_scorer", fail_if_loaded)
    result = inference._resolve_auxiliary_model(config, None)
    assert result["source"] == "disabled"
    assert result["scorer"] is None
