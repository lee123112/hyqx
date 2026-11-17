"""主流程测试：统计守恒、血缘、媒体搬运、失败分支与 CLI。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import inference
from utils.io import TaskLock

from conftest import read_jsonl

ENTRY = "demo.jsonl"


@pytest.fixture()
def done(output_root, stage):
    """在固定 uniqueId 上跑一次完整流程。"""
    task = stage(output_root, "demoS2_f1", ENTRY)
    result = inference.run(str(task / "input" / ENTRY), "demoS2_f1", None, str(output_root),{"enabled":True})
    return task, result


def test_statistics_are_conserved_and_real(done):
    _task, result = done
    stats = result["statistics"]
    assert result["status"] == "partial"
    assert stats["total_lines"] == 13
    assert stats["valid_records"] == 12
    assert stats["bad_lines"] == 1
    assert stats["output"] == 7
    assert stats["rejected"] == 5
    assert stats["not_applicable"] == 2
    assert stats["conservation_ok"] is True
    assert stats["tokenizer"] == "not_performed"
    assert stats["degraded"] == 0
    # 真实计数：fixture 内含 1 个控制字符、1 个零宽字符、1 个 U+FFFD、1 段 script
    assert stats["control_chars_removed"] == 1
    assert stats["invisible_chars_removed"] == 0
    assert stats["replacement_chars"] == 1
    assert stats["script_style_removed"] == 0
    assert stats["media_copied"] == 3
    assert stats["duplicates"] == 1
    assert stats.get("reason:empty_text_after_cleaning",0) == 0
    assert stats["reason:media_missing:image"] == 2
    assert stats["reason:duplicate_sample_id"] == 1
    assert stats["reason:invalid_json"] == 1
    assert stats["reason:invalid_type:text"] == 1
    assert stats["reason:missing_field:sample_id"] == 1


def test_run_directory_is_complete_and_carryable(done):
    _task, result = done
    run_dir = Path(result["runDir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == "03_text_cleaned.jsonl"
    listed = {item["path"] for item in manifest["artifacts"]}
    assert {"03_text_cleaned.jsonl", "03_rejects.jsonl"} <= listed
    assert {"media/img_001.png", "media/vid_001.mp4", "media/aud_001.wav"} <= listed
    assert "manifest.json" not in listed  # manifest 自身无需列为 artifact
    for relative in listed:
        assert (run_dir / relative).is_file(), relative
    # [v4.2] 血缘/阶段归档已停用：新运行不再生成 lineage.json / stage_context.json
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    assert (run_dir / "run.log").is_file()


@pytest.mark.skip(reason="v4.2 血缘/阶段归档已停用：stage_context.json 不再生成")
def test_stage_lineage_and_prior_lineage(done):
    _task, result = done
    context = json.loads(
        (Path(result["runDir"]) / "stage_context.json").read_text(encoding="utf-8"))
    assert context["stageId"] == "S2"
    assert context["entrypoint"] == "03_text_cleaned.jsonl"
    assert len(context["stage_lineage"]) == 1
    record = context["stage_lineage"][0]
    for field in ("operator", "operator_version", "uniqueId", "inputFile", "status"):
        assert record.get(field), field
    assert record["operator"] == "text_normalization"
    assert record["status"] == "partial"
    assert record["config"]["business"]["enabled"] is True
    # 上游 S1 血缘原样保留
    assert context["prior_lineage"][0]["stageId"] == "S1"
    assert context["prior_lineage"][0]["operator"] == "data_screening"


@pytest.mark.skip(reason="v4.2 血缘/阶段归档已停用：run() 不再返回 stageLineage")
def test_stage_lineage_view_strips_temporary_output_paths(done):
    _task, result = done
    from utils.lineage import to_stage_lineage

    stripped = to_stage_lineage(result["stageLineage"])
    assert "outputFile" not in stripped[0]
    assert stripped[0]["artifacts"][0]["path"] == "03_text_cleaned.jsonl"
    # 本地快照仍保留临时输出路径，便于阶段内传递与排查
    assert result["stageLineage"][0]["outputFile"].endswith("03_text_cleaned.jsonl")


def test_text_object_shape_and_metrics(done):
    _task,result=done
    records={r['sample_id']:r for r in read_jsonl(Path(result['outputFile']))}
    image=records['demo_03_img_001'];text=image['text']
    assert text['content']=='<b>红色</b>方块&nbsp; 在桌面上 '
    assert text['original']==text['content']
    assert text['lang']=='zh'
    assert not {'tokens','token_offsets','tokenizer'} & text.keys()
    # [v4.2] status 双字段：int status + process_status
    assert image['processing']['03']['status'] == 1
    assert image['processing']['03']['process_status'] == 'success'
    assert image['processing']['02']['status']=='success'
    assert image['metadata']['license']=='CC0'
    assert image['image']=='media/img_001.png'


def test_not_applicable_keeps_record_without_text(done):
    _task, result = done
    records = {item["sample_id"]: item for item in read_jsonl(Path(result["outputFile"]))}
    assert records["demo_03_av_001"]["text"] is None
    # [v4.2] status 双字段：int status + process_status
    assert records["demo_03_av_001"]["processing"]["03"]["status"] == 1
    assert records["demo_03_av_001"]["processing"]["03"]["process_status"] == "not_applicable"
    assert records["demo_03_av_001"]["processing"]["03"]["reason"] == "video_audio_pair"
    assert records["demo_03_av_002"]["processing"]["03"]["status"] == 1
    assert records["demo_03_av_002"]["processing"]["03"]["process_status"] == "not_applicable"
    assert records["demo_03_av_002"]["processing"]["03"]["reason"] == "missing_description"


def test_original_preserved_on_rerun(done):
    _task, result = done
    records = {item["sample_id"]: item for item in read_jsonl(Path(result["outputFile"]))}
    obj = records["demo_03_obj_001"]["text"]
    assert obj["content"] == "已有内容"
    assert obj["original"] == "已有 原始内容"


def test_rejects_carry_reason_and_artifact(done):
    _task, result = done
    rejects = read_jsonl(Path(result["rejectsFile"]))
    by_reason = {}
    for entry in rejects:
        by_reason.setdefault(entry["reason"], []).append(entry)
    assert by_reason["invalid_json"][0]["line"] == 13
    assert by_reason["duplicate_sample_id"][0]["evidence"]["first_seen_line"] == 3
    missing_media = by_reason["media_missing:image"][0]
    assert missing_media["record"]["image"] is None  # 不留指向不存在文件的活跃引用
    assert missing_media["line"] > 0
    assert by_reason["invalid_type:text"][0]["sample_id"] == "demo_03_badtype_001"
    # <p>空格</p>仍是已有文字；表1不允许HTML清理将其变成空文本。
    assert "empty_text_after_cleaning" not in by_reason


def test_empty_input_is_explicit_failure(output_root, stage):
    task = stage(output_root, "empty_case", "input_empty.jsonl", with_manifest=False)
    result = inference.run(str(task / "input" / "input_empty.jsonl"), "empty_case", None,
                           str(output_root))
    assert result["status"] == "failed"
    assert result["outputFile"] is None
    assert "empty_input" in result["message"]
    assert result["statistics"]["total_lines"] == 0
    # [v4.2] 血缘输出已停用：run() 返回不再携带 lineageFile 键
    assert "lineageFile" not in result


def test_invalid_encoding_is_explicit_failure(output_root, stage):
    task = stage(output_root, "bad_encoding", "input_bad_encoding.jsonl", with_manifest=False)
    result = inference.run(str(task / "input" / "input_bad_encoding.jsonl"), "bad_encoding",
                           None, str(output_root))
    assert result["status"] == "failed"
    assert result["statistics"]["error"] == "invalid_encoding"
    assert result["outputFile"] is None


def test_missing_input_is_explicit_failure(output_root):
    result = inference.run(str(output_root / "none.jsonl"), "missing_case", None,
                           str(output_root))
    assert result["status"] == "failed"
    assert result["statistics"]["error"] == "input_not_found"


def test_invalid_unique_id_rejected(output_root, stage):
    task = stage(output_root, "ok_case", ENTRY)
    for bad in ("..", "a/b", "a\\b", "CON", "c:", "."):
        result = inference.run(str(task / "input" / ENTRY), bad, None, str(output_root))
        assert result["status"] == "failed", bad
        assert result["statistics"]["error"] == "invalid_unique_id", bad


def test_foreign_process_lock_yields_task_busy(output_root, stage):
    task = stage(output_root, "locked_case", ENTRY)
    lock_dir = Path(output_root) / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "locked_case.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        result = inference.run(str(task / "input" / ENTRY), "locked_case", None,
                               str(output_root))
        assert result["status"] == "failed"
        assert result["statistics"]["error"] == "task_busy"
    finally:
        os.close(fd)
        lock_path.unlink()


def test_same_process_reentrant_lock_allows_http_wrapping(output_root, stage):
    """HTTP 层先锁整请求、run() 再锁一次，同进程内必须可重入。"""
    task = stage(output_root, "reentrant_case", ENTRY)
    with TaskLock(output_root, "reentrant_case"):
        result = inference.run(str(task / "input" / ENTRY), "reentrant_case", None,
                               str(output_root))
    assert result["status"] == "partial"


def test_runs_are_isolated_and_prior_history_kept(output_root, stage):
    """同一 uniqueId 重复运行：新 run 目录不复用上游 run 产物，历史阶段血缘保留。"""
    task = stage(output_root, "multi_run", ENTRY)
    first = inference.run(str(task / "input" / ENTRY), "multi_run", None, str(output_root))
    second = inference.run(str(task / "input" / ENTRY), "multi_run", None, str(output_root))
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()
    assert first["outputFile"] != second["outputFile"]
    assert first["runDir"] != second["runDir"]
    # [v4.2] 血缘/阶段归档已停用：重复运行不再生成血缘快照与阶段上下文
    assert "lineageFile" not in first
    assert not (Path(second["runDir"]) / "stage_context.json").exists()


def test_business_overrides_and_deployment_protection(output_root, stage):
    task = stage(output_root, "override_case", ENTRY)
    overrides = {
        "enabled": True,
        "operator": {"name": "hijacked", "version": "v9"},
        "stage_id": "S9",
        "server": {"max_response_bytes": 1},
    }
    result = inference.run(str(task / "input" / ENTRY), "override_case", None,
                           str(output_root), overrides)
    assert result["status"] == "partial"
    # [v4.2] 血缘停用后改从 load_config 快照直接校验部署身份不可被请求改写
    cfg, snapshot = inference.load_config(None, overrides)
    assert cfg.operator.name == "text_normalization"
    assert cfg.stage_id == "S2"
    assert snapshot["deployment"]["server"]["max_response_bytes"] == 67108864
    assert snapshot["business"]["enabled"] is True
    assert sorted(snapshot["ignored_deployment_keys"]) == ["operator", "server", "stage_id"]


def test_invalid_config_returns_failed_without_success_artifact(output_root, stage):
    task = stage(output_root, "bad_config", ENTRY)
    result = inference.run(str(task / "input" / ENTRY), "bad_config", None, str(output_root),
                           {"ad_patterns": ["([未闭合"]})
    assert result["status"] == "failed"
    assert result["outputFile"] is None
    # [v4.2] 血缘输出已停用：run() 返回不再携带 lineageFile 键
    assert "lineageFile" not in result
    assert result["statistics"]["config_error"] == "ValueError"


def test_unknown_config_key_rejected(output_root, stage):
    task = stage(output_root, "unknown_key", ENTRY)
    result = inference.run(str(task / "input" / ENTRY), "unknown_key", None, str(output_root),
                           {"tokenize_mode": "hmm"})
    assert result["status"] == "failed"
    assert result["statistics"]["config_error"] == "ValueError"


def test_cli_prints_json_summary(output_root, stage, capsys):
    task = stage(output_root, "cli_case", ENTRY)
    code = inference.main(["--input", str(task / "input" / ENTRY),
                           "--unique-id", "cli_case",
                           "--output-dir", str(output_root),
                           "--override", "enabled=true"])
    assert code == 0
    captured = capsys.readouterr().out
    payload = json.loads(captured[captured.index("{"):])
    assert payload["status"] == "partial"
    assert payload["statistics"]["tokenizer"] == "not_performed"
    assert Path(payload["outputFile"]).is_file()


def test_media_files_are_byte_identical_after_carry(done, sample_dir):
    _task, result = done
    run_dir = Path(result["runDir"])
    for relative in ("media/img_001.png", "media/vid_001.mp4", "media/aud_001.wav"):
        assert (run_dir / relative).read_bytes() == (sample_dir / relative).read_bytes()
