"""算子主编排测试：真实样例端到端、任务隔离、路径安全、失败路径与 CLImain。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference


@pytest.fixture(autouse=True)
def _force_no_model_weights(tmp_path, monkeypatch):
    """本用例集按「无权重环境」编写（显式降级/显式失败）；把 MODEL_ROOT 指向空目录，
    使降级断言在已挂载权重的机器上同样可复现。业务断言不变。"""
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "_no_model_weights"))

from utils import alignment as al
from utils import lineage as op_lineage

SAMPLE = Path(inference.BASE) / "dataset" / "sample"
ENTRY = SAMPLE / "demo.jsonl"


def _run(tmp_path, unique_id="task_a", **kwargs):
    return inference.run(str(ENTRY), unique_id, output_root=str(tmp_path), **kwargs)


def _lines(path) -> list:
    return [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


def _package_weights_present() -> bool:
    """交付包内是否随附 CLIP 权重（v1.0 起权重按算子内置于 ``<算子目录>/weight/``）。"""
    roots = [Path(inference.BASE) / "weight", Path(inference.BASE).parent / "weight"]
    return any(root.is_dir() and any(root.rglob("ViT-B-32.pt")) for root in roots)


# ------------------------------------------------------------------ 端到端真值

def test_sample_run_matches_expected_true_counts(tmp_path):
    result = _run(tmp_path)
    assert result["status"] == "degraded"     # 样例含人工改描述，本机无权重 → 显式延后
    assert result["operator"] == "16_whole_alignment"
    assert result["stageId"] == "S3"
    stats = result["statistics"]

    # Audio/text now waits for real #13 evidence instead of becoming no_image.
    assert stats["input"] == 19
    assert stats["bad_lines"] == 1
    assert stats["structural_rejects"] == 2
    assert stats["valid"] == 16
    assert stats["applicable"] == 16
    assert stats["not_applicable"] == 0

    assert stats["auto_aligned"] == 2
    assert stats["manual_aligned"] == 1
    assert stats["pending_manual_review"] == 7
    assert stats["uncertain"] == 1
    assert stats["not_aligned"] == 5

    assert stats["output"] == 11
    assert stats["review"] == 8
    assert stats["isolated"] == 5
    assert stats["rejected"] == 3
    assert stats["conservation_ok"] is True, stats["conservation_failures"]

    assert stats["rescored"] == 0
    assert stats["rescore_requested"] == 1
    assert stats["rescore_deferred"] == 1
    assert stats["degraded"] is True
    assert stats["models_available"] is False
    assert stats["veto_locked"] == 2      # #12 人工否决 + 输入内已有锁定各一条

    assert stats["sampling_hits"] == 1
    assert stats["media_carried"] == 2
    assert stats["thresholds"] == {"isolation": 0.20, "auto_align": 0.32}
    assert stats["route_enabled"] is True

    # 降级原因：无权重环境固定为 model_weights_not_configured。交付包已随附 weight/，
    # 带权重机器上 MODEL_ROOT 被指向空目录后原因变为 weights_not_found: ViT-B-32.pt，
    # 故该字符串只在无权重环境校验；降级行为本身已由上面的 statistics 逐条覆盖。
    if not _package_weights_present():
        assert stats["degradation_reason"] == "model_weights_not_configured"


def test_rescore_is_logged_exactly_once_after_description_rewrite(tmp_path,
                                                                 stub_encoder):
    """表1 #16 算法 4) 完成判据：人改描述后日志中有**一次**重新评分。"""
    result = _run(tmp_path, encoder=stub_encoder(0.36))
    log = (Path(result["runDir"]) / "run.log").read_text(encoding="utf-8")
    lines = [line for line in log.splitlines() if "[rescore]" in line]
    assert len(lines) == 1, log
    assert "pair-t14--i14" in lines[0]
    assert "score_after=0.36" in lines[0]
    summary = [line for line in log.splitlines() if "[rescore_summary]" in line]
    assert len(summary) == 1
    assert result["statistics"]["rescored"] == 1


