"""上游字段归一化测试（``utils/upstream.py``，只读解析，不做业务判定）。"""
from __future__ import annotations

import pytest
from conftest import (deep_merge, make_pair, make_record, rec_17, rec_18, rec_19,
                      rec_20, rec_21, rec_audio, rec_image, rec_license, rec_text,
                      rec_video, rec_version)

from utils import upstream


# --------------------------------------------------------------------------- #
# 模态归一化
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    (["image", "text"], ("image", "text")),
    (["TEXT", "Image"], ("image", "text")),
    (["图像", "文本"], ("image", "text")),
    (["img", "audio", "txt"], ("image", "audio", "text")),
    (["voice", "video"], ("audio", "video")),
    ([], ()),
    (None, ()),
    (["unknown"], ()),
])
def test_normalize_modalities(raw, expected):
    assert upstream.normalize_modalities(raw) == expected


def test_modalities_inferred_from_references_when_absent():
    payload = deep_merge(rec_text(), rec_image(), rec_audio())
    assert upstream.collect_modalities(payload) == ("image", "audio", "text")


def test_declared_modalities_union_with_inferred():
    payload = deep_merge({"modalities": ["text"]}, rec_text(), rec_image())
    assert upstream.collect_modalities(payload) == ("image", "text")


# --------------------------------------------------------------------------- #
# 文本抽取
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("factory,expected_field", [
    (lambda: rec_text("一只猫", "object"), "text.content"),
    (lambda: rec_text("一只猫", "string"), "text"),
    (lambda: rec_text("一只猫", "metadata"), "metadata.description"),
    (lambda: rec_text("一只猫", "description"), "description"),
])
def test_extract_text_sources(factory, expected_field):
    spec = upstream.extract_text(factory())
    assert spec["content"] == "一只猫"
    assert spec["field"] == expected_field


def test_extract_text_prefers_object_content_over_metadata():
    payload = deep_merge(rec_text("主体描述", "object"),
                         {"metadata": {"description": "兜底描述"}})
    assert upstream.extract_text(payload)["content"] == "主体描述"


def test_extract_text_missing_returns_none():
    spec = upstream.extract_text({"sample_id": "x"})
    assert spec["content"] is None
    assert spec["field"] is None


def test_extract_text_blank_is_none():
    assert upstream.extract_text(rec_text("   "))["content"] is None


# --------------------------------------------------------------------------- #
# 媒体引用
# --------------------------------------------------------------------------- #

def test_collect_media_links_records_modality_and_path():
    payload = deep_merge(rec_image("media/a.png"), rec_audio("media/b.wav"),
                         rec_video("media/c.mp4"))
    links = upstream.collect_media_links(payload)
    assert [(item["modality"], item["path"]) for item in links] == [
        ("image", "media/a.png"), ("audio", "media/b.wav"), ("video", "media/c.mp4")]


def test_collect_media_links_accepts_string_and_array():
    payload = {"image": "media/a.png", "video": [{"path": "media/f1.png"},
                                                 {"path": "media/f2.png"}]}
    links = upstream.collect_media_links(payload)
    assert [item["path"] for item in links] == ["media/a.png", "media/f1.png",
                                                "media/f2.png"]


def test_collect_media_links_flags_unsafe_reference():
    payload = {"image": {"path": "/abs/escape.png"}}
    links = upstream.collect_media_links(payload)
    assert links[0]["safe"] is False
    assert links[0]["reason"] == "unsupported_media_path"


def test_collect_media_links_empty_or_invalid_shapes():
    assert upstream.collect_media_links({"image": None}) == []
    assert upstream.collect_media_links({"image": {"path": ""}}) == []
    assert upstream.collect_media_links({"image": {"path": "media/../../x.png"}})[0]["safe"] is False


# --------------------------------------------------------------------------- #
# 质量结论（#20）
# --------------------------------------------------------------------------- #

def test_resolve_quality_reads_processing_20():
    quality = upstream.resolve_quality(rec_20("success", "accepted"))
    assert quality["present"] is True
    assert quality["status"] == "success"
    assert quality["decision"] == "accepted"
    assert quality["outcome"] == "accepted"


def test_resolve_quality_flagged_is_accepted_but_noted():
    quality = upstream.resolve_quality(rec_20("pending_review", "flagged",
                                              label="样本-质检：flagged"))
    assert quality["outcome"] == "accepted"
    assert quality["flagged"] is True


