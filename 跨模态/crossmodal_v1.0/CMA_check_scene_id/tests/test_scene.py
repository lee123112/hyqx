"""业务模块 utils/scene.py 单元测试（表1 #02 冻结口径）。

覆盖：模态判定、场景编号取值来源、补编号（只绑定一次）、场景组模态数核验、
开关关闭（配对集跳过）、设备并发只读、统计守恒、配置定义域与只读项。
"""
from __future__ import annotations

import pytest

from utils import scene
from utils.schemas import BusinessOverrides, ConfigError, OperatorConfig


# --------------------------------------------------------------------- 基础
def test_modality_order_is_four_modalities():
    assert scene.MODALITY_ORDER == ("text", "image", "video", "audio")


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("   ", False), ([], False), ({}, False),
    ("text", True), ({"content": "x"}, True), (["a"], True), (0, True), (False, True),
])
def test_is_present(value, expected):
    assert scene.is_present(value) is expected


def test_modalities_of_counts_present_fields():
    record = {"text": "t", "image": {"path": "a.png"}, "video": None, "audio": "  "}
    assert scene.modalities_of(record) == ["text", "image"]


def test_modalities_of_empty_record():
    assert scene.modalities_of({"sample_id": "s"}) == []


# ------------------------------------- 兼容 #01 文件登记项级输入（files[] 一行一文件）
def test_modalities_of_reads_file_entries():
    record = {"sample_id": "s", "files": [
        {"modality": "image", "path": "media/a.png"},
        {"modality": "text", "path": "media/a.txt"}]}
    assert scene.modalities_of(record) == ["text", "image"]


def test_modalities_of_infers_modality_from_extension():
    record = {"sample_id": "s", "files": [{"path": "media/clip.mp4"}]}
    assert scene.modalities_of(record) == ["video"]


def test_modalities_of_unions_sample_fields_and_file_entries():
    record = {"sample_id": "s", "text": "t", "files": [{"modality": "audio",
                                                        "path": "media/a.wav"}]}
    assert scene.modalities_of(record) == ["text", "audio"]


def test_modalities_of_falls_back_to_top_level_modality():
    assert scene.modalities_of({"sample_id": "s", "modality": "img"}) == ["image"]


def test_modalities_of_falls_back_to_operator_01_namespace():
    record = {"sample_id": "s", "operator_01": {"modality": "voice",
                                                "file_link": "media/a.wav"}}
    assert scene.modalities_of(record) == ["audio"]


def test_modalities_of_reads_operator_01_file_link_extension():
    record = {"sample_id": "s", "operator_01": {"file_link": "media/a.png"}}
    assert scene.modalities_of(record) == ["image"]


@pytest.mark.parametrize("value,expected", [
    ("img", "image"), ("IMAGE", "image"), ("voice", "audio"), ("vid", "video"),
    ("caption", "text"), ("a.PNG", "image"), (["text"], "text"), ("unknown", None),
    (None, None), ("", None),
])
def test_normalise_modality(value, expected):
    assert scene._normalise_modality(value) == expected


def test_file_references_returns_modality_and_path():
    record = {"files": [{"modality": "image", "path": "media/a.png"},
                        {"modality": "voice", "link": "media/b.wav"},
                        {"modality": "image"}]}
    assert scene.file_references(record) == [("image", "media/a.png"),
                                             ("audio", "media/b.wav")]


def test_file_entries_ignores_non_list():
    assert scene.file_entries({"files": "media/a.png"}) == []
    assert scene.file_entries({"files": [1, "x", {"path": "a.png"}]}) == [{"path": "a.png"}]


def test_file_discriminator_none_for_sample_level_record():
    assert scene.file_discriminator({"sample_id": "s", "text": "t"}) is None


def test_file_discriminator_uses_files_and_file_link():
    assert scene.file_discriminator({"files": [{"path": "b"}, {"path": "a"}]}) == "files:a|b"
    assert scene.file_discriminator({"file_link": "media/a.png"}) == "file:media/a.png"
    assert scene.file_discriminator({"operator_01": {"file_link": "oss://b"}}) == "file:oss://b"