def test_sample_artifacts_and_manifest_are_consistent(tmp_path):
    result = _run(tmp_path)
    run_dir = Path(result["runDir"])
    # [v4.2] 血缘输出已停用：不再产出 lineage.json / stage_lineage.json
    for name in ("manifest.json", "config.snapshot.json", "run.log", "media/cat.png"):
        assert (run_dir / name).is_file(), name
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_lineage.json").exists()
    assert "lineage" not in result and "stageLineage" not in result

    assert len(_lines(result["outputFile"])) == result["statistics"]["output"]
    assert len(_lines(result["reviewFile"])) == result["statistics"]["review"]
    assert len(_lines(result["isolationFile"])) == result["statistics"]["isolated"]
    assert len(_lines(result["notApplicableFile"])) == result["statistics"]["not_applicable"]
    assert len(_lines(result["rejectsFile"])) == result["statistics"]["rejected"]

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["entrypoint"] == "16_whole_alignment.jsonl"
    assert manifest["media"]["carried"] == ["media/cat.png", "media/street.png"]
    assert manifest["media"]["missing"] == ["media/missing.png", "media/voice.wav"]
    assert manifest["humanEventsFile"] == "16_human_events.jsonl"
    assert manifest["counts"]["16_whole_alignment.jsonl"] == 11
    assert manifest["statistics"]["conservation_ok"] is True


def test_output_is_utf8_and_keeps_upstream_fields(tmp_path):
    result = _run(tmp_path)
    records = [json.loads(line) for line in _lines(result["outputFile"])]
    first = records[0]
    assert "样例描述" in first["left"]["record"]["text"]["content"]
    assert first["alignment"]["whole"]["decision"] == "auto_aligned"
    assert first["processing"]["16"]["operator"] == "16_whole_alignment"
    assert (Path(result["outputFile"]).read_bytes()[:1] != b"")
    assert Path(result["outputFile"]).read_bytes().decode("utf-8")


def test_logs_never_contain_raw_descriptions(tmp_path):
    result = _run(tmp_path)
    log = (Path(result["runDir"]) / "run.log").read_text(encoding="utf-8")
    assert "[start]" in log and "[finish]" in log
    assert "样例描述" not in log
    assert "输入=19" in log


def test_degraded_records_never_carry_a_fake_rescored_score(tmp_path):
    result = _run(tmp_path)
    allowed = ("rescore", "image_text_score", "none", "alignment.whole",
               "processing.12", "alignment.image_text")
    decisions = set()
    for line in _lines(result["outputFile"]):
        whole = json.loads(line)["alignment"]["whole"]
        decisions.add(whole["decision"])
        assert whole["score_source"] in allowed
        if whole["rescored"]:
            assert whole["score_source"] == "rescore"
        if whole.get("description_edit") and not whole["rescored"]:
            # 重评分延后：不得写入任何新分数，只标真实失败状态（[v4.2] 字符串态在 process_status）
            assert whole["description_edit"]["score_after"] is None
            assert whole["description_edit"]["process_status"] != "ok"
            assert whole["description_edit"]["status"] == 0
    assert al.DECISION_PENDING_MANUAL_REVIEW in decisions
    assert al.DECISION_AUTO_ALIGNED in decisions


# ------------------------------------------------------------------ 隔离与安全

def test_two_unique_ids_do_not_share_directories(tmp_path):
    first = _run(tmp_path, "task_a")
    second = _run(tmp_path, "task_b")
    assert first["runDir"] != second["runDir"]
    assert "task_a" in first["runDir"] and "task_b" in second["runDir"]
    assert first["outputFile"] != second["outputFile"]
    # [v4.2] 血缘输出已停用：任务目录不再写 lineage.jsonl 历史
    assert not (tmp_path / "task_a" / "lineage.jsonl").exists()
    assert not (tmp_path / "task_b" / "lineage.jsonl").exists()


def test_rerun_creates_new_run_directory(tmp_path):
    first = _run(tmp_path, "task_a")
    second = _run(tmp_path, "task_a")
    assert Path(first["runDir"]).name == "run001"
    assert Path(second["runDir"]).name == "run002"


def test_explicit_run_id_reuses_same_run_directory(tmp_path):
    """显式 runId 允许覆盖同一运行目录（幂等重跑）。"""
    first = _run(tmp_path, "task_a", run_id="runX")
    assert Path(first["runDir"]).name == "runX"
    second = _run(tmp_path, "task_a", run_id="runX")
    assert second["runDir"] == first["runDir"]
    assert second["status"] == "degraded"


def test_invalid_explicit_run_id_fails(tmp_path):
    result = _run(tmp_path, "task_a", run_id="bad-name")
    assert result["status"] == "failed" and result["reason"] == "invalid_run_id"


@pytest.mark.parametrize("bad", ["", "..", "../task_b", "a/b", "C:", "con"])
def test_invalid_unique_id_fails(tmp_path, bad):
    result = inference.run(str(ENTRY), bad, output_root=str(tmp_path))
    assert result["status"] == "failed"
    assert result["reason"].startswith("invalid_unique_id")


def test_missing_input_file_fails(tmp_path):
    result = inference.run(str(tmp_path / "nope.jsonl"), "task_a",
                           output_root=str(tmp_path))
    assert result["status"] == "failed" and result["reason"] == "input_not_found"


