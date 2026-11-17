"""run() 级测试：两条路线、台账、统计守恒、任务隔离、路径安全、血缘与文件对象。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import inference
from tests import fixtures
from utils import io as io_utils
from utils import temporal
from utils import transport
from utils.schemas import ConfigError

BASE_DIR = Path(__file__).resolve().parents[1]
SAMPLE = BASE_DIR / "dataset" / "sample" / "demo.jsonl"

#: dataset/sample/demo.jsonl 的期望统计（8 处业务分支 + 2 条坏行 + 1 条空行）
SAMPLE_ENABLED = {
    "input_records": 18, "valid_records": 16, "bad_lines": 2, "blank_lines": 1,
    "passed_records": 6, "failed_records": 1, "not_applicable_records": 5,
    "pending_calibration_records": 0, "skipped_records": 0, "origin_quarantined": 4,
    "main_records": 11, "calibrated_records": 6, "uncalibrated_records": 1,
    "rejects_entries": 6,
}
SAMPLE_DISABLED = {
    "input_records": 18, "valid_records": 16, "bad_lines": 2, "blank_lines": 1,
    "passed_records": 0, "failed_records": 0, "not_applicable_records": 0,
    "pending_calibration_records": 0, "skipped_records": 15, "origin_quarantined": 1,
    "main_records": 15, "calibrated_records": 0, "uncalibrated_records": 0,
    "rejects_entries": 3,
}


@pytest.fixture(scope="session", autouse=True)
def _ensure_sample():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_sample", BASE_DIR / "dataset" / "sample" / "make_sample.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    assert SAMPLE.is_file()


def _collect(result):
    stats = result["statistics"]
    return {key: stats[key] for key in SAMPLE_ENABLED}


# --------------------------------------------------------------------------- #
# 路线
# --------------------------------------------------------------------------- #

def test_disabled_route_passthrough(output_root):
    result = inference.run(str(SAMPLE), "task_off", output_root=str(output_root),
                           allow_external_input=True)
    stats = result["statistics"]
    assert stats["enabled"] is False
    assert result["entrypoint"] == temporal.MAIN_NAME_DISABLED
    assert _collect(result) == SAMPLE_DISABLED
    assert stats["conservation_ok"] is True
    assert result["status"] == "partial"
    records = fixtures.read_jsonl(result["outputFile"])
    assert len(records) == SAMPLE_DISABLED["main_records"]
    for record in records:
        assert "temporal_alignment" not in record
        # [v4.2] status 双字段：int status + process_status
        assert record["processing"]["15"]["status"] == 1
        assert record["processing"]["15"]["process_status"] == temporal.STATUS_SKIPPED
    assert fixtures.read_jsonl(result["sampleQuarantineFile"]) == []
    assert fixtures.read_jsonl(result["pendingFile"]) == []


def test_enabled_route_counts_and_ledgers(output_root):
    result = inference.run(str(SAMPLE), "task_on", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    stats = result["statistics"]
    assert result["entrypoint"] == temporal.MAIN_NAME_ENABLED
    assert _collect(result) == SAMPLE_ENABLED
    assert stats["conservation_ok"] is True
    assert stats["reasons"]["timeline_invalid"] == 1
    assert stats["reasons"]["timeline_unreadable"] == 2
    assert stats["reasons"]["json_parse_error"] == 1
    assert stats["reasons"]["record_not_object"] == 1
    assert stats["reasons"]["missing_sample_id"] == 1
    assert stats["reasons"]["no_timestamp_timeline"] == 1
    assert stats["reasons"]["missing_audio_timeline"] == 1
    assert stats["reasons"]["missing_text_timeline"] == 1
    assert stats["reasons"]["missing_video_timeline"] == 1
    assert stats["reasons"]["no_temporal_binding"] == 1
    assert stats["reasons"]["max_delta_exceeded"] == 1
    # 可搬运引用：tl-normal 的 subtitles.srt 与 tl-sidecar-ok 的 tl_ok.json；
    # tl-normal 的 absent_clip.mp4 缺失；tl-sidecar-broken 在读 sidecar 时即进原始隔离，不搬运
    assert stats["media_carried"] == 2
    assert stats["media_missing"] == 1

    main = fixtures.read_jsonl(result["outputFile"])
    assert len(main) == SAMPLE_ENABLED["main_records"]
    statuses = [record["temporal_alignment"]["process_status"] for record in main]
    assert statuses.count("passed") == 6
    assert statuses.count("not_applicable") == 5

    # 不适用：不隔离、不能写成通过
    for record in main:
        block = record["temporal_alignment"]
        if block["process_status"] == "not_applicable":
            assert block["status"] == 1 and block["passed"] is None
            assert block["destination"] == temporal.DEST_SAMPLE_POOL

    quarantine = fixtures.read_jsonl(result["sampleQuarantineFile"])
    assert len(quarantine) == 1
    entry = quarantine[0]
    assert entry["label"] == temporal.LABEL_FAIL
    assert entry["destination"] == temporal.DEST_SAMPLE_QUARANTINE
    assert entry["record"]["sample_id"] == "tl-over-threshold"
    assert entry["record"]["temporal_alignment"]["max_delta_sec"] == pytest.approx(2.0)
    assert entry["record"]["temporal_alignment"]["passed"] is False

    rejects = fixtures.read_jsonl(result["rejectsFile"])
    assert len(rejects) == SAMPLE_ENABLED["rejects_entries"]
    reasons = [item["reason_code"] for item in rejects]
    assert temporal.REASON_JSON_PARSE_ERROR in reasons
    assert temporal.REASON_RECORD_NOT_OBJECT in reasons
    assert temporal.REASON_MISSING_SAMPLE_ID in reasons


def test_rejects_keep_line_numbers(output_root, tmp_path):
    source = tmp_path / "mixed.jsonl"
    fixtures.write_jsonl(source, [fixtures.make_record("ok-1")],
                         raw_tail='{"broken": ')
    source.write_text(source.read_text(encoding="utf-8")
                      + json.dumps(fixtures.make_record("ok-2"), ensure_ascii=False) + "\n"
                      + '"not-an-object"\n', encoding="utf-8")
    result = inference.run(str(source), "task_lines", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    stats = result["statistics"]
    assert stats["input_records"] == 4
    assert stats["bad_lines"] == 2
    assert stats["passed_records"] == 2
    rejects = fixtures.read_jsonl(result["rejectsFile"])
    assert [item["line_number"] for item in rejects] == [2, 4]
    assert stats["conservation_ok"] is True


def test_sidecar_problem_labels(output_root, tmp_path):
    source = tmp_path / "sidecar.jsonl"
    fixtures.write_jsonl(source, [
        {"sample_id": "missing-sidecar", "timeline_file": "media/none.json"},
        fixtures.make_record("bad-ts", timeline={
            "video": [{"id": "f0", "timestamp_sec": "abc"}],
            "audio": [{"id": "a0", "start_sec": 0.0}],
            "text": [{"id": "t0", "start_sec": 0.0}],
        }),
    ])
    result = inference.run(str(source), "task_sidecar", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    rejects = fixtures.read_jsonl(result["rejectsFile"])
    labels = {item["sample_id"]: item["label"] for item in rejects}
    assert labels["missing-sidecar"] == temporal.LABEL_ORIGIN_DAMAGED
    assert labels["bad-ts"] == temporal.LABEL_ORIGIN_UNPARSABLE
    assert result["statistics"]["origin_quarantined"] == 2


def test_require_calibration_goes_degraded(output_root, tmp_path):
    source = tmp_path / "noanchors.jsonl"
    fixtures.write_jsonl(source, [fixtures.make_record("no-anchor", anchor_offset=None)])
    result = inference.run(str(source), "task_pending", output_root=str(output_root),
                           overrides={"enabled": True, "require_calibration": True},
                           allow_external_input=True)
    stats = result["statistics"]
    assert result["status"] == "degraded"
    assert stats["degraded"] is True
    assert stats["pending_calibration_records"] == 1
    assert stats["failed_records"] == 0
    assert stats["passed_records"] == 0
    assert stats["main_records"] == 0
    assert stats["conservation_ok"] is True
    pending = fixtures.read_jsonl(result["pendingFile"])
    assert pending[0]["action"] == temporal.ACTION_WAITING_MANUAL
    assert pending[0]["destination"] == temporal.DEST_PIPELINE_WAIT
    assert fixtures.read_jsonl(result["sampleQuarantineFile"]) == []


# --------------------------------------------------------------------------- #
# 空输入与编码
# --------------------------------------------------------------------------- #

def test_empty_input(output_root, tmp_path):
    source = tmp_path / "empty.jsonl"
    source.write_text("", encoding="utf-8")
    result = inference.run(str(source), "task_empty", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    assert result["status"] == "success"
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["input_records"] == 0
    assert result["statistics"]["conservation_ok"] is True
    assert (Path(result["outputFile"]).read_text(encoding="utf-8")) == ""


def test_blank_lines_only(output_root, tmp_path):
    source = tmp_path / "blank.jsonl"
    source.write_text("\n\n   \n", encoding="utf-8")
    result = inference.run(str(source), "task_blank", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    stats = result["statistics"]
    assert stats["blank_lines"] == 3
    assert stats["input_records"] == 0
    assert stats["empty_input"] is False
    assert stats["conservation_ok"] is True


def test_encoding_error_fails_loudly(output_root, tmp_path):
    source = tmp_path / "gbk.jsonl"
    source.write_bytes("样例".encode("gbk") + b"\n")
    result = inference.run(str(source), "task_encoding", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    stats = result["statistics"]
    assert stats["encoding_error"] is True
    assert stats["file_read_incomplete"] is True
    assert stats["bad_lines"] == 1
    assert result["status"] == "failed"
    rejects = fixtures.read_jsonl(result["rejectsFile"])
    assert rejects[0]["label"] == temporal.LABEL_ORIGIN_ENCODING
    assert rejects[0]["reason_code"] == temporal.REASON_ENCODING_ERROR
    assert stats["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def test_config_missing_file(output_root, tmp_path):
    with pytest.raises(ConfigError):
        inference.run(str(SAMPLE), "task_cfg", output_root=str(output_root),
                      config_path=tmp_path / "nope.yaml", allow_external_input=True)


@pytest.mark.parametrize("payload", [
    {"max_time_delta_sec": 0.1},
    {"max_time_delta_sec": 2.5},
    {"max_time_delta_sec": "0.5"},
    {"enabled": "yes"},
    {"interpolation": "spline"},
    {"interpolation_method": "quadratic"},
    {"min_anchors_for_calibration": 1},
    {"max_bindings": 0},
    {"max_evidence_bindings": 1001},
    {"operator": {"name": "other", "version": "v1.0.0"}},
    {"operator": {"number": 16, "name": "temporal_consistency", "version": "v1.0.0"}},
    {"operator": {"number": 15, "name": "temporal_consistency", "version": ""}},
    {"unknown_key": 1},
])
def test_config_invalid_values_rejected(output_root, tmp_path, config_writer, payload):
    path = config_writer(**payload)
    with pytest.raises(ConfigError):
        inference.run(str(SAMPLE), "task_badcfg", output_root=str(output_root),
                      config_path=path, allow_external_input=True)


def test_config_file_must_be_object(output_root, tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        inference.run(str(SAMPLE), "task_cfg2", output_root=str(output_root),
                      config_path=path, allow_external_input=True)


@pytest.mark.parametrize("overrides", [
    {"interpolation": "spline"},
    {"operator": {"name": "temporal_consistency", "version": "v9"}},
    {"unknown": 1},
    {"enabled": 1},
    [1, 2],
])
def test_override_validation(output_root, overrides):
    with pytest.raises(ConfigError):
        inference.run(str(SAMPLE), "task_ovr", output_root=str(output_root),
                      overrides=overrides, allow_external_input=True)


def test_override_threshold_affects_result(output_root, tmp_path):
    source = tmp_path / "thr.jsonl"
    fixtures.write_jsonl(source, [fixtures.make_record("mid", video=(0.0, 1.0),
                                                       audio=(0.4, 1.4),
                                                       text=(0.4, 1.4))])
    lenient = inference.run(str(source), "task_thr1", output_root=str(output_root),
                            overrides={"enabled": True, "max_time_delta_sec": 0.5},
                            allow_external_input=True)
    strict = inference.run(str(source), "task_thr2", output_root=str(output_root),
                           overrides={"enabled": True, "max_time_delta_sec": 0.2},
                           allow_external_input=True)
    assert lenient["statistics"]["passed_records"] == 1
    assert strict["statistics"]["failed_records"] == 1
    assert strict["statistics"]["passed_records"] == 0


def test_threshold_lower_bound_is_usable(output_root, tmp_path):
    source = tmp_path / "thr2.jsonl"
    fixtures.write_jsonl(source, [fixtures.make_record("exact", video=(0.0, 1.0),
                                                       audio=(0.2, 1.2),
                                                       text=(0.2, 1.2))])
    result = inference.run(str(source), "task_thr3", output_root=str(output_root),
                           overrides={"enabled": True, "max_time_delta_sec": 0.2},
                           allow_external_input=True)
    assert result["statistics"]["passed_records"] == 1


# --------------------------------------------------------------------------- #
# 任务隔离与路径安全
# --------------------------------------------------------------------------- #

def test_task_isolation(output_root):
    first = inference.run(str(SAMPLE), "task_a", output_root=str(output_root),
                          overrides={"enabled": True}, allow_external_input=True)
    second = inference.run(str(SAMPLE), "task_b", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    assert Path(first["runDir"]).parts[-3] == "task_a"
    assert Path(second["runDir"]).parts[-3] == "task_b"
    assert not Path(first["runDir"]).parent.parent == Path(second["runDir"]).parent.parent
    assert all(str(Path(path).resolve()).startswith(str(output_root.resolve()))
               for path in (first["outputFile"], second["outputFile"]))


def test_output_stays_inside_task_dir(output_root):
    result = inference.run(str(SAMPLE), "task_scope", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    task_root = output_root / "task_scope"
    for key in ("outputFile", "rejectsFile", "sampleQuarantineFile", "pendingFile",
                "manifestPath"):
        assert io_utils.is_within(task_root.resolve(), Path(result[key]).resolve())


def test_run_id_increments(output_root):
    first = inference.run(str(SAMPLE), "task_runs", output_root=str(output_root),
                          overrides={"enabled": True}, allow_external_input=True)
    second = inference.run(str(SAMPLE), "task_runs", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()


def test_input_file_outside_task_rejected(output_root, tmp_path):
    outside = tmp_path / "outside.jsonl"
    fixtures.write_jsonl(outside, [fixtures.make_record("x")])
    with pytest.raises(ValueError):
        inference.run(str(outside), "task_escape", output_root=str(output_root),
                      overrides={"enabled": True}, allow_external_input=False)


@pytest.mark.parametrize("unique_id", ["", "..", ".", "a/b", "a\\b", "con", "nul.json",
                                       "../x", "C:\\abs", "has:colon", "/abs"])
def test_unique_id_rejected(unique_id):
    with pytest.raises(ValueError):
        io_utils.validate_unique_id(unique_id)


def test_input_file_from_task_input_dir(output_root, tmp_path):
    """阶段内后续算子：inputFile 指向任务目录内上一节点的中间产物。"""
    task = io_utils.task_dir(output_root, "task_chain")
    previous = task / "temporal_consistency" / "run001" / temporal.MAIN_NAME_ENABLED
    fixtures.write_jsonl(previous, [fixtures.make_record("chained")])
    result = inference.run(str(previous), "task_chain", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=False)
    assert result["statistics"]["input_records"] == 1
    assert result["statistics"]["passed_records"] == 1


# --------------------------------------------------------------------------- #
# 血缘 / 清单 / 文件对象
# --------------------------------------------------------------------------- #

def test_lineage_outputs_disabled(output_root):
    """[v4.2] 血缘/阶段归档已停用：run() 不回传血缘键，也不写 lineage/context 文件。"""
    result = inference.run(str(SAMPLE), "task_lineage", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    for key in ("lineageFile", "stageLineage", "priorLineage"):
        assert key not in result
    run_dir = Path(result["runDir"])
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    # 业务统计仍完整
    assert result["statistics"]["passed_records"] == 6
    assert result["statistics"]["conservation_ok"] is True


def test_prior_lineage_is_read_not_written(output_root):
    task = io_utils.task_dir(output_root, "task_prior")
    input_dir = task / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    history = [{"operator": "previous_stage", "operator_version": "v1.0.0",
                "uniqueId": "task_prior", "inputFile": "x.jsonl", "status": "success"}]
    (input_dir / "lineage_history.json").write_text(
        json.dumps(history, ensure_ascii=False), encoding="utf-8")
    fixtures.write_jsonl(input_dir / "demo.jsonl", [fixtures.make_record("prior")])
    result = inference.run("input/demo.jsonl", "task_prior", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=False)
    # [v4.2] 不建立阶段血缘，也不回传 priorLineage；历史文件必须原样保留
    assert "priorLineage" not in result
    assert json.loads((input_dir / "lineage_history.json").read_text(encoding="utf-8")) \
        == history


def test_manifest_lists_every_payload(output_root):
    result = inference.run(str(SAMPLE), "task_manifest", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    manifest = transport.load_manifest(result["runDir"])
    assert manifest["format"] == transport.MANIFEST_FORMAT
    listed = set(transport.iter_manifest_paths(manifest))
    assert temporal.MAIN_NAME_ENABLED in listed
    assert temporal.REJECTS_NAME in listed
    for relative in listed:
        assert (Path(result["runDir"]) / relative).is_file(), relative


def test_intermediate_and_stage_payload_shapes(output_root):
    """[v4.2] 中间态/阶段末响应构造已停用；统一走 outputFile + result。"""
    result = inference.run(str(SAMPLE), "task_shape", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    assert not hasattr(inference, "build_intermediate_lineage")
    assert not hasattr(inference, "build_stage_payload")
    payload = inference.stage_file_object(result)
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == temporal.MAIN_NAME_ENABLED


def test_stage_file_object_roundtrip(output_root, tmp_path):
    result = inference.run(str(SAMPLE), "task_pack", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    payload = inference.stage_file_object(result)
    assert payload["format"] == transport.FILE_OBJECT_FORMAT
    assert payload["entrypoint"] == temporal.MAIN_NAME_ENABLED
    paths = {item["path"] for item in payload["files"]}
    assert temporal.MAIN_NAME_ENABLED in paths
    assert transport.MANIFEST_NAME in paths

    restored = tmp_path / "restored"
    entry = transport.restore_file_object(payload, restored)
    assert entry.read_text(encoding="utf-8") == \
        Path(result["outputFile"]).read_text(encoding="utf-8")
    assert (restored / temporal.SAMPLE_QUARANTINE_NAME).is_file()


def test_stage_file_object_size_limit(output_root):
    result = inference.run(str(SAMPLE), "task_limit", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    with pytest.raises(transport.OutputTooLargeError):
        inference.stage_file_object(result, max_response_bytes=64)


def test_media_carried_into_run_dir(output_root):
    result = inference.run(str(SAMPLE), "task_media", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    assert result["statistics"]["media_carried"] == 2
    assert "media/subtitles.srt" in result["mediaPaths"]
    assert (Path(result["runDir"]) / "media" / "tl_ok.json").is_file()


def test_main_output_is_utf8_and_parsable(output_root):
    result = inference.run(str(SAMPLE), "task_utf8", output_root=str(output_root),
                           overrides={"enabled": True}, allow_external_input=True)
    raw = Path(result["outputFile"]).read_bytes()
    text = raw.decode("utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == result["statistics"]["main_records"]
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_sample_manifest_declares_entrypoint():
    manifest = json.loads((BASE_DIR / "dataset" / "sample" / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["format"] == transport.MANIFEST_FORMAT
    assert manifest["entrypoint"] == "demo.jsonl"
    for artifact in manifest["artifacts"]:
        assert (BASE_DIR / "dataset" / "sample" / artifact["path"]).is_file()
