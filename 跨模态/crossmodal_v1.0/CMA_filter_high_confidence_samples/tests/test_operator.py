"""算子 19 端到端单测：``inference.run``、CLI、统计守恒、血缘、路径安全。

所有断言都基于真实运行产物（输出行数、台账行数、statistics、lineage 内容），
不依赖任何外部数据或模型。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import (DEFAULT_CONFIG_YAML, make_pair, make_record, rec_12, rec_13,
                      rec_14, rec_15, rec_16, rec_17, rec_18, rec_license,
                      tmp_path_yaml, write_jsonl)

import inference
from utils import io as io_utils
from utils import pool_filter, transport
from utils.schemas import ConfigError, FrozenOverrideError


# --------------------------------------------------------------------------- #
# 样例构造
# --------------------------------------------------------------------------- #

def full_sample() -> list:
    """覆盖四道门与全部台账的真实样例（含坏行、空行、缺字段、重复标识）。"""
    return [
        # 1–3 入池（三种评分路线各一）
        make_record("hc-0001", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
        make_record("hc-0002", rec_13(True, "passed"), rec_16("manual_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
        make_record("hc-0003", rec_14(True, "passed"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("passed", True, "within_threshold")),
        # 4–7 必过项失败 → 样本隔离库
        make_record("hc-0004", rec_12("isolated"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
        make_record("hc-0005", rec_12("auto_aligned", clip_score=0.97),
                    rec_16("not_aligned"), rec_license("可训练"),
                    rec_15("passed", True)),
        make_record("hc-0006", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("不可训练"), rec_15("not_applicable", None)),
        make_record("hc-0007", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("failed", False, "max_delta_exceeded")),
        # 8–11 结论缺失/待人工 → 流程状态（不隔离）
        make_record("hc-0008", rec_12("pending_manual"),
                    rec_16("pending_manual_review"), rec_license("可训练"),
                    rec_15("not_applicable", None)),
        make_record("hc-0009", rec_16("auto_aligned"), rec_license("可训练"),
                    rec_15("not_applicable", None)),
        make_record("hc-0010", rec_13(None, "pending", reason="missing_transcript"),
                    rec_16("auto_aligned"), rec_license("可训练"),
                    rec_15("not_applicable", None)),
        make_record("hc-0011", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_15("not_applicable", None)),
        # 12 局部标注待人工 → 仍入池（局部标注不是硬条件）
        make_record("hc-0012", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None),
                    rec_17("pending_manual_review"), rec_18("pending_manual")),
        # 13 上游 #18 已隔离 → 尊重上游隔离
        make_record("hc-0013", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None),
                    rec_18("isolated")),
        # 14 成对记录 → 入池
        make_pair("pair-0014",
                  left=make_record("hc-0014-text", rec_13(True, "passed"),
                                   rec_license("可训练")),
                  right=make_record("hc-0014-image", rec_12("auto_aligned"),
                                    rec_16("auto_aligned"),
                                    rec_15("not_applicable", None))),
        # 15 媒体引用非法 → 原始隔离区
        make_record("hc-0015", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None),
                    {"image": "/abs/escape.png"}),
        # 16 双路线一通过一失败 → 隔离
        make_record("hc-0016", rec_12("auto_aligned"), rec_13(False, "failed"),
                    rec_16("auto_aligned"), rec_license("可训练"),
                    rec_15("not_applicable", None)),
        # 17 缺标识 / 18 重复标识
        make_record(None, rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
        make_record("hc-0017", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
        make_record("hc-0017", rec_12("auto_aligned"), rec_16("auto_aligned"),
                    rec_license("可训练"), rec_15("not_applicable", None)),
        # 坏行 / 非对象 / 空行
        '{"sample_id": "broken", ',
        "[1, 2, 3]",
        "",
    ]


@pytest.fixture()
def sample_file(tmp_path) -> Path:
    return write_jsonl(tmp_path / "demo.jsonl", full_sample())


def run_sample(sample_file: Path, config_file: Path, output_root: Path,
               unique_id: str = "task_a", **kwargs) -> dict:
    return inference.run(input_file=str(sample_file), unique_id=unique_id,
                         config_path=str(config_file), output_root=str(output_root),
                         **kwargs)


# --------------------------------------------------------------------------- #
# 主流程与统计
# --------------------------------------------------------------------------- #

def test_run_produces_expected_counts(sample_file, config_file, output_root):
    result = run_sample(sample_file, config_file, output_root)
    stats = result["statistics"]
    assert stats["input_records"] == 21
    assert stats["valid_records"] == 19
    assert stats["bad_lines"] == 2
    assert stats["blank_lines"] == 1
    assert stats["admitted_records"] == 6
    assert stats["sample_isolated"] == 6
    assert stats["pending_records"] == 4
    assert stats["origin_quarantined"] == 3
    assert stats["conservation_ok"] is True
    assert stats["conservation_failures"] == []


def test_output_file_line_count_matches_admitted(sample_file, config_file, output_root):
    result = run_sample(sample_file, config_file, output_root)
    lines = [line for line in Path(result["outputFile"]).read_text(
        encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == result["statistics"]["admitted_records"]
    for line in lines:
        record = json.loads(line)
        # [v4.2] status 双字段：int status + process_status
        assert record["processing"]["19"]["status"] == 1
        assert record["processing"]["19"]["process_status"] == "admitted"
        assert record["operator_19"]["status"] == 1
        assert record["operator_19"]["process_status"] == "admitted"
        assert record["operator_19"]["decision"] == "admit"


def test_ledger_line_counts_match_statistics(sample_file, config_file, output_root):
    result = run_sample(sample_file, config_file, output_root)
    stats = result["statistics"]

    def count(path):
        return len([line for line in Path(path).read_text(encoding="utf-8").splitlines()
                    if line.strip()])

    assert count(result["quarantineFile"]) == stats["sample_isolated"]
    assert count(result["pendingFile"]) == stats["pending_records"]
    assert count(result["rejectsFile"]) == stats["rejects_entries"]
    assert stats["rejects_entries"] == stats["bad_lines"] + stats["origin_quarantined"]


def test_isolation_reasons_are_traceable(sample_file, config_file, output_root):
    result = run_sample(sample_file, config_file, output_root)
    rows = [json.loads(line) for line in
            Path(result["quarantineFile"]).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    reasons = sorted(row["reason"] for row in rows)
    assert reasons == sorted([pool_filter.REASON_LICENSE_NOT_TRAINABLE,
                              pool_filter.REASON_ROUTE_FAILED,
                              pool_filter.REASON_ROUTE_FAILED,
                              pool_filter.REASON_TEMPORAL_FAILED,
                              pool_filter.REASON_UPSTREAM_ISOLATED,
                              pool_filter.REASON_WHOLE_NOT_ALIGNED])
    for row in rows:
        assert row["label"] == pool_filter.LABEL_ISOLATED
        assert row["destination"] == pool_filter.DEST_QUARANTINE
        assert row["record"]["sample_id"] or row["record"]["pair_id"]


def test_pending_reasons_are_traceable(sample_file, config_file, output_root):
    result = run_sample(sample_file, config_file, output_root)
    rows = [json.loads(line) for line in
            Path(result["pendingFile"]).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    reasons = sorted(row["reason"] for row in rows)
    assert reasons == sorted([pool_filter.REASON_LICENSE_MISSING,
                              pool_filter.REASON_NO_ROUTE,
                              pool_filter.REASON_ROUTE_PENDING,
                              pool_filter.REASON_WHOLE_PENDING])
    for row in rows:
        assert row["destination"] == pool_filter.DEST_WAIT
        assert row["label"].startswith("流程-")


def test_rejects_ledger_holds_bad_lines_and_origin_records(sample_file, config_file,
                                                           output_root):
    result = run_sample(sample_file, config_file, output_root)
    rows = [json.loads(line) for line in
            Path(result["rejectsFile"]).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    reasons = sorted(row["reason"] for row in rows)
    assert reasons == sorted([pool_filter.REASON_JSON_PARSE,
                              pool_filter.REASON_MISSING_IDENTIFIER,
                              pool_filter.REASON_NOT_OBJECT,
                              pool_filter.REASON_DUPLICATE_IDENTIFIER,
                              pool_filter.REASON_UNSAFE_MEDIA_PATH])
    assert all(row["line"] >= 1 for row in rows)


def test_statistics_carry_no_model_and_rule_only(sample_file, config_file, output_root):
    stats = run_sample(sample_file, config_file, output_root)["statistics"]
    assert stats["model"] is None
    assert stats["models_available"] is False
    assert stats["rule_based"] is True
    assert stats["local_annotation_hard_requirement"] is False


def test_lineage_outputs_disabled(sample_file, config_file, output_root):
    """[v4.2] 血缘/阶段归档已停用：run() 不回传血缘键，也不写 lineage/context 文件。"""
    result = run_sample(sample_file, config_file, output_root)
    for key in ("lineageFile", "contextFile", "stageLineage", "priorLineage",
                "priorStageContexts"):
        assert key not in result
    run_dir = Path(result["runDir"])
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    assert result["statistics"]["admitted_records"] == 6


def test_intermediate_and_last_shapes(sample_file, config_file, output_root):
    """[v4.2] 中间态/阶段末响应构造已停用；统一走 outputFile + result。"""
    result = run_sample(sample_file, config_file, output_root)
    assert not hasattr(inference, "build_intermediate_lineage")
    assert not hasattr(inference, "build_stage_payload")
    payload = transport.pack_file_object(
        Path(result["runDir"]), pool_filter.MAIN_NAME,
        [pool_filter.MAIN_NAME, transport.MANIFEST_NAME],
        max_response_bytes=result["transport"]["maxResponseBytes"])
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == pool_filter.MAIN_NAME


def test_output_is_utf8_and_input_is_untouched(sample_file, config_file, output_root):
    before = sample_file.read_bytes()
    result = run_sample(sample_file, config_file, output_root)
    assert sample_file.read_bytes() == before
    raw = Path(result["outputFile"]).read_bytes()
    raw.decode("utf-8")
    assert "hc-0012" in raw.decode("utf-8")


def test_manifest_and_artifacts_are_registered(sample_file, config_file, output_root):
    result = run_sample(sample_file, config_file, output_root)
    manifest = json.loads(Path(result["manifestPath"]).read_text(encoding="utf-8"))
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == "19_candidate_pool.jsonl"
    paths = [item["path"] for item in manifest["artifacts"]]
    assert "19_candidate_pool.jsonl" in paths
    assert "19_sample_quarantine.jsonl" in paths
    assert "19_pending.jsonl" in paths
    assert "19_rejects.jsonl" in paths


# --------------------------------------------------------------------------- #
# 任务隔离与路径安全
# --------------------------------------------------------------------------- #

#: 运行时间戳：两次独立运行的取值天然不同，比对业务等价性时统一抹平
_TIME_KEYS = frozenset({"evaluated_at", "generated_at", "generatedAt", "started_at",
                        "finished_at", "evaluatedAt"})


def _scrub_times(value):
    """递归把时间戳字段替换为占位符（仅用于跨运行的内容比对）。"""
    if isinstance(value, dict):
        return {key: ("<time>" if key in _TIME_KEYS else _scrub_times(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_times(item) for item in value]
    return value


def _records_without_times(path: Path) -> list:
    return [_scrub_times(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_two_unique_ids_are_isolated(sample_file, config_file, output_root):
    first = run_sample(sample_file, config_file, output_root, unique_id="task_a")
    second = run_sample(sample_file, config_file, output_root, unique_id="task_b")
    assert Path(first["runDir"]) != Path(second["runDir"])
    assert "task_a" in first["runDir"] and "task_b" in second["runDir"]
    # 内容等价性：抹掉运行时间戳后再比（``evaluated_at`` 逐次运行本就不同）
    assert _records_without_times(Path(first["outputFile"])) == \
        _records_without_times(Path(second["outputFile"]))


def test_same_unique_id_creates_new_run_without_overwrite(sample_file, config_file,
                                                          output_root):
    first = run_sample(sample_file, config_file, output_root, unique_id="task_a")
    second = run_sample(sample_file, config_file, output_root, unique_id="task_a")
    assert first["runId"] != second["runId"]
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "", ".", "..", "CON"])
def test_invalid_unique_id_rejected(sample_file, config_file, output_root, bad):
    with pytest.raises(ValueError):
        run_sample(sample_file, config_file, output_root, unique_id=bad)


def test_input_outside_task_rejected(sample_file, config_file, output_root):
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(sample_file), unique_id="task_a",
                      config_path=str(config_file), output_root=str(output_root),
                      allow_external_input=False)


def test_task_lock_conflict_raises(sample_file, config_file, output_root):
    with io_utils.TaskLock(Path(output_root), "task_lock"):
        with pytest.raises(io_utils.TaskBusyError):
            run_sample(sample_file, config_file, output_root, unique_id="task_lock")


# --------------------------------------------------------------------------- #
# 空输入 / 坏输入 / 编码
# --------------------------------------------------------------------------- #

def test_empty_input_reports_empty_flag(tmp_path, config_file, output_root):
    empty = write_jsonl(tmp_path / "empty.jsonl", [])
    result = inference.run(input_file=str(empty), unique_id="empty_f1",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["empty_input"] is True
    assert stats["input_records"] == 0
    assert stats["conservation_ok"] is True
    assert result["status"] == "success"


def test_all_bad_lines_is_failed_not_crash(tmp_path, config_file, output_root):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{also bad\n[1,2,3]\n", encoding="utf-8")
    result = inference.run(input_file=str(bad), unique_id="bad_f1",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["valid_records"] == 0
    assert stats["bad_lines"] == 3
    assert result["status"] == "failed"
    assert stats["conservation_ok"] is True


def test_invalid_utf8_bytes_are_counted_not_silently_dropped(tmp_path, config_file,
                                                             output_root):
    path = tmp_path / "gbk.jsonl"
    path.write_bytes('{"sample_id": "\u4e2d\u6587"}\n'.encode("gbk"))
    result = inference.run(input_file=str(path), unique_id="gbk_f1",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["encoding_error"] is True
    assert stats["bad_lines"] >= 1
    assert stats["rejects_entries"] >= 1
    assert stats["conservation_ok"] is True


def test_degraded_when_nothing_can_be_decided(tmp_path, config_file, output_root):
    rows = [make_record("only-pending-%d" % index, rec_16("pending_manual_review"),
                        rec_12("pending_manual"), rec_license("可训练"),
                        rec_15("not_applicable", None))
            for index in range(3)]
    path = write_jsonl(tmp_path / "pending.jsonl", rows)
    result = inference.run(input_file=str(path), unique_id="p_f1",
                           config_path=str(config_file), output_root=str(output_root))
    assert result["statistics"]["admitted_records"] == 0
    assert result["statistics"]["pending_records"] == 3
    assert result["status"] == "degraded"
    assert result["statistics"]["degraded"] is True


def test_strict_policy_turns_missing_upstream_into_failure(tmp_path, config_file,
                                                           output_root):
    rows = [make_record("p-1", rec_16("pending_manual_review"), rec_12("pending_manual"),
                        rec_license("可训练"), rec_15("not_applicable", None))]
    path = write_jsonl(tmp_path / "strict.jsonl", rows)
    result = inference.run(input_file=str(path), unique_id="strict_f1",
                           config_path=str(config_file), output_root=str(output_root),
                           overrides={"missing_upstream_policy": "failed"})
    assert result["status"] == "failed"
    assert result["statistics"]["pending_records"] == 1


# --------------------------------------------------------------------------- #
# 配置校验
# --------------------------------------------------------------------------- #

def test_config_defaults_are_frozen_table1_values(config_file):
    config = inference.load_config(str(config_file))
    assert config.enabled is True
    assert config.required_alignment == "auto|human"
    assert config.require_temporal_if_present is True
    assert config.training_authorization == "required"
    assert config.min_enabled_routes == 1
    assert config.missing_upstream_policy == "waiting_manual"


def test_config_carries_operator_identity_and_transport_block(config_file):
    """计划里的 operator.name/label、transport.format/max_response_bytes 必须落地。"""
    config = inference.load_config(str(config_file))
    assert config.operator.number == 19
    assert config.operator.name == "high_confidence_pool"
    assert config.operator.label == "筛选高置信样本入池"
    assert config.transport.format == "crossmodal-files-v1"
    assert config.transport.max_response_bytes == 67108864
    assert config.operator.enabled is True


def test_config_rejects_wrong_transport_format(config_file):
    path = tmp_path_yaml(config_file, "transport:\n  format: other-v9\n")
    with pytest.raises(ConfigError):
        inference.load_config(str(path))


def test_config_rejects_too_small_transport_limit(config_file):
    path = tmp_path_yaml(config_file, "transport:\n  max_response_bytes: 10\n")
    with pytest.raises(ConfigError):
        inference.load_config(str(path))


def test_config_rejects_inconsistent_operator_enabled(config_file):
    path = tmp_path_yaml(config_file, "operator:\n  number: 19\n  enabled: false\n")
    with pytest.raises(ConfigError):
        inference.load_config(str(path))


def test_config_accepts_plan_style_operator_enabled_true(config_file):
    path = tmp_path_yaml(config_file, "operator:\n  number: 19\n  enabled: true\n")
    config = inference.load_config(str(path))
    assert config.enabled is True and config.operator.enabled is True


def test_run_exposes_transport_limits_from_config(sample_file, config_file, output_root):
    result = run_sample(sample_file, config_file, output_root)
    assert result["transport"]["format"] == "crossmodal-files-v1"
    assert result["transport"]["maxResponseBytes"] == 67108864


def test_config_cannot_be_disabled(config_file):
    with pytest.raises(ConfigError):
        inference.load_config(str(config_file), {"enabled": False})


@pytest.mark.parametrize("overrides", [
    {"training_authorization": "optional"},
    {"min_enabled_routes": 0},
    {"min_enabled_routes": 4},
    {"missing_upstream_policy": "ignore"},
    {"unknown_key": 1},
    {"clip": {"model_name": "ViT-B-32"}},
])
def test_invalid_config_rejected(config_file, overrides):
    with pytest.raises(ConfigError):
        inference.load_config(str(config_file), overrides)


def test_missing_config_file_is_input_error(tmp_path):
    with pytest.raises(inference.InputError):
        inference.load_config(str(tmp_path / "does-not-exist.yaml"))


def test_broken_yaml_is_input_error(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("operator: [unclosed\n", encoding="utf-8")
    with pytest.raises(inference.InputError):
        inference.load_config(str(path))


def test_config_snapshot_records_business_only(config_file):
    config = inference.load_config(str(config_file))
    snapshot = inference.lineage.config_snapshot(config)
    assert snapshot["operator"]["name"] == "high_confidence_pool"
    business = snapshot["business"]
    assert business["required_alignment"] == "auto|human"
    assert business["local_annotation_hard_requirement"] is False
    text = json.dumps(snapshot, ensure_ascii=False).lower()
    for forbidden in ("password", "token", "cookie", "secret"):
        assert forbidden not in text


def test_overrides_allow_business_keys_only(config_file):
    """请求级白名单只保留 4 个业务键；固定口径键不在其中（见下面的拒绝用例）。"""
    config = inference.load_config(str(config_file),
                                   {"min_enabled_routes": 2,
                                    "missing_upstream_policy": "failed"})
    assert config.min_enabled_routes == 2
    assert config.missing_upstream_policy == "failed"
    assert config.required_alignment == "auto|human"
    assert config.require_temporal_if_present is True


@pytest.mark.parametrize("overrides", [
    {"required_alignment": "auto"},
    {"required_alignment": "human"},
    {"required_alignment": "auto|human"},
    {"require_temporal_if_present": False},
    {"require_temporal_if_present": True},
])
def test_overrides_cannot_change_frozen_caliber(config_file, overrides):
    """表1 #19 固定口径：请求携带这两个键（即使值相同）一律 422 级拒绝。"""
    with pytest.raises(FrozenOverrideError) as excinfo:
        inference.load_config(str(config_file), overrides)
    message = str(excinfo.value)
    key = next(iter(overrides))
    assert key in message
    assert "固定口径" in message
    assert "四项必过项不得去掉任何一项" in message


