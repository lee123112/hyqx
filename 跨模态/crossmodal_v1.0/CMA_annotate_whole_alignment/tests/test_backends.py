"""模型后端测试：真实余弦、实例复用、以及"不可用时绝不伪造分数"的降级表态。"""
from __future__ import annotations

import copy

import pytest

import make_sample
from utils import backends


def test_normalize_arch_aliases():
    assert backends.normalize_arch("CLIP-ViT/B-32") == backends.DEFAULT_ARCH
    assert backends.normalize_arch("clip-vit-base-patch32") == backends.DEFAULT_ARCH
    assert backends.normalize_arch("openai/clip-vit-base-patch32") == backends.DEFAULT_ARCH
    assert backends.normalize_arch(None) == backends.DEFAULT_ARCH
    assert backends.normalize_arch("  ") == backends.DEFAULT_ARCH
    assert backends.normalize_arch("custom-arch") == "custom-arch"


def test_resolve_device_is_explicit():
    assert backends.resolve_device("cpu") == "cpu"
    assert backends.resolve_device("auto") in {"cpu", "cuda"}
    assert backends.resolve_device("cuda") in {"cpu", "cuda"}


def test_model_signature_distinguishes_weights_and_device():
    base = {"library": "open_clip", "model_name": "ViT-B-32", "weights": "/a",
            "device": "cpu", "batch_size": 8}
    same = dict(base)
    other_weights = dict(base, weights="/b")
    other_device = dict(base, device="cuda")
    assert backends.model_signature(base) == backends.model_signature(same)
    assert backends.model_signature(base) != backends.model_signature(other_weights)
    assert backends.model_signature(base) != backends.model_signature(other_device)


def test_encoder_cache_reuses_one_instance():
    backends.clear_encoder_cache()
    calls = []

    def factory():
        calls.append(1)
        return object()

    first = backends.cached_encoder("sig-a", factory)
    second = backends.cached_encoder("sig-a", factory)
    assert first is second and len(calls) == 1

    sentinel = object()
    backends.register_encoder("sig-b", sentinel)
    assert backends.cached_encoder("sig-b", lambda: object()) is sentinel
    backends.clear_encoder_cache()
    assert backends.cached_encoder("sig-b", lambda: "rebuilt") == "rebuilt"


def test_default_config_degrades_without_weights_or_lies(raw_cfg):
    # 「未配置权重」环境：交付包内已随附 weight/clip/ViT-B-32.pt，直接把配置权重置空，
    # 才能在带权重机器上确定性复现「后端不可用绝不伪造分数」的表态。
    cfg = copy.deepcopy(raw_cfg)
    cfg.setdefault("model", {})["weights"] = ""

    encoder, reason, identity = backends.build_clip_backend(cfg, strict=False)
    assert encoder is None
    assert reason == backends.REASON_NO_WEIGHTS
    assert identity is None
    with pytest.raises(backends.BackendUnavailable):
        backends.build_clip_backend(cfg, strict=True)


def test_library_none_is_explicitly_disabled(raw_cfg):
    cfg = copy.deepcopy(raw_cfg)
    cfg["model"]["library"] = "none"
    encoder, reason, _identity = backends.build_clip_backend(cfg, strict=False)
    assert encoder is None and reason == backends.REASON_DISABLED


def test_missing_weights_file_reports_path(raw_cfg, tmp_path):
    cfg = copy.deepcopy(raw_cfg)
    cfg["model"]["weights"] = str(tmp_path / "nope.bin")
    encoder, reason, _identity = backends.build_clip_backend(cfg, strict=False)
    assert encoder is None and reason.startswith("weights_not_found")


@pytest.mark.parametrize("library", ["auto", "open_clip", "clip", "transformers"])
def test_no_encoder_is_ever_invented_without_weights(library):
    cfg = {"model": {"library": library, "weights": None}}
    encoder, reason, identity = backends.build_clip_backend(cfg, strict=False)
    assert encoder is None and reason and identity is None


def test_clip_backend_identity_marks_untrained_architecture():
    try:
        backend = backends.ClipViTB32Backend(weights=None)
    except backends.BackendUnavailable as exc:
        pytest.skip("本地无 open_clip/clip 依赖：%s" % exc)
    identity = backend.model_identity()
    assert identity["name"] == backends.CLIP_NAME
    assert identity["real_backend"] is False
    assert identity["weights"] is None


def test_load_image_reports_missing_and_unreadable(tmp_path):
    image, reason = backends.load_image(None)
    assert image is None and reason == "media_missing"
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"definitely not a png")
    image, reason = backends.load_image(broken)
    assert image is None and reason in {"media_missing", "media_unreadable"}


def test_load_image_decodes_real_png(tmp_path):
    pytest.importorskip("PIL")
    path = tmp_path / "ok.png"
    make_sample.write_png(path, 4, 3, (10, 20, 30))
    image, reason = backends.load_image(path)
    assert reason is None
    assert image is not None and image.size == (4, 3)


def test_cosine_similarity_is_real_math():
    assert backends.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert backends.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert backends.cosine_similarity([2.0, 0.0], [3.0, 0.0]) == pytest.approx(1.0)
    assert backends.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    with pytest.raises(ValueError):
        backends.cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])