@pytest.mark.parametrize("value,expected", [
    ("http://a/b.png", True), ("https://a/b", True), ("oss://b/c", True),
    ("s3://b/c", True), ("media/a.png", False), ("/abs/a.png", False), (None, False),
])
def test_is_remote_reference(value, expected):
    assert scene.is_remote_reference(value) is expected


def test_scan_record_same_sample_id_different_files_is_not_duplicate():
    """#01 一行一个文件登记项：同一对象的多个文件不是重复登记。"""
    stats = scene.initial_statistics()
    seen = set()
    section = _section()
    first = scene.scan_record({"sample_id": "s-1", "scene_id": "scene-A",
                               "files": [{"modality": "image", "path": "media/a.png"}]},
                              1, stats, seen, section, "t_f")
    second = scene.scan_record({"sample_id": "s-1", "scene_id": "scene-A",
                                "files": [{"modality": "text", "path": "media/a.txt"}]},
                               2, stats, seen, section, "t_f")
    assert first["reject"] is False and second["reject"] is False
    assert stats["duplicate_records"] == 0
    groups = scene.bind_scene_groups([first, second], section, stats)
    assert groups[0]["status"] == scene.STATUS_VERIFIED
    assert groups[0]["modalities"] == ["text", "image"]


def test_scan_record_same_sample_id_and_same_file_is_duplicate():
    stats = scene.initial_statistics()
    seen = set()
    section = _section()
    payload = {"sample_id": "s-1", "scene_id": "scene-A",
               "files": [{"modality": "image", "path": "media/a.png"}]}
    scene.scan_record(dict(payload), 1, stats, seen, section, "t_f")
    duplicate = scene.scan_record(dict(payload), 2, stats, seen, section, "t_f")
    assert duplicate["route"] == scene.ROUTE_REJECT
    assert duplicate["reason"] == scene.REASON_DUPLICATE
    assert stats["duplicate_records"] == 1


# ----------------------------------------------------------- 场景编号取值来源
@pytest.mark.parametrize("key", ["scene_id", "sceneId", "group_id", "groupId", "scene"])
def test_resolve_platform_scene_id_top_level_keys(key):
    assert scene.resolve_platform_scene_id({key: "scene-A"}) == "scene-A"


@pytest.mark.parametrize("key", ["scene_id", "sceneId", "group_id", "groupId", "scene"])
def test_resolve_platform_scene_id_metadata_keys(key):
    assert scene.resolve_platform_scene_id({"metadata": {key: "scene-B"}}) == "scene-B"


def test_resolve_platform_scene_id_prefers_top_level():
    record = {"scene_id": "top", "metadata": {"scene_id": "meta"}}
    assert scene.resolve_platform_scene_id(record) == "top"


def test_resolve_platform_scene_id_none_when_blank():
    assert scene.resolve_platform_scene_id({"scene_id": "  "}) is None
    assert scene.resolve_platform_scene_id({}) is None


def test_resolve_bound_scene_id_reads_previous_binding():
    record = {"processing": {"02": {"scene_id": "scene-C", "scene_source": "supplemented"}}}
    bound = scene.resolve_bound_scene_id(record)
    assert bound == ("scene-C", "supplemented")


def test_resolve_bound_scene_id_none_without_processing():
    assert scene.resolve_bound_scene_id({"sample_id": "s"}) == (None, None)
    assert scene.resolve_bound_scene_id({"processing": "bad"}) == (None, None)
    assert scene.resolve_bound_scene_id({"processing": {"02": "bad"}}) == (None, None)


def test_generate_scene_id_is_deterministic_and_unique():
    first = scene.generate_scene_id("task_file", "s-001")
    again = scene.generate_scene_id("task_file", "s-001")
    other = scene.generate_scene_id("task_file", "s-002")
    assert first == again
    assert first != other
    assert first.startswith(scene.GENERATED_SCENE_PREFIX)
    assert len(first) == len(scene.GENERATED_SCENE_PREFIX) + 12


def test_generate_scene_id_differs_between_tasks():
    assert scene.generate_scene_id("taskA_file", "s") != scene.generate_scene_id("taskB_file", "s")


def test_group_id_matches_sibling_operator_convention():
    assert scene.group_id_for("scene-A") == "grp-scene-scene-A"