def test_overrides_cannot_weaken_required_alignment_even_with_other_keys(config_file):
    """混在正常业务键里的固定口径键同样拒绝，绝不放行收窄。"""
    with pytest.raises(FrozenOverrideError):
        inference.load_config(str(config_file),
                              {"min_enabled_routes": 2,
                               "required_alignment": "auto"})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_success(sample_file, config_file, output_root, capsys, monkeypatch):
    monkeypatch.setenv("OPERATOR_CONFIG", str(config_file))
    code = inference.main(["--input", str(sample_file), "--unique-id", "cli_a",
                           "--output-root", str(output_root)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == 0
    assert payload["data"]["statistics"]["admitted_records"] == 6


def test_cli_reports_config_error(sample_file, config_file, output_root, capsys,
                                  monkeypatch):
    monkeypatch.setenv("OPERATOR_CONFIG", str(config_file))
    code = inference.main(["--input", str(sample_file), "--unique-id", "cli_b",
                           "--output-root", str(output_root),
                           "--override", '{"min_enabled_routes": 0}'])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == 1


def test_cli_rejects_frozen_caliber_override(sample_file, config_file, output_root,
                                             capsys, monkeypatch):
    """CLI ``--override`` 同样不能削弱固定口径：退出码 2 + 明确中文错误。"""
    monkeypatch.setenv("OPERATOR_CONFIG", str(config_file))
    code = inference.main(["--input", str(sample_file), "--unique-id", "cli_frozen",
                           "--output-root", str(output_root),
                           "--override", '{"required_alignment": "auto"}'])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == 1
    assert "固定口径" in payload["msg"]
    assert payload["data"]["errorType"] == "FrozenOverrideError"


def test_cli_reports_path_error(sample_file, config_file, output_root, capsys,
                                monkeypatch):
    monkeypatch.setenv("OPERATOR_CONFIG", str(config_file))
    code = inference.main(["--input", str(sample_file), "--unique-id", "../escape",
                           "--output-root", str(output_root)])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == 1


def test_cli_failed_status_exit_code(tmp_path, config_file, output_root, capsys,
                                     monkeypatch):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("nope\n", encoding="utf-8")
    monkeypatch.setenv("OPERATOR_CONFIG", str(config_file))
    code = inference.main(["--input", str(bad), "--unique-id", "cli_c",
                           "--output-root", str(output_root)])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == 0
    assert payload["data"]["status"] == "failed"


def test_processor_module_exposes_main():
    import processor
    assert callable(processor.main)


# --------------------------------------------------------------------------- #
# 随包样例可复现
# --------------------------------------------------------------------------- #

def test_shipped_sample_runs_and_conserves(tmp_path, config_file, output_root):
    base = Path(__file__).resolve().parents[1]
    sample = base / "dataset" / "sample" / "demo.jsonl"
    if not sample.is_file():
        pytest.skip("样例文件尚未生成")
    result = inference.run(input_file=str(sample), unique_id="shipped_f1",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["conservation_ok"] is True
    assert stats["input_records"] > 0
    assert stats["admitted_records"] > 0
    assert stats["sample_isolated"] > 0


def test_shipped_sample_carries_declared_media_with_same_relative_path(
        tmp_path, config_file, output_root):
    """声明的媒体真实存在时必须按原相对路径搬运，且登记为 artifact。"""
    base = Path(__file__).resolve().parents[1]
    sample = base / "dataset" / "sample" / "demo.jsonl"
    if not sample.is_file():
        pytest.skip("样例文件尚未生成")
    result = inference.run(input_file=str(sample), unique_id="shipped_media",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["media_refs"] == 1
    assert stats["media_missing"] == 0
    assert stats["media_carried"] == 1
    carried = Path(result["runDir"]) / "media" / "hc-0003.png"
    assert carried.is_file()
    assert carried.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    paths = [item["path"] for item in result["artifacts"]]
    assert "media/hc-0003.png" in paths
