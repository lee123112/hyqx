"""CLIP-ViT/B-32 适配器连通性测试（只验证管线，不验证精度）。

本测试用 ``open_clip`` **只构建架构**（``weights=None``，不联网、不下载、不做任何
等价于"模型分数"的业务输出），验证：
- tokenize → encode_text → encode_image → L2 归一化 → 余弦 全链路真的跑通；
- 图像不可读时返回可解释原因而不是伪造向量；
- 输出维度与 CLIP-ViT/B-32 一致（512）。

**随机初始化的架构输出没有任何语义含义**，因此本文件不断言任何分数取值，
也不作为模型质量证据。真实权重到位后请设置 ``OP12_CLIP_WEIGHTS`` 运行
真实权重集成检查（见 DEVELOPMENT_STATUS.md）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from conftest import write_png  # noqa: E402
from utils import backends  # noqa: E402
from utils import scoring  # noqa: E402


@pytest.fixture(scope="module")
def open_clip_encoder():
    pytest.importorskip("open_clip")
    try:
        encoder = backends.open_clip_encoder("ViT-B-32", None, "cpu", 2)
    except backends.BackendUnavailable as exc:  # 环境缺依赖/架构时明确跳过并给出原因
        pytest.skip("open_clip 架构不可用：%s" % exc)
    return encoder


def test_real_clip_architecture_pipeline(open_clip_encoder, tmp_path):
    image_path = write_png(tmp_path / "a.png")
    missing = tmp_path / "missing.png"

    text_vectors = open_clip_encoder.encode_texts(["一只金毛犬在草地上接飞盘", "红色方块"])
    image_vectors, errors = open_clip_encoder.encode_images([image_path, missing])

    assert len(text_vectors) == 2
    assert len(text_vectors[0]) == 512
    assert len(image_vectors) == 2
    assert image_vectors[1] is None
    assert errors == [None, "media_missing"]

    norms = [sum(value * value for value in vector) ** 0.5 for vector in text_vectors]
    for norm in norms:
        assert norm == pytest.approx(1.0, abs=1e-4)

    score = scoring.cosine_similarity(text_vectors[1], image_vectors[0])
    assert -1.0 <= score <= 1.0


def test_real_clip_architecture_reports_undecodable_image(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    vectors, errors = backends.open_clip_encoder(
        "ViT-B-32", None, "cpu", 1).encode_images([broken])
    assert vectors == [None]
    assert errors == ["media_unreadable"]


def test_public_backend_requires_weights():
    """公共后端在权重缺失时必须明确不可用，不能让架构模式泄漏到生产路径。"""
    encoder, reason, identity = backends.build_clip_backend(
        {"model": {"library": "open_clip", "model_name": "ViT-B-32", "weights": None,
                   "device": "cpu", "batch_size": 2, "allow_degraded": True}})
    assert encoder is None
    assert reason == "model_weights_not_configured"
    assert identity is None


def test_real_weights_integration_is_optional(tmp_path):
    """真实权重集成检查：仅在提供 OP12_CLIP_WEIGHTS 时运行，否则明确跳过。"""
    import os

    weights = os.environ.get("OP12_CLIP_WEIGHTS")
    if not weights:
        pytest.skip("未提供本地 CLIP 权重路径（OP12_CLIP_WEIGHTS），真实权重未验证")
    if not Path(weights).exists():
        pytest.skip("OP12_CLIP_WEIGHTS 指向的权重不存在：%s" % weights)
    encoder, reason, identity = backends.build_clip_backend(
        {"model": {"library": "auto", "model_name": "ViT-B-32", "weights": weights,
                   "device": "cpu", "batch_size": 1, "allow_degraded": False}})
    assert encoder is not None and reason is None
    assert identity["degraded"] is False
    vectors = encoder.encode_texts(["一只金毛犬"])
    assert len(vectors[0]) == 512
