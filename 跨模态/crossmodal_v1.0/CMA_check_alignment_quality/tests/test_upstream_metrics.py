"""上游指标复用与 10% 子集补测测试。

覆盖表1 #20 研发边界：PPL / BRISQUE / SNR **优先复用四条预训练产线**，
无上游时才在 ``quality_subset_fallback`` 子集补测；没有本地后端时
只能记 ``insufficient_evidence``，**绝不产出固定或随机分数**。
"""
from __future__ import annotations

from pathlib import Path

from utils import upstream_metrics as um
from tests.conftest import write_mixed_wav, write_wav


# --------------------------------------------------------------------------- #
# 上游取值
# --------------------------------------------------------------------------- #

def test_resolve_upstream_ppl_from_qc_object() -> None:
    record = {"sample_id": "s1", "qc": {"text_fluency": {"ppl": 42.5}}}
    resolved = um.resolve_upstream_metric(record, "ppl")
    assert resolved is not None
    assert resolved["value"] == 42.5
    assert resolved["source"] == "upstream"
    assert "ppl" in resolved["path"]


def test_resolve_upstream_brisque_from_image_object() -> None:
    record = {"sample_id": "s1", "image": {"path": "media/a.png", "brisque": 31.25}}
    resolved = um.resolve_upstream_metric(record, "brisque")
    assert resolved is not None and resolved["value"] == 31.25


def test_resolve_upstream_snr_from_processing_metrics() -> None:
    record = {"sample_id": "s1",
              "processing": {"06": {"metrics": {"snr_db": 18.0}}}}
    resolved = um.resolve_upstream_metric(record, "snr")
    assert resolved is not None and resolved["value"] == 18.0


def test_resolve_upstream_metric_returns_none_when_absent() -> None:
    assert um.resolve_upstream_metric({"sample_id": "s1"}, "ppl") is None
    assert um.resolve_upstream_metric({"sample_id": "s1"}, "brisque") is None
    assert um.resolve_upstream_metric({"sample_id": "s1"}, "snr") is None


def test_resolve_upstream_metric_rejects_non_numeric_and_negative() -> None:
    assert um.resolve_upstream_metric({"qc": {"text_fluency": {"ppl": "abc"}}},
                                      "ppl") is None
    assert um.resolve_upstream_metric({"qc": {"text_fluency": {"ppl": True}}},
                                      "ppl") is None
    assert um.resolve_upstream_metric({"image": {"brisque": -1.0}}, "brisque") is None


def test_resolve_upstream_reverse_reuses_previous_conclusion() -> None:
    record = {"sample_id": "s1",
              "qc": {"reverse_verify": {"passed": True, "score": 0.71,
                                        "check_status": "checked",
                                        "model": {"name": "CLIP-ViT/B-32"}}}}
    resolved = um.resolve_upstream_reverse(record)
    assert resolved is not None
    assert resolved["passed"] is True
    assert resolved["score"] == 0.71


def test_resolve_upstream_reverse_absent() -> None:
    assert um.resolve_upstream_reverse({"sample_id": "s1"}) is None


# --------------------------------------------------------------------------- #
# 10% 子集补测选择
# --------------------------------------------------------------------------- #

def test_fallback_subset_is_deterministic_and_roughly_ratio() -> None:
    keys = ["sample-%04d" % index for index in range(2000)]
    first = um.select_fallback_keys(keys, 0.10)
    second = um.select_fallback_keys(keys, 0.10)
    assert first == second                      # 确定性
    assert 0.07 <= len(first) / len(keys) <= 0.13   # 抽样比例贴近 10%
    assert um.is_fallback_selected("sample-0001", 0.10) == \
        ("sample-0001" in first)


def test_fallback_ratio_bounds() -> None:
    keys = ["s%d" % index for index in range(100)]
    assert len(um.select_fallback_keys(keys, 0.0)) == 0
    assert len(um.select_fallback_keys(keys, 1.0)) == len(keys)


# --------------------------------------------------------------------------- #
# 本地补测：SNR 真实可算，PPL/BRISQUE 无后端时如实标记
# --------------------------------------------------------------------------- #

def test_local_snr_measurement_is_real(tmp_path: Path) -> None:
    mixed = write_mixed_wav(tmp_path / "mixed.wav")
    result = um.local_measure("snr", audio_path=mixed, pcm=None, framerate=16000)
    assert result["status"] == "ok"
    assert result["value"] is not None and result["value"] > 0
    assert result["backend"]


def test_local_snr_on_all_silent_is_insufficient(tmp_path: Path) -> None:
    silent = write_wav(tmp_path / "silent.wav", silent=True)
    result = um.local_measure("snr", audio_path=silent)
    assert result["status"] == "insufficient_evidence"
    assert result["value"] is None
    assert result["reason"] == "audio_silent_or_too_short"


def test_local_snr_on_broken_audio_reports_undecodable(tmp_path: Path) -> None:
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not-audio")
    result = um.local_measure("snr", audio_path=broken)
    assert result["status"] == "insufficient_evidence"
    assert result["reason"] == "audio_undecodable"


def test_evaluate_metric_prefers_upstream_and_never_fakes() -> None:
    upstream = {"value": 12.5, "path": "audio.snr_db"}
    reused = um.evaluate_metric("snr", mode="upstream_first", upstream=upstream,
                                applicable=True, fallback_selected=False)
    assert reused["status"] == "ok"
    assert reused["source"] == "upstream"
    assert reused["value"] == 12.5

    sampled_out = um.evaluate_metric("snr", mode="upstream_first", upstream=None,
                                     applicable=True, fallback_selected=False)
    assert sampled_out["status"] == "insufficient_evidence"
    assert sampled_out["reason"] == "outside_fallback_subset"
    assert sampled_out["value"] is None

    not_applicable = um.evaluate_metric("ppl", mode="upstream_first", upstream=None,
                                        applicable=False, fallback_selected=True)
    assert not_applicable["status"] == "not_applicable"
    assert not_applicable["value"] is None

    disabled = um.evaluate_metric("brisque", mode="disable", upstream=None,
                                  applicable=True, fallback_selected=True)
    assert disabled["status"] == "not_applicable"
    assert disabled["reason"] == "metric_disabled"


def test_local_ppl_reports_missing_backend(tmp_path: Path) -> None:
    result = um.local_measure("ppl", text="金毛犬在草地上接飞盘")
    assert result["status"] in ("ok", "insufficient_evidence")
    if result["status"] == "insufficient_evidence":
        assert result["reason"] == "ppl_backend_missing"
        assert result["value"] is None


def test_local_brisque_reports_missing_backend(media_root: Path) -> None:
    result = um.local_measure("brisque", image_path=media_root / "media" / "ok_a.png")
    assert result["status"] in ("ok", "insufficient_evidence")
    if result["status"] == "insufficient_evidence":
        assert result["reason"] == "brisque_backend_missing"
        assert result["value"] is None


def test_metric_modes_are_declared() -> None:
    assert "upstream_first" in um.METRIC_MODES
    assert "local_required" in um.METRIC_MODES
    assert "upstream_only" in um.METRIC_MODES
    assert "disable" in um.METRIC_MODES
    assert set(um.METRIC_KINDS) == {"ppl", "brisque", "snr"}
