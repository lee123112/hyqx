"""业务规则测试（表1 #01 冻结口径）：来源/格式/授权/链接/分流/统计守恒。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import (DEFAULT_BATCH, DEFAULT_SOURCE, DEFAULT_SOURCE_ID,
                            DEFAULT_VERSION, SOURCE_AUDIO_LABEL, SOURCE_IMAGE_LABEL,
                            SOURCE_PLATFORM_LABEL, SOURCE_TEXT_LABEL, SOURCE_VIDEO_LABEL,
                            evaluate, make_record, write_media)

from utils import intake as it


# --------------------------------------------------------------------------- #
# 冻结配置
# --------------------------------------------------------------------------- #

def test_sources_are_frozen_and_normalized():
    assert tuple(it.SOURCES) == ("collection_platform", "text_pipeline", "image_pipeline",
                                 "audio_pipeline", "video_pipeline")
    assert it.normalize_source(SOURCE_PLATFORM_LABEL) == it.SOURCE_PLATFORM
    assert it.normalize_source(SOURCE_TEXT_LABEL) == it.SOURCE_TEXT
    assert it.normalize_source(SOURCE_IMAGE_LABEL) == it.SOURCE_IMAGE
    assert it.normalize_source(SOURCE_AUDIO_LABEL) == it.SOURCE_AUDIO
    assert it.normalize_source(SOURCE_VIDEO_LABEL) == it.SOURCE_VIDEO
    assert it.normalize_source("image_pipeline") == it.SOURCE_IMAGE
    assert it.normalize_source("  " + SOURCE_PLATFORM_LABEL + " ") == it.SOURCE_PLATFORM
    assert it.normalize_source("自建爬虫批次") is None
    assert it.normalize_source(None) is None
    assert it.normalize_source(123) is None
    assert it.source_label(it.SOURCE_IMAGE) == SOURCE_IMAGE_LABEL


def test_format_whitelist_is_exactly_table1():
    assert it.FORMAT_GROUPS == {
        "text": ("txt", "json", "csv"),
        "image": ("jpg", "png", "webp"),
        "audio": ("wav", "mp3", "flac"),
        "video": ("mp4", "mov", "avi"),
    }
    assert it.WHITELISTED_EXTENSIONS == tuple(
        ext for exts in it.FORMAT_GROUPS.values() for ext in exts)
    assert it.infer_modality("jpg") == "image"
    assert it.infer_modality("PNG") == "image"
    assert it.infer_modality("wav") == "audio"
    assert it.infer_modality("flac") == "audio"
    assert it.infer_modality("mov") == "video"
    assert it.infer_modality("csv") == "text"
    # 表1 白名单未列出的格式必须拒绝（含同格式别名 jpeg）
    assert it.infer_modality("jpeg") is None
    assert it.infer_modality("gif") is None
    assert it.infer_modality("mkv") is None
    assert it.infer_modality("") is None


def test_extension_of_link_and_query_stripping():
    assert it.extension_of("oss://bucket/a/b.JPG") == "jpg"
    assert it.extension_of("media/x.png?sign=1&x=2") == "png"
    assert it.extension_of("media/无扩展名") == ""
    assert it.extension_of(None) == ""


def test_license_normalization():
    assert it.normalize_license("可训练") == it.LICENSE_TRAINABLE
    assert it.normalize_license(" trainable ") == it.LICENSE_TRAINABLE
    assert it.normalize_license("不可训练") == it.LICENSE_UN_TRAINABLE
    assert it.normalize_license("untrainable") == it.LICENSE_UN_TRAINABLE
    assert it.normalize_license("待确认") == it.LICENSE_PENDING
    assert it.normalize_license("pending") == it.LICENSE_PENDING
    assert it.normalize_license("") == it.LICENSE_MISSING
    assert it.normalize_license(None) == it.LICENSE_MISSING
    assert it.normalize_license("待法务确认") == it.LICENSE_UNKNOWN


def test_link_transportability():
    remote = it.classify_link("oss://bucket/a.jpg")
    assert remote["remote"] is True and remote["scheme"] == "oss"
    assert it.classify_link("https://host/a.png")["remote"] is True
    assert it.classify_link("s3://bucket/a.wav")["remote"] is True

    relative = it.classify_link("media/a/b.jpg")
    assert relative["remote"] is False and relative["relative"] == "media/a/b.jpg"
    assert relative["scheme"] is None

    for bad in ("C:\\tmp\\a.jpg", "/abs/a.jpg", "../a.jpg", "media/../../a.jpg",
                "a\\b.jpg", "", "   ", None):
        with pytest.raises(ValueError):
            it.classify_link(bad)


def test_expand_objects_from_files_list_and_shorthand():
    objects = it.expand_objects({"sample_id": "s1",
                                 "files": [{"path": "a.jpg"}, {"path": "b.wav",
                                                              "modality": "audio"}]})
    assert [item["file_link"] for item in objects] == ["a.jpg", "b.wav"]
    assert [item["declared_modality"] for item in objects] == [None, "audio"]
    assert [item["file_index"] for item in objects] == [0, 1]

    shorthand = it.expand_objects({"sample_id": "s2",
                                   "media": [{"oss_uri": "oss://b/c.png"}]})
    assert len(shorthand) == 1 and shorthand[0]["file_link"] == "oss://b/c.png"

    single = it.expand_objects({"sample_id": "s3", "file_path": "x.mp4",
                                "modality": "video"})
    assert single[0]["file_link"] == "x.mp4"
    assert single[0]["declared_modality"] == "video"

    missing = it.expand_objects({"sample_id": "s4"})
    assert len(missing) == 1 and missing[0]["file_link"] is None


def test_expand_objects_rejects_non_object_file_entry():
    objects = it.expand_objects({"sample_id": "s5", "files": ["not-an-object"]})
    assert len(objects) == 1
    assert objects[0]["invalid_entry"] is True
    assert objects[0]["file_link"] is None


# --------------------------------------------------------------------------- #
# 四路分流
# --------------------------------------------------------------------------- #

def test_registered_object_keeps_only_link_and_own_namespace(biz):
    record = make_record("s-keep", extra={"processing": {"04": {"status": "ok"}},
                                         "scene_id": "scene-1", "未知字段": {"a": 1}})
    outcome = evaluate(record, biz=biz)
    assert outcome.decision == it.DECISION_REGISTERED
    assert outcome.file_check == it.FILE_CHECK_LINK_ONLY
    assert outcome.modality == "image"
    assert outcome.format_ext == "jpg"

    item = it.build_entry(outcome, business=biz, unique_id="t_1", run_id="run001",
                          lineno=1, role="workitem")
    info = item["operator_01"]
    assert info["file_link"] == "oss://bucket/obj/s-keep.jpg"
    assert info["source"] == it.SOURCE_IMAGE
    assert info["batch_id"] == DEFAULT_BATCH
    assert info["license"] == "可训练"
    assert item["scene_id"] == "scene-1"
    assert item["未知字段"] == {"a": 1}
    assert item["processing"]["04"] == {"status": "ok"}
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert item["processing"]["01"]["status"] == 1
    assert item["processing"]["01"]["process_status"] == "registered"
    assert item["processing"]["01"]["destination"] == "工作集"
    assert "outputFile" not in item


def test_format_not_allowed_goes_to_origin_quarantine(biz):
    outcome = evaluate(make_record("s-gif", ext="gif"), biz=biz)
    assert outcome.decision == it.DECISION_ORIGIN
    assert outcome.reason_code == "format_not_allowed"
    assert outcome.label == it.LABEL_ORIGIN
    assert outcome.destination == "原始隔离区"
    entry = it.build_entry(outcome, business=biz, unique_id="t_1", run_id="run001",
                           lineno=3, role="origin_quarantine")
    assert entry["lineno"] == 3
    assert entry["original_record"]["sample_id"] == "s-gif"


@pytest.mark.parametrize("link", ["C:\\tmp\\a.jpg", "/abs/a.jpg", "../a.jpg", "a\\b.jpg"])
def test_non_transportable_link_goes_to_origin_quarantine(biz, link):
    record = make_record("s-path", files=[{"path": link}])
    outcome = evaluate(record, biz=biz)
    assert outcome.decision == it.DECISION_ORIGIN
    assert outcome.reason_code == "link_not_transportable"


def test_missing_sample_id_goes_to_origin_quarantine(biz):
    record = make_record("s-x", drop=("sample_id",))
    outcome = evaluate(record, biz=biz, sample_id="")
    assert outcome.decision == it.DECISION_ORIGIN
    assert outcome.reason_code == "missing_sample_id"


def test_declared_modality_mismatch_goes_to_return(biz):
    record = make_record("s-mm", ext="jpg", modality="audio")
    record["modality"] = "audio"
    outcome = evaluate(record, biz=biz)
    assert outcome.decision == it.DECISION_RETURN
    assert outcome.reason_code == "modality_mismatch"


@pytest.mark.parametrize("license_value,expected", [
    ("不可训练", "license_untrainable"),
    ("待确认", "license_pending"),
    (None, "license_missing"),
    ("法务再看", "license_unknown"),
])
def test_non_trainable_license_goes_to_sample_quarantine(biz, license_value, expected):
    outcome = evaluate(make_record("s-lic", license=license_value), biz=biz)
    assert outcome.decision == it.DECISION_SAMPLE
    assert outcome.reason_code == expected
    assert outcome.label == it.LABEL_SAMPLE
    assert outcome.destination == "样本隔离库"
    assert outcome.action == "isolate"


@pytest.mark.parametrize("field", ["source", "source_id", "batch_id", "upstream_version"])
def test_incomplete_registration_goes_to_return(biz, field):
    record = make_record("s-reg", drop=(field,))
    outcome = evaluate(record, biz=biz)
    assert outcome.decision == it.DECISION_RETURN
    assert outcome.reason_code == "incomplete_registration"
    assert field in outcome.missing_fields
    entry = it.build_entry(outcome, business=biz, unique_id="t_1", run_id="run001",
                           lineno=1, role="return")
    assert entry["label"] == it.LABEL_RETURN
    assert entry["action"] == "return"
    assert entry["destination"] == "采集平台批次登记"


def test_unknown_source_goes_to_return(biz):
    outcome = evaluate(make_record("s-src", source="自建爬虫"), biz=biz)
    assert outcome.decision == it.DECISION_RETURN
    assert outcome.reason_code == "incomplete_registration"
    assert "source" in outcome.missing_fields


def test_missing_link_goes_to_return(biz):
    record = make_record("s-nolink", files=[])
    outcome = evaluate(record, biz=biz)
    assert outcome.decision == it.DECISION_RETURN
    assert outcome.reason_code == "missing_file_link"


def test_source_modality_check_is_opt_in(biz):
    record = make_record("s-cross", ext="wav", modality="audio",
                         source=SOURCE_IMAGE_LABEL)
    strict = dict(biz)
    strict["check_source_modality"] = True
    outcome = evaluate(record, biz=strict)
    assert outcome.decision == it.DECISION_RETURN
    assert outcome.reason_code == "source_modality_mismatch"
    # 默认关闭时按「来源与模态一致性不做校验」通过
    assert evaluate(record, biz=biz).decision == it.DECISION_REGISTERED


def test_duplicate_registration_is_detected_by_key(biz):
    first = evaluate(make_record("s-dup"), biz=biz)
    second = evaluate(make_record("s-dup"), biz=biz)
    assert it.registration_key(first) == it.registration_key(second)
    assert it.registration_key(first) != it.registration_key(
        evaluate(make_record("s-dup", ext="png"), biz=biz))


# --------------------------------------------------------------------------- #
# 本地文件检查（whitelist / header / strict）
# --------------------------------------------------------------------------- #

def test_header_mode_verifies_local_file(biz, tmp_path):
    record = make_record("s-local", remote=False, ext="png")
    write_media(tmp_path, "media/s-local.png", "png")
    outcome = evaluate(record, biz=biz, local_dirs=[tmp_path])
    assert outcome.decision == it.DECISION_REGISTERED
    assert outcome.file_check == it.FILE_CHECK_LOCAL_VERIFIED


def test_header_mode_detects_signature_mismatch(biz, tmp_path):
    record = make_record("s-broken", remote=False, ext="png")
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "s-broken.png").write_bytes(b"not a real png at all")
    outcome = evaluate(record, biz=biz, local_dirs=[tmp_path])
    assert outcome.decision == it.DECISION_ORIGIN
    assert outcome.reason_code == "file_signature_mismatch"


def test_empty_local_file_goes_to_origin_quarantine(biz, tmp_path):
    record = make_record("s-empty", remote=False, ext="wav")
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "s-empty.wav").write_bytes(b"")
    outcome = evaluate(record, biz=biz, local_dirs=[tmp_path])
    assert outcome.decision == it.DECISION_ORIGIN
    assert outcome.reason_code == "file_empty"


def test_whitelist_mode_skips_file_opening(biz, whitelist_config, tmp_path):
    from utils import schemas

    config = schemas.load_config(whitelist_config)
    record = make_record("s-skip", remote=False, ext="png")
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "s-skip.png").write_bytes(b"broken")
    outcome = evaluate(record, biz=config.business_dict(), local_dirs=[tmp_path])
    assert outcome.decision == it.DECISION_REGISTERED
    assert outcome.file_check == it.FILE_CHECK_LINK_ONLY


def test_strict_mode_requires_local_file(biz, strict_config, tmp_path):
    from utils import schemas

    business = schemas.load_config(strict_config).business_dict()
    missing = evaluate(make_record("s-nolocal", remote=False, ext="png"),
                       biz=business, local_dirs=[tmp_path])
    assert missing.decision == it.DECISION_ORIGIN
    assert missing.reason_code == "file_not_accessible"

    remote = evaluate(make_record("s-remote"), biz=business, local_dirs=[tmp_path])
    assert remote.decision == it.DECISION_ORIGIN
    assert remote.reason_code == "file_not_accessible"


# --------------------------------------------------------------------------- #
# 统计守恒
# --------------------------------------------------------------------------- #

def test_statistics_conservation_on_full_matrix(biz):
    stats = it.empty_statistics()
    rows = [
        make_record("s-a"),                       # registered
        make_record("s-b", ext="wav", modality="audio",
                    source=SOURCE_AUDIO_LABEL),  # registered
        make_record("s-c", ext="gif"),            # origin
        make_record("s-d", license="不可训练"),    # sample
        make_record("s-e", drop=("batch_id",)),   # return
    ]
    for index, record in enumerate(rows, start=1):
        outcome = evaluate(record, biz=biz, lineno=index + 1)
        it.accumulate(stats, outcome)
    stats["input"] = 5
    stats["valid"] = 5
    stats["input_lines"] = 7
    stats["blank_lines"] = 2
    stats["rejected"] = 0
    assert stats["objects"] == 5
    assert stats["registered"] == 2
    assert stats["origin_quarantined"] == 1
    assert stats["sample_quarantined"] == 1
    assert stats["returned"] == 1
    assert it.conservation_failures(stats) == []
    assert it.derive_status(stats, biz) == "partial"


def test_conservation_failures_are_reported(biz):
    stats = it.empty_statistics()
    it.accumulate(stats, evaluate(make_record("s-a"), biz=biz))
    stats["input"] = 5
    stats["valid"] = 1
    stats["input_lines"] = 7
    failures = it.conservation_failures(stats)
    assert any("input" in item for item in failures)
    stats["conservation_failures"] = failures
    assert it.derive_status(stats, biz) == "failed"


def test_derive_status_variants(biz):
    disabled = dict(biz)
    disabled["enabled"] = False
    assert it.derive_status(it.empty_statistics(), disabled) == "disabled"
    assert it.derive_status(it.empty_statistics(), biz) == "empty"
    clean = it.empty_statistics()
    it.accumulate(clean, evaluate(make_record("s-a"), biz=biz))
    clean["input"] = 1
    clean["valid"] = 1
    clean["input_lines"] = 1
    assert it.derive_status(clean, biz) == "success"


def test_empty_statistics_keys_are_stable():
    stats = it.empty_statistics()
    for key in ("input_lines", "blank_lines", "input", "rejected", "valid", "objects",
                "registered", "origin_quarantined", "sample_quarantined", "returned",
                "duplicates", "link_only", "local_verified", "batches"):
        assert key in stats, key


def test_default_business_exposes_frozen_bill(biz):
    assert biz["enabled"] is True
    assert biz["file_check_mode"] == it.FILE_CHECK_MODE_HEADER
    assert biz["dedupe"] is True
    assert biz["check_source_modality"] is False
    assert list(biz["sources"]) == list(it.SOURCES)
    assert biz["allowed_license"] == it.ALLOWED_LICENSE_LABEL
    assert biz["transport"]["format"] == "crossmodal-files-v1"


def test_batch_registry_groups_by_source_and_batch(biz):
    stats = it.empty_statistics()
    outcomes = []
    for record in (make_record("r1"), make_record("r2"),
                   make_record("r3", batch_id="BATCH-20261001-02"),
                   make_record("r4", license="不可训练")):
        outcome = evaluate(record, biz=biz)
        it.accumulate(stats, outcome)
        outcomes.append(outcome)
    registry = it.build_batch_registry(
        outcomes, business=biz, unique_id="t_1", run_id="run001",
        statistics=stats, generated_at="2026-10-08T10:00:00+08:00")
    assert registry["format"] == "crossmodal-batch-registry-v1"
    assert registry["entrypoint"] == it.MAIN_NAME
    assert len(registry["batches"]) == 2
    first = [item for item in registry["batches"]
             if item["batch_id"] == DEFAULT_BATCH][0]
    assert first["registered"] == 2
    assert first["source"] == it.SOURCE_IMAGE
    assert first["source_label"] == DEFAULT_SOURCE
    assert first["source_id"] == DEFAULT_SOURCE_ID
    assert first["upstream_version"] == DEFAULT_VERSION
    assert registry["totals"]["registered"] == 3


def test_reject_entry_truncates_raw_preview(biz):
    long_line = "{" + "x" * 1000
    entry = it.build_reject_entry(7, "json_parse_error", long_line, biz,
                                  unique_id="t_1", run_id="run001")
    assert entry["lineno"] == 7
    assert entry["label"] == it.LABEL_ORIGIN
    assert entry["destination"] == "原始隔离区"
    assert len(entry["preview"]) <= it.MAX_PREVIEW_CHARS