def test_group_id_sanitises_unsafe_characters():
    assert scene.group_id_for("a/b c") == "grp-scene-a_b_c"


def test_group_id_truncates_long_ids():
    value = scene.group_id_for("x" * 500)
    assert len(value) <= len(scene.GROUP_ID_PREFIX) + scene.GROUP_ID_MAX_LEN


# ------------------------------------------------------------ 设备并发只读
def test_capture_readonly_extracts_platform_fields():
    record = {
        "capture_platform": "platform-1",
        "device_concurrency": 4,
        "metadata": {"device_failover": "readonly", "capturePlatform": "ignored"},
    }
    captured = scene.capture_readonly(record)
    assert captured["capture_platform"] == "platform-1"
    assert captured["device_concurrency"] == 4
    assert captured["device_failover"] == "readonly"


def test_capture_readonly_empty_when_absent():
    assert scene.capture_readonly({"sample_id": "s"}) == {}


# ------------------------------------------------------------ 配置定义域
def test_config_defaults_match_table1():
    config = OperatorConfig(operator={"name": "scene_id_verification", "version": "v1.0.0"},
                            stage_id="S1")
    assert config.enabled is False                 # 表1 #02 默认关闭
    assert config.scene_id_source == scene.SCENE_SOURCE_PLATFORM
    assert config.min_modalities == scene.DEFAULT_MIN_MODALITIES == 2
    assert config.bind_once is True                # 只绑定一次（只读）
    assert config.device_control == scene.DEVICE_CONTROL_READ_ONLY


@pytest.mark.parametrize("value", [1, 0, 5, 6, -1, "2", 2.0, True, None])
def test_min_modalities_out_of_range_rejected(value):
    with pytest.raises(Exception):
        OperatorConfig(operator={"name": "n", "version": "v"}, min_modalities=value)


@pytest.mark.parametrize("value", [2, 3, 4])
def test_min_modalities_range_accepted(value):
    config = OperatorConfig(operator={"name": "n", "version": "v"}, min_modalities=value)
    assert config.min_modalities == value


@pytest.mark.parametrize("value", ["platform", "generate"])
def test_scene_id_source_accepted(value):
    config = OperatorConfig(operator={"name": "n", "version": "v"}, scene_id_source=value)
    assert config.scene_id_source == value


@pytest.mark.parametrize("value", ["platform ", "both", "none", "", "PLATFORM", None, 1])
def test_scene_id_source_rejected(value):
    with pytest.raises(Exception):
        OperatorConfig(operator={"name": "n", "version": "v"}, scene_id_source=value)


def test_bind_once_cannot_be_disabled():
    with pytest.raises(Exception):
        OperatorConfig(operator={"name": "n", "version": "v"}, bind_once=False)


def test_device_control_is_read_only():
    with pytest.raises(Exception):
        OperatorConfig(operator={"name": "n", "version": "v"}, device_control="schedule")


def test_enabled_must_be_strict_bool():
    with pytest.raises(Exception):
        OperatorConfig(operator={"name": "n", "version": "v"}, enabled="false")
    config = OperatorConfig(operator={"name": "n", "version": "v"}, enabled=True)
    assert config.enabled is True


def test_unknown_config_key_rejected():
    with pytest.raises(Exception):
        OperatorConfig(operator={"name": "n", "version": "v"}, unknown_key=1)


def test_overrides_allow_enabled_source_and_min_modalities():
    parsed = BusinessOverrides(enabled=True, scene_id_source="generate", min_modalities=3)
    assert parsed.provided() == {"enabled": True, "scene_id_source": "generate",
                                 "min_modalities": 3}


def test_overrides_reject_readonly_and_unknown_keys():
    for payload in ({"bind_once": False}, {"device_control": "schedule"},
                    {"operator": {"name": "x"}}, {"stage_id": "S9"}, {"nope": 1}):
        with pytest.raises(Exception):
            BusinessOverrides(**payload)


