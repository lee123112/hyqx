"""编排主流程测试：输入校验、流式处理、四路产物、统计、血缘与任务隔离。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference
from tests.conftest import (DEFAULT_BATCH, ENTRY_NAME, SOURCE_AUDIO_LABEL,
                            build_input_dir, make_record, read_jsonl, write_jsonl,
                            write_media)
from utils import intake as it
from utils import io as io_utils
from utils import schemas


def run_rows(tmp_path, rows, *, unique_id="op_task", config_path=None, overrides=None,
             media=None):
    entry = build_input_dir(tmp_path, rows, unique_id=unique_id, media=media)
    result = inference.run(input_file=str(entry), unique_id=unique_id,
                           output_root=str(tmp_path), config_path=config_path,
                           overrides=overrides)
    return result


def out(result, name):
    return Path(result["runDir"]) / name


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def test_run_registers_pass_objects_and_conserves(tmp_path, mixed_rows):
    result = run_rows(tmp_path, mixed_rows, media={"media/m-pass-local-01.png": "png"})
    stats = result["statistics"]
    assert result["status"] == "partial"
    assert stats["input_lines"] == 12
    assert stats["input"] == 12
    assert stats["valid"] == 10
    assert stats["rejected"] == 2
    assert stats["objects"] == 10
    assert stats["registered"] == 4
    assert stats["origin_quarantined"] == 2
    assert stats["sample_quarantined"] == 2
    assert stats["returned"] == 2
    assert stats["duplicates"] == 1
    assert stats["local_verified"] == 1
    assert stats["link_only"] == 7
    assert stats["batches"] == 3
    assert stats["conservation_failures"] == []
    assert stats["conservation_ok"] is True
    assert stats["report_status"] == "partial"

    items = read_jsonl(out(result, it.MAIN_NAME))
    assert len(items) == stats["registered"] == 4
    assert {item["operator_01"]["sample_id"] for item in items} == {
        "m-pass-01", "m-pass-02", "m-pass-local-01", "m-dup-01"}
    # 主输出只保存链接，不复制媒体
    for item in items:
        assert item["operator_01"]["file_link"].startswith(("oss://", "media/"))
    assert not list(Path(result["runDir"]).glob("*.jpg"))
    assert not list(Path(result["runDir"]).glob("*.png"))


def test_run_writes_all_ledgers_and_registry(tmp_path, mixed_rows):
    result = run_rows(tmp_path, mixed_rows, media={"media/m-pass-local-01.png": "png"})
    origin = read_jsonl(out(result, it.ORIGIN_QUARANTINE_NAME))
    sample = read_jsonl(out(result, it.SAMPLE_QUARANTINE_NAME))
    returned = read_jsonl(out(result, it.RETURN_NAME))
    assert len(origin) == 4          # 2 条格式/文件问题 + 2 条坏行
    assert len(sample) == 2
    assert len(returned) == 2
    assert {item["reason_code"] for item in origin} == {
        "format_not_allowed", "missing_sample_id", "json_parse_error",
        "record_not_object"}
    assert {item["label"] for item in origin} == {it.LABEL_ORIGIN}
    assert {item["label"] for item in sample} == {it.LABEL_SAMPLE}
    assert {item["label"] for item in returned} == {it.LABEL_RETURN}
    assert all("sample_id" in item for item in returned)

    registry = json.loads(out(result, it.REGISTRY_NAME).read_text(encoding="utf-8"))
    assert registry["format"] == "crossmodal-batch-registry-v1"
    assert registry["entrypoint"] == it.MAIN_NAME
    assert registry["totals"]["registered"] == 4
    groups = {(item["source"], item["batch_id"]): item
              for item in registry["batches"]}
    image_batch = groups[(it.SOURCE_IMAGE, DEFAULT_BATCH)]
    assert image_batch["source_label"] == "预训练图像产线交付"
    assert image_batch["registered"] == 3
    assert image_batch["origin_quarantined"] == 2
    assert image_batch["sample_quarantined"] == 2
    assert image_batch["returned"] == 1
    assert image_batch["object_count"] == 8
    assert groups[(it.SOURCE_AUDIO, DEFAULT_BATCH)]["registered"] == 1
    assert groups[(it.SOURCE_IMAGE, "unknown")]["returned"] == 1


def test_run_manifest_and_artifacts(tmp_path, mixed_rows):
    result = run_rows(tmp_path, mixed_rows, media={"media/m-pass-local-01.png": "png"})
    manifest = json.loads(out(result, "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == it.MAIN_NAME
    paths = [item["path"] for item in manifest["artifacts"]]
    for name in (it.MAIN_NAME, it.ORIGIN_QUARANTINE_NAME, it.SAMPLE_QUARANTINE_NAME,
                 it.RETURN_NAME, it.REGISTRY_NAME, it.LOG_NAME):
        assert name in paths, name
    roles = {item["path"]: item["role"] for item in manifest["artifacts"]}
    assert roles[it.MAIN_NAME] == "main"
    assert roles[it.REGISTRY_NAME] == "batch_registry"

    artifacts = {item["path"]: item for item in result["artifacts"]}
    assert artifacts[it.MAIN_NAME]["records"] == 4
    assert artifacts[it.MAIN_NAME]["bytes"] > 0
    assert artifacts[it.LOG_NAME]["role"] == "log"


def test_run_lineage_output_is_disabled(tmp_path, mixed_rows):
    """[v4.2] 血缘输出已停用：run() 不再回传 lineage，也不再写 lineage.json。"""
    result = run_rows(tmp_path, mixed_rows)
    assert "lineage" not in result
    assert "stageContext" not in result
    assert not (out(result, "lineage.json")).exists()
    assert not (out(result, "stage_context.json")).exists()
    assert result["outputFile"].endswith(it.MAIN_NAME)


def test_run_log_has_no_raw_payload(tmp_path, mixed_rows):
    result = run_rows(tmp_path, mixed_rows)
    log = out(result, it.LOG_NAME).read_text(encoding="utf-8")
    assert "[config]" in log and "[scan]" in log and "[register]" in log
    assert "[conservation]" in log
    assert "oss://bucket" not in log
    assert "坏掉的一行" not in log


def test_run_empty_input_is_success_empty(tmp_path):
    entry = write_jsonl(tmp_path / "tmp_empty.jsonl", [])
    result = inference.run(input_file=str(entry), unique_id="empty_task",
                           output_root=str(tmp_path), allow_external_input=True)
    assert result["status"] == "empty"
    assert result["statistics"]["input"] == 0
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["conservation_ok"] is True
    assert read_jsonl(out(result, it.MAIN_NAME)) == []


def test_run_blank_lines_are_counted_not_dropped(tmp_path):
    entry = write_jsonl(tmp_path / "tmp_blank.jsonl",
                        ["", json.dumps(make_record("b-1"), ensure_ascii=False)])
    result = inference.run(input_file=str(entry), unique_id="blank_task",
                           output_root=str(tmp_path), allow_external_input=True)
    stats = result["statistics"]
    assert stats["blank_lines"] == 1
    assert stats["input"] == 1
    assert stats["registered"] == 1
    assert stats["conservation_ok"] is True


def test_run_disabled_operator_registers_nothing(tmp_path, mixed_rows):
    result = run_rows(tmp_path, mixed_rows, overrides={"enabled": False})
    assert result["status"] == "disabled"
    assert result["statistics"]["registered"] == 0
    assert result["statistics"]["input"] == 0
    assert result["statistics"]["enabled"] is False
    assert read_jsonl(out(result, it.MAIN_NAME)) == []
    assert read_jsonl(out(result, it.ORIGIN_QUARANTINE_NAME)) == []


def test_run_bad_encoding_line_goes_to_origin_quarantine(tmp_path):
    path = tmp_path / "tmp_enc.jsonl"
    good = json.dumps(make_record("e-1"), ensure_ascii=False)
    path.write_bytes(("中文坏行\n").encode("gbk") + (good + "\n").encode("utf-8"))
    result = inference.run(input_file=str(path), unique_id="enc_task",
                           output_root=str(tmp_path), allow_external_input=True)
    stats = result["statistics"]
    assert stats["registered"] == 1
    assert stats["valid"] == 1
    assert stats["rejected"] == 1
    assert stats["origin_quarantined"] == 0
    assert stats["reject_reasons"] == {"invalid_utf8": 1}
    assert stats["conservation_ok"] is True
    origin = read_jsonl(out(result, it.ORIGIN_QUARANTINE_NAME))
    assert origin[0]["reason_code"] == "invalid_utf8"
    assert origin[0]["lineno"] == 1
    assert origin[0]["label"] == it.LABEL_ORIGIN


def test_run_utf8_bom_is_tolerated(tmp_path):
    path = tmp_path / "tmp_bom.jsonl"
    payload = json.dumps(make_record("bom-1"), ensure_ascii=False)
    path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8") + b"\n")
    result = inference.run(input_file=str(path), unique_id="bom_task",
                           output_root=str(tmp_path), allow_external_input=True)
    assert result["statistics"]["registered"] == 1
    assert result["statistics"]["rejected"] == 0


def test_run_missing_input_raises(tmp_path):
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(tmp_path / "nope.jsonl"), unique_id="missing_task",
                      output_root=str(tmp_path), allow_external_input=True)


def test_run_rejects_invalid_override(tmp_path, mixed_rows):
    with pytest.raises(schemas.ConfigError):
        run_rows(tmp_path, mixed_rows, overrides={"file_check_mode": "turbo"})
    with pytest.raises(schemas.ConfigError):
        run_rows(tmp_path, mixed_rows, overrides={"nope": 1})
    with pytest.raises(schemas.ConfigError):
        run_rows(tmp_path, mixed_rows, overrides={"enabled": "true"})


def test_run_rejects_invalid_config_file(tmp_path, mixed_rows, invalid_mode_config):
    with pytest.raises(schemas.ConfigError):
        run_rows(tmp_path, mixed_rows, config_path=invalid_mode_config)


def test_run_whitelist_mode_marks_link_only(tmp_path):
    rows = [make_record("w-1", remote=False, ext="png")]
    entry = build_input_dir(tmp_path, rows, unique_id="whitelist_task",
                            media={"media/w-1.png": "png"})
    result = inference.run(input_file=str(entry), unique_id="whitelist_task",
                           output_root=str(tmp_path),
                           config_path=str(Path(__file__).resolve().parents[1]
                                           / "configs" / "config.yaml"),
                           overrides={"file_check_mode": "whitelist"})
    assert result["statistics"]["link_only"] == 1
    assert result["statistics"]["local_verified"] == 0
    assert result["statistics"]["registered"] == 1


def test_run_strict_mode_isolates_unreadable_local_file(tmp_path):
    rows = [make_record("st-1", remote=False, ext="png"),
            make_record("st-2", remote=False, ext="png")]
    entry = build_input_dir(tmp_path, rows, unique_id="strict_task",
                            media={"media/st-1.png": "png"})
    result = inference.run(input_file=str(entry), unique_id="strict_task",
                           output_root=str(tmp_path),
                           config_path=str(Path(__file__).resolve().parents[1]
                                           / "configs" / "config.yaml"),
                           overrides={"file_check_mode": "strict"})
    stats = result["statistics"]
    assert stats["registered"] == 1
    assert stats["origin_quarantined"] == 1
    assert stats["local_verified"] == 1
    origin = read_jsonl(out(result, it.ORIGIN_QUARANTINE_NAME))
    assert origin[0]["reason_code"] == "file_not_accessible"


def test_run_preserves_upstream_namespaces(tmp_path):
    record = make_record("ns-1", extra={
        "processing": {"04": {"status": "cleaned"}},
        "alignment": {"type": "image_text", "status": "auto_aligned"},
        "qc": {"verdict": "pass"},
        "metadata": {"scene": "street"},
        "license_note": "公开库",
    })
    result = run_rows(tmp_path, [record])
    item = read_jsonl(out(result, it.MAIN_NAME))[0]
    assert item["processing"]["04"] == {"status": "cleaned"}
    # [v4.2] status 双字段：本算子记录级 int status + 原字符串迁入 process_status
    assert item["processing"]["01"]["status"] == 1
    assert item["processing"]["01"]["process_status"] == "registered"
    assert item["operator_01"]["status"] == 1
    assert item["operator_01"]["process_status"] == "registered"
    assert item["alignment"]["status"] == "auto_aligned"
    assert item["qc"] == {"verdict": "pass"}
    assert item["metadata"] == {"scene": "street"}
    assert item["license_note"] == "公开库"
    assert item["operator_01"]["sample_id"] == "ns-1"


def test_run_is_isolated_between_unique_ids(tmp_path, mixed_rows):
    first = run_rows(tmp_path, mixed_rows, unique_id="iso_a",
                     media={"media/m-pass-local-01.png": "png"})
    second = run_rows(tmp_path, [make_record("only-b")], unique_id="iso_b")
    assert first["statistics"]["registered"] == 4
    assert second["statistics"]["registered"] == 1
    assert Path(first["runDir"]).is_relative_to(tmp_path / "iso_a")
    assert Path(second["runDir"]).is_relative_to(tmp_path / "iso_b")
    assert {item["operator_01"]["uniqueId"]
            for item in read_jsonl(out(first, it.MAIN_NAME))} == {"iso_a"}
    assert {item["operator_01"]["uniqueId"]
            for item in read_jsonl(out(second, it.MAIN_NAME))} == {"iso_b"}


def test_run_rerun_uses_new_run_dir(tmp_path):
    rows = [make_record("rr-1")]
    entry = build_input_dir(tmp_path, rows, unique_id="rerun_task")
    first = inference.run(input_file=str(entry), unique_id="rerun_task",
                          output_root=str(tmp_path))
    second = inference.run(input_file=str(entry), unique_id="rerun_task",
                           output_root=str(tmp_path))
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["runDir"]) != Path(second["runDir"])
    assert Path(first["runDir"]).is_dir()


def test_run_rejects_unique_id_escape(tmp_path):
    for bad in ("../escape", "a/b", "a\\b", "C:name", "CON", ""):
        with pytest.raises(ValueError):
            inference.run(input_file=None, unique_id=bad, output_root=str(tmp_path))


def test_atomic_writer_does_not_truncate_source_when_paths_match(tmp_path):
    source = write_jsonl(tmp_path / "same.jsonl", [make_record("a-1"), make_record("a-2")])
    original = source.read_text(encoding="utf-8")
    with io_utils.PartialJsonlWriter(source) as writer:
        for line in io_utils.iter_raw_lines(source):
            writer.write({"raw": line.decode("utf-8").strip()})
    assert source.read_text(encoding="utf-8") != original
    rows = read_jsonl(source)
    assert [item["raw"] for item in rows] == original.strip().split("\n")
    assert not list(tmp_path.glob("*.partial"))


def test_atomic_writer_discards_partial_on_failure(tmp_path):
    target = tmp_path / "out.jsonl"
    with pytest.raises(RuntimeError):
        with io_utils.PartialJsonlWriter(target) as writer:
            writer.write({"a": 1})
            raise RuntimeError("boom")
    assert not target.exists()
    assert not list(tmp_path.glob("*.partial"))


# --------------------------------------------------------------------------- #
# 阶段返回形态
# --------------------------------------------------------------------------- #

def test_intermediate_and_stage_payload_shapes(tmp_path, mixed_rows):
    """[v4.2] 阶段返回形态统一：血缘视图已停用，只保留 outputFile 文件内容对象。"""
    result = run_rows(tmp_path, mixed_rows)
    assert not hasattr(inference, "build_intermediate_lineage")
    assert not hasattr(inference, "build_stage_payload")
    payload = inference.stage_file_object(result)
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == it.MAIN_NAME
    assert all("/" not in item["path"] and "\\" not in item["path"]
               for item in payload["files"])


def test_stage_file_object_packs_ledgers(tmp_path, mixed_rows):
    result = run_rows(tmp_path, mixed_rows, media={"media/m-pass-local-01.png": "png"})
    payload = inference.stage_file_object(result)
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == it.MAIN_NAME
    paths = [item["path"] for item in payload["files"]]
    assert it.ORIGIN_QUARANTINE_NAME in paths
    assert it.REGISTRY_NAME in paths
    for item in payload["files"]:
        assert item["encoding"] in ("utf-8", "base64")
        if item["encoding"] == "utf-8":
            assert isinstance(item["content"], str)
