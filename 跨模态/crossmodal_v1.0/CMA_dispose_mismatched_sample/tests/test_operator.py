"""算子 21 运行级测试：分流、守恒、任务隔离、路径安全、降级与阶段返回形态。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference
from tests.conftest import make_entry, read_json, read_jsonl, write_jsonl
from utils import io as io_utils
from utils import ledger
from utils.schemas import ConfigError

#: 样例逐条期望（与 dataset/DATA_SCHEMA.md §4 一致）
EXPECTED = {
    "input_lines": 18,
    "blank_lines": 1,
    "input_records": 17,
    "valid_records": 14,
    "bad_lines": 3,
    "released": 7,
    "passed": 3,
    "process_kept": 1,
    "not_applicable": 1,
    "waiting_manual": 2,
    "origin_isolated": 2,
    "sample_isolated": 4,
    "returned": 1,
    "locked": 2,
    "main_entries": 7,
    "ledger_entries": 17,
    "origin_entries": 2,
    "sample_entries": 4,
    "return_entries": 1,
    "pending_entries": 2,
    "pending_process": 0,
    "reject_entries": 3,
    "media_carried": 2,
    "isolated_media_carried": 0,
}


def _run_sample(output_root, unique_id="smoke_f1", **kwargs):
    # 旧fixture有未接入人工平台分支：仅这些测试显式验证降级能力。
    overrides=dict(kwargs.pop("overrides", {}) or {})
    overrides.setdefault("allow_degraded",True)
    kwargs["overrides"]=overrides
    return inference.run(input_file=str(kwargs.pop("input_file", None) or
                                        Path(__file__).resolve().parents[1]
                                        / "dataset" / "sample" / "demo.jsonl"),
                         unique_id=unique_id, output_root=str(output_root),
                         allow_external_input=True, **kwargs)


# ------------------------------------------------------------------ 样例回归
def test_sample_dataset_classification_matches_spec(output_root):
    result = _run_sample(output_root)
    stats = result["statistics"]
    for key, expected in EXPECTED.items():
        assert stats[key] == expected, "%s=%r 期望 %r" % (key, stats[key], expected)
    assert stats["conservation_ok"] is True


def test_sample_run_status_is_degraded_because_manual_platform_missing(output_root):
    result = _run_sample(output_root)
    assert result["status"] == "degraded"
    assert result["statistics"]["degraded_mode"] is True
    assert "manual_platform_unavailable" in result["statistics"]["degraded_reasons"]


def test_sample_run_with_manual_platform_available_is_not_degraded(output_root):
    result = _run_sample(output_root, unique_id="with_platform",
                         overrides={"manual_platform_available": True})
    stats = result["statistics"]
    assert result["status"] == "partial"
    assert stats["waiting_manual"] == 0
    assert stats["process_kept"] == 3
    assert stats["released"] == 7
    assert stats["conservation_ok"] is True


def test_sample_isolated_records_never_carry_media(output_root):
    """表1 明令：不得复制隔离样本（禁止负样本工厂）。"""
    result = _run_sample(output_root)
    run_dir = Path(result["runDir"])
    carried = {path.relative_to(run_dir).as_posix()
               for path in run_dir.rglob("*") if path.is_file()}
    assert "media/pass_ok.png" in carried
    assert "media/pass_ok.wav" in carried
    assert "media/origin_broken.png" not in carried
    assert "media/sample_bad.png" not in carried
    assert result["statistics"]["isolated_media_carried"] == 0


def test_sample_outputs_are_utf8_and_ledger_has_six_fields(output_root):
    result = _run_sample(output_root)
    for name in (inference.MAIN_NAME, inference.LEDGER_NAME, inference.ORIGIN_NAME,
                 inference.SAMPLE_NAME, inference.RETURN_NAME, inference.PENDING_NAME,
                 inference.REJECT_NAME):
        path = Path(result["runDir"]) / name
        text = path.read_bytes().decode("utf-8")  # 严格 UTF-8
        assert text == "" or text.endswith("\n")
    for entry in read_jsonl(Path(result["runDir"]) / inference.LEDGER_NAME):
        for name in ledger.LEDGER_FIELDS:
            assert name in entry


def test_ledger_entries_equal_valid_plus_bad_lines(output_root):
    result = _run_sample(output_root)
    run_dir = Path(result["runDir"])
    assert len(read_jsonl(run_dir / inference.LEDGER_NAME)) == EXPECTED["ledger_entries"]
    assert len(read_jsonl(run_dir / inference.REJECT_NAME)) == EXPECTED["reject_entries"]


def test_released_records_reach_main_output_and_isolated_do_not(output_root):
    result = _run_sample(output_root)
    main = read_jsonl(Path(result["runDir"]) / inference.MAIN_NAME)
    ids = {item["sample_id"] for item in main}
    assert ids == {"s-pass-1", "s-pass-2", "s-dup", "s-proc-1", "s-proc-2", "s-na-1",
                   "s-manual-1"}
    sample_rows = read_jsonl(Path(result["runDir"]) / inference.SAMPLE_NAME)
    sample_ids = {row["sample_id"] for row in sample_rows}
    assert sample_ids == {"s-sample-1", "s-sample-2", "s-lock-1", "s-ret-2"}
    assert all(row["locked"] for row in sample_rows
               if row["sample_id"] in ("s-lock-1", "s-ret-2"))


def test_return_file_lists_upstream_destination(output_root):
    result = _run_sample(output_root)
    rows = read_jsonl(Path(result["runDir"]) / inference.RETURN_NAME)
    assert len(rows) == 1
    assert rows[0]["sample_id"] == "s-ret-1"
    assert rows[0]["destination"] == "return_to_upstream"
    assert rows[0]["retry_count"] == 1
    assert rows[0]["upstream_destination"] == "09_准备候选描述"


def test_pending_manual_file_lists_human_queue_items(output_root):
    result = _run_sample(output_root)
    rows = read_jsonl(Path(result["runDir"]) / inference.PENDING_NAME)
    assert len(rows) == 2
    assert {row["sample_id"] for row in rows} == {"s-proc-1", "s-manual-1"}
    assert all(row["destination"] == "human_queue" for row in rows)
    assert all(row["degraded"] is True for row in rows)


def test_passed_records_carry_operator_namespace_not_overwriting_upstream(output_root):
    result = _run_sample(output_root)
    main = {item["sample_id"]: item for item in
            read_jsonl(Path(result["runDir"]) / inference.MAIN_NAME)}
    record = main["s-pass-1"]
    assert record["operator_21"]["decision"] == ledger.DECISION_PASS
    assert record["processing"]["21"]["label"] == "样本-评分：通过"
    # 上游标签/动作/去向原样保留
    assert record["label"] == "样本-评分：通过"


# ------------------------------------------------------------------ 空/坏/编码/缺字段
def test_empty_input_file_produces_empty_outputs(output_root, tmp_path):
    entry = write_jsonl(tmp_path / "empty.jsonl", [])
    result = inference.run(input_file=str(entry), unique_id="empty_task",
                           output_root=str(output_root), allow_external_input=True)
    stats = result["statistics"]
    assert stats["input_records"] == 0
    assert stats["empty_input"] is True
    assert stats["conservation_ok"] is True
    assert result["status"] == "success"
    assert (Path(result["runDir"]) / inference.MAIN_NAME).read_text(encoding="utf-8") == ""


def test_blank_lines_only_are_not_bad_lines(output_root, tmp_path):
    entry = tmp_path / "blank.jsonl"
    entry.write_text("\n\n   \n", encoding="utf-8")
    result = inference.run(input_file=str(entry), unique_id="blank_task",
                           output_root=str(output_root), allow_external_input=True)
    stats = result["statistics"]
    assert stats["blank_lines"] == 3
    assert stats["bad_lines"] == 0
    assert stats["input_records"] == 0
    assert stats["conservation_ok"] is True


def test_corrupt_json_and_non_object_lines_go_to_rejects(output_root, tmp_path):
    entry = write_jsonl(tmp_path / "bad.jsonl",
                        [make_entry("ok-1", label="样本-评分：通过")],
                        raw_lines=['{"sample_id": ', '[1, 2, 3]'])
    result = inference.run(input_file=str(entry), unique_id="bad_task",
                           output_root=str(output_root), allow_external_input=True)
    stats = result["statistics"]
    assert stats["bad_lines"] == 2
    assert stats["reject_entries"] == 2
    reasons = [row["reason_code"] for row in
               read_jsonl(Path(result["runDir"]) / inference.REJECT_NAME)]
    assert reasons == ["invalid_json", "invalid_record"]


def test_missing_sample_id_goes_to_rejects_with_line_number(output_root, tmp_path):
    entry = write_jsonl(tmp_path / "noid.jsonl",
                        [{"label": "样本-语义不通", "action": "isolate"}])
    result = inference.run(input_file=str(entry), unique_id="noid_task",
                           output_root=str(output_root), allow_external_input=True)
    stats = result["statistics"]
    assert stats["bad_lines"] == 1
    row = read_jsonl(Path(result["runDir"]) / inference.REJECT_NAME)[0]
    assert row["sample_id"] is None
    assert row["line"] == 1
    assert row["reason_code"] == "missing_sample_id"
    assert row["destination"] == "raw_isolation"


def test_non_utf8_input_fails_structurally(output_root, tmp_path):
    entry = tmp_path / "gbk.jsonl"
    entry.write_bytes("样本-评分：通过".encode("gbk") + b"\n")
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(entry), unique_id="gbk_task",
                      output_root=str(output_root), allow_external_input=True)


# ------------------------------------------------------------------ 配置
def test_config_disabled_is_rejected(output_root, tmp_path, sample_entry):
    with pytest.raises(ConfigError):
        inference.run(input_file=str(sample_entry), unique_id="cfg1_f1",
                      output_root=str(output_root), overrides={"enabled": False},
                      allow_external_input=True)


@pytest.mark.parametrize("overrides", [
    {"return_limit": -1},
    {"max_retry_per_item": 0},
    {"return_limit": 5, "max_retry_per_item": 2},
    {"max_embedded_record_bytes": 0},
    {"manual_platform_available": "yes"},
    {"allow_degraded": 1},
])
def test_invalid_business_overrides_are_rejected(overrides, output_root, sample_entry):
    with pytest.raises(ConfigError):
        inference.run(input_file=str(sample_entry), unique_id="cfg2_f1",
                      output_root=str(output_root), overrides=overrides,
                      allow_external_input=True)


@pytest.mark.parametrize("overrides", [
    {"operator": {"name": "x", "version": "1"}},
    {"stage_id": "S9"},
    {"raw_issue_destination": "somewhere"},
    {"quality_issue_destination": "nowhere"},
    {"scoring_tiers_not_isolated": False},
    {"transport": {"format": "x"}},
    {"unknown_key": 1},
])
def test_readonly_and_unknown_keys_cannot_be_overridden(overrides, output_root, sample_entry):
    with pytest.raises(ConfigError):
        inference.run(input_file=str(sample_entry), unique_id="cfg3_f1",
                      output_root=str(output_root), overrides=overrides,
                      allow_external_input=True)


def test_broken_yaml_config_fails(output_root, sample_entry, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("operator: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        inference.run(input_file=str(sample_entry), unique_id="cfg4_f1",
                      output_root=str(output_root), config_path=str(bad),
                      allow_external_input=True)


def test_missing_config_file_fails(output_root, sample_entry, tmp_path):
    with pytest.raises(ConfigError):
        inference.run(input_file=str(sample_entry), unique_id="cfg5_f1",
                      output_root=str(output_root),
                      config_path=str(tmp_path / "nope.yaml"), allow_external_input=True)


def test_allow_degraded_false_with_missing_manual_platform_fails(output_root, tmp_path):
    entry = write_jsonl(tmp_path / "manual.jsonl",
                        [make_entry("m-1", label="样本-评分：待人工", requires_human=True)])
    with pytest.raises(ledger.DependencyError):
        inference.run(input_file=str(entry), unique_id="strict_task",
                      output_root=str(output_root), overrides={"allow_degraded": False},
                      allow_external_input=True)


def test_failed_run_removes_empty_run_directory(output_root, tmp_path):
    entry = write_jsonl(tmp_path / "manual2.jsonl",
                        [make_entry("m-2", label="样本-评分：待人工", requires_human=True)])
    with pytest.raises(ledger.DependencyError):
        inference.run(input_file=str(entry), unique_id="strict_task2",
                      output_root=str(output_root), overrides={"allow_degraded": False},
                      allow_external_input=True)
    task = Path(output_root) / "strict_task2"
    runs = list((task / ledger.OPERATOR).glob("run*"))
    assert all(not any(run.iterdir()) for run in runs)


# ------------------------------------------------------------------ 路径与任务隔离
@pytest.mark.parametrize("unique_id", ["", "..", "../escape", "a/b", "a\\b", "C:\\tmp",
                                       "CON", "nul", " spaced"])
def test_invalid_unique_id_is_rejected(unique_id, output_root):
    with pytest.raises(ValueError):
        inference.run(input_file="x.jsonl", unique_id=unique_id,
                      output_root=str(output_root), allow_external_input=True)


def test_input_file_outside_task_is_rejected(output_root, tmp_path, sample_entry):
    task = io_utils.task_dir(Path(output_root), "guard_task")
    with pytest.raises(ValueError):
        inference.run(input_file=str(tmp_path / "outside.jsonl"), unique_id="guard_task",
                      output_root=str(output_root))
    assert task.exists()


def test_two_unique_ids_do_not_pollute_each_other(output_root, sample_entry):
    first = _run_sample(output_root, unique_id="task_a")
    second = _run_sample(output_root, unique_id="task_b")
    assert first["runDir"] != second["runDir"]
    assert Path(first["runDir"]).parent.parent.name == "task_a"
    assert Path(second["runDir"]).parent.parent.name == "task_b"

    def _ids(run_dir):
        return sorted(item["sample_id"] for item in
                      read_jsonl(Path(run_dir) / inference.MAIN_NAME))

    assert _ids(first["runDir"]) == _ids(second["runDir"])


def test_second_run_gets_new_run_id_without_overwriting(output_root, sample_entry):
    first = _run_sample(output_root, unique_id="rerun_task")
    second = _run_sample(output_root, unique_id="rerun_task")
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["runDir"]).is_dir()
    assert Path(second["runDir"]).is_dir()


def test_rerun_is_deterministic(output_root, sample_entry):
    first = _run_sample(output_root, unique_id="det_task")
    second = _run_sample(output_root, unique_id="det_task")
    keys = ("input_records", "valid_records", "bad_lines", "released", "passed",
            "process_kept", "not_applicable", "waiting_manual", "origin_isolated",
            "sample_isolated", "returned", "locked")
    assert {key: first["statistics"][key] for key in keys} == \
        {key: second["statistics"][key] for key in keys}


def test_unique_id_cannot_be_addressed_by_another_task(output_root, sample_entry):
    _run_sample(output_root, unique_id="owner_task")
    with pytest.raises(ValueError):
        inference.run(input_file=str(sample_entry), unique_id="intruder_task",
                      output_root=str(output_root))


# ------------------------------------------------------------------ 血缘与返回形态
@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_lineage_contains_required_fields_and_real_statistics(output_root):
    result = _run_sample(output_root)
    node = result["stageLineage"][-1]
    for name in ("operator", "operator_version", "uniqueId", "stageId", "inputFile",
                 "status", "startedAt", "finishedAt", "config", "statistics",
                 "outputFile", "artifacts"):
        assert name in node, name
    assert node["operator"] == ledger.OPERATOR
    assert node["uniqueId"] == "smoke_f1"
    assert node["statistics"]["released"] == EXPECTED["released"]
    assert node["config"]["business"]["return_limit"] == 1
    assert node["config"]["deployment"]["stage_id"] == "S3"
    assert node["config"]["overridden_keys"] == ["allow_degraded"]


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_intermediate_payload_is_single_node_object_with_temp_output(output_root):
    result = _run_sample(output_root)
    payload = inference.build_intermediate_lineage(result)
    assert isinstance(payload, dict)
    assert payload["outputFile"].endswith(inference.MAIN_NAME)
    assert Path(payload["outputFile"]).is_absolute()


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_stage_payload_is_array_without_temp_output(output_root):
    result = _run_sample(output_root)
    payload = inference.build_stage_payload(result)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert "outputFile" not in payload[0]
    assert payload[0]["artifacts"]


def test_stage_file_object_packs_entrypoint_and_media(output_root):
    result = _run_sample(output_root)
    payload = inference.stage_file_object(result)
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == inference.MAIN_NAME
    paths = {item["path"] for item in payload["files"]}
    assert inference.LEDGER_NAME in paths
    assert "media/pass_ok.png" in paths
    assert "manifest.json" in paths


def test_manifest_lists_artifacts_and_stage_id(output_root):
    result = _run_sample(output_root)
    manifest = read_json(Path(result["runDir"]) / "manifest.json")
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == inference.MAIN_NAME
    assert manifest["formal_operator_numbers"] == [21]
    assert manifest["stage_id"] == "S4"  # [v4.2] §0.3：M21→S4
    listed = {item["path"] for item in manifest["artifacts"]}
    for name in (inference.MAIN_NAME, inference.LEDGER_NAME, inference.ORIGIN_NAME,
                 inference.SAMPLE_NAME, inference.RETURN_NAME, inference.PENDING_NAME,
                 inference.REJECT_NAME):
        assert name in listed


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_stage_context_keeps_stage_and_prior_lineage(output_root, sample_entry):
    result = _run_sample(output_root)
    context = read_json(Path(result["runDir"]) / "stage_context.json")
    assert context["stageId"] == "S3"
    assert context["uniqueId"] == "smoke_f1"
    assert context["entrypoint"] == inference.MAIN_NAME
    assert len(context["stage_lineage"]) == 1
    assert context["prior_lineage"] == []


def test_run_log_records_real_counts(output_root):
    result = _run_sample(output_root)
    text = (Path(result["runDir"]) / "run.log").read_text(encoding="utf-8")
    assert "operator=%s" % ledger.OPERATOR in text
    assert "input_records=%d" % EXPECTED["input_records"] in text
    assert "released=%d" % EXPECTED["released"] in text


def test_stage_internal_input_file_chains_from_previous_output(output_root, sample_entry):
    first = _run_sample(output_root, unique_id="chain_task")
    # [v4.2] 血缘输出已停用：链式输入直接取上一次运行的 outputFile
    second = inference.run(input_file=first["outputFile"], unique_id="chain_task",
                           output_root=str(output_root),overrides={"allow_degraded":True})
    assert second["runId"] == "run002"
    assert second["statistics"]["conservation_ok"] is True


def test_nested_disposition_full_records_are_supported(output_root, tmp_path):
    from tests.conftest import make_full_record
    entry = write_jsonl(tmp_path / "nested.jsonl", [
        make_full_record("n-1", label="样本-评分：通过", action="keep"),
        make_full_record("n-2", label="原始文件-损坏", action="isolate",
                         destination="原始隔离区"),
    ])
    result = inference.run(input_file=str(entry), unique_id="nested_task",
                           output_root=str(output_root), allow_external_input=True)
    stats = result["statistics"]
    assert stats["passed"] == 1
    assert stats["origin_isolated"] == 1
    assert stats["conservation_ok"] is True


def test_output_root_env_variable_is_used(output_root, sample_entry, monkeypatch):
    assert io_utils.resolve_root(output_root) == Path(output_root).resolve()
    result = _run_sample(output_root, unique_id="env_task")
    assert Path(result["runDir"]).is_relative_to(Path(output_root).resolve())


def test_json_output_is_valid_and_parseable(output_root):
    result = _run_sample(output_root)
    raw = (Path(result["runDir"]) / inference.MAIN_NAME).read_text(encoding="utf-8")
    for line in raw.splitlines():
        json.loads(line)


def test_statistics_reason_distribution_is_real(output_root):
    result = _run_sample(output_root)
    stats = result["statistics"]
    assert stats["reason_counts"]["semantic_mismatch"] >= 1
    assert stats["reason_counts"]["sensitive"] >= 1
    assert sum(stats["reason_counts"].values()) == (
        stats["origin_isolated"] + stats["sample_isolated"] + stats["returned"]
        + stats["waiting_manual"] + stats["bad_lines"])


def test_conservation_failure_cleans_partial_outputs(monkeypatch, output_root, sample_entry):
    original = ledger.evaluate_record

    def broken(*args, **kwargs):
        result = original(*args, **kwargs)
        result["route"] = "released"
        result["decision"] = ledger.DECISION_PASS
        result["record"] = {"sample_id": "x"}
        return result

    monkeypatch.setattr(inference.ledger, "evaluate_record", broken)
    with pytest.raises(inference.InputError):
        _run_sample(output_root, unique_id="broken_task")
    main = Path(output_root) / "broken_task" / ledger.OPERATOR / "run001" / inference.MAIN_NAME
    assert not main.exists()
