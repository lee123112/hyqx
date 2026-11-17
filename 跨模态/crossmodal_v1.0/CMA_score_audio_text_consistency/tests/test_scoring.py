"""专项算法与模型适配层测试。

覆盖：文本规范化、Levenshtein 与编辑距离比例、余弦相似度、真实 WAV 读取与重采样、
判定规则边界，以及**真实** transformers 本地权重加载与前向（微型权重，仅验契约）。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from tests import fixtures
from utils import audio as audio_mod
from utils import scoring


# --------------------------------------------------------------------------- #
# 文本规范化 / 编辑距离
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("Hello， 世界!", "hello世界"),
    ("  多云  转晴  ", "多云转晴"),
    ("ＡＢＣ１２３", "abc123"),
    ("", ""),
    ("，。！？；：", ""),
])
def test_normalize_text(raw, expected):
    assert scoring.normalize_text(raw) == expected


@pytest.mark.parametrize("left,right,distance", [
    ("abc", "abc", 0),
    ("abc", "abd", 1),
    ("", "abc", 3),
    ("kitten", "sitting", 3),
    ("中文测试", "中文测试", 0),
    ("今天天气很好", "今天天气不错", 2),
])
def test_levenshtein(left, right, distance):
    assert scoring.levenshtein(left, right) == distance


@pytest.mark.parametrize("left,right,ratio", [
    ("abcd", "abcd", 0.0),
    ("abcd", "abce", 0.25),
    ("abcd", "xxxx", 1.0),
    ("", "abcd", 1.0),
    ("", "", 0.0),
    ("今天天气很好", "今天天气很好", 0.0),
])
def test_edit_distance_ratio(left, right, ratio):
    assert scoring.edit_distance_ratio(left, right) == pytest.approx(ratio)


def test_edit_distance_ratio_ignores_punctuation_and_case():
    assert scoring.edit_distance_ratio("Hello, 世界!", "hello世界") == pytest.approx(0.0)


def test_edit_distance_ratio_is_symmetric():
    assert scoring.edit_distance_ratio("散步吧", "去散步") == pytest.approx(
        scoring.edit_distance_ratio("去散步", "散步吧"))


# --------------------------------------------------------------------------- #
# 余弦相似度
# --------------------------------------------------------------------------- #

def test_cosine_similarity_identical_is_one():
    vector = np.array([0.2, -0.4, 0.6], dtype="float32")
    assert scoring.cosine_similarity(vector, vector) == pytest.approx(1.0, abs=1e-6)


def test_cosine_similarity_orthogonal_is_zero():
    left = np.array([1.0, 0.0], dtype="float32")
    right = np.array([0.0, 1.0], dtype="float32")
    assert scoring.cosine_similarity(left, right) == pytest.approx(0.0, abs=1e-6)


def test_cosine_similarity_rejects_zero_vector():
    with pytest.raises(scoring.ScoreError):
        scoring.cosine_similarity(np.zeros(4, dtype="float32"), np.ones(4, dtype="float32"))


def test_cosine_similarity_rejects_dimension_mismatch():
    with pytest.raises(scoring.ScoreError):
        scoring.cosine_similarity(np.ones(4, dtype="float32"), np.ones(5, dtype="float32"))


# --------------------------------------------------------------------------- #
# 判定规则边界
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("edit_ratio,passed,reason", [
    (0.0, True, "passed"),
    (0.2999, True, "passed"),
    (0.30, True, "passed"),              # 不超过阈值即通过（<= 0.30）
    (0.3001, False, "edit_distance_exceeded"),
    (0.90, False, "edit_distance_exceeded"),
])
def test_decide_edit_rule_boundaries(edit_ratio, passed, reason):
    """[v4.2 §4.13] 判定只看 CER/编辑距离比例，裸余弦不参与。"""
    verdict, why = scoring.decide_edit(edit_ratio, edit_ratio_max=0.30)
    assert verdict is passed
    assert why == reason


# --------------------------------------------------------------------------- #
# 音频读取 / 重采样
# --------------------------------------------------------------------------- #

def test_read_wav_mono_16k(tmp_path):
    path = fixtures.write_wav(tmp_path / "a.wav", duration=0.5, freq=200.0)
    waveform, sample_rate = audio_mod.read_wav(path)
    assert sample_rate == fixtures.SAMPLE_RATE
    assert waveform.dtype == np.float32
    assert waveform.ndim == 1
    assert waveform.shape[0] == pytest.approx(0.5 * fixtures.SAMPLE_RATE, abs=2)
    assert float(np.max(np.abs(waveform))) > 0.05


def test_read_wav_downmixes_stereo(tmp_path):
    path = fixtures.write_wav(tmp_path / "stereo.wav", duration=0.3, channels=2)
    waveform, sample_rate = audio_mod.read_wav(path)
    assert sample_rate == fixtures.SAMPLE_RATE
    assert waveform.ndim == 1
    assert waveform.shape[0] == pytest.approx(0.3 * fixtures.SAMPLE_RATE, abs=2)


def test_read_wav_rejects_silence(tmp_path):
    path = fixtures.write_wav(tmp_path / "silent.wav", duration=0.3, silent=True)
    with pytest.raises(audio_mod.AudioReadError):
        audio_mod.read_wav(path)


def test_read_wav_rejects_garbage(tmp_path):
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not-a-wave-file" * 8)
    with pytest.raises(audio_mod.AudioReadError):
        audio_mod.read_wav(path)


def test_read_wav_rejects_missing_file(tmp_path):
    with pytest.raises(audio_mod.AudioReadError):
        audio_mod.read_wav(tmp_path / "nope.wav")


def test_resample_linear_length_and_preserves_shape():
    source = np.sin(np.linspace(0, 20.0, 8000)).astype("float32")
    result = audio_mod.resample_linear(source, 8000, 16000)
    assert result.shape[0] == 16000
    assert result.dtype == np.float32


def test_resample_linear_is_identity_when_rates_equal():
    source = np.linspace(-1.0, 1.0, 100).astype("float32")
    result = audio_mod.resample_linear(source, 16000, 16000)
    assert np.allclose(result, source)


def test_load_audio_mono_truncates_max_seconds(tmp_path):
    path = fixtures.write_wav(tmp_path / "long.wav", duration=2.0)
    waveform, sample_rate, meta = audio_mod.load_audio_mono(
        path, target_rate=fixtures.SAMPLE_RATE, max_seconds=0.5)
    assert sample_rate == fixtures.SAMPLE_RATE
    assert waveform.shape[0] == pytest.approx(0.5 * fixtures.SAMPLE_RATE, abs=2)
    assert meta["truncated"] is True
    assert meta["source_duration"] == pytest.approx(2.0, abs=0.05)


# --------------------------------------------------------------------------- #
# 运行环境探测
# --------------------------------------------------------------------------- #

def test_probe_runtime_reports_real_availability():
    runtime = scoring.probe_runtime()
    assert set(["torch", "transformers", "numpy"]).issubset(runtime)
    assert isinstance(runtime["torch"], bool)
    assert isinstance(runtime["versions"], dict)


def test_probe_models_reports_missing_when_absent(tmp_path):
    report = scoring.probe_models(tmp_path / "absent", "wav2vec2-base", "bert-base-chinese")
    assert report["available"] is False
    assert report["missing"]
    assert report["audio_path"] is None and report["text_path"] is None


def test_probe_models_reports_available_for_tiny_models(tiny_models):
    report = scoring.probe_models(tiny_models["root"], tiny_models["audio_dir"],
                                  tiny_models["text_dir"])
    assert report["available"] is True
    assert report["missing"] == []
    assert report["audio_path"] and report["text_path"]


# --------------------------------------------------------------------------- #
# 真实 transformers 本地权重适配（微型权重，仅验证契约）
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def real_scorer(tiny_models):
    """真实加载微型本地权重（不使用任何测试替身）。"""
    return scoring.AudioTextScorer.load(
        tiny_models["audio_path"], tiny_models["text_path"], device="cpu")


def test_scorer_loads_local_weights_without_network(tiny_models, real_scorer):
    assert real_scorer.audio_model_id == scoring.AUDIO_MODEL_ID
    assert real_scorer.text_model_id == scoring.TEXT_MODEL_ID
    assert real_scorer.audio_model_version
    assert real_scorer.text_model_version
    assert real_scorer.hidden_size == fixtures.TINY_HIDDEN


def test_scorer_embeds_real_audio_and_text(real_scorer, tmp_path):
    path = fixtures.write_wav(tmp_path / "probe.wav", duration=0.6)
    waveform, sample_rate = audio_mod.read_wav(path)
    audio_vector = real_scorer.embed_audio(waveform, sample_rate)
    text_vector = real_scorer.embed_text("音频文本一致性测试样例")
    assert audio_vector.shape == (fixtures.TINY_HIDDEN,)
    assert text_vector.shape == (fixtures.TINY_HIDDEN,)
    assert np.all(np.isfinite(audio_vector)) and np.all(np.isfinite(text_vector))


def test_scorer_score_is_real_and_deterministic(real_scorer, tmp_path):
    path = fixtures.write_wav(tmp_path / "probe2.wav", duration=0.6, freq=240.0)
    waveform, sample_rate = audio_mod.read_wav(path)
    first = real_scorer.score(waveform, sample_rate, "音频文本一致性测试样例")
    second = real_scorer.score(waveform, sample_rate, "音频文本一致性测试样例")
    assert set(["cosine", "audio_model", "text_model"]).issubset(first)
    assert -1.0 <= first["cosine"] <= 1.0
    assert first["cosine"] == pytest.approx(second["cosine"], abs=1e-6)
    assert first["audio_model"] == scoring.AUDIO_MODEL_ID
    assert first["text_model"] == scoring.TEXT_MODEL_ID


def test_scorer_rejects_empty_text(real_scorer, tmp_path):
    path = fixtures.write_wav(tmp_path / "probe3.wav", duration=0.4)
    waveform, sample_rate = audio_mod.read_wav(path)
    with pytest.raises(scoring.ScoreError):
        real_scorer.score(waveform, sample_rate, "   ，。")


def test_scorer_requires_matching_hidden_size(tmp_path, tiny_models):
    """伪造维度不一致的文本模型目录时必须显式失败，而不是产出无意义分数。"""
    import shutil

    from transformers import BertConfig, BertModel, BertTokenizer

    bad_root = tmp_path / "bad"
    text_dir = bad_root / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    (text_dir / "vocab.txt").write_text(
        "\n".join(fixtures.TINY_SPECIALS) + "\n", encoding="utf-8")
    BertTokenizer(vocab_file=str(text_dir / "vocab.txt")).save_pretrained(str(text_dir))
    BertConfig(hidden_size=8, num_hidden_layers=1, num_attention_heads=2,
               intermediate_size=8, vocab_size=len(fixtures.TINY_SPECIALS)).save_pretrained(str(text_dir))
    BertModel(BertConfig(hidden_size=8, num_hidden_layers=1, num_attention_heads=2,
                         intermediate_size=8, vocab_size=len(fixtures.TINY_SPECIALS))).save_pretrained(str(text_dir))
    with pytest.raises(scoring.ScoreError):
        scoring.AudioTextScorer.load(tiny_models["audio_path"], str(text_dir), device="cpu")
    assert shutil is not None


def test_scorer_load_fails_explicitly_when_weights_absent(tmp_path):
    with pytest.raises(scoring.DependencyError):
        scoring.AudioTextScorer.load(str(tmp_path / "none-a"), str(tmp_path / "none-b"),
                                     device="cpu")


def test_score_error_is_value_error_subclass():
    assert issubclass(scoring.ScoreError, ValueError)
    assert issubclass(scoring.DependencyError, RuntimeError)
    assert math.isfinite(1.0)
