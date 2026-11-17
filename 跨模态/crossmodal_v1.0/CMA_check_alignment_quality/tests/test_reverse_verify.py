"""图文 CLIP 反向检索测试。

使用 ``tests/conftest.py`` 中**明确标注的测试替身编码器**验证排序/分档逻辑；
替身不携带语义，不用于断言任何精度。模型不可用时必须显式
``insufficient_evidence`` + ``model_unavailable``，**不得伪造分数**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from utils import reverse_verify as rv
from tests.conftest import StubEncoder


def _entry(sample_id: str, text: str, image: str) -> dict:
    return {"sample_id": sample_id, "text": text, "image": image,
            "image_path": Path(image)}


def _stub() -> StubEncoder:
    return StubEncoder(dim=2, vectors={
        "text:小狗": [1.0, 0.0],
        "text:猫": [0.0, 1.0],
        "text:飞机": [0.9, 0.1],
        "image:a.png": [1.0, 0.0],
        "image:b.png": [0.0, 1.0],
    })


def test_top1_hit_is_consistent() -> None:
    entries = [_entry("s1", "小狗", "a.png"), _entry("s2", "猫", "b.png")]
    result = rv.verify_pool(entries, _stub(), top_k=2, min_consistency=0.25)
    first = result["results"]["s1"]
    assert first["check_status"] == "checked"
    assert first["hit_at_1"] is True
    assert first["rank"] == 1
    assert first["consistent"] is True
    assert first["similarity"] == pytest.approx(1.0, abs=1e-6)


def test_miss_is_inconsistent() -> None:
    entries = [_entry("s1", "小狗", "a.png"), _entry("s2", "飞机", "b.png")]
    result = rv.verify_pool(entries, _stub(), top_k=2, min_consistency=0.25)
    second = result["results"]["s2"]
    assert second["rank"] == 2
    assert second["hit_at_1"] is False
    assert second["consistent"] is False


def test_single_candidate_pool_is_insufficient_evidence() -> None:
    entries = [_entry("s1", "小狗", "a.png")]
    result = rv.verify_pool(entries, _stub(), top_k=2, min_consistency=0.25)
    only = result["results"]["s1"]
    assert only["check_status"] == "insufficient_evidence"
    assert only["reason"] == "insufficient_candidates"
    assert only["consistent"] is None
    assert result["pool_size"] == 1


def test_low_similarity_blocks_consistency() -> None:
    """命中 top-1 但相似度低于阈值 → 不一致（reason 与「未命中」区分开）。"""
    encoder = StubEncoder(dim=2, vectors={
        "text:小狗": [1.0, 0.9],          # 与自身图像相似度约 0.743
        "text:猫": [0.0, 1.0],
        "image:a.png": [1.0, 0.0],
        "image:b.png": [0.0, 1.0],
    })
    entries = [_entry("s1", "小狗", "a.png"), _entry("s2", "猫", "b.png")]
    result = rv.verify_pool(entries, encoder, top_k=2, min_consistency=0.9)
    item = result["results"]["s1"]
    assert item["hit_at_1"] is True
    assert item["rank"] == 1
    assert item["consistent"] is False
    assert item["reason"] == "below_min_consistency"
    assert item["similarity"] == pytest.approx(0.7433, abs=1e-3)


def test_unreadable_image_makes_evidence_insufficient() -> None:
    encoder = StubEncoder(dim=2, fail_paths={"a.png"}, vectors={
        "text:小狗": [1.0, 0.0],
        "image:b.png": [0.0, 1.0],
    })
    entries = [_entry("s1", "小狗", "a.png"), _entry("s2", "猫", "b.png")]
    result = rv.verify_pool(entries, encoder, top_k=2, min_consistency=0.25)
    assert result["results"]["s1"]["check_status"] == "insufficient_evidence"
    assert result["results"]["s1"]["reason"] == "image_unreadable"


def test_missing_encoder_is_degraded_not_faked() -> None:
    entries = [_entry("s1", "小狗", "a.png"), _entry("s2", "猫", "b.png")]
    result = rv.verify_pool(entries, None, top_k=2, min_consistency=0.25)
    assert result["model_available"] is False
    for sample_id in ("s1", "s2"):
        item = result["results"][sample_id]
        assert item["check_status"] == "insufficient_evidence"
        assert item["reason"] == "model_unavailable"
        assert item["consistent"] is None
        assert item["similarity"] is None


def test_candidates_sorted_by_similarity_then_id() -> None:
    entries = [_entry("s1", "小狗", "a.png"), _entry("s2", "猫", "b.png")]
    result = rv.verify_pool(entries, _stub(), top_k=2, min_consistency=0.25)
    candidates = result["results"]["s1"]["candidates"]
    assert [item["sample_id"] for item in candidates] == ["s1", "s2"]
    assert candidates[0]["similarity"] >= candidates[1]["similarity"]
    assert len(candidates) <= 2


def test_model_payload_reports_real_identity() -> None:
    entries = [_entry("s1", "小狗", "a.png"), _entry("s2", "猫", "b.png")]
    result = rv.verify_pool(entries, _stub(), top_k=2, min_consistency=0.25)
    model = result["results"]["s1"]["model"]
    assert model["name"] == "stub-encoder"
    assert model["test_double"] is True
    assert model["degraded"] is False


def test_build_encoder_returns_none_without_local_weights(tmp_path: Path) -> None:
    encoder, reason = rv.build_encoder({"weights": None, "model_name": "ViT-B-32",
                                       "device": "cpu"})
    assert encoder is None
    assert reason in ("clip_weights_missing", "clip_library_missing")


def test_build_encoder_returns_none_for_missing_weight_file(tmp_path: Path) -> None:
    encoder, reason = rv.build_encoder({"weights": str(tmp_path / "nope.pt"),
                                        "model_name": "ViT-B-32", "device": "cpu"})
    assert encoder is None
    assert reason == "clip_weights_missing"


def test_shared_encoder_injection_is_used_and_restored() -> None:
    stub = _stub()
    previous = rv.get_shared_encoder()
    try:
        rv.set_shared_encoder(stub)
        encoder, reason = rv.build_encoder({"weights": None, "model_name": "ViT-B-32",
                                            "device": "cpu"})
        assert encoder is stub
        assert reason is None
    finally:
        rv.set_shared_encoder(previous)


def test_shared_encoder_roundtrip_returns_last_value() -> None:
    previous = rv.get_shared_encoder()
    stub = _stub()
    rv.set_shared_encoder(stub)
    try:
        assert rv.get_shared_encoder() is stub
    finally:
        rv.set_shared_encoder(previous)
    assert rv.get_shared_encoder() is previous


class _RecordingEncoder(StubEncoder):
    """记录图像编码调用次数的替身（用于验证去重只编码唯一图像）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.image_calls: list = []

    def encode_images(self, paths):
        self.image_calls.append([Path(p).name for p in paths])
        return super().encode_images(paths)


