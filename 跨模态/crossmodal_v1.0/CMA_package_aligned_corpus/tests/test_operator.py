"""算子 23 编排层测试：真实计数、守恒、交付门禁、ZIP 内容、任务隔离、CLI。"""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from conftest import (DEFAULT_RECORDS, BASE, build_input_package, build_scenario,
                      config_variant, isolation_entry, quality_report,
                      sampling_conclusion, token_record, version_notes,
                      write_json, write_jsonl)
import inference
from utils import delivery
from utils.schemas import ConfigError


# --------------------------------------------------------------------------- #
# 待发布 / 可交付两态
# --------------------------------------------------------------------------- #

def test_pending_release_when_report_not_written_back(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=False)
    result = inference.run(input_file=str(entry), unique_id="task_pending",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert result["status"] == "degraded"
    assert result["versionState"] == delivery.VERSION_STATE_PENDING
    assert result["deliverable"] is False
    assert set(result["packageName"].split("_")[:2]) == {"23", "pending"}
    assert "待发布" in result["message"]
    assert stats["input_records"] == 18
    assert stats["valid_records"] == 16
    assert stats["blank_lines"] == 1
    assert stats["bad_lines"] == 2
    assert stats["packaged_records"] == 10
    assert stats["excluded_isolated"] == 3
    assert stats["origin_quarantined"] == 3
    assert stats["rejects_entries"] == 5
    assert stats["conservation_ok"] is True
    assert stats["model"] is None and stats["rule_based"] is True
    assert stats["deliverable_requirements"]["training_files_present"] is True
    assert stats["deliverable_requirements"]["quality_report_written_back"] is False
    assert stats["deliverable_requirements"]["sampling_written_back"] is False


def test_deliverable_when_materials_complete_and_written_back(config_file, output_root,
                                                             tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_ready",
                           config_path=str(config_file), output_root=str(output_root))
    assert result["status"] == "success"
    assert result["versionState"] == delivery.VERSION_STATE_DELIVERABLE
    assert result["deliverable"] is True
    assert result["packageName"] == delivery.PACKAGE_DELIVERABLE_NAME
    assert "可交付" in result["message"]
    assert result["statistics"]["conservation_ok"] is True


def test_pending_release_when_version_notes_missing(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True,
                           include_version_notes=False)
    result = inference.run(input_file=str(entry), unique_id="task_incomplete",
                           config_path=str(config_file), output_root=str(output_root))
    assert result["deliverable"] is False
    assert result["versionState"] == delivery.VERSION_STATE_PENDING
    assert delivery.MATERIAL_VERSION_NOTES in result["materials"]["missing"]
    assert result["status"] == "degraded"


def test_strict_mode_fails_when_not_deliverable(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=False)
    strict = config_variant(config_file, "require_deliverable: true\n",
                            name="config_strict.yaml")
    result = inference.run(input_file=str(entry), unique_id="task_strict",
                           config_path=str(strict), output_root=str(output_root))
    assert result["status"] == "failed"
    assert result["deliverable"] is False


# --------------------------------------------------------------------------- #
# ZIP 内容
# --------------------------------------------------------------------------- #

def test_package_contains_training_file_materials_and_manifest(config_file, output_root,
                                                               tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_zip",
                           config_path=str(config_file), output_root=str(output_root))
    package = Path(result["packagePath"])
    assert package.is_file()
    with zipfile.ZipFile(package) as bundle:
        names = bundle.namelist()
        assert "train/samples.jsonl" in names
        assert "manifest.json" in names
        assert "version.json" in names
        for role in ("quality_report", "sampling_conclusion", "isolation_list",
                     "version_notes"):
            assert any(item.startswith("materials/") and role.split("_")[0] in item
                       for item in names), (role, names)
        train = bundle.read("train/samples.jsonl").decode("utf-8").strip().splitlines()
        assert len(train) == result["statistics"]["packaged_records"]
        ids = [json.loads(line)["sample_id"] for line in train]
        # 隔离样本不得进入训练文件
        assert "cm-0011" not in ids and "cm-0012" not in ids and "cm-0013" not in ids
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == delivery.PACKAGE_FORMAT
        assert manifest["version_state"] == delivery.VERSION_STATE_DELIVERABLE
        assert manifest["entrypoint"] == "train/samples.jsonl"
        assert manifest["statistics"]["packaged_records"] == len(train)
        version = json.loads(bundle.read("version.json").decode("utf-8"))
        assert version["enabled_routes"] == ["image_text"]
        assert version["annotation_granularity"] == "whole"
        assert version["dataset_version"] == "v1.0.0"
        assert version["status"] == "可交付"
        # 媒体按声明相对路径随包携带
        assert "media/cm-0001.png" in names


def test_pending_package_marks_pending_state(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=False)
    result = inference.run(input_file=str(entry), unique_id="task_pending_pkg",
                           config_path=str(config_file), output_root=str(output_root))
    with zipfile.ZipFile(result["packagePath"]) as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        version = json.loads(bundle.read("version.json").decode("utf-8"))
    assert manifest["version_state"] == delivery.VERSION_STATE_PENDING
    assert manifest["missing_requirements"]
    assert version["status"] == "待发布"


def test_package_omits_media_when_disabled(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_nomedia",
                           config_path=str(config_file), output_root=str(output_root),
                           overrides={"include_media": False})
    with zipfile.ZipFile(result["packagePath"]) as bundle:
        names = bundle.namelist()
    assert not any(name.startswith("media/") for name in names)
    assert result["statistics"]["media_carried"] == 0
    assert result["statistics"]["media_referenced"] > 0


# --------------------------------------------------------------------------- #
# 计数与守恒
# --------------------------------------------------------------------------- #

def test_applicability_counts_packaged_records(config_file, output_root, tmp_path):
    """默认样例的版本说明只声明 image_text，因此 3 条音/视频样本标 not_applicable。"""
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_applicability",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["enabled_routes"] == ["image_text"]
    assert stats["not_applicable_records"] == 3
    assert stats["applicable_records"] == 7
    assert stats["applicability_unknown_records"] == 0
    assert stats["conservation_ok"] is True


def test_operator_settings_snippet_from_plan_is_accepted(output_root, tmp_path,
                                                         tmp_path_factory):
    """计划文档 §3 给出的 YAML 片段必须可直接使用（settings 字符串数组）。"""
    plan_config = tmp_path_factory.mktemp("plan_cfg") / "config.yaml"
    plan_config.write_text(
        "operator:\n"
        "  number: 23\n"
        "  name: 压缩交付对齐语料包\n"
        "  version: v1.0.0\n"
        "  enabled: true\n"
        "  settings:\n"
        '    - "enabled=true"\n'
        '    - "archive_format=zip"\n'
        '    - "status_before_review=待发布"\n'
        '    - "status_after_review=可交付"\n'
        '    - "exclude_isolated=true。"\n'
        "transport:\n"
        "  format: crossmodal-files-v1\n"
        "  max_response_bytes: 67108864\n", encoding="utf-8")
    config = inference.load_config(str(plan_config))
    assert config.enabled is True and config.archive_format == "zip"
    assert config.status_before_review == "待发布"
    assert config.status_after_review == "可交付"
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_plan_cfg",
                           config_path=str(plan_config), output_root=str(output_root))
    assert result["status"] == "success"
    assert result["deliverable"] is True