def test_initial_statistics_shape():
    stats = scene.initial_statistics()
    for key in ("input_lines", "blank_lines", "input_records", "valid_records",
                "origin_rejected", "duplicate_records", "main_entries", "fusion_records",
                "pairing_records", "pairing_no_scene_id", "pairing_insufficient_modalities",
                "verified_groups", "insufficient_groups", "scene_groups_total",
                "platform_scene_ids", "supplemented_scene_ids", "scene_groups_verified_members",
                "media_carried", "media_missing", "media_errors", "media_remote",
                "origin_entries",
                "reject_entries", "pairing_entries", "group_entries", "disabled_records"):
        assert stats[key] == 0, key
    assert stats["degraded"] is False
    assert stats["active_backend"] == "rule"


def test_bump_increments_and_creates():
    stats = {}
    scene.bump(stats, "x")
    scene.bump(stats, "x", 4)
    assert stats["x"] == 5


# ------------------------------------------------------------------- 记录扫描
def _section(**overrides):
    base = {
        "enabled": True,
        "scene_id_source": "platform",
        "min_modalities": 2,
        "operator": "scene_id_verification",
        "operator_version": "v1.0.0",
        "stage_id": "S1",
    }
    base.update(overrides)
    return base


def test_scan_record_platform_id_resolved():
    stats = scene.initial_statistics()
    candidate = scene.scan_record(
        {"sample_id": "s-1", "scene_id": "scene-A", "text": "t", "image": "a.png"},
        1, stats, set(), _section(), "task_file")
    assert candidate["scene_id"] == "scene-A"
    assert candidate["scene_source"] == scene.SCENE_SOURCE_PLATFORM
    assert candidate["modalities"] == ["text", "image"]
    assert candidate["reject"] is False
    assert stats["platform_scene_ids"] == 1


def test_scan_record_reuses_previous_binding():
    stats = scene.initial_statistics()
    candidate = scene.scan_record(
        {"sample_id": "s-1", "text": "t", "image": "a.png",
         "processing": {"02": {"scene_id": "scene-old", "scene_source": "platform"}}},
        1, stats, set(), _section(), "task_file")
    assert candidate["scene_id"] == "scene-old"
    assert stats["platform_scene_ids"] == 1
    assert stats["supplemented_scene_ids"] == 0


def test_scan_record_platform_mode_keeps_missing_id_empty():
    stats = scene.initial_statistics()
    candidate = scene.scan_record({"sample_id": "s-1", "text": "t"}, 1, stats, set(),
                                  _section(), "task_file")
    assert candidate["scene_id"] is None
    assert candidate["scene_source"] == scene.SOURCE_NONE
    assert stats["platform_scene_ids"] == 0
    assert stats["supplemented_scene_ids"] == 0


def test_scan_record_generate_mode_binds_once():
    stats = scene.initial_statistics()
    section = _section(scene_id_source="generate")
    first = scene.scan_record({"sample_id": "s-1", "text": "t", "image": "a.png"},
                              1, stats, set(), section, "task_file")
    second = scene.scan_record({"sample_id": "s-2", "text": "t", "image": "a.png"},
                               2, stats, set(), section, "task_file")
    assert first["scene_source"] == scene.SCENE_SOURCE_SUPPLEMENTED
    assert first["scene_id"].startswith(scene.GENERATED_SCENE_PREFIX)
    assert first["scene_id"] != second["scene_id"]
    assert stats["supplemented_scene_ids"] == 2


def test_scan_record_duplicate_sample_id_is_rejected_not_isolated():
    stats = scene.initial_statistics()
    seen = set()
    scene.scan_record({"sample_id": "s-1", "text": "t"}, 1, stats, seen, _section(), "t_f")
    candidate = scene.scan_record({"sample_id": "s-1", "text": "t"}, 2, stats, seen,
                                  _section(), "t_f")
    assert candidate["route"] == scene.ROUTE_REJECT
    assert candidate["reason"] == scene.REASON_DUPLICATE
    assert candidate["destination"] == scene.DESTINATION_OPERATOR_REJECTS
    assert stats["duplicate_records"] == 1