def test_duplicate_images_are_encoded_once_and_reported(tmp_path: Path) -> None:
    # 同一张图被扩成两条样本（内容相同、目录不同）——候选池只应编码一次
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    payload = b"\x89PNG\r\n\x1a\n same-bytes"
    (first_dir / "x.png").write_bytes(payload)
    (second_dir / "x.png").write_bytes(payload)
    other = tmp_path / "y.png"
    other.write_bytes(b"\x89PNG\r\n\x1a\n other-bytes")

    encoder = _RecordingEncoder(dim=2, vectors={
        "text:小狗": [1.0, 0.0],
        "text:猫": [0.0, 1.0],
        "image:x.png": [1.0, 0.0],
        "image:y.png": [0.0, 1.0],
    })
    entries = [
        _entry("s1", "小狗", str(first_dir / "x.png")),
        _entry("s2", "猫", str(other)),
        _entry("s1_copy", "小狗", str(second_dir / "x.png")),
    ]
    result = rv.verify_pool(entries, encoder, top_k=3, min_consistency=0.25)

    assert result["pool_size"] == 3                       # 条目口径不变
    assert result["unique_image_count"] == 2              # 实际唯一图像
    assert result["duplicate_image_entries"] == 1
    assert len(encoder.image_calls) == 1                  # 只调用一次
    assert len(encoder.image_calls[0]) == 2               # 且只送唯一图像
    # 去重不改变判定：重复图像的两条样本仍各自参与排名
    assert result["results"]["s1"]["hit_at_1"] is True
    assert result["results"]["s1_copy"]["hit_at_1"] is True
    assert result["results"]["s2"]["hit_at_1"] is True


def test_unreadable_image_falls_back_to_per_entry_encoding(tmp_path: Path) -> None:
    encoder = _RecordingEncoder(dim=2, vectors={
        "text:小狗": [1.0, 0.0],
        "image:a.png": [1.0, 0.0],
    })
    missing = tmp_path / "missing.png"
    existing = tmp_path / "a.png"
    existing.write_bytes(b"same-bytes-2")
    entries = [_entry("s1", "小狗", str(missing)),
               _entry("s2", "小狗", str(existing))]
    result = rv.verify_pool(entries, encoder, top_k=2, min_consistency=0.25)
    # 读不到摘要的条目退回逐条编码：单独一次调用，不被静默丢弃也不并入去重批次
    assert [len(call) for call in encoder.image_calls] == [1, 1]
    assert encoder.image_calls[1] == ["missing.png"]
    assert result["results"]["s1"]["check_status"] == "checked"
    assert result["unique_image_count"] == 1
    assert result["duplicate_image_entries"] == 1
