"""端到端编排测试：输入校验、流式处理、异常台账、配置快照、真实统计、血缘与两种阶段形态。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import inference
from tests.conftest import build_input_dir, make_record, write_jsonl
from utils import io as io_utils
from utils import spot_check as sc
from utils import strata as strata_mod
from utils.schemas import ConfigError


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def read_jsonl(path) -> list:
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def run_op(tmp_path: Path, rows, *, report: bool = True, overrides=None,
           unique_id: str = "task_1", name: str = "24_candidate_pool.jsonl",
           sub: str = "in"):
    entry = build_input_dir(tmp_path / sub, rows, entry_name=name, report=report)
    return inference.run(input_file=str(entry), unique_id=unique_id,
                         output_root=tmp_path / "out", overrides=overrides,
                         allow_external_input=True)


def uniform_rows(verdict, count=10, **kwargs):
    return [make_record("u-%02d" % index, verdict=verdict, **kwargs)
            for index in range(count)]


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #

def test_run_produces_full_artifacts_and_true_statistics(tmp_path):
    rows = uniform_rows("pass")
    rows.append("")
    rows.append("{not-json")
    rows.append("[1, 2, 3]")
    rows.append(dict(rows[0]))                      # 重复 sample_id
    rows.append({"scene_id": "scene-hyd"})          # 缺 sample_id
    result = run_op(tmp_path, rows)

    stats = result["statistics"]
    assert stats["input_lines"] == 15
    assert stats["blank_lines"] == 1
    assert stats["input"] == 14
    assert stats["rejected"] == 2
    assert stats["valid"] == 12
    assert stats["origin_quarantined"] == 2
    assert stats["duplicates"] == 1
    assert stats["sampled"] == 1
    assert stats["sampled_passed"] == 1
    assert stats["sample_quarantined"] == 0
    assert stats["output"] == 10
    assert stats["conservation_ok"] is True
    assert stats["empty_input"] is False
    assert sc.conservation_failures(stats) == []
    assert stats["writeback"]["completed"] is True
    assert stats["writeback"]["source_report_available"] is True
    assert result["status"] == "partial"
    assert result["reportStatus"] == sc.REPORT_STATUS_WRITTEN

    run_dir = Path(result["runDir"])
    assert len(read_jsonl(run_dir / sc.MAIN_NAME)) == 10
    assert len(read_jsonl(run_dir / sc.REJECTS_NAME)) == 2
    assert len(read_jsonl(run_dir / sc.ORIGIN_QUARANTINE_NAME)) == 2
    assert len(read_jsonl(run_dir / sc.SAMPLED_NAME)) == 1
    assert (run_dir / sc.REPORT_NAME).is_file()
    assert (run_dir / sc.CONCLUSION_NAME).is_file()
    conclusion = json.loads((run_dir / sc.CONCLUSION_NAME).read_text(encoding="utf-8"))
    assert conclusion["status"] == sc.REPORT_STATUS_WRITTEN
    assert conclusion["written_back"] is True
    assert conclusion["version_state"] == sc.VERSION_STATE_DELIVERABLE
    assert (run_dir / "manifest.json").is_file()
    # [v4.2] 血缘/阶段归档已停用：不再落盘 lineage.json / stage_context.json
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    assert not list(run_dir.glob("*.partial"))
    assert not (run_dir / "frame.partial").exists()


def test_run_result_has_no_lineage_outputs(tmp_path):
    """[v4.2] 血缘/阶段归档已停用：返回值不再含 lineage 键，也不落盘血缘文件。"""
    result = run_op(tmp_path, uniform_rows("pass"))
    run_dir = Path(result["runDir"])
    for key in ("lineage", "stageLineage", "priorLineage", "stageContext",
                "lineageFile"):
        assert key not in result, key
    assert result["business"]["sample_rate"] == pytest.approx(0.10)
    assert result["statistics"]["conservation_ok"] is True
    assert {item["path"] for item in result["artifacts"]} >= {
        sc.MAIN_NAME, sc.REPORT_NAME, sc.CONCLUSION_NAME, sc.SAMPLED_NAME, sc.DISPUTES_NAME,
        sc.SAMPLE_QUARANTINE_NAME, sc.PENDING_NAME, sc.ORIGIN_QUARANTINE_NAME,
        sc.REJECTS_NAME}
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()


def test_override_sample_rate_is_reflected_in_business(tmp_path):
    result = run_op(tmp_path, uniform_rows("pass", count=10),
                    overrides={"sample_rate": 0.20})
    assert result["business"]["sample_rate"] == pytest.approx(0.20)
    assert result["statistics"]["sampled"] == 2


# --------------------------------------------------------------------------- #
# 边界与异常输入
# --------------------------------------------------------------------------- #

def test_empty_input_is_success_with_zero_output(tmp_path):
    result = run_op(tmp_path, [], report=True)
    assert result["status"] == "success"
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["input"] == 0
    assert result["statistics"]["output"] == 0
    assert result["statistics"]["sampled"] == 0
    assert result["statistics"]["conservation_ok"] is True
    assert read_jsonl(Path(result["runDir"]) / sc.MAIN_NAME) == []


def test_bad_lines_are_recorded_with_physical_line_numbers(tmp_path):
    rows = uniform_rows("pass", count=3)
    rows.insert(1, "{oops")
    rows.append("42")
    result = run_op(tmp_path, rows)
    rejects = read_jsonl(Path(result["runDir"]) / sc.REJECTS_NAME)
    assert [item["line"] for item in rejects] == [2, 5]
    assert {item["reason"] for item in rejects} == {"json_parse_error", "record_not_object"}
    assert all(item["destination"] == sc.DESTINATION_ORIGIN for item in rejects)


def test_missing_sample_id_and_duplicates_are_quarantined_not_dropped(tmp_path):
    rows = uniform_rows("pass", count=3)
    rows.append({"scene_id": "scene-hyd"})
    rows.append(dict(rows[0]))
    result = run_op(tmp_path, rows)
    origin = read_jsonl(Path(result["runDir"]) / sc.ORIGIN_QUARANTINE_NAME)
    assert [item["label"] for item in origin] == [sc.LABEL_ORIGIN_MISSING_FIELD,
                                                  sc.LABEL_ORIGIN_DUPLICATE]
    assert result["statistics"]["duplicates"] == 1
    assert all(item["record"] for item in origin)


def test_non_utf8_input_raises_input_error(tmp_path):
    entry = tmp_path / "in" / "gbk.jsonl"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes("中文字段\n".encode("gbk"))
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(entry), unique_id="task_gbk",
                      output_root=tmp_path / "out", allow_external_input=True)


def test_missing_input_file_raises_input_error(tmp_path):
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(tmp_path / "nope.jsonl"), unique_id="task_x",
                      output_root=tmp_path / "out", allow_external_input=True)


@pytest.mark.parametrize("override", [
    {"sample_rate": 0.05},
    {"sample_rate": 0.30},
    {"enabled": False},
    {"strata": ["scene", "modality"]},
    {"include_auto_aligned": False},
    {"report_source_names": ["../20_quality_report.json"]},
    {"unknown_key": 1},
])
def test_invalid_configuration_is_rejected(override):
    with pytest.raises(ConfigError):
        inference.run(input_file="x.jsonl", unique_id="task_cfg",
                      output_root="out_cfg", overrides=override)


def test_third_party_scale_override_is_accepted():
    from utils import schemas

    config = schemas.load_config(None, {"sample_rate": 0.15, "require_reviewer_identity": False})
    assert config.sample_rate == pytest.approx(0.15)
    assert config.require_reviewer_identity is False


# --------------------------------------------------------------------------- #
# 任务隔离与路径安全
# --------------------------------------------------------------------------- #

def test_two_unique_ids_do_not_pollute_each_other(tmp_path):
    first = run_op(tmp_path, uniform_rows("pass"), unique_id="task_a", sub="in_a")
    second = run_op(tmp_path, uniform_rows("pass", count=6), unique_id="task_b", sub="in_b")
    assert Path(first["runDir"]).is_relative_to(Path(tmp_path / "out"))
    assert "task_a" in first["runDir"] and "task_b" in second["runDir"]
    assert first["statistics"]["output"] == 10
    assert second["statistics"]["output"] == 6
    assert second["uniqueId"] == "task_b"


def test_same_unique_id_rerun_keeps_previous_run(tmp_path):
    first = run_op(tmp_path, uniform_rows("pass"), unique_id="task_r", sub="in_a")
    second = run_op(tmp_path, uniform_rows("pass"), unique_id="task_r", sub="in_b")
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["runDir"]).is_dir()


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "C:", "CON", "x "])
def test_invalid_unique_id_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        inference.run(input_file=str(tmp_path / "x.jsonl"), unique_id=bad,
                      output_root=tmp_path / "out", allow_external_input=True)


def test_input_file_outside_task_is_rejected(tmp_path):
    entry = build_input_dir(tmp_path / "in", uniform_rows("pass"))
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(entry), unique_id="task_safe",
                      output_root=tmp_path / "out")


def test_task_first_stage_reads_input_dir_and_chain_uses_previous_output(tmp_path):
    root = tmp_path / "out"
    task = io_utils.task_dir(root, "task_chain")
    input_dir = task / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(input_dir / "24_candidate_pool.jsonl", uniform_rows("pass"))
    from tests.conftest import write_quality_report

    write_quality_report(input_dir / "20_quality_report.json")

    first = inference.run(input_file=None, unique_id="task_chain", output_root=root)
    assert first["status"] in ("success", "partial")
    chained = inference.run(input_file=first["outputFile"], unique_id="task_chain",
                            output_root=root)
    assert chained["runId"] == "run002"
    assert chained["statistics"]["output"] == 10


# --------------------------------------------------------------------------- #
# 阶段形态
# --------------------------------------------------------------------------- #

def test_stage_file_object_is_unified_shape(tmp_path):
    result = run_op(tmp_path, uniform_rows("pass"))
    assert not hasattr(inference, "build_intermediate_lineage")
    assert not hasattr(inference, "build_stage_payload")

    stage_object = inference.stage_file_object(result)
    assert stage_object["format"] == "crossmodal-files-v1"
    assert stage_object["entrypoint"] == sc.MAIN_NAME
    names = [item["path"] for item in stage_object["files"]]
    assert sc.MAIN_NAME in names
    assert sc.REPORT_NAME in names
    assert sc.SAMPLED_NAME in names
    assert "manifest.json" in names
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# 上游报告与降级
# --------------------------------------------------------------------------- #

def test_missing_source_report_degrades_but_still_writes_its_own_report(tmp_path):
    result = run_op(tmp_path, uniform_rows("pass"), report=False,overrides={"allow_degraded":True})
    assert result["status"] == "degraded"
    assert result["statistics"]["writeback"]["source_report_available"] is False
    report = json.loads((Path(result["runDir"]) / sc.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["source_report"]["available"] is False
    assert report["status"] == sc.REPORT_STATUS_WRITTEN


def test_missing_source_report_fails_when_degradation_disabled(tmp_path):
    with pytest.raises(inference.DependencyError):
        run_op(tmp_path, uniform_rows("pass"), report=False,
               overrides={"allow_degraded": False})


# --------------------------------------------------------------------------- #
# 抽检判定分支（整批同结论 → 抽样命中不影响分支覆盖）
# --------------------------------------------------------------------------- #

def test_all_pass_seals_writeback(tmp_path):
    result = run_op(tmp_path, uniform_rows("pass"))
    stats = result["statistics"]
    assert stats["sampled_passed"] == 1
    assert stats["writeback"]["completed"] is True
    assert stats["review_required_records"] == 0
    assert stats["human_verified_records"] == 1
    report = json.loads((Path(result["runDir"]) / sc.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["writeback"]["version_state"] == sc.VERSION_STATE_DELIVERABLE
    main = read_jsonl(Path(result["runDir"]) / sc.MAIN_NAME)
    sampled = [item for item in main if item["processing"]["24"]["sampled"]]
    assert len(sampled) == 1
    assert sampled[0]["processing"]["24"]["human_verified"] is True


def test_all_pending_keeps_report_waiting_writeback(tmp_path):
    result = run_op(tmp_path, uniform_rows(None))
    stats = result["statistics"]
    assert stats["sampled_pending"] == 1
    assert stats["pending_manual"] == 1
    assert stats["writeback"]["completed"] is False
    assert result["status"] == "waiting_manual"
    assert result["reportStatus"] == sc.REPORT_STATUS_WAITING
    report = json.loads((Path(result["runDir"]) / sc.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["writeback"]["version_state"] == sc.VERSION_STATE_PENDING
    assert report["writeback"]["gate"]["all_sampled_concluded"] is False
    pending = read_jsonl(Path(result["runDir"]) / sc.PENDING_NAME)
    assert [item["reason_code"] for item in pending] == ["no_human_conclusion"]


def test_all_rejected_are_quarantined_and_auto_alignment_cancelled(tmp_path):
    result = run_op(tmp_path, uniform_rows("not_aligned"))
    stats = result["statistics"]
    assert stats["sampled_rejected"] == 1
    assert stats["sample_quarantined"] == 1
    assert stats["auto_alignment_cancelled"] == 1
    assert stats["output"] == 9
    assert stats["disputes"] == 1
    quarantine = read_jsonl(Path(result["runDir"]) / sc.SAMPLE_QUARANTINE_NAME)
    assert quarantine[0]["label"] == sc.LABEL_SAMPLE_SEMANTIC
    assert quarantine[0]["destination"] == sc.DESTINATION_SAMPLE
    assert quarantine[0]["record"]["alignment"]["cancelled_by"] == "operator_24"
    main_ids = {item["sample_id"] for item in
                read_jsonl(Path(result["runDir"]) / sc.MAIN_NAME)}
    assert quarantine[0]["sample_id"] not in main_ids
    report = json.loads((Path(result["runDir"]) / sc.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["writeback"]["cancelled_auto_alignment"] == [quarantine[0]["sample_id"]]


def test_all_uncertain_stays_in_pipeline_and_opens_dispute(tmp_path):
    result = run_op(tmp_path, uniform_rows("uncertain"))
    stats = result["statistics"]
    assert stats["sampled_uncertain"] == 1
    assert stats["sample_quarantined"] == 0
    assert stats["disputes"] == 1
    assert stats["writeback"]["completed"] is False
    assert result["status"] == "waiting_manual"
    disputes = read_jsonl(Path(result["runDir"]) / sc.DISPUTES_NAME)
    assert disputes[0]["handover"] == ["operator_21", "operator_25"]
    assert disputes[0]["label"] == sc.LABEL_UNCERTAIN


def test_invalid_review_evidence_is_counted_as_pending(tmp_path):
    result = run_op(tmp_path, uniform_rows("pass", reviewer=None))
    stats = result["statistics"]
    assert stats["sampled_pending"] == 1
    assert stats["human_verified_records"] == 0
    assert stats["writeback"]["completed"] is False
    pending = read_jsonl(Path(result["runDir"]) / sc.PENDING_NAME)
    assert pending[0]["reason_code"] == "review_invalid:missing_reviewer"


def test_upstream_isolated_records_are_not_in_sampling_frame(tmp_path):
    rows = uniform_rows("pass", count=4)
    rows.append(make_record("iso-1", verdict="pass",
                            extra={"processing": {"21": {"isolated": True,
                                                         "decision": "isolated"}}}))
    result = run_op(tmp_path, rows)
    stats = result["statistics"]
    assert stats["not_applicable_records"] == 1
    assert stats["output"] == 5
    main = read_jsonl(Path(result["runDir"]) / sc.MAIN_NAME)
    isolated = [item for item in main if item["sample_id"] == "iso-1"][0]
    assert isolated["processing"]["24"]["sampled"] is False
    assert isolated["processing"]["24"]["reason_code"] == "upstream_isolated"


def test_disabled_auto_aligned_records_are_still_sampled_by_default(tmp_path):
    """表1 明确「自动对齐也进入抽样框」；默认配置下自动对齐样本参与抽样。"""
    rows = [make_record("auto-%02d" % index, alignment="auto_aligned", verdict="pass")
            for index in range(10)]
    result = run_op(tmp_path, rows)
    sampled = [item for item in read_jsonl(Path(result["runDir"]) / sc.MAIN_NAME)
               if item["processing"]["24"]["sampled"]]
    assert len(sampled) == 1
    assert sampled[0]["alignment"]["status"] == strata_mod.ALIGNMENT_AUTO


def test_sampling_matches_stratified_library_selection(tmp_path):
    rows = []
    rows += [make_record("st-a-%02d" % index, verdict="pass") for index in range(20)]
    rows += [make_record("st-b-%02d" % index, modalities=("text", "audio"),
                         alignment="manual_aligned", verdict="pass") for index in range(12)]
    rows += [make_record("st-c-%02d" % index, scene="scene-sd",
                         modalities=("text", "video"), verdict="pass") for index in range(8)]
    result = run_op(tmp_path, rows, overrides={"sample_rate": 0.20})
    stats = result["statistics"]
    assert stats["sampled"] == 8
    main = read_jsonl(Path(result["runDir"]) / sc.MAIN_NAME)
    sampled = {item["sample_id"] for item in main
               if item["processing"]["24"]["sampled"]}
    assert len(sampled) == 8
    assert stats["achieved_sample_rate"] == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_success_and_failure_exit_codes(tmp_path, capsys):
    entry = build_input_dir(tmp_path / "in", uniform_rows("pass"))
    code = inference.main(["--input", str(entry), "--unique-id", "cli_ok",
                           "--output-root", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["code"] == 0
    assert payload["data"]["statistics"]["output"] == 10

    bad = inference.main(["--input", str(entry), "--unique-id", "../escape",
                          "--output-root", str(tmp_path / "out")])
    assert bad == 2
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])["code"] == 1

    cfg = inference.main(["--input", str(entry), "--unique-id", "cli_cfg",
                          "--output-root", str(tmp_path / "out"),
                          "--override", "sample_rate=0.5"])
    assert cfg == 2


def test_cli_is_last_operator_prints_file_object(tmp_path, capsys):
    entry = build_input_dir(tmp_path / "in", uniform_rows("pass"))
    code = inference.main(["--input", str(entry), "--unique-id", "cli_last",
                           "--output-root", str(tmp_path / "out"), "--is-last-operator"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # [v4.2] CLI 两种形态都不再回传 dataShape / lineageFile
    assert "dataShape" not in payload["data"]
    assert "lineageFile" not in payload["data"]
    assert payload["data"]["outputFile"]["format"] == "crossmodal-files-v1"


def test_cleanup_leaves_no_smoke_artifacts(tmp_path):
    result = run_op(tmp_path, uniform_rows("pass"))
    run_dir = Path(result["runDir"])
    leftovers = [item.name for item in run_dir.iterdir()
                 if item.suffix in (".partial", ".final", ".tmp")]
    assert leftovers == []
    shutil.rmtree(tmp_path / "out", ignore_errors=True)
