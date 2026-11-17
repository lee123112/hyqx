"""CLIP-ViT/B-32 适配器连通性测试（只验证管线，不验证精度）。

用 ``open_clip`` **只构建架构**（``weights=None``，不联网、不下载），验证：
- tokenize → encode_text → encode_image → L2 归一化 → 余弦 全链路真的跑通；
- 输出维度与 CLIP-ViT/B-32 一致（512）；
- 帧图像缺失/损坏时返回可解释原因，而不是伪造向量。

**随机初始化的架构输出没有任何语义含义**，因此本文件不断言任何分数取值，
也不作为模型质量证据。真实权重到位后设置 ``OP14_CLIP_WEIGHTS`` 运行真实权重检查。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from conftest import write_png  # noqa: E402
from utils import backends  # noqa: E402
from utils import scoring  # noqa: E402


@pytest.fixture(scope="module")
def architecture_encoder():
    pytest.importorskip("open_clip")
    try:
        encoder = backends.open_clip_encoder("ViT-B-32", None, "cpu", 2)
    except backends.BackendUnavailable as exc:
        pytest.skip("open_clip 架构不可用：%s" % exc)
    return encoder


def test_architecture_pipeline_produces_unit_vectors(architecture_encoder, tmp_path):
    image_path = write_png(tmp_path / "a.png")
    missing = tmp_path / "missing.png"

    text_vectors = architecture_encoder.encode_texts(["一只狗在草地上奔跑", "红色方块"])
    image_vectors, errors = architecture_encoder.encode_images([image_path, missing])

    assert len(text_vectors) == 2
    assert len(text_vectors[0]) == 512
    assert len(image_vectors) == 2
    assert image_vectors[1] is None
    assert errors == [None, "media_missing"]

    for vector in text_vectors:
        norm = sum(value * value for value in vector) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-4)

    score = scoring.cosine_similarity(text_vectors[0], image_vectors[0])
    assert -1.0 <= score <= 1.0


def test_architecture_reports_undecodable_frame(architecture_encoder, tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    vectors, errors = architecture_encoder.encode_images([broken])
    assert vectors == [None]
    assert errors == ["media_unreadable"]


def test_public_backend_without_weights_is_unavailable():
    encoder, reason, identity = backends.build_clip_backend(
        {"clip": {"library": "auto", "model_name": "ViT-B-32", "weights": None,
                  "device": "cpu", "batch_size": 2, "allow_degraded": True}})
    assert encoder is None
    assert reason == backends.REASON_NO_WEIGHTS
    assert identity is None


def test_public_backend_without_weights_raises_when_strict():
    with pytest.raises(backends.BackendUnavailable):
        backends.build_clip_backend(
            {"clip": {"library": "auto", "model_name": "ViT-B-32", "weights": None,
                      "device": "cpu", "batch_size": 2, "allow_degraded": False}})


def test_public_backend_with_missing_path_is_unavailable(tmp_path):
    encoder, reason, _identity = backends.build_clip_backend(
        {"clip": {"library": "auto", "model_name": "ViT-B-32",
                  "weights": str(tmp_path / "nope.pt"), "device": "cpu",
                  "batch_size": 2, "allow_degraded": True}})
    assert encoder is None
    assert reason.startswith("weights_not_found")


def test_library_none_disables_backend():
    encoder, reason, identity = backends.build_clip_backend(
        {"clip": {"library": "none", "model_name": "ViT-B-32", "weights": "x",
                  "device": "cpu", "batch_size": 2, "allow_degraded": True}})
    assert (encoder, identity) == (None, None)
    assert reason == backends.REASON_DISABLED


def test_normalize_arch_accepts_table1_aliases():
    for alias in ("CLIP-ViT/B-32", "clip-vit-b-32", "ViT-B/32", "openai/clip-vit-base-patch32"):
        assert backends.normalize_arch(alias) == "ViT-B-32"
    assert backends.normalize_arch(None) == "ViT-B-32"


def test_model_signature_is_stable_and_sensitive():
    base = {"library": "auto", "model_name": "ViT-B-32", "weights": "w.pt",
            "device": "cpu", "batch_size": 8}
    assert backends.model_signature(base) == backends.model_signature(dict(base))
    changed = dict(base, batch_size=4)
    assert backends.model_signature(base) != backends.model_signature(changed)


def test_cached_encoder_reuses_same_instance():
    backends.clear_encoder_cache()
    calls = []

    def factory():
        calls.append(1)
        return object()

    first = backends.cached_encoder("sig-a", factory)
    second = backends.cached_encoder("sig-a", factory)
    third = backends.cached_encoder("sig-b", factory)
    assert first is second
    assert third is not first
    assert len(calls) == 2


def test_register_encoder_allows_reusing_image_text_instance():
    """复用图文评分已加载实例：允许编排侧登记共享编码器。"""
    encoder = object()
    backends.clear_encoder_cache()
    backends.register_encoder("shared-signature", encoder)
    assert backends.get_registered_encoder("shared-signature") is encoder
    assert backends.get_registered_encoder("other-signature") is None
    backends.clear_encoder_cache()


def test_openai_tokenizer_truncates_long_text_without_changing_short_text():
    """回归 #14 长字幕：OpenAI ``clip.tokenize`` 必须显式 ``truncate=True``。

    旧实现用默认 ``truncate=False``，>77 token 的文本抛 ``RuntimeError``（被算子记成
    ``pending/score_error``）；修复后长文本截断到 context length，**短文本 token 序列
    必须与旧行为完全一致**（分数口径不变）。
    """
    clip = pytest.importorskip("clip")
    short = "一只狗在草地上奔跑"
    long_text = "一只橘色小猫在木地板上追逐毛线球，" * 40
    assert (clip.tokenize([short], truncate=True).tolist()
            == clip.tokenize([short]).tolist()), "短文本 token 必须与改造前一致"
    with pytest.raises(RuntimeError):
        clip.tokenize([long_text])
    ids = clip.tokenize([long_text], truncate=True)
    assert tuple(ids.shape) == (1, 77)


def test_openai_backend_encodes_long_text_without_runtime_error():
    """回归 #14 长字幕：走 OpenAI clip 后端的 ``_OpenAIClipBackend.encode_texts`` 不抛错。

    用本算子内置权重（或 ``OP14_CLIP_WEIGHTS``）真实前向：>77 token 文本应返回 512 维
    单位向量，而不是 ``RuntimeError: Input ... is too long for context length 77``。
    无 ``clip`` 包/权重时按既有约定 skip。
    """
    pytest.importorskip("clip")
    weights = os.environ.get("OP14_CLIP_WEIGHTS")
    if not weights:
        bundled = BASE / "weight" / "clip" / "ViT-B-32.pt"
        weights = str(bundled) if bundled.exists() else None
    if not weights or not Path(weights).exists():
        pytest.skip("无 OpenAI clip 权重（OP14_CLIP_WEIGHTS 或内置 weight/clip/ViT-B-32.pt）")
    try:
        encoder = backends._openai_clip_encoder(weights, "cpu", 2)
    except backends.BackendUnavailable as exc:
        pytest.skip("OpenAI clip 后端不可用：%s" % exc)
    assert encoder is not None

    long_text = ("♪ >> WELCOME TO THE VERY FIRST EPISODE OF NASA SCIENCE LIVE. "
                 "THIS IS A CHANGE TO TAKE YOU BEHIND THE SCENES TO GET TO KNOW THE SPACE "
                 "AGENCY. TODAY WE ARE TALKING ABOUT MOON. ") * 3
    vectors = encoder.encode_texts([long_text, "短字幕"])
    assert len(vectors) == 2
    assert all(len(vector) == 512 for vector in vectors)
    norm = sum(value * value for value in vectors[0]) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-4)


def test_real_weights_integration_is_optional():
    """真实权重集成检查：仅在提供 OP14_CLIP_WEIGHTS 时运行，否则明确跳过。"""
    weights = os.environ.get("OP14_CLIP_WEIGHTS")
    if not weights:
        pytest.skip("未提供本地 CLIP 权重路径（OP14_CLIP_WEIGHTS），真实权重未验证")
    if not Path(weights).exists():
        pytest.skip("OP14_CLIP_WEIGHTS 指向的权重不存在：%s" % weights)
    encoder, reason, identity = backends.build_clip_backend(
        {"clip": {"library": "auto", "model_name": "ViT-B-32", "weights": weights,
                  "device": "cpu", "batch_size": 1, "allow_degraded": False}})
    assert encoder is not None and reason is None
    assert identity["degraded"] is False
    assert len(encoder.encode_texts(["一只狗"])[0]) == 512