def test_media_and_external_reference_counts(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_media",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["media_referenced"] == 9
    assert stats["media_carried"] == 4
    assert stats["media_missing"] == 1
    assert stats["external_references"] == 1
    assert stats["media_carried"] + stats["media_missing"] == stats["media_distinct"]


def test_duplicate_sample_id_goes_to_origin_quarantine(config_file, output_root, tmp_path):
    rows = list(DEFAULT_RECORDS) + [token_record("cm-0001")]
    entry = build_scenario(tmp_path / "task", written_back=True, records=rows)
    result = inference.run(input_file=str(entry), unique_id="task_dup",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 4
    assert stats["origin_by_reason"][delivery.REASON_DUPLICATE_IDENTIFIER] == 1
    assert stats["conservation_ok"] is True


def test_empty_input_is_not_a_crash(config_file, output_root, tmp_path):
    entry = write_jsonl(tmp_path / "task" / "samples.jsonl", [])
    build_input_package(tmp_path / "task", records=[], materials={
        "quality_report": quality_report(written_back=True),
        "sampling_conclusion": sampling_conclusion(written_back=True),
        "isolation_list": [],
        "version_notes": version_notes()})
    result = inference.run(input_file=str(entry), unique_id="task_empty",
                           config_path=str(config_file), output_root=str(output_root))
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["packaged_records"] == 0
    assert result["deliverable"] is False
    assert result["versionState"] == delivery.VERSION_STATE_PENDING
    assert result["statistics"]["conservation_ok"] is True


def test_all_bad_lines_fail_explicitly(config_file, output_root, tmp_path):
    entry = build_input_package(tmp_path / "task",
                                records=['{"a": ', '["x"]', ""],
                                materials={
                                    "quality_report": quality_report(written_back=True),
                                    "sampling_conclusion": sampling_conclusion(written_back=True),
                                    "isolation_list": [],
                                    "version_notes": version_notes()})
    result = inference.run(input_file=str(entry), unique_id="task_bad",
                           config_path=str(config_file), output_root=str(output_root))
    assert result["status"] == "failed"
    assert result["statistics"]["valid_records"] == 0
    assert result["statistics"]["bad_lines"] == 2
    assert result["statistics"]["conservation_ok"] is True


def test_encoding_error_line_is_recorded(config_file, output_root, tmp_path):
    root = tmp_path / "task"
    build_input_package(root, records=[token_record("cm-0001")], materials={
        "quality_report": quality_report(written_back=True),
        "sampling_conclusion": sampling_conclusion(written_back=True),
        "isolation_list": [],
        "version_notes": version_notes()})
    raw = (root / "samples.jsonl").read_bytes() + b"\xff\xfe\x00bad\n"
    (root / "samples.jsonl").write_bytes(raw)
    result = inference.run(input_file=str(root / "samples.jsonl"),
                           unique_id="task_encoding", config_path=str(config_file),
                           output_root=str(output_root))
    assert result["statistics"]["encoding_error"] is True
    assert result["statistics"]["bad_lines"] == 1
    assert result["statistics"]["rejects_entries"] == 1


# --------------------------------------------------------------------------- #
# 任务隔离与运行目录
# --------------------------------------------------------------------------- #

def test_two_unique_ids_are_physically_isolated(config_file, output_root, tmp_path):
    entry_a = build_scenario(tmp_path / "a", written_back=True)
    entry_b = build_scenario(tmp_path / "b", written_back=False)
    first = inference.run(input_file=str(entry_a), unique_id="task_one",
                          config_path=str(config_file), output_root=str(output_root))
    second = inference.run(input_file=str(entry_b), unique_id="task_two",
                           config_path=str(config_file), output_root=str(output_root))
    path_a, path_b = Path(first["runDir"]), Path(second["runDir"])
    assert path_a != path_b
    assert path_a.parts[-3] == "task_one"
    assert path_b.parts[-3] == "task_two"
    assert (output_root / "task_one" / delivery.OPERATOR).is_dir()
    assert (output_root / "task_two" / delivery.OPERATOR).is_dir()


def test_rerun_creates_new_run_dir_without_overwriting(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    first = inference.run(input_file=str(entry), unique_id="task_rerun",
                          config_path=str(config_file), output_root=str(output_root))
    second = inference.run(input_file=str(entry), unique_id="task_rerun",
                           config_path=str(config_file), output_root=str(output_root))
    assert first["runId"] == "run001" and second["runId"] == "run002"
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "C:", "CON", "../escape"])
def test_invalid_unique_id_is_rejected(config_file, output_root, tmp_path, bad):
    entry = build_scenario(tmp_path / "task", written_back=True)
    with pytest.raises(ValueError):
        inference.run(input_file=str(entry), unique_id=bad,
                      config_path=str(config_file), output_root=str(output_root))


def test_input_outside_task_is_rejected_when_restricted(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(entry), unique_id="task_x",
                      config_path=str(config_file), output_root=str(output_root),
                      allow_external_input=False)


def test_missing_input_file_raises(config_file, output_root, tmp_path):
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(tmp_path / "nope.jsonl"), unique_id="task_y",
                      config_path=str(config_file), output_root=str(output_root))