def test_scan_record_missing_sample_id_goes_origin_quarantine():
    stats = scene.initial_statistics()
    candidate = scene.scan_record({"text": "t"}, 3, stats, set(), _section(), "t_f")
    assert candidate["route"] == scene.ROUTE_ORIGIN
    assert candidate["reason"] == scene.REASON_MISSING_SAMPLE_ID
    assert candidate["destination"] == scene.DESTINATION_RAW_ISOLATION
    assert stats["origin_rejected"] == 1


def test_scan_record_never_overwrites_upstream_processing():
    stats = scene.initial_statistics()
    record = {"sample_id": "s-1", "scene_id": "scene-A", "text": "t", "image": "a.png",
              "processing": {"01": {"status": "success"}}, "unknown_ext": [1, 2]}
    candidate = scene.scan_record(record, 1, stats, set(), _section(), "t_f")
    built = scene.build_record(candidate, _section())
    assert built["processing"]["01"] == {"status": "success"}
    assert built["unknown_ext"] == [1, 2]
    assert built["processing"]["02"]["scene_id"] == "scene-A"


# --------------------------------------------------------------- 场景组核验
def _candidate(sample_id, scene_id, modalities, source="platform"):
    return {
        "line": 1, "sample_id": sample_id, "scene_id": scene_id, "scene_source": source,
        "modalities": list(modalities), "record": {"sample_id": sample_id},
        "reject": False, "route": None, "status": None, "reason": None, "label": None,
        "action": None, "destination": None,
    }


def test_bind_scene_groups_verified_when_union_reaches_minimum():
    stats = scene.initial_statistics()
    candidates = [_candidate("s-1", "scene-A", ["text"]),
                  _candidate("s-2", "scene-A", ["image"])]
    groups = scene.bind_scene_groups(candidates, _section(), stats)
    assert len(groups) == 1
    assert groups[0]["status"] == scene.STATUS_VERIFIED
    assert groups[0]["modalities"] == ["text", "image"]
    assert groups[0]["member_count"] == 2
    assert all(item["route"] == scene.ROUTE_FUSION for item in candidates)
    assert stats["verified_groups"] == 1
    assert stats["fusion_records"] == 2
    assert stats["scene_groups_verified_members"] == 2


def test_bind_scene_groups_insufficient_goes_to_pairing_without_isolation():
    stats = scene.initial_statistics()
    candidates = [_candidate("s-1", "scene-B", ["text"]),
                  _candidate("s-2", "scene-B", ["text"])]
    groups = scene.bind_scene_groups(candidates, _section(), stats)
    assert groups[0]["status"] == scene.STATUS_INSUFFICIENT
    for item in candidates:
        assert item["route"] == scene.ROUTE_PAIRING
        assert item["status"] == scene.STATUS_INSUFFICIENT
        assert item["destination"] == scene.DESTINATION_PAIRING_ROUTE
    assert stats["insufficient_groups"] == 1
    assert stats["pairing_insufficient_modalities"] == 2
    assert stats["fusion_records"] == 0


def test_bind_scene_groups_respects_min_modalities_four():
    stats = scene.initial_statistics()
    candidates = [
        _candidate("s-1", "scene-A", ["text", "image", "video", "audio"]),
        _candidate("s-2", "scene-B", ["text", "image"])]
    groups = scene.bind_scene_groups(candidates, _section(min_modalities=4), stats)
    by_id = {item["scene_id"]: item for item in groups}
    assert by_id["scene-A"]["status"] == scene.STATUS_VERIFIED
    assert by_id["scene-B"]["status"] == scene.STATUS_INSUFFICIENT
    assert stats["verified_groups"] == 1
    assert stats["insufficient_groups"] == 1


def test_bind_scene_groups_no_scene_id_routes_to_pairing():
    stats = scene.initial_statistics()
    candidates = [_candidate("s-1", None, ["text", "image"], source=scene.SOURCE_NONE)]
    groups = scene.bind_scene_groups(candidates, _section(), stats)
    assert groups == []
    assert candidates[0]["route"] == scene.ROUTE_PAIRING
    assert candidates[0]["status"] == scene.STATUS_NO_SCENE_ID
    assert candidates[0]["reason"] == scene.REASON_NO_SCENE_ID
    assert stats["pairing_no_scene_id"] == 1
    assert stats["scene_groups_total"] == 0


