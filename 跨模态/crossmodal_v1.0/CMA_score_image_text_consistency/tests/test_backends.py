"""算子 #12 后端选择测试：模型不可用只能显式降级/失败，绝不伪造分数。

说明：本文件不验证模型精度，只验证"权重/依赖/设备不可用时后端如何表态"。
真实 CLIP 前向的连通性验证见 tests/test_clip_plumbing.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from utils import backends  # noqa: E402
from utils import scoring  # noqa: E402


def _cfg(**model_overrides):
    config = scoring.validate_config({})
    config["model"].update(model_overrides)
    return config


def test_library_none_degrades_explicitly():
    encoder, reason, identity = backends.build_clip_backend(_cfg(library="none"))
    assert encoder is None
    assert reason == "backend_disabled_by_config"
    assert identity is None


def test_library_none_without_degradation_raises():
    with pytest.raises(backends.BackendUnavailable):
        backends.build_clip_backend(_cfg(library="none", allow_degraded=False))


def test_missing_weights_does_not_fabricate_scores():
    encoder, reason, identity = backends.build_clip_backend(_cfg(library="auto", weights=None))
    assert encoder is None
    assert reason == "model_weights_not_configured"
    assert identity is None


def test_missing_weights_without_degradation_raises():
    with pytest.raises(backends.BackendUnavailable) as excinfo:
        backends.build_clip_backend(_cfg(library="auto", weights=None, allow_degraded=False))
    assert "model_weights_not_configured" in str(excinfo.value)


def test_nonexistent_weights_path_is_unavailable(tmp_path):
    missing = tmp_path / "not-there.bin"
    encoder, reason, identity = backends.build_clip_backend(
        _cfg(library="open_clip", weights=str(missing)))
    assert encoder is None
    assert "weights_not_found" in reason


@pytest.mark.parametrize("library", ["open_clip", "clip", "transformers"])
def test_unknown_model_name_is_unavailable(library, tmp_path):
    weights = tmp_path / "weights.bin"
    weights.write_bytes(b"not-a-real-checkpoint")
    encoder, reason, identity = backends.build_clip_backend(
        _cfg(library=library, weights=str(weights)))
    assert encoder is None
    assert reason


def test_invalid_library_rejected_by_config():
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config({"model": {"library": "magic"}})


def test_bad_device_rejected_by_config():
    with pytest.raises(scoring.ConfigError):
        scoring.validate_config({"model": {"device": "tpu"}})


def test_resolve_device():
    assert backends.resolve_device("cpu") == "cpu"
    assert backends.resolve_device("auto") in {"cpu", "cuda"}
    assert backends.resolve_device("cuda") in {"cpu", "cuda"}


def test_detector_defaults_to_not_configured():
    detector, reason = backends.build_detector(_cfg())
    assert detector is None
    assert reason == "detector_weights_not_configured"


def test_detector_nonexistent_weights(tmp_path):
    config = _cfg()
    config["detector"]["weights"] = str(tmp_path / "nope.pt")
    detector, reason = backends.build_detector(config)
    assert detector is None
    assert "detector_weights_not_found" in reason


def test_encoder_cache_reuses_single_instance():
    calls = []

    class _Dummy(object):
        pass

    def factory():
        calls.append(1)
        return _Dummy()

    backends.clear_encoder_cache()
    first = backends.cached_encoder("signature-a", factory)
    second = backends.cached_encoder("signature-a", factory)
    third = backends.cached_encoder("signature-b", factory)
    assert first is second
    assert third is not first
    assert len(calls) == 2
    backends.clear_encoder_cache()


def test_model_signature_changes_with_weights():
    base = scoring.validate_config({})["model"]
    same = dict(base)
    different = dict(base, weights="/models/clip.bin")
    assert backends.model_signature(base) == backends.model_signature(same)
    assert backends.model_signature(base) != backends.model_signature(different)


def test_detector_evidence_helper_reports_tier_unchanged():
    evidence = backends.detector_evidence("applied", labels=["dog"],
                                         keywords=["dog", "草地"], reason=None)
    assert evidence["status"] == "applied"
    assert evidence["matched_keywords"] == ["dog"]
    assert evidence["tier_unchanged"] is True

    unavailable = backends.detector_evidence("unavailable", reason="no_weights")
    assert unavailable["status"] == "unavailable"
    assert unavailable["labels"] == []
    assert unavailable["tier_unchanged"] is True