# --------------------------------------------------------------------------- #
# 配置边界
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("yaml_text", [
    "enabled: false\n",
    "archive_format: tar\n",
    "exclude_isolated: false\n",
    "status_before_review: 草稿\n",
    "status_after_review: 完成\n",
    "unknown_key: 1\n",
])
def test_invalid_config_is_rejected(config_file, output_root, tmp_path, yaml_text):
    entry = build_scenario(tmp_path / "task", written_back=True)
    bad = config_variant(config_file, yaml_text, name="config_bad.yaml")
    with pytest.raises(ConfigError):
        inference.run(input_file=str(entry), unique_id="task_cfg",
                      config_path=str(bad), output_root=str(output_root))


def test_overrides_whitelist(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    with pytest.raises(ConfigError):
        inference.run(input_file=str(entry), unique_id="task_ov",
                      config_path=str(config_file), output_root=str(output_root),
                      overrides={"exclude_isolated": False})
    with pytest.raises(ConfigError):
        inference.run(input_file=str(entry), unique_id="task_ov2",
                      config_path=str(config_file), output_root=str(output_root),
                      overrides={"status_after_review": "完成"})


# --------------------------------------------------------------------------- #
# 血缘与产物
# --------------------------------------------------------------------------- #

@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_lineage_and_artifacts_are_written(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_lineage",
                           config_path=str(config_file), output_root=str(output_root))
    lineage_path = Path(result["lineageFile"])
    payload = json.loads(lineage_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) == 1
    node = payload[0]
    for key in ("operator", "operator_version", "uniqueId", "stageId", "inputFile",
                "outputFile", "status", "startedAt", "finishedAt", "config",
                "statistics", "artifacts"):
        assert key in node, key
    assert node["operator"] == delivery.OPERATOR
    assert node["formalOperatorNumber"] == 23
    assert node["statistics"]["packaged_records"] == 10
    assert node["config"]["business"]["archive_format"] == "zip"
    assert any(item["path"] == delivery.MAIN_NAME for item in node["artifacts"])
    context = json.loads(Path(result["contextFile"]).read_text(encoding="utf-8"))
    assert context["stageId"] == delivery.STAGE_ID
    assert context["uniqueId"] == "task_lineage"
    assert context["entrypoint"] == delivery.MAIN_NAME
    log_text = Path(result["logFile"]).read_text(encoding="utf-8")
    assert "run_start" in log_text and "run_finish" in log_text
    # 日志不写入完整原文
    assert "一只橘猫坐在窗台上" not in log_text


def test_manifest_lists_all_artifacts_and_no_absolute_paths(config_file, output_root,
                                                           tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_manifest",
                           config_path=str(config_file), output_root=str(output_root))
    manifest = json.loads(Path(result["manifestPath"]).read_text(encoding="utf-8"))
    paths = [item["path"] for item in manifest["artifacts"]]
    assert delivery.MAIN_NAME in paths
    assert delivery.EXCLUDED_NAME in paths
    assert delivery.REJECTS_NAME in paths
    assert delivery.MATERIALS_NAME in paths
    assert result["packageName"] in paths
    assert "manifest.json" not in paths
    assert not any(path.startswith(("/", "C:")) for path in paths)
    assert Path(result["outputFile"]).read_text(encoding="utf-8").strip().splitlines()


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_stage_lineage_forms(config_file, output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="task_stage",
                           config_path=str(config_file), output_root=str(output_root))
    intermediate = inference.build_intermediate_lineage(result)
    assert intermediate["outputFile"] == result["outputFile"]
    final = inference.build_stage_payload(result)
    assert isinstance(final, list)
    assert all("outputFile" not in node for node in final)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _cli(args, cwd=BASE):
    # 必须继承完整环境（Windows 上缺 SYSTEMROOT 会导致解释器初始化失败）；
    # 刻意不设 PYTHONIOENCODING，用于验证 CLI 自行把 stdout 固定为 UTF-8。
    env = dict(__import__("os").environ)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    return subprocess.run([sys.executable, "processor.py"] + args, cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", env=env)


def test_cli_returns_zero_with_intermediate_response(output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    proc = _cli(["--input", str(entry), "--unique-id", "cli_ok",
                 "--output-root", str(output_root)])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["code"] == 0
    # [v4.2] 血缘输出已停用：CLI 直接打印 run() 结果（无 lineageFile）
    node = payload["data"]
    assert node["versionState"] == delivery.VERSION_STATE_DELIVERABLE
    assert node["statistics"]["packaged_records"] == 10
    assert Path(node["outputFile"]).is_file()


def test_cli_last_operator_returns_file_object_and_package(output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=False)
    proc = _cli(["--input", str(entry), "--unique-id", "cli_last",
                 "--output-root", str(output_root), "--is-last-operator"])
    assert proc.returncode == 1, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["code"] == 1
    assert set(payload["data"]) == {"outputFile"}
    file_object = payload["data"]["outputFile"]
    assert file_object["entrypoint"] == delivery.MAIN_NAME
    paths = [item["path"] for item in file_object["files"]]
    assert delivery.PACKAGE_PENDING_NAME in paths


def test_cli_reports_failure_with_exit_code(output_root, tmp_path):
    entry = build_scenario(tmp_path / "task", written_back=True)
    proc = _cli(["--input", str(entry), "--unique-id", "../escape",
                 "--output-root", str(output_root)])
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["code"] == 1
