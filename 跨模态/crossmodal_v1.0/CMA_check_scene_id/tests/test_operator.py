"""inference.run 端到端测试：真实样例、异常输入、配置、隔离、守恒与血缘。

所有断言基于真实落盘产物与真实统计，不使用固定分数或伪造降级。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference
from utils import scene
from utils.schemas import ConfigError
from tests.support import count_lines, read_json, read_jsonl, raw_lines


# --------------------------------------------------------------------- 工具
def write_lines(path, lines):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join("%s\n" % line for line in lines), encoding="utf-8")
    return target


def run_on(sample_dir, output_root, unique_id="task_file", **kwargs):
    return inference.run(input_file=str(Path(sample_dir) / "demo.jsonl"),
                         unique_id=unique_id, output_root=str(output_root),
                         allow_external_input=True, **kwargs)


# ------------------------------------------------------------ 默认关闭（配对集）
def test_default_disabled_routes_everything_to_pairing(sample_dir, output_root):
    result = run_on(sample_dir, output_root)
    stats = result["statistics"]
    # 输入本身含 3 行原始文件问题与 1 条重复标识，状态如实为 partial（见文件末断言）
    assert result["status"] == "partial"
    assert stats["enabled"] is False
    assert stats["input_lines"] == 16
    assert stats["blank_lines"] == 1
    assert stats["input_records"] == 15
    assert stats["origin_rejected"] == 3
    assert stats["valid_records"] == 12
    assert stats["duplicate_records"] == 1
    assert stats["main_entries"] == 11
    assert stats["disabled_records"] == 11
    assert stats["pairing_records"] == 11
    assert stats["fusion_records"] == 0
    assert stats["scene_groups_total"] == 0
    assert stats["supplemented_scene_ids"] == 0
    assert stats["conservation_ok"] is True
    # 工具未启用 ≠ 数据无问题：输入里仍有 3 行原始文件问题 + 1 条重复标识，
    # 因此按契约如实上报 partial，而不是用 success 掩盖输入质量问题
    assert result["status"] == "partial"


def test_disabled_records_keep_upstream_scene_id_untouched(sample_dir, output_root):
    result = run_on(sample_dir, output_root)
    main = read_jsonl(Path(result["runDir"]) / inference.MAIN_NAME)
    by_id = {item["sample_id"]: item for item in main}
    assert by_id["s-001"]["scene_id"] == "scene-A"
    assert "group_id" not in by_id["s-006"]
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert by_id["s-001"]["processing"]["02"]["status"] == 0
    assert by_id["s-001"]["processing"]["02"]["process_status"] == scene.STATUS_DISABLED


# ------------------------------------------------------------ 融合集（打开）
def test_enabled_platform_groups_and_pairing_split(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["enabled"] is True
    assert stats["valid_records"] == 12
    assert stats["main_entries"] == 11
    # platform_scene_ids 统计的是「带采集平台编号的记录条数」（7 条），
    # 去重后的场景编号只有 4 个，即 scene_groups_total
    assert stats["platform_scene_ids"] == 7
    assert stats["scene_groups_total"] == 4
    assert stats["supplemented_scene_ids"] == 0
    assert stats["scene_groups_total"] == 4
    assert stats["verified_groups"] == 2
    assert stats["insufficient_groups"] == 2
    assert stats["fusion_records"] == 4
    assert stats["pairing_records"] == 7
    assert stats["pairing_no_scene_id"] == 4
    assert stats["pairing_insufficient_modalities"] == 3
    assert stats["scene_groups_verified_members"] == stats["fusion_records"]
    assert stats["conservation_ok"] is True


def test_enabled_fusion_group_scene_a_is_verified(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    groups = read_jsonl(Path(result["runDir"]) / scene.GROUPS_NAME)
    by_id = {item["scene_id"]: item for item in groups}
    group_a = by_id["scene-A"]
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert group_a["status"] == 1
    assert group_a["process_status"] == scene.STATUS_VERIFIED
    assert group_a["modalities"] == ["text", "image", "video", "audio"]
    assert group_a["member_count"] == 3
    assert group_a["group_id"] == "grp-scene-scene-A"
    assert by_id["scene-B"]["status"] == 0
    assert by_id["scene-B"]["process_status"] == scene.STATUS_INSUFFICIENT
    assert by_id["scene-B"]["modalities"] == ["text"]
    assert by_id["scene-D"]["status"] == 1


def test_enabled_scene_a_members_are_routed_to_fusion(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    main = read_jsonl(Path(result["runDir"]) / inference.MAIN_NAME)
    by_id = {item["sample_id"]: item for item in main}
    for sample_id in ("s-001", "s-002", "s-009"):
        assert by_id[sample_id]["processing"]["02"]["route"] == scene.ROUTE_FUSION
        assert by_id[sample_id]["group_id"] == "grp-scene-scene-A"
        assert by_id[sample_id]["scene_group"]["status"] == 1
        assert by_id[sample_id]["scene_group"]["process_status"] == scene.STATUS_VERIFIED
    assert by_id["s-003"]["processing"]["02"]["route"] == scene.ROUTE_PAIRING
    assert by_id["s-003"]["processing"]["02"]["reason"] == scene.REASON_INSUFFICIENT
    assert by_id["s-006"]["processing"]["02"]["reason"] == scene.REASON_NO_SCENE_ID
    # 缺场景编号 / 模态不足都不隔离：全部留在主产物交给 #3
    assert len(main) == 11


def test_enabled_pairing_sidecar_matches_statistics(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    run_dir = Path(result["runDir"])
    pairing = read_jsonl(run_dir / scene.PAIRING_NAME)
    assert len(pairing) == result["statistics"]["pairing_records"] == 7
    reasons = sorted(item["reason"] for item in pairing)
    assert reasons.count(scene.REASON_NO_SCENE_ID) == 4
    assert reasons.count(scene.REASON_INSUFFICIENT) == 3


def test_enabled_no_quarantine_for_missing_scene_id(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    stats = result["statistics"]
    # 原始隔离只收纳原始文件问题（坏 JSON / 非对象 / 缺 sample_id）；
    # 缺场景编号与模态不足都不隔离，本算子不产生任何样本隔离清单
    assert stats["origin_rejected"] == 3
    assert "sample_isolated" not in stats
    run_dir = Path(result["runDir"])
    assert not any("sample_quarantine" in path.name for path in run_dir.iterdir())


# ------------------------------------------------------------ 补编号（只绑定一次）
def test_generate_mode_supplements_and_verifies(sample_dir, output_root):
    result = run_on(sample_dir, output_root,
                    overrides={"enabled": True, "scene_id_source": "generate"})
    stats = result["statistics"]
    assert stats["supplemented_scene_ids"] == 4
    assert stats["verified_groups"] == 5
    assert stats["insufficient_groups"] == 3
    assert stats["scene_groups_total"] == 8
    assert stats["fusion_records"] == 7
    assert stats["pairing_records"] == 4
    assert stats["pairing_no_scene_id"] == 0
    assert stats["pairing_insufficient_modalities"] == 4
    assert stats["scene_groups_verified_members"] == stats["fusion_records"]


def test_generate_mode_binds_each_supplemented_id_exactly_once(sample_dir, output_root):
    result = run_on(sample_dir, output_root,
                    overrides={"enabled": True, "scene_id_source": "generate"})
    main = read_jsonl(Path(result["runDir"]) / inference.MAIN_NAME)
    supplied = [item for item in main
                if item["processing"]["02"]["scene_source"] == scene.SCENE_SOURCE_SUPPLEMENTED]
    ids = [item["scene_id"] for item in supplied]
    assert len(ids) == len(set(ids)) == 4
    assert all(item.startswith(scene.GENERATED_SCENE_PREFIX) for item in ids)
    assert all(item["processing"]["02"]["bound_once"] is True for item in supplied)


def test_generate_mode_reuses_previous_binding_on_rerun(sample_dir, output_root):
    first = run_on(sample_dir, output_root,
                   overrides={"enabled": True, "scene_id_source": "generate"})
    main_first = read_jsonl(Path(first["runDir"]) / inference.MAIN_NAME)
    # 把第一次主产物作为第二次输入（模拟工程侧重跑同一条链）
    second = inference.run(input_file=str(first["outputFile"]), unique_id="task_file",
                           output_root=str(output_root), allow_external_input=True,
                           overrides={"enabled": True, "scene_id_source": "generate"})
    main_second = read_jsonl(Path(second["runDir"]) / inference.MAIN_NAME)
    first_ids = {item["sample_id"]: item["scene_id"] for item in main_first}
    second_ids = {item["sample_id"]: item["scene_id"] for item in main_second}
    assert first_ids == second_ids
    assert second["statistics"]["supplemented_scene_ids"] == 4


def test_min_modalities_three_reduces_fusion_groups(sample_dir, output_root):
    result = run_on(sample_dir, output_root,
                    overrides={"enabled": True, "min_modalities": 3})
    stats = result["statistics"]
    # scene-A 有 4 种模态 → 通过；scene-D 只有 text+audio → 不足
    assert stats["verified_groups"] == 1
    assert stats["insufficient_groups"] == 3
    assert stats["fusion_records"] == 3
    assert stats["pairing_records"] == 8
    assert stats["conservation_ok"] is True


def test_min_modalities_four_only_full_modality_scene_passes(sample_dir, output_root):
    result = run_on(sample_dir, output_root,
                    overrides={"enabled": True, "min_modalities": 4})
    assert result["statistics"]["verified_groups"] == 1
    assert result["statistics"]["fusion_records"] == 3


# ------------------------------------------------------------ 异常与边界输入
def test_empty_file(output_root, tmp_path):
    source = write_lines(tmp_path / "empty.jsonl", [])
    result = inference.run(input_file=str(source), unique_id="empty_task",
                           output_root=str(output_root), allow_external_input=True)
    stats = result["statistics"]
    assert stats["input_lines"] == 0
    assert stats["input_records"] == 0
    assert stats["valid_records"] == 0
    assert stats["empty_input"] is True
    assert stats["conservation_ok"] is True
    assert result["status"] == "success"


def test_blank_lines_only(output_root, tmp_path):
    source = write_lines(tmp_path / "blank.jsonl", ["", "   ", "\t"])
    result = inference.run(input_file=str(source), unique_id="blank_task",
                           output_root=str(output_root), allow_external_input=True)
    stats = result["statistics"]
    assert stats["input_lines"] == 3
    assert stats["blank_lines"] == 3
    assert stats["input_records"] == 0
    assert stats["empty_input"] is True


def test_corrupted_json_and_non_object_are_recorded_with_line_numbers(output_root, tmp_path):
    source = write_lines(tmp_path / "broken.jsonl", [
        '{"sample_id": "s-1", "scene_id": "scene-A", "text": "t", "image": "a.png"}',
        '{"sample_id": "s-2", ',
        "[1, 2, 3]",
        '{"text": "no id"}',
    ])
    result = inference.run(input_file=str(source), unique_id="broken_task",
                           output_root=str(output_root), allow_external_input=True,
                           overrides={"enabled": True})
    run_dir = Path(result["runDir"])
    origin = read_jsonl(run_dir / scene.ORIGIN_NAME)
    assert len(origin) == 3
    assert [item["line"] for item in origin] == [2, 3, 4]
    assert origin[0]["reason"] == "json_parse_error"
    assert origin[1]["reason"] == "record_not_object"
    assert origin[2]["reason"] == scene.REASON_MISSING_SAMPLE_ID
    assert result["statistics"]["origin_rejected"] == 3
    assert count_lines(run_dir / inference.MAIN_NAME) == 1


def test_duplicate_sample_id_goes_to_rejects_not_main(output_root, tmp_path):
    source = write_lines(tmp_path / "dup.jsonl", [
        '{"sample_id": "s-1", "scene_id": "scene-A", "text": "t", "image": "a.png"}',
        '{"sample_id": "s-1", "scene_id": "scene-A", "text": "t", "image": "a.png"}',
    ])
    result = inference.run(input_file=str(source), unique_id="dup_task",
                           output_root=str(output_root), allow_external_input=True,
                           overrides={"enabled": True})
    run_dir = Path(result["runDir"])
    rejects = read_jsonl(run_dir / scene.REJECT_NAME)
    assert len(rejects) == 1
    assert rejects[0]["reason"] == scene.REASON_DUPLICATE
    assert rejects[0]["line"] == 2
    assert count_lines(run_dir / inference.MAIN_NAME) == 1
    assert result["statistics"]["duplicate_records"] == 1
    assert result["status"] == "partial"


def test_non_utf8_input_fails_structurally(output_root, tmp_path):
    source = tmp_path / "gbk.jsonl"
    source.write_bytes('{"sample_id": "s-1", "text": "中文"}'.encode("gbk"))
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(source), unique_id="gbk_task",
                      output_root=str(output_root), allow_external_input=True)
    # 失败时不得留下半截产物
    assert not (Path(output_root) / "gbk_task").exists() or not any(
        (Path(output_root) / "gbk_task").rglob("*.jsonl"))


def test_missing_input_file(output_root, tmp_path):
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(tmp_path / "nope.jsonl"), unique_id="missing_task",
                      output_root=str(output_root), allow_external_input=True)


def test_input_without_input_file_argument(output_root):
    with pytest.raises(inference.InputError):
        inference.run(input_file=None, unique_id="none_task",
                      output_root=str(output_root), allow_external_input=False)


# ------------------------------------------------------------------- 配置
@pytest.mark.parametrize("overrides", [
    {"min_modalities": 1}, {"min_modalities": 5}, {"min_modalities": "2"},
    {"scene_id_source": "none"}, {"scene_id_source": "both"}, {"enabled": "true"},
    {"bind_once": False}, {"device_control": "schedule"}, {"stage_id": "S9"},
    {"unknown": 1},
])
def test_invalid_overrides_raise_config_error(output_root, sample_dir, overrides):
    with pytest.raises(ConfigError):
        run_on(sample_dir, output_root, overrides=overrides)


def test_missing_config_file(output_root, sample_dir):
    with pytest.raises(ConfigError):
        run_on(sample_dir, output_root, config_path=str(output_root / "nope.yaml"))


def test_broken_yaml_config(output_root, sample_dir, tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("operator: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        run_on(sample_dir, output_root, config_path=str(broken))


def test_config_top_level_not_mapping(output_root, sample_dir, tmp_path):
    bad = tmp_path / "list.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        run_on(sample_dir, output_root, config_path=str(bad))


def test_config_snapshot_and_overridden_keys(sample_dir, output_root):
    # [v4.2] 血缘停用后配置快照不再随 run() 返回，直接验证快照构造函数
    config = inference.load_config()
    updated, keys = inference.apply_overrides(
        config, {"enabled": True, "min_modalities": 3}, None)
    assert keys == ["enabled", "min_modalities"]
    snapshot = inference.config_snapshot(updated, None, keys)
    assert snapshot["business"]["min_modalities"] == 3
    assert snapshot["business"]["enabled"] is True
    assert snapshot["deployment"]["stage_id"] == "S1"
    assert snapshot["deployment"]["operator"]["name"] == "scene_id_verification"


def test_unknown_config_field_in_yaml_rejected(output_root, sample_dir, tmp_path):
    bad = tmp_path / "extra.yaml"
    bad.write_text(
        "operator:\n  name: scene_id_verification\n  version: v1.0.0\n"
        "stage_id: S1\nenabled: false\nnot_a_field: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        run_on(sample_dir, output_root, config_path=str(bad))


# ------------------------------------------------------------ 任务隔离 / 路径
@pytest.mark.parametrize("unique_id", [
    "", "  ", ".", "..", "a/b", "a\\b", "C:/tmp", "con", "nul.txt", "x\x00y",
    "trail.", "trail ", "\\\\server\\share",
])
def test_illegal_unique_id_rejected(unique_id, output_root, sample_dir):
    with pytest.raises(ValueError):
        run_on(sample_dir, output_root, unique_id=unique_id)


def test_two_unique_ids_are_isolated(sample_dir, output_root):
    first = run_on(sample_dir, output_root, unique_id="task_a", overrides={"enabled": True})
    second = run_on(sample_dir, output_root, unique_id="task_b")
    assert first["runDir"] != second["runDir"]
    assert "task_a" in first["runDir"]
    assert "task_b" in second["runDir"]
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()


def test_cross_task_input_file_rejected(sample_dir, output_root):
    first = run_on(sample_dir, output_root, unique_id="task_a")
    with pytest.raises(ValueError):
        inference.run(input_file=first["outputFile"], unique_id="task_b",
                      output_root=str(output_root), allow_external_input=False)


def test_input_file_traversal_rejected(output_root, sample_dir):
    with pytest.raises(ValueError):
        inference.run(input_file="../../etc/passwd", unique_id="task_x",
                      output_root=str(output_root), allow_external_input=False)


def test_repeat_run_creates_new_run_dir_and_is_reproducible(sample_dir, output_root):
    first = run_on(sample_dir, output_root, overrides={"enabled": True})
    second = run_on(sample_dir, output_root, overrides={"enabled": True})
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()
    assert Path(first["outputFile"]).read_bytes() == Path(second["outputFile"]).read_bytes()


def test_task_lock_rejects_concurrent_write(output_root, sample_dir):
    """锁文件已存在（模拟其他进程持锁）时必须返回 task_busy 而不是并发写。"""
    from utils import io as io_utils

    root = io_utils.resolve_root(str(output_root))
    lock_dir = root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "locked_task.lock").write_text("999999", encoding="ascii")
    with pytest.raises(io_utils.TaskBusyError):
        inference.run(input_file=str(Path(sample_dir) / "demo.jsonl"),
                      unique_id="locked_task", output_root=str(output_root),
                      allow_external_input=True)


def test_task_lock_is_reentrant_in_same_process(output_root):
    from utils import io as io_utils

    root = io_utils.resolve_root(str(output_root))
    root.joinpath("reentrant_task").mkdir(parents=True, exist_ok=True)
    with io_utils.TaskLock(root, "reentrant_task"):
        with io_utils.TaskLock(root, "reentrant_task"):
            pass
    assert not (root / ".locks" / "reentrant_task.lock").exists()


# ------------------------------------------------------------ 血缘 / 产物
def test_lineage_output_is_disabled_and_statistics_are_real(sample_dir, output_root):
    """[v4.2] 血缘输出已停用：run() 不再回传 localNode/stageLineage/priorLineage。"""
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    assert "localNode" not in result
    assert "stageLineage" not in result
    assert "priorLineage" not in result
    assert "lineageFile" not in result
    assert result["statistics"]["main_entries"] == 11
    assert result["statistics"]["conservation_ok"] is True
    run_dir = Path(result["runDir"])
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()


def test_removed_lineage_view_helpers(sample_dir, output_root):
    """[v4.2] 阶段响应形态统一：血缘视图辅助函数已随血缘停用注释保留（代码注释保留、暂不删除）。"""
    run_on(sample_dir, output_root, overrides={"enabled": True})
    assert not hasattr(inference, "build_intermediate_lineage")
    assert not hasattr(inference, "build_stage_payload")


def test_stage_file_object_contains_entrypoint_manifest_and_media(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    payload = inference.stage_file_object(result)
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == inference.MAIN_NAME
    paths = {item["path"] for item in payload["files"]}
    assert inference.MAIN_NAME in paths
    assert "manifest.json" in paths
    assert "media/img_a.png" in paths
    assert all(not Path(item).is_absolute() for item in paths)


def test_manifest_lists_all_artifacts(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    manifest = read_json(Path(result["runDir"]) / "manifest.json")
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == inference.MAIN_NAME
    listed = {item["path"] for item in manifest["artifacts"]}
    for name in inference.OUTPUT_NAMES:
        assert name in listed
    assert manifest["formal_operator_numbers"] == [2]
    assert manifest["stage_id"] == "S1"


def test_run_log_records_real_counts(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    log_text = (Path(result["runDir"]) / "run.log").read_text(encoding="utf-8")
    assert "[start]" in log_text
    assert "[counts]" in log_text
    assert "[finish]" in log_text
    assert "input_records=15" in log_text
    assert "scene_groups=4" in log_text


def test_statistics_match_actual_file_line_counts(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    run_dir = Path(result["runDir"])
    stats = result["statistics"]
    assert count_lines(run_dir / inference.MAIN_NAME) == stats["main_entries"]
    assert count_lines(run_dir / scene.ORIGIN_NAME) == stats["origin_entries"]
    assert count_lines(run_dir / scene.REJECT_NAME) == stats["reject_entries"]
    assert count_lines(run_dir / scene.PAIRING_NAME) == stats["pairing_entries"]
    assert count_lines(run_dir / scene.GROUPS_NAME) == stats["group_entries"]


def test_media_carried_and_missing_counted(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["media_carried"] == 10
    assert stats["media_missing"] == 1
    assert stats["media_errors"] == 1
    assert stats["media_remote"] == 0
    assert (Path(result["runDir"]) / "media" / "img_a.png").is_file()


# ------------------------------- 兼容 demo 10.6 上游 #01 的「一行一个文件登记项」形态
def test_upstream_01_file_registration_shape_is_supported(output_root, tmp_path):
    """#01 `01_workitems.jsonl`：一行一个文件条目，同一 sample_id 多行。

    本算子必须按「模态并集」成组，并且不把同一对象的多个文件误判为重复登记。
    """
    bundle = tmp_path / "bundle01"
    (bundle / "media").mkdir(parents=True, exist_ok=True)
    (bundle / "media" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    (bundle / "media" / "a.wav").write_bytes(b"RIFF\x00\x00WAVE")
    source = write_lines(bundle / "01_workitems.jsonl", [
        json.dumps({"sample_id": "d1", "scene_id": "scene-A",
                    "files": [{"modality": "image", "path": "media/a.png"}],
                    "processing": {"01": {"status": "registered"}},
                    "operator_01": {"modality": "image", "file_link": "media/a.png"}},
                   ensure_ascii=False),
        json.dumps({"sample_id": "d1", "scene_id": "scene-A",
                    "files": [{"modality": "audio", "path": "media/a.wav"}],
                    "operator_01": {"modality": "audio", "file_link": "media/a.wav"}},
                   ensure_ascii=False),
        json.dumps({"sample_id": "d2", "scene_id": "scene-B",
                    "files": [{"modality": "image", "path": "media/a.png"}],
                    "operator_01": {"modality": "image", "file_link": "media/a.png"}},
                   ensure_ascii=False),
        json.dumps({"sample_id": "d3", "scene_id": "scene-C",
                    "files": [{"path": "oss://bucket/remote.png"}],
                    "operator_01": {"modality": "image",
                                   "file_link": "oss://bucket/remote.png"}},
                   ensure_ascii=False),
    ])
    result = inference.run(input_file=str(source), unique_id="upstream01_f1",
                           output_root=str(output_root), allow_external_input=True,
                           overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["input_records"] == 4
    assert stats["duplicate_records"] == 0          # 同 sample_id 不同文件不算重复
    assert stats["main_entries"] == 4
    assert stats["scene_groups_total"] == 3
    assert stats["verified_groups"] == 1            # scene-A：image + audio
    assert stats["insufficient_groups"] == 2        # scene-B / scene-C 只有 image
    assert stats["fusion_records"] == 2
    assert stats["pairing_records"] == 2
    assert stats["media_carried"] == 2              # a.png / a.wav 各一次
    assert stats["media_remote"] == 1               # oss:// 远端链接只登记不搬运
    assert stats["conservation_ok"] is True
    assert result["status"] == "success"

    main = read_jsonl(Path(result["runDir"]) / inference.MAIN_NAME)
    by_key = {(item["sample_id"], item["processing"]["02"]["modalities"][0]): item
              for item in main}
    assert by_key[("d1", "image")]["group_id"] == "grp-scene-scene-A"
    assert by_key[("d1", "image")]["processing"]["01"] == {"status": "registered"}
    groups = read_jsonl(Path(result["runDir"]) / scene.GROUPS_NAME)
    group_a = [item for item in groups if item["scene_id"] == "scene-A"][0]
    assert group_a["modalities"] == ["image", "audio"]
    assert group_a["member_count"] == 2
    assert (Path(result["runDir"]) / "media" / "a.png").is_file()


def test_main_output_is_utf8_without_ascii_escapes(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    text = Path(result["outputFile"]).read_text(encoding="utf-8")
    assert "\\u" not in text
    assert "场景" in text or "scene" in text


def test_history_lineage_is_not_accumulated(sample_dir, output_root):
    """[v4.2] 血缘停用后，链式调用不再累积阶段血缘。"""
    first = run_on(sample_dir, output_root, unique_id="chain_task",
                   overrides={"enabled": True})
    second = inference.run(input_file=first["outputFile"], unique_id="chain_task",
                           output_root=str(output_root), allow_external_input=True,
                           overrides={"enabled": True})
    assert "stageLineage" not in second
    assert first["runId"] != second["runId"]


def test_no_partial_output_left_when_conservation_fails(sample_dir, output_root, monkeypatch):
    original = scene.check_conservation
    monkeypatch.setattr(scene, "check_conservation", lambda stats: False)
    with pytest.raises(inference.InputError):
        run_on(sample_dir, output_root, unique_id="bad_conservation",
               overrides={"enabled": True})
    run_root = Path(output_root) / "bad_conservation"
    leftovers = [path for path in run_root.rglob("*.jsonl")] if run_root.exists() else []
    assert leftovers == []
    monkeypatch.setattr(scene, "check_conservation", original)


def test_no_model_backend_and_no_degradation(sample_dir, output_root):
    result = run_on(sample_dir, output_root, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["degraded"] is False
    assert stats["active_backend"] == "rule"
    assert stats.get("model") is None