def test_missing_config_file_fails(tmp_path):
    result = inference.run(str(ENTRY), "task_a", config_path=str(tmp_path / "no.yaml"),
                           output_root=str(tmp_path))
    assert result["status"] == "failed"
    assert result["reason"].startswith("config_error")


def test_unparsable_config_fails(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("whole_alignment: [1, 2\n", encoding="utf-8")
    result = inference.run(str(ENTRY), "task_a", config_path=str(broken),
                           output_root=str(tmp_path))
    assert result["status"] == "failed"
    assert result["reason"].startswith("config_error")


def test_explicit_missing_human_events_fails(tmp_path):
    result = inference.run(str(ENTRY), "task_a", output_root=str(tmp_path),
                           human_events_path=str(tmp_path / "none.jsonl"))
    assert result["status"] == "failed"
    assert result["reason"] == "human_events_not_found"


def test_disallowed_degradation_fails_the_task(tmp_path):
    source = (Path(inference.BASE) / "configs" / "config.yaml").read_text(encoding="utf-8")
    strict = tmp_path / "strict.yaml"
    strict.write_text(source.replace("allow_degraded: true", "allow_degraded: false"),
                      encoding="utf-8")
    result = inference.run(str(ENTRY), "task_a", config_path=str(strict),
                           output_root=str(tmp_path))
    assert result["status"] == "failed"
    assert result["reason"].startswith("rescore_requires_model")


# ------------------------------------------------------------------ 边界输入

def test_empty_input_is_a_successful_empty_run(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    result = inference.run(str(empty), "task_a", output_root=str(tmp_path))
    assert result["status"] == "success"
    stats = result["statistics"]
    assert stats["input"] == 0 and stats["empty_input"] is True
    assert stats["conservation_ok"] is True
    assert _lines(result["outputFile"]) == []


def test_input_of_only_blank_lines(tmp_path):
    blanks = tmp_path / "blanks.jsonl"
    blanks.write_text("\n\n\n", encoding="utf-8")
    result = inference.run(str(blanks), "task_a", output_root=str(tmp_path))
    stats = result["statistics"]
    assert stats["input"] == 0 and stats["blank_lines"] == 3
    assert stats["conservation_ok"] is True


def test_threshold_overrides_change_buckets(tmp_path):
    result = _run(tmp_path, "task_a", overrides={"auto_align_threshold": 0.50,
                                                 "isolation_threshold": 0.40})
    stats = result["statistics"]
    # 0.41/0.55 之类不再自动对齐；阈值必须如实回写到统计
    assert stats["thresholds"] == {"isolation": 0.40, "auto_align": 0.50}
    assert stats["conservation_ok"] is True
    assert result["config"]["whole_alignment"]["auto_align_threshold"] == 0.50


def test_request_override_cannot_touch_model(tmp_path):
    result = _run(tmp_path, "task_a", overrides={"weights": "/tmp/other.bin"})
    assert result["status"] == "failed"
    assert result["reason"].startswith("config_error")


# ------------------------------------------------------------------ CLI

def _cli_report(capsys) -> dict:
    """CLI 的规范日志会先写 stdout，报告 JSON 从第一个 "{" 开始。"""
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])


def test_cli_main_reports_middle_shape(tmp_path, capsys):
    code = inference.main(["--input", str(ENTRY), "--unique-id", "cli_a",
                           "--output-root", str(tmp_path)])
    assert code == 0
    report = _cli_report(capsys)
    assert report["status"] == "degraded"
    assert report["dataShape"] == {"outputFile": "file-object"}   # [v4.2] 统一 outputFile
    assert report["statistics"]["conservation_ok"] is True
    assert report["thresholds"] == {"isolation": 0.20, "auto_align": 0.32}


def test_cli_main_reports_stage_end_shape(tmp_path, capsys):
    code = inference.main(["--input", str(ENTRY), "--unique-id", "cli_b",
                           "--output-root", str(tmp_path), "--is-last-operator"])
    assert code == 0
    report = _cli_report(capsys)
    assert report["dataShape"] == {"outputFile": "file-object"}   # [v4.2] 去 lineageFile
    assert report["entrypoint"] == "16_whole_alignment.jsonl"


def test_cli_main_returns_nonzero_on_failure(tmp_path, capsys):
    code = inference.main(["--input", str(tmp_path / "missing.jsonl"),
                           "--unique-id", "cli_c", "--output-root", str(tmp_path)])
    assert code == 1
    report = _cli_report(capsys)
    assert report["status"] == "failed"