def test_bind_scene_groups_supplemented_single_member_group():
    stats = scene.initial_statistics()
    candidates = [_candidate("s-1", "scene-auto-abc", ["text", "audio"],
                             source=scene.SCENE_SOURCE_SUPPLEMENTED)]
    groups = scene.bind_scene_groups(candidates, _section(), stats)
    assert groups[0]["status"] == scene.STATUS_VERIFIED
    assert groups[0]["scene_source"] == scene.SCENE_SOURCE_SUPPLEMENTED
    assert candidates[0]["route"] == scene.ROUTE_FUSION


def test_bind_scene_groups_disabled_passes_everything_to_pairing():
    stats = scene.initial_statistics()
    candidates = [_candidate("s-1", "scene-A", ["text", "image"]),
                  _candidate("s-2", None, ["image"], source=scene.SOURCE_NONE)]
    groups = scene.bind_scene_groups(candidates, _section(enabled=False), stats)
    assert groups == []
    assert stats["disabled_records"] == 2
    assert stats["pairing_records"] == 2
    assert stats["fusion_records"] == 0
    assert stats["scene_groups_total"] == 0
    for item in candidates:
        assert item["route"] == scene.ROUTE_PAIRING
        assert item["status"] == scene.STATUS_DISABLED
        assert item["reason"] == scene.REASON_TOOL_DISABLED
        assert item["scene_source"] == scene.SOURCE_DISABLED


def test_bind_scene_groups_rejects_are_not_grouped():
    stats = scene.initial_statistics()
    rejected = _candidate("s-1", "scene-A", ["text"])
    rejected["reject"] = True
    rejected["route"] = scene.ROUTE_ORIGIN
    candidates = [rejected, _candidate("s-2", "scene-A", ["image"])]
    groups = scene.bind_scene_groups(candidates, _section(), stats)
    assert len(groups) == 1
    assert rejected["route"] == scene.ROUTE_ORIGIN
    assert stats["scene_groups_total"] == 1


# --------------------------------------------------------------- 产物构造
def test_build_record_shape_and_processing_namespace():
    stats = scene.initial_statistics()
    section = _section()
    candidate = scene.scan_record(
        {"sample_id": "s-1", "scene_id": "scene-A", "text": "t", "image": "a.png",
         "capture_platform": "capture-1"}, 1, stats, set(), section, "task_file")
    scene.bind_scene_groups([candidate], section, stats)
    built = scene.build_record(candidate, section)
    assert built["scene_id"] == "scene-A"
    assert built["group_id"] == "grp-scene-scene-A"
    # [v4.2] status 双字段：输出记录 int status + 原字符串迁入 process_status
    assert built["scene_group"]["status"] == 1
    assert built["scene_group"]["process_status"] == scene.STATUS_VERIFIED
    node = built["processing"]["02"]
    assert node["status"] == 1
    assert node["process_status"] == scene.STATUS_VERIFIED
    assert node["route"] == scene.ROUTE_FUSION
    assert node["scene_source"] == scene.SCENE_SOURCE_PLATFORM
    assert node["min_modalities"] == 2
    assert node["bound_once"] is True
    assert node["enabled"] is True
    assert node["capture_readonly"] == {"capture_platform": "capture-1"}
    assert node["model"] is None


def test_build_record_disabled_does_not_invent_scene_fields():
    stats = scene.initial_statistics()
    section = _section(enabled=False)
    candidate = scene.scan_record({"sample_id": "s-1", "text": "t", "image": "a.png"},
                                  1, stats, set(), section, "task_file")
    scene.bind_scene_groups([candidate], section, stats)
    built = scene.build_record(candidate, section)
    assert "scene_id" not in built
    assert "group_id" not in built
    assert "scene_group" not in built or built["scene_group"] is None
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert built["processing"]["02"]["status"] == 0
    assert built["processing"]["02"]["process_status"] == scene.STATUS_DISABLED


def test_build_record_is_deterministic():
    stats = scene.initial_statistics()
    section = _section()
    record = {"sample_id": "s-1", "scene_id": "scene-A", "text": "t", "image": "a.png"}
    first = scene.scan_record(dict(record), 1, stats, set(), section, "task_file")
    second = scene.scan_record(dict(record), 1, stats, set(), section, "task_file")
    scene.bind_scene_groups([first], section, stats)
    scene.bind_scene_groups([second], section, stats)
    assert scene.build_record(first, section) == scene.build_record(second, section)