def test_resolve_quality_isolated_outcomes():
    assert upstream.resolve_quality(rec_20("isolated", "sample_quarantine"))["outcome"] == "isolated"
    assert upstream.resolve_quality(rec_20("failed", "origin_quarantine"))["outcome"] == "isolated"


def test_resolve_quality_skipped_means_absent_conclusion():
    quality = upstream.resolve_quality(rec_20("skipped", "skipped"))
    assert quality["present"] is False
    assert quality["outcome"] == "absent"


def test_resolve_quality_missing():
    quality = upstream.resolve_quality({"sample_id": "x"})
    assert quality["present"] is False
    assert quality["outcome"] == "absent"
    assert quality["source"] is None


# --------------------------------------------------------------------------- #
# 隔离结论（#21 / #20 / #17 / #18）
# --------------------------------------------------------------------------- #

def test_resolve_isolation_from_21():
    result = upstream.resolve_isolation(rec_21("isolated"))
    assert result["isolated"] is True
    assert result["source"] == "processing.21"


def test_resolve_isolation_from_20_quarantine():
    result = upstream.resolve_isolation(rec_20("isolated", "sample_quarantine"))
    assert result["isolated"] is True
    assert result["reason"] == "quality_sample_quarantine"


def test_resolve_isolation_from_17_incomplete_and_18_isolated():
    assert upstream.resolve_isolation(rec_17("incomplete"))["isolated"] is True
    assert upstream.resolve_isolation(rec_18("isolated"))["isolated"] is True
    assert upstream.resolve_isolation(rec_19("admitted"))["isolated"] is False


def test_resolve_isolation_not_isolated_when_pending():
    assert upstream.resolve_isolation(rec_21("pending_manual"))["isolated"] is False


def test_resolve_isolation_honours_explicit_lock_flags():
    assert upstream.resolve_isolation({"locked": True})["reason"] == "upstream_locked"
    assert upstream.resolve_isolation(
        {"metadata": {"retry_locked": True}})["source"] == "metadata.retry_locked"
    assert upstream.resolve_isolation({"metadata": {"locked": False}})["isolated"] is False


# --------------------------------------------------------------------------- #
# 授权与版本
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("where", ["admission", "metadata", "authorization", "top"])
def test_resolve_license_positions(where):
    assert upstream.resolve_license(rec_license("可训练", where)) == "可训练"


def test_resolve_license_missing():
    assert upstream.resolve_license({"sample_id": "x"}) is None


def test_resolve_version_prefers_metadata_version():
    assert upstream.resolve_version(rec_version("v2.1")) == "v2.1"
    assert upstream.resolve_version({"version": "v3"}) == "v3"
    assert upstream.resolve_version({"sample_id": "x"}) is None


# --------------------------------------------------------------------------- #
# 局部标注证据
# --------------------------------------------------------------------------- #

def test_resolve_local_annotation_evidence():
    assert upstream.resolve_local_annotation(rec_17("confirmed"))["present"] is True
    assert upstream.resolve_local_annotation(rec_18("prefilled"))["present"] is True
    assert upstream.resolve_local_annotation({"phrase_box_relations": []})["present"] is False
    assert upstream.resolve_local_annotation(rec_17("skipped"))["present"] is False


# --------------------------------------------------------------------------- #
# 综合解析
# --------------------------------------------------------------------------- #

def test_resolve_record_collects_spec():
    payload = make_record("tc-0001", rec_19(), rec_20(), rec_license(),
                          rec_text(), rec_image(), rec_version("v1.0"))
    spec = upstream.resolve_record(payload)
    assert spec["identifier"] == "tc-0001"
    assert spec["sample_id"] == "tc-0001"
    assert spec["pair_id"] is None
    assert spec["modalities"] == ("image", "text")
    assert spec["text"]["content"] == "一只猫趴在草地上"
    assert spec["quality"]["outcome"] == "accepted"
    assert spec["isolation"]["isolated"] is False
    assert spec["license"] == "可训练"
    assert spec["version"] == "v1.0"
    assert spec["unsafe_media"] == []


def test_resolve_record_pair_identifier_prefers_pair_id():
    payload = make_pair("pair-0007")
    spec = upstream.resolve_record(payload)
    assert spec["identifier"] == "pair-0007"
    assert spec["pair_id"] == "pair-0007"
    assert spec["sample_id"] is None


def test_resolve_record_flags_unsafe_media():
    payload = make_record("tc-x", rec_20(), rec_text(), {"image": {"path": "../x.png"}})
    spec = upstream.resolve_record(payload)
    assert len(spec["unsafe_media"]) == 1
    assert spec["unsafe_media"][0]["path"] == "../x.png"
