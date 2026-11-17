"""端到端单测：``inference.run`` 的输入校验、流式处理、异常台账、血缘与两种返回形态。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference
from tests.conftest import (ENTRY_NAME, auto_aligned_not_sampled_record,
                            base_record, build_input_dir, isolated_record,
                            passed_record, read_jsonl, set_processing,
                            uncertain_record, with_review, with_rounds,
                            write_jsonl)
from utils import dispatch as dp
from utils import io as io_utils
from utils import schemas, transport


def run_task(tmp_path: Path, rows, *, unique_id: str = "t_1", overrides=None,
             input_file=None, entry_name: str = ENTRY_NAME, allow_external=False,
             config_path=None):
    root = tmp_path / "out"
    if input_file is None:
        build_input_dir(root, rows, unique_id=unique_id, entry_name=entry_name)
    return inference.run(input_file=input_file, unique_id=unique_id, output_root=root,
                         overrides=overrides, config_path=config_path,
                         allow_external_input=allow_external)


def run_dir(result) -> Path:
    return Path(result["runDir"])


# --------------------------------------------------------------------------- #
# 正常、关闭与空输入
# --------------------------------------------------------------------------- #

def test_run_enabled_dispatches_tasks_and_keeps_conservation(tmp_path):
    rows = [uncertain_record("n-unc-01"), uncertain_record("n-unc-02", source="16"),
            set_processing(uncertain_record("n-dis-01"),
                           "24", {"decision": "uncertain", "role": "dispute",
                                  "sampled": True}),
            passed_record("n-pass-01"), isolated_record("n-iso-01")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["conservation_ok"] is True
    assert stats["conservation_failures"] == []
    assert stats["input"] == 5
    assert stats["valid"] == 5
    assert stats["tasks_generated"] == 3
    assert stats["not_eligible"] == 1
    assert stats["not_applicable_records"] == 1
    assert stats["waiting_dispatch"] == 3
    assert result["status"] == "waiting_manual"
    assert result["reviewStatus"] == "waiting_manual"
    assert io_utils.count_lines(run_dir(result) / dp.TASKS_NAME) == 3
    assert io_utils.count_lines(run_dir(result) / dp.PENDING_NAME) == 3
    assert io_utils.count_lines(run_dir(result) / dp.MAIN_NAME) == 5


def test_run_disabled_by_default_generates_no_task(tmp_path):
    rows = [uncertain_record("d-unc-01"), passed_record("d-pass-01")]
    result = run_task(tmp_path, rows)
    stats = result["statistics"]
    assert stats["conservation_ok"] is True
    assert stats["tasks_generated"] == 0
    assert stats["not_triggered"] == 1
    assert result["status"] == "disabled"
    assert io_utils.count_lines(run_dir(result) / dp.TASKS_NAME) == 0
    assert io_utils.count_lines(run_dir(result) / dp.MAIN_NAME) == 2


def test_run_empty_file_is_success(tmp_path):
    root = tmp_path / "out"
    (root / "empty_1" / "input").mkdir(parents=True)
    (root / "empty_1" / "input" / ENTRY_NAME).write_text("", encoding="utf-8")
    result = inference.run(unique_id="empty_1", output_root=root, overrides={"enabled": True})
    assert result["status"] == "success"
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["input"] == 0
    assert result["statistics"]["conservation_ok"] is True


def test_run_only_blank_lines(tmp_path):
    root = tmp_path / "out"
    (root / "blank_1" / "input").mkdir(parents=True)
    (root / "blank_1" / "input" / ENTRY_NAME).write_text("\n\n\n", encoding="utf-8")
    result = inference.run(unique_id="blank_1", output_root=root, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["blank_lines"] == 3
    assert stats["input"] == 0
    assert stats["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 异常台账
# --------------------------------------------------------------------------- #

def test_corrupt_and_non_object_lines_go_to_rejects(tmp_path):
    rows = ['{"sample_id": "bad-01", "text": {', "[1, 2]", "not json at all",
            uncertain_record("ok-01")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["rejected"] == 3
    assert stats["valid"] == 1
    assert stats["conservation_ok"] is True
    rejects = read_jsonl(run_dir(result) / dp.REJECTS_NAME)
    assert len(rejects) == 3
    for entry in rejects:
        assert set(dp.REQUIRED_LEDGER_FIELDS).issubset(entry)
        assert len(entry["raw_preview"]) <= dp.RAW_PREVIEW_LIMIT


def test_missing_sample_id_goes_to_origin_quarantine(tmp_path):
    rows = [{"scene_id": "scene-hyd", "text": {"content": "缺少标识"}},
            uncertain_record("ok-02")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    assert stats["conservation_ok"] is True
    entry = read_jsonl(run_dir(result) / dp.ORIGIN_QUARANTINE_NAME)[0]
    assert entry["reason"] == "missing_sample_id"
    assert entry["label"] == dp.LABEL_ORIGIN_MISSING_FIELD


def test_blank_sample_id_is_treated_as_missing(tmp_path):
    rows = [{"sample_id": "   ", "text": {"content": "空标识"}},
            uncertain_record("ok-03")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    assert result["statistics"]["origin_quarantined"] == 1


def test_duplicate_sample_id_is_registered_not_dropped(tmp_path):
    rows = [uncertain_record("dup-01"), uncertain_record("dup-01"),
            uncertain_record("dup-02")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["duplicates"] == 1
    assert stats["origin_quarantined"] == 1
    assert stats["tasks_generated"] == 2
    assert stats["conservation_ok"] is True


def test_non_utf8_input_fails_structurally(tmp_path):
    root = tmp_path / "out"
    directory = root / "gbk_1" / "input"
    directory.mkdir(parents=True)
    (directory / ENTRY_NAME).write_bytes("中文\n".encode("gbk"))
    with pytest.raises(inference.InputError):
        inference.run(unique_id="gbk_1", output_root=root, overrides={"enabled": True})


def test_missing_input_file_fails_structurally(tmp_path):
    with pytest.raises(inference.InputError):
        inference.run(unique_id="nofile_1", output_root=tmp_path / "out")


# --------------------------------------------------------------------------- #
# 配置错误
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("overrides", [
    {"enabled": "yes"},
    {"enabled": 1},
    {"unknown_key": True},
    {"max_review_count": 3},
    {"eligible": ["uncertain"]},
    {"eligible": ["uncertain", "sampling_dispute", "returned", "extra"]},
])
def test_invalid_overrides_are_rejected(tmp_path, overrides):
    with pytest.raises(schemas.ConfigError):
        run_task(tmp_path, [uncertain_record("cfg-01")], overrides=overrides)


def test_broken_yaml_config_fails(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("operator: [unclosed\n", encoding="utf-8")
    with pytest.raises(schemas.ConfigError):
        run_task(tmp_path, [uncertain_record("cfg-02")], overrides={"enabled": True},
                 config_path=broken)


def test_missing_config_file_fails(tmp_path):
    with pytest.raises(schemas.ConfigError):
        run_task(tmp_path, [uncertain_record("cfg-03")],
                 config_path=tmp_path / "not_here.yaml")


def test_default_config_is_disabled_and_frozen_values():
    config = schemas.load_config()
    assert config.enabled is False
    assert config.max_review_count == 2
    assert tuple(config.eligible) == dp.ELIGIBLE_CATEGORIES
    snapshot = schemas.config_snapshot(config, ["enabled"])
    assert snapshot["business"]["enabled"] is False
    assert snapshot["overridden_keys"] == ["enabled"]


# --------------------------------------------------------------------------- #
# 任务隔离与路径安全
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "CON", "x.", "nul.txt",
                                 "with\x01ctrl"])
def test_invalid_unique_id_is_rejected(bad):
    with pytest.raises(ValueError):
        io_utils.validate_unique_id(bad)


def test_two_unique_ids_do_not_pollute_each_other(tmp_path):
    root = tmp_path / "out"
    build_input_dir(root, [uncertain_record("iso-a-01")], unique_id="iso_a")
    build_input_dir(root, [uncertain_record("iso-b-01"),
                           uncertain_record("iso-b-02")], unique_id="iso_b")
    first = inference.run(unique_id="iso_a", output_root=root, overrides={"enabled": True})
    second = inference.run(unique_id="iso_b", output_root=root, overrides={"enabled": True})
    assert first["statistics"]["tasks_generated"] == 1
    assert second["statistics"]["tasks_generated"] == 2
    assert first["uniqueId"] == "iso_a"
    assert io_utils.count_lines(run_dir(first) / dp.TASKS_NAME) == 1
    assert io_utils.count_lines(run_dir(second) / dp.TASKS_NAME) == 2


def test_cross_task_input_file_is_rejected(tmp_path):
    root = tmp_path / "out"
    build_input_dir(root, [uncertain_record("cross-a-01")], unique_id="cross_a")
    foreign = build_input_dir(root, [uncertain_record("cross-b-01")], unique_id="cross_b")
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(foreign), unique_id="cross_a", output_root=root)
    with pytest.raises(inference.InputError):
        inference.run(input_file="../../escape.jsonl", unique_id="cross_a", output_root=root)


def test_absolute_path_outside_task_is_rejected(tmp_path):
    root = tmp_path / "out"
    build_input_dir(root, [uncertain_record("abs-01")], unique_id="abs_a")
    outside = write_jsonl(tmp_path / "outside.jsonl", [uncertain_record("x-01")])
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(outside), unique_id="abs_a", output_root=root)


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "out"
    build_input_dir(root, [uncertain_record("sym-01")], unique_id="sym_a")
    outside = write_jsonl(tmp_path / "secret.jsonl", [uncertain_record("s-01")])
    link = root / "sym_a" / "link.jsonl"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(link), unique_id="sym_a", output_root=root)


def test_temp_marker_files_are_cleaned_up(tmp_path):
    result = run_task(tmp_path, [uncertain_record("tmp-01")], overrides={"enabled": True})
    leftovers = [path.name for path in run_dir(result).iterdir()
                 if path.name.startswith("_")]
    assert leftovers == []


# --------------------------------------------------------------------------- #
# 幂等 / 可复现 / 断点
# --------------------------------------------------------------------------- #

def test_rerun_creates_new_run_without_overwriting(tmp_path):
    rows = [uncertain_record("rerun-01")]
    first = run_task(tmp_path, rows, unique_id="rerun_a", overrides={"enabled": True})
    second = inference.run(unique_id="rerun_a", output_root=tmp_path / "out",
                           overrides={"enabled": True})
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert run_dir(first).is_dir() and run_dir(second).is_dir()


def test_main_output_is_byte_reproducible(tmp_path):
    rows = [uncertain_record("rep-01"), passed_record("rep-02"),
            with_review(uncertain_record("rep-03"), "fail",
                        dimensions={"semantics": "fail"})]
    first = run_task(tmp_path, rows, unique_id="rep_a", overrides={"enabled": True})
    first_bytes = (run_dir(first) / dp.MAIN_NAME).read_bytes()
    second = inference.run(unique_id="rep_a", output_root=tmp_path / "out",
                           overrides={"enabled": True})
    second_bytes = (run_dir(second) / dp.MAIN_NAME).read_bytes()
    assert first_bytes == second_bytes


def test_task_lock_is_reentrant_in_process_and_scoped_per_task(tmp_path):
    """同一进程内允许重入（HTTP 先锁、``inference.run`` 再锁），不同任务互不影响。"""
    root = io_utils.resolve_root(tmp_path / "out")
    lock_file = root / ".locks" / "lock_a.lock"
    outer = io_utils.TaskLock(root, "lock_a").acquire()
    inner = io_utils.TaskLock(root, "lock_a").acquire()
    inner.release()
    assert lock_file.exists()
    with io_utils.TaskLock(root, "lock_b"):
        assert (root / ".locks" / "lock_b.lock").exists()
    outer.release()
    assert not lock_file.exists()


def test_task_lock_rejects_busy_lock_file(tmp_path):
    """锁文件已存在（另一个写操作持有）时必须拒绝，不覆盖他人任务。"""
    root = io_utils.resolve_root(tmp_path / "out")
    (root / ".locks").mkdir(parents=True, exist_ok=True)
    (root / ".locks" / "busy_a.lock").write_text("999999", encoding="ascii")
    with pytest.raises(io_utils.TaskBusyError):
        io_utils.TaskLock(root, "busy_a").acquire()
    assert (root / ".locks" / "busy_a.lock").read_text(encoding="ascii") == "999999"


# --------------------------------------------------------------------------- #
# 链式输入与 v4.2 返回形态（血缘输出已停用）
# --------------------------------------------------------------------------- #

def test_chained_input_file_from_previous_output(tmp_path):
    """阶段内链式续跑：用上一中间响应的临时 outputFile 作为本节点 inputFile。"""
    root = tmp_path / "out"
    build_input_dir(root, [uncertain_record("chain-01")], unique_id="chain_a")
    first = inference.run(unique_id="chain_a", output_root=root, overrides={"enabled": True})
    upstream_output = first["outputFile"]
    second = inference.run(input_file=upstream_output, unique_id="chain_a", output_root=root,
                           overrides={"enabled": True})
    assert second["statistics"]["input"] == 1
    # [v4.2] 血缘输出已停用：run() 不再返回 lineage，输入引用改看 inputFile（任务相对入口）
    assert "lineage" not in second
    assert "stageContext" not in second
    assert second["inputFile"] == dp.MAIN_NAME


def test_lineage_and_stage_context_are_not_written(tmp_path):
    """[v4.2] 血缘/阶段归档停用后不得再落 lineage.json / stage_context.json。"""
    result = run_task(tmp_path, [uncertain_record("mid-01")], overrides={"enabled": True})
    assert not (run_dir(result) / "lineage.json").exists()
    assert not (run_dir(result) / "stage_context.json").exists()
    assert "lineage" not in result and "stageContext" not in result


def test_stage_end_file_object_is_complete_and_restorable(tmp_path):
    result = run_task(tmp_path, [uncertain_record("pack-01")], overrides={"enabled": True})
    payload = inference.stage_file_object(result)
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == dp.MAIN_NAME
    names = [item["path"] for item in payload["files"]]
    assert dp.MAIN_NAME in names
    assert dp.TASKS_NAME in names
    assert dp.REPORT_NAME in names
    assert transport.MANIFEST_NAME in names
    restored = transport.restore_file_object(payload, tmp_path / "restored")
    assert restored.name == dp.MAIN_NAME
    assert restored.read_bytes() == (run_dir(result) / dp.MAIN_NAME).read_bytes()


def test_stage_file_object_respects_response_limit(tmp_path):
    result = run_task(tmp_path, [uncertain_record("limit-01")], overrides={"enabled": True})
    with pytest.raises(transport.OutputTooLargeError):
        inference.stage_file_object(result, max_response_bytes=16)


# --------------------------------------------------------------------------- #
# 产物与统计口径（[v4.2] 血缘输出已停用，改查 run() 的 artifacts）
# --------------------------------------------------------------------------- #

def test_artifacts_are_real(tmp_path):
    rows = [uncertain_record("art-01"), passed_record("art-02")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    assert result["artifacts"]
    for artifact in result["artifacts"]:
        assert (run_dir(result) / artifact["path"]).is_file()
        if artifact["path"].endswith(".jsonl"):
            assert artifact["records"] == io_utils.count_lines(
                run_dir(result) / artifact["path"])
    assert result["statistics"]["valid"] == 2


def test_manifest_is_written_without_stage_context(tmp_path):
    result = run_task(tmp_path, [uncertain_record("ctx-01")], overrides={"enabled": True})
    manifest = transport.load_manifest(run_dir(result))
    assert manifest["entrypoint"] == dp.MAIN_NAME
    # [v4.2] 阶段归档停用：不再写 stage_context.json / lineage.json
    assert not (run_dir(result) / "stage_context.json").exists()
    assert not (run_dir(result) / "lineage.json").exists()


def test_repeated_run_uses_new_run_directory(tmp_path):
    root = tmp_path / "out"
    build_input_dir(root, [uncertain_record("hist-01")], unique_id="hist_a")
    first = inference.run(unique_id="hist_a", output_root=root, overrides={"enabled": True})
    second = inference.run(unique_id="hist_a", output_root=root, overrides={"enabled": True})
    assert first["runId"] == "run001" and second["runId"] == "run002"
    assert Path(first["runDir"]) != Path(second["runDir"])
    assert not (run_dir(second) / "lineage.json").exists()


def test_ledgers_carry_required_fields(tmp_path):
    rows = [uncertain_record("led-01")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    for name in (dp.TASKS_NAME, dp.PENDING_NAME, dp.CONCLUSIONS_NAME,
                 dp.NOT_DISPATCHED_NAME):
        for entry in read_jsonl(run_dir(result) / name):
            assert set(dp.REQUIRED_LEDGER_FIELDS).issubset(entry)
            assert entry["uniqueId"] == "t_1"
            assert entry["runId"] == result["runId"]


def test_isolated_records_are_excluded_from_main_output(tmp_path):
    rows = [with_review(uncertain_record("mn-01"), "fail",
                        dimensions={"semantics": "fail"}),
            uncertain_record("mn-02")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    main_ids = [row["sample_id"] for row in read_jsonl(run_dir(result) / dp.MAIN_NAME)]
    assert "mn-01" not in main_ids
    assert "mn-02" in main_ids
    quarantine = read_jsonl(run_dir(result) / dp.SAMPLE_QUARANTINE_NAME)
    assert [entry["sample_id"] for entry in quarantine] == ["mn-01"]


def test_passed_records_return_to_candidate_pool(tmp_path):
    rows = [with_review(uncertain_record("rp-01"), "pass")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    entries = read_jsonl(run_dir(result) / dp.RETURN_NAME)
    assert [entry["sample_id"] for entry in entries] == ["rp-01"]
    assert entries[0]["destination"] == dp.DESTINATION_POOL
    assert "operator_19" in entries[0]["handover"]


def test_max_review_records_are_locked_and_not_dispatched(tmp_path):
    rows = [with_rounds(uncertain_record("lk-01"), 2)]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    stats = result["statistics"]
    assert stats["max_review_reached"] == 1
    assert stats["tasks_generated"] == 0
    assert stats["locked"] == 1
    entry = read_jsonl(run_dir(result) / dp.SAMPLE_QUARANTINE_NAME)[0]
    assert entry["action"] == dp.ACTION_LOCK
    assert entry["label"] == dp.LABEL_MAX_REVIEW


def test_no_eligible_sample_yields_no_task(tmp_path):
    rows = [passed_record("no-01"), passed_record("no-02")]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    assert result["statistics"]["tasks_generated"] == 0
    assert result["statistics"]["not_eligible"] == 2
    assert result["status"] == "success"
    assert io_utils.count_lines(run_dir(result) / dp.NOT_DISPATCHED_NAME) >= 0


def test_report_records_handover_targets(tmp_path):
    rows = [with_review(uncertain_record("hov-01"), "pass"),
            with_review(uncertain_record("hov-02"), "fail",
                        dimensions={"semantics": "fail"})]
    result = run_task(tmp_path, rows, overrides={"enabled": True})
    report = json.loads((run_dir(result) / dp.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["handover"]["passed_to"] == ["operator_19", "operator_20"]
    assert report["handover"]["failed_to"] == ["operator_21"]
    assert report["handover"]["pending_to"] == ["operator_25_platform"]
    assert report["review"]["max_review_count"] == 2
    assert report["review"]["repeat_spot_check"] is False