def test_build_group_entry_fields():
    stats = scene.initial_statistics()
    section = _section()
    candidates = [_candidate("s-1", "scene-A", ["text"]),
                  _candidate("s-2", "scene-A", ["video"])]
    groups = scene.bind_scene_groups(candidates, section, stats)
    entry = scene.build_group_entry(groups[0], section)
    assert entry["scene_id"] == "scene-A"
    assert entry["group_id"] == "grp-scene-scene-A"
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert entry["status"] == 1
    assert entry["process_status"] == scene.STATUS_VERIFIED
    assert entry["modalities"] == ["text", "video"]
    assert entry["member_count"] == 2
    assert entry["member_sample_ids"] == ["s-1", "s-2"]
    assert entry["min_modalities"] == 2
    assert entry["scene_source"] == scene.SCENE_SOURCE_PLATFORM


def test_busy_line_entry_contract_fields():
    entry = scene.busy_line_entry(operator="scene_id_verification", stage_id="S1", line=9,
                                  reason="json_parse_error", message="JSON 解析失败",
                                  event_time="2026-10-06T10:00:00+08:00", batch_id="b-1")
    for key in ("line", "reason", "message", "label", "stage", "operator", "action",
                "destination", "event_time", "batch_id", "retry_count"):
        assert key in entry
    assert entry["destination"] == scene.DESTINATION_RAW_ISOLATION
    assert entry["retry_count"] == 0


def test_reject_entry_contract_fields():
    entry = scene.reject_entry(operator="scene_id_verification", stage_id="S1", line=4,
                               sample_id="s-1", reason=scene.REASON_DUPLICATE,
                               message="重复 sample_id", event_time="t", batch_id=None)
    assert entry["destination"] == scene.DESTINATION_OPERATOR_REJECTS
    assert entry["label"] == scene.LABEL_DUPLICATE
    assert entry["sample_id"] == "s-1"


def test_check_conservation_accepts_consistent_stats():
    stats = scene.initial_statistics()
    stats.update({
        "input_lines": 10, "blank_lines": 1, "input_records": 9,
        "valid_records": 8, "origin_rejected": 1, "duplicate_records": 1,
        "main_entries": 7, "fusion_records": 3, "pairing_records": 4,
        "pairing_no_scene_id": 1, "pairing_insufficient_modalities": 3,
        "verified_groups": 1, "insufficient_groups": 1, "scene_groups_total": 2,
        "scene_groups_verified_members": 3,
        "origin_entries": 1, "reject_entries": 1, "pairing_entries": 4, "group_entries": 2,
        "enabled": True,
    })
    assert scene.check_conservation(stats) is True


def test_check_conservation_detects_imbalance():
    stats = scene.initial_statistics()
    stats.update({
        "input_lines": 10, "blank_lines": 1, "input_records": 9,
        "valid_records": 8, "origin_rejected": 1, "duplicate_records": 0,
        "main_entries": 7, "fusion_records": 3, "pairing_records": 4,
        "pairing_no_scene_id": 1, "pairing_insufficient_modalities": 3,
        "verified_groups": 1, "insufficient_groups": 1, "scene_groups_total": 2,
        "scene_groups_verified_members": 2,   # 与 fusion_records 不一致
        "origin_entries": 1, "reject_entries": 0, "pairing_entries": 4, "group_entries": 2,
        "enabled": True,
    })
    assert scene.check_conservation(stats) is False


def test_check_conservation_requires_no_groups_when_disabled():
    stats = scene.initial_statistics()
    stats.update({
        "input_lines": 2, "blank_lines": 0, "input_records": 2,
        "valid_records": 2, "origin_rejected": 0, "duplicate_records": 0,
        "main_entries": 2, "fusion_records": 1, "pairing_records": 1,
        "verified_groups": 1, "scene_groups_total": 1, "scene_groups_verified_members": 1,
        "enabled": False,
    })
    assert scene.check_conservation(stats) is False
