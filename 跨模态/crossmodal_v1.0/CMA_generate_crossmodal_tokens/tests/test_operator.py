"""端到端编排测试（``inference.run``）：统计守恒、产物形态、任务隔离、路径安全。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (DEFAULT_CONFIG_YAML, deep_merge, make_record, read_jsonl,
                      rec_19, rec_20, rec_21, rec_audio, rec_image, rec_license,
                      rec_text, rec_video, rec_version, write_jsonl)

import inference
from utils import tokens
from utils.schemas import ConfigError


def build_rows():
    """构造一组覆盖全部分流的输入行（含坏行与空行）。"""
    rows = [
        # 生成（图文）
        make_record("tc-0001", rec_19(), rec_20(), rec_license(),
                    rec_text("一只猫趴在草地上"), rec_image("media/cat.png"),
                    rec_version("v1.0")),
        # 生成（图文，含局部标注证据；默认关闭局部路线）
        make_record("tc-0002", rec_20(), rec_license(), rec_text("狗在跑"),
                    rec_image("media/dog.png")),
        # 无质量结论 → 待处理
        make_record("tc-0003", rec_19(), rec_license(), rec_text("无结论"),
                    rec_image("media/x.png")),
        # 隔离区样本 → 跳过
        make_record("tc-0004", rec_20(), rec_21(), rec_license(), rec_text("已隔离"),
                    rec_image("media/y.png")),
        # 不可训练 → 单独列出
        make_record("tc-0005", rec_20(), rec_license("不可训练"), rec_text("不可训练"),
                    rec_image("media/z.png")),
        # 音文样本（默认音文路线关闭）→ 待处理
        make_record("tc-0006", rec_20(), rec_license(), rec_text("语音内容"),
                    rec_audio("media/a.wav")),
        # 缺标识 → 原始隔离
        make_record(None, rec_20(), rec_license(), rec_text("缺标识"), rec_image()),
        # 非法媒体路径 → 原始隔离
        make_record("tc-0008", rec_20(), rec_license(), rec_text("非法路径"),
                    {"image": {"path": "/abs/escape.png"}}),
        '{"sample_id": "broken", ',
        "[1, 2, 3]",
        "",
    ]
    return rows


@pytest.fixture()
def sample_file(tmp_path) -> Path:
    return write_jsonl(tmp_path / "19_candidate_pool.jsonl", build_rows())


def test_run_generates_and_conserves(sample_file, config_file, output_root):
    result = inference.run(input_file=str(sample_file), unique_id="task_ok",
                           config_path=str(config_file),
                           output_root=str(output_root))
    stats = result["statistics"]
    assert stats["input_records"] == 10
    assert stats["blank_lines"] == 1
    assert stats["bad_lines"] == 2
    assert stats["valid_records"] == 8
    assert stats["generated_records"] == 2
    assert stats["pending_records"] == 2
    assert stats["untrainable_records"] == 1
    assert stats["isolated_skipped_records"] == 1
    assert stats["origin_quarantined"] == 2
    assert stats["rejects_entries"] == 4
    assert stats["conservation_ok"] is True
    assert stats["conservation_failures"] == []
    assert result["status"] == "success"


def test_run_writes_expected_files(sample_file, config_file, output_root):
    result = inference.run(input_file=str(sample_file), unique_id="task_files",
                           config_path=str(config_file),
                           output_root=str(output_root))
    run_dir = Path(result["runDir"])
    assert (run_dir / "samples.jsonl").is_file()
    assert (run_dir / "samples_image_text.jsonl").is_file()
    assert (run_dir / tokens.TOKENS_NAME).is_file()
    assert (run_dir / tokens.METADATA_NAME).is_file()
    assert (run_dir / tokens.PENDING_NAME).is_file()
    assert (run_dir / tokens.FLAGGED_NAME).is_file()
    assert (run_dir / tokens.UNTRAINABLE_NAME).is_file()
    assert (run_dir / tokens.ISOLATED_NAME).is_file()
    assert (run_dir / tokens.REJECTS_NAME).is_file()
    assert (run_dir / "manifest.json").is_file()
    assert (run_dir / tokens.LOG_NAME).is_file()
    # 默认配置下音文/视文/局部标注路线不产出对应文件
    assert not (run_dir / "samples_audio_text.jsonl").exists()
    assert not (run_dir / "samples_video_text.jsonl").exists()
    assert not (run_dir / "samples_local_annotation.jsonl").exists()
    # 原文件仅保留对象存储链接：不复制任何媒体
    assert not (run_dir / "media").exists()


def test_samples_lines_match_statistics(sample_file, config_file, output_root):
    result = inference.run(input_file=str(sample_file), unique_id="task_lines",
                           config_path=str(config_file),
                           output_root=str(output_root))
    run_dir = Path(result["runDir"])
    samples = read_jsonl(run_dir / "samples.jsonl")
    route_rows = read_jsonl(run_dir / "samples_image_text.jsonl")
    assert len(samples) == result["statistics"]["generated_records"] == 2
    assert len(route_rows) == 2
    for row in samples:
        node = row["processing"]["22"]
        # [v4.2] status 双字段：int status + 字符串 process_status
        assert node["status"] == 1
        assert node["process_status"] == "generated"
        assert node["token_text"].startswith("<image>")
        assert row["operator_22"]["token_char_count"] == len(node["token_text"])
    assert len(read_jsonl(run_dir / tokens.METADATA_NAME)) == 2
    assert len(read_jsonl(run_dir / tokens.TOKENS_NAME)) == 2
    assert len(read_jsonl(run_dir / tokens.PENDING_NAME)) == 2
    assert len(read_jsonl(run_dir / tokens.UNTRAINABLE_NAME)) == 1
    assert len(read_jsonl(run_dir / tokens.ISOLATED_NAME)) == 1
    assert len(read_jsonl(run_dir / tokens.REJECTS_NAME)) == 4


def test_token_row_starts_with_image_marker(sample_file, config_file, output_root):
    result = inference.run(input_file=str(sample_file), unique_id="task_token",
                           config_path=str(config_file),
                           output_root=str(output_root))
    rows = read_jsonl(Path(result["runDir"]) / tokens.TOKENS_NAME)
    assert rows[0]["token_text"].startswith("<image>")
    assert rows[0]["route"] == "image_text"
    assert rows[0]["marker_validation"]["valid"] is True
    assert rows[0]["subword_token_ids"] is None


def test_isolation_rows_never_appear_in_samples(sample_file, config_file, output_root):
    result = inference.run(input_file=str(sample_file), unique_id="task_iso",
                           config_path=str(config_file),
                           output_root=str(output_root))
    run_dir = Path(result["runDir"])
    sample_ids = {row["sample_id"] for row in read_jsonl(run_dir / "samples.jsonl")}
    isolated = {row["sample_id"] for row in read_jsonl(run_dir / tokens.ISOLATED_NAME)}
    untrainable = {row["sample_id"] for row in read_jsonl(run_dir / tokens.UNTRAINABLE_NAME)}
    pending = {row["sample_id"] for row in read_jsonl(run_dir / tokens.PENDING_NAME)}
    assert sample_ids == {"tc-0001", "tc-0002"}
    assert sample_ids.isdisjoint(isolated | untrainable | pending)


def test_flagged_quality_is_generated_and_listed_separately(tmp_path, config_file,
                                                            output_root):
    rows = [
        make_record("tc-flag1", rec_20("pending_review", "flagged",
                                       label="样本-质检：flagged 旁路"),
                    rec_license(), rec_text("flagged 样本"), rec_image("media/f.png")),
        make_record("tc-flag2", rec_20(), rec_license(), rec_text("正常样本"),
                    rec_image("media/g.png")),
    ]
    path = write_jsonl(tmp_path / "candidates.jsonl", rows)
    result = inference.run(input_file=str(path), unique_id="task_flag",
                           config_path=str(config_file), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["generated_records"] == 2
    assert stats["flagged_records"] == 1
    assert stats["conservation_ok"] is True
    run_dir = Path(result["runDir"])
    flagged = read_jsonl(run_dir / tokens.FLAGGED_NAME)
    assert len(flagged) == 1
    assert flagged[0]["sample_id"] == "tc-flag1"
    assert flagged[0]["label"] == tokens.LABEL_GENERATED
    assert "flagged" in flagged[0]["label_detail"]
    # flagged 样本仍在主样本文件中（属「不拦样本」的旁路子集）
    sample_ids = {row["sample_id"] for row in read_jsonl(run_dir / "samples.jsonl")}
    assert sample_ids == {"tc-flag1", "tc-flag2"}
    assert (run_dir / "samples.jsonl").read_text(encoding="utf-8").count("flagged") >= 1


def test_all_routes_config_produces_route_files(tmp_path, all_routes_config_file,
                                                output_root):
    rows = [
        make_record("tc-1001", rec_20(), rec_license(), rec_text("图文"),
                    rec_image("media/a.png")),
        make_record("tc-1002", rec_20(), rec_license(), rec_text("音文"),
                    rec_audio("media/b.wav")),
        make_record("tc-1003", rec_20(), rec_license(), rec_text("视文"),
                    rec_video("media/c.mp4")),
        make_record("tc-1004", rec_20(), rec_license(), rec_text("多模态"),
                    rec_image("media/d.png"), rec_audio("media/e.wav")),
    ]
    path = write_jsonl(tmp_path / "candidates.jsonl", rows)
    result = inference.run(input_file=str(path), unique_id="task_routes",
                           config_path=str(all_routes_config_file),
                           output_root=str(output_root))
    run_dir = Path(result["runDir"])
    stats = result["statistics"]
    assert stats["generated_records"] == 4
    assert stats["by_route"]["image_text"] == 2
    assert stats["by_route"]["audio_text"] == 2
    assert stats["by_route"]["video_text"] == 1
    assert stats["by_route"]["local_annotation"] == 0
    assert (run_dir / "samples_audio_text.jsonl").is_file()
    assert (run_dir / "samples_video_text.jsonl").is_file()
    assert stats["conservation_ok"] is True


def test_local_annotation_route_extra_membership(tmp_path, all_routes_config_file,
                                                 output_root):
    rows = [make_record("tc-2001", rec_20(), rec_license(), rec_text("局部"),
                        rec_image("media/a.png"),
                        {"local_annotations": {"status": "confirmed",
                                               "confirmed_boxes": [{"box_id": "b1"}]}})]
    path = write_jsonl(tmp_path / "candidates.jsonl", rows)
    result = inference.run(input_file=str(path), unique_id="task_local",
                           config_path=str(all_routes_config_file),
                           output_root=str(output_root))
    run_dir = Path(result["runDir"])
    assert (run_dir / "samples_local_annotation.jsonl").is_file()
    assert result["statistics"]["by_route"]["local_annotation"] == 1
    assert result["statistics"]["multi_route_records"] >= 1


def test_empty_input_is_not_an_error(tmp_path, config_file, output_root):
    path = write_jsonl(tmp_path / "empty.jsonl", [])
    result = inference.run(input_file=str(path), unique_id="task_empty",
                           config_path=str(config_file),
                           output_root=str(output_root))
    assert result["status"] == "success"
    assert result["statistics"]["input_records"] == 0
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["generated_records"] == 0
    assert result["statistics"]["conservation_ok"] is True


def test_all_bad_lines_fails(tmp_path, config_file, output_root):
    path = write_jsonl(tmp_path / "bad.jsonl", ['{"a":', "not json"])
    result = inference.run(input_file=str(path), unique_id="task_bad",
                           config_path=str(config_file),
                           output_root=str(output_root))
    assert result["status"] == "failed"
    assert result["statistics"]["valid_records"] == 0
    assert result["statistics"]["bad_lines"] == 2


def test_non_utf8_file_is_quarantined_not_silently_dropped(tmp_path, config_file,
                                                           output_root):
    path = tmp_path / "gbk.jsonl"
    path.write_bytes('{"sample_id": "中文"}\n'.encode("gbk"))
    result = inference.run(input_file=str(path), unique_id="task_enc",
                           config_path=str(config_file),
                           output_root=str(output_root))
    stats = result["statistics"]
    assert stats["encoding_error"] is True
    assert stats["file_read_incomplete"] is True
    assert stats["rejects_entries"] >= 1
    rejects = read_jsonl(Path(result["runDir"]) / tokens.REJECTS_NAME)
    assert any(row.get("reason") == tokens.REASON_ENCODING for row in rejects)


def test_strict_policy_fails_on_missing_quality(tmp_path, output_root):
    config = tmp_path / "strict.yaml"
    config.write_text(DEFAULT_CONFIG_YAML.replace(
        "missing_quality_policy: waiting_manual", "missing_quality_policy: failed"),
        encoding="utf-8")
    rows = [make_record("tc-3001", rec_19(), rec_license(), rec_text("x"),
                        rec_image("media/a.png"))]
    path = write_jsonl(tmp_path / "candidates.jsonl", rows)
    result = inference.run(input_file=str(path), unique_id="task_strict",
                           config_path=str(config), output_root=str(output_root))
    assert result["status"] == "failed"


def test_quality_only_task_is_degraded(tmp_path, output_root):
    config = tmp_path / "strict.yaml"
    config.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    rows = [make_record("tc-4001", rec_19(), rec_license(), rec_text("x"),
                        rec_image("media/a.png"))]
    path = write_jsonl(tmp_path / "candidates.jsonl", rows)
    result = inference.run(input_file=str(path), unique_id="task_deg",
                           config_path=str(config), output_root=str(output_root))
    assert result["status"] == "degraded"
    assert result["statistics"]["generated_records"] == 0
    assert result["statistics"]["pending_records"] == 1


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_lineage_and_stage_context_not_in_file_object(sample_file, config_file,
                                                      output_root):
    result = inference.run(input_file=str(sample_file), unique_id="task_lineage",
                           config_path=str(config_file),
                           output_root=str(output_root))
    run_dir = Path(result["runDir"])
    lineage_path = run_dir / "lineage.json"
    assert lineage_path.is_file()
    node = json.loads(lineage_path.read_text(encoding="utf-8"))[0]
    for key in ("operator", "operator_version", "uniqueId", "inputFile", "status",
                "startedAt", "finishedAt", "config", "statistics", "outputFile"):
        assert key in node, key
    assert node["operator"] == tokens.OPERATOR
    assert node["statistics"]["generated_records"] == 2
    assert node["model"] is None
    context = json.loads((run_dir / "stage_context.json").read_text(encoding="utf-8"))
    assert context["stage_id"] == "S5"
    assert context["formalOperatorNumber"] == 22


def test_two_unique_ids_are_physically_isolated(sample_file, config_file, output_root):
    first = inference.run(input_file=str(sample_file), unique_id="task_a",
                          config_path=str(config_file), output_root=str(output_root))
    second = inference.run(input_file=str(sample_file), unique_id="task_b",
                           config_path=str(config_file), output_root=str(output_root))
    assert first["runDir"] != second["runDir"]
    assert "task_a" in first["runDir"] and "task_b" in second["runDir"]
    first_rows = read_jsonl(Path(first["outputFile"]))
    second_rows = read_jsonl(Path(second["outputFile"]))
    assert [row["sample_id"] for row in first_rows] == \
        [row["sample_id"] for row in second_rows]
    assert [row["processing"]["22"]["token_text"] for row in first_rows] == \
        [row["processing"]["22"]["token_text"] for row in second_rows]
    # [v4.2] status 双字段：int status + 字符串 process_status
    assert all(row["processing"]["22"]["status"] == 1
               for row in first_rows + second_rows)
    assert all(row["processing"]["22"]["process_status"] == "generated"
               for row in first_rows + second_rows)


def test_rerun_same_unique_id_does_not_overwrite_previous_run(sample_file, config_file,
                                                              output_root):
    first = inference.run(input_file=str(sample_file), unique_id="task_repeat",
                          config_path=str(config_file), output_root=str(output_root))
    second = inference.run(input_file=str(sample_file), unique_id="task_repeat",
                           config_path=str(config_file), output_root=str(output_root))
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["runDir"]).is_dir() and Path(second["runDir"]).is_dir()


def test_invalid_unique_ids_rejected(sample_file, config_file, output_root):
    for bad in ("../escape", "a/b", "CON", "", "x."):
        with pytest.raises(ValueError):
            inference.run(input_file=str(sample_file), unique_id=bad,
                          config_path=str(config_file), output_root=str(output_root))


def test_missing_input_file_raises(sample_file, config_file, output_root, tmp_path):
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(tmp_path / "nope.jsonl"), unique_id="task_x",
                      config_path=str(config_file), output_root=str(output_root))


def test_directory_as_input_raises(sample_file, config_file, output_root):
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(sample_file.parent), unique_id="task_dir",
                      config_path=str(config_file), output_root=str(output_root))


def test_restrict_to_task_rejects_external_input(sample_file, config_file, output_root):
    with pytest.raises(inference.InputError):
        inference.run(input_file=str(sample_file), unique_id="task_restrict",
                      config_path=str(config_file), output_root=str(output_root),
                      allow_external_input=False)


def test_config_rejects_unknown_keys(config_file, output_root, sample_file):
    with pytest.raises(ConfigError):
        inference.load_config(str(config_file), {"nope": 1})


def test_config_rejects_disabling_tool(config_file):
    with pytest.raises(ConfigError):
        inference.load_config(str(config_file), {"enabled": False})


def test_config_rejects_exclude_isolated_false(tmp_path):
    config = tmp_path / "bad.yaml"
    config.write_text(DEFAULT_CONFIG_YAML.replace("exclude_isolated: true",
                                                  "exclude_isolated: false"),
                      encoding="utf-8")
    with pytest.raises(ConfigError):
        inference.load_config(str(config))


def test_config_rejects_renaming_samples_file(tmp_path):
    config = tmp_path / "bad.yaml"
    config.write_text(DEFAULT_CONFIG_YAML.replace("samples_file_name: samples.jsonl",
                                                  "samples_file_name: other.jsonl"),
                      encoding="utf-8")
    with pytest.raises(ConfigError):
        inference.load_config(str(config))


def test_config_rejects_changing_readonly_markers(tmp_path):
    config = tmp_path / "bad.yaml"
    config.write_text(DEFAULT_CONFIG_YAML.replace('image: "<image>"',
                                                  'image: "[img]"'),
                      encoding="utf-8")
    with pytest.raises(ConfigError):
        inference.load_config(str(config))


def test_config_rejects_disabling_image_text_route(config_file):
    with pytest.raises(ConfigError):
        inference.load_config(str(config_file), {"routes": {"image_text": False}})


def test_config_accepts_enabling_audio_route_via_override(tmp_path, output_root):
    rows = [make_record("tc-5001", rec_20(), rec_license(), rec_text("音文"),
                        rec_audio("media/b.wav"))]
    path = write_jsonl(tmp_path / "candidates.jsonl", rows)
    result = inference.run(input_file=str(path), unique_id="task_override",
                           output_root=str(output_root),
                           overrides={"routes": {"audio_text": True}})
    assert result["statistics"]["generated_records"] == 1
    assert (Path(result["runDir"]) / "samples_audio_text.jsonl").is_file()


def test_output_is_utf8_and_atomic_temp_files_removed(sample_file, config_file,
                                                      output_root):
    result = inference.run(input_file=str(sample_file), unique_id="task_utf8",
                           config_path=str(config_file), output_root=str(output_root))
    run_dir = Path(result["runDir"])
    for name in ("samples.jsonl", tokens.TOKENS_NAME, tokens.METADATA_NAME):
        text = (run_dir / name).read_text(encoding="utf-8")
        assert "一只猫" in text or "狗在跑" in text or name == tokens.METADATA_NAME
    leftovers = [item.name for item in run_dir.iterdir() if item.name.endswith(".swp")]
    assert leftovers == []


def test_log_has_no_credentials_or_full_text(sample_file, config_file, output_root):
    result = inference.run(input_file=str(sample_file), unique_id="task_log",
                           config_path=str(config_file), output_root=str(output_root))
    log_text = (Path(result["runDir"]) / tokens.LOG_NAME).read_text(encoding="utf-8")
    assert "一只猫趴在草地上" not in log_text
    assert "token=" not in log_text.lower()
    assert "[输入] run_start" in log_text and "[完成] run_finish" in log_text
    assert "generated=2" in log_text
    first_line = log_text.splitlines()[0]
    assert first_line.startswith("[20")            # [YYYY-MM-DD HH:mm:ss+ZZZZ]


# --------------------------------------------------------------------------- #
# 视文路线出厂默认开启（v1.0.1）
# --------------------------------------------------------------------------- #

def test_shipped_config_enables_video_route_by_default():
    """出厂配置不再把「未配置视文路线」当成「无路线」，避免视频样本全进待处理。"""
    from utils.schemas import OperatorConfig

    shipped = OperatorConfig.model_validate({
        "stage_id": "S5", "enabled": True, "exclude_isolated": True,
        "samples_file_name": "samples.jsonl",
        "markers": {"image": "<image>", "audio": "<audio>", "video": "<video>"},
        "missing_quality_policy": "waiting_manual",
    })
    assert shipped.routes.video_text is True
    assert shipped.routes.audio_text is False
    assert shipped.routes.local_annotation is False

    repo_config = Path(__file__).resolve().parents[1] / "configs" / "config.yaml"
    assert "video_text: true" in repo_config.read_text(encoding="utf-8")


def test_video_samples_generate_when_video_route_default_on(tmp_path, output_root):
    rows = [make_record("tc-v1", rec_20(), rec_license(), rec_text("一名男子在弹吉他"),
                        rec_video("media/v.mp4"))]
    path = write_jsonl(tmp_path / "video.jsonl", rows)
    config = tmp_path / "video_default.yaml"
    config.write_text(DEFAULT_CONFIG_YAML.replace(
        "  audio_text: false\n  video_text: false\n  local_annotation: false\n",
        "  audio_text: false\n  video_text: true\n  local_annotation: false\n"),
        encoding="utf-8")
    result = inference.run(input_file=str(path), unique_id="task_video_default",
                           config_path=str(config), output_root=str(output_root))
    stats = result["statistics"]
    assert stats["generated_records"] == 1
    assert stats["by_route"]["video_text"] == 1
    assert (Path(result["runDir"]) / "samples_video_text.jsonl").is_file()
    token = read_jsonl(Path(result["runDir"]) / tokens.TOKENS_NAME)[0]
    assert token["token_text"].startswith("<video>")
