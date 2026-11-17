"""CLIP 管线连通性测试（真实 open_clip 前向，**只验证管线不断言分数**）。

诚实边界：
- 本机未部署 CLIP-ViT/B-32 训练权重，这里只用 ``pretrained=None`` 构建**架构**
  （随机初始化），目的是验证「图像→裁图→预处理→批量前向→L2 归一化→余弦」的形状与
  数值契约，以及坏图/缺图的容错分支；
- 随机初始化的架构**不包含语义**，因此本文件不产生任何可解释的语义分数，
  也不作为表1 业务效果证据。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from conftest import png_bytes  # noqa: E402
from utils import backends  # noqa: E402
from utils import scoring  # noqa: E402

open_clip = pytest.importorskip("open_clip")


def _norm(vector):
    return math.sqrt(sum(value * value for value in vector))


def test_architecture_only_embeddings_are_unit_and_512d(tmp_path):
    encoder = backends.open_clip_encoder(weights=None, device="cpu", batch_size=4)
    assert encoder.untrained_architecture_only is True
    assert encoder.embed_dim == 512

    image = tmp_path / "shot.png"
    image.write_bytes(png_bytes())
    boxes = [{"box_id": "b1", "bbox": {"x": 0, "y": 0, "w": 32, "h": 32,
                                       "coord": "xywh-pixel"}},
             {"box_id": "b2", "bbox": {"x": 32, "y": 16, "w": 32, "h": 32,
                                       "coord": "xywh-pixel"}}]

    text_vectors = encoder.encode_texts(["金毛犬", "草地"])
    crop_vectors, errors = encoder.encode_crops(image, boxes)

    assert errors == [None, None]
    assert len(crop_vectors) == 2
    for vector in list(text_vectors) + list(crop_vectors):
        assert len(vector) == 512
        assert _norm(vector) == pytest.approx(1.0, abs=1e-3)

    score = scoring.cosine_similarity(text_vectors[0], crop_vectors[0])
    assert -1.0 <= score <= 1.0


def test_architecture_only_is_deterministic(tmp_path):
    encoder = backends.open_clip_encoder(weights=None, device="cpu", batch_size=4)
    image = tmp_path / "shot.png"
    image.write_bytes(png_bytes())
    boxes = [{"box_id": "b1", "bbox": {"x": 1, "y": 1, "w": 20, "h": 20,
                                       "coord": "xywh-pixel"}}]
    first, _ = encoder.encode_crops(image, boxes)
    second, _ = encoder.encode_crops(image, boxes)
    assert first[0] == pytest.approx(second[0])


def test_missing_image_reports_media_missing(tmp_path):
    encoder = backends.open_clip_encoder(weights=None, device="cpu", batch_size=4)
    boxes = [{"box_id": "b1", "bbox": {"x": 0, "y": 0, "w": 4, "h": 4,
                                       "coord": "xywh-pixel"}}]
    vectors, errors = encoder.encode_crops(tmp_path / "gone.png", boxes)
    assert vectors == [None]
    assert errors == ["media_missing"]


def test_unreadable_image_reports_media_unreadable(tmp_path):
    encoder = backends.open_clip_encoder(weights=None, device="cpu", batch_size=4)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not-an-image")
    boxes = [{"box_id": "b1", "bbox": {"x": 0, "y": 0, "w": 4, "h": 4,
                                       "coord": "xywh-pixel"}}]
    vectors, errors = encoder.encode_crops(broken, boxes)
    assert vectors == [None]
    assert errors == ["media_unreadable"]


def test_model_signature_is_stable_and_sensitive():
    first = backends.model_signature({"library": "auto", "model_name": "CLIP-ViT/B-32",
                                      "weights": None, "device": "cpu",
                                      "batch_size": 8})
    second = backends.model_signature({"library": "auto", "model_name": "ViT-B-32",
                                       "weights": None, "device": "cpu",
                                       "batch_size": 8})
    other = backends.model_signature({"library": "auto", "model_name": "ViT-B-32",
                                      "weights": None, "device": "cuda",
                                      "batch_size": 8})
    assert first == second
    assert first != other


def test_installed_libraries_reported():
    assert "open_clip" in backends.installed_libraries()
