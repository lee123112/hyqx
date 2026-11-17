"""算子 14 端到端业务测试（``inference.run``）。

覆盖：正常评分、关闭路线透传、空文件、坏行、缺字段、重复标识、错误编码、
错误配置、任务隔离、路径安全、统计守恒、血缘完整性、降级/失败路径、
两种 ``isLastOperator`` 返回形态与运行目录不覆盖。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import inference  # noqa: E402
from fixtures import (build_input_tree, default_frames, default_media,  # noqa: E402
                      make_config, make_flat_record, make_pair, read_jsonl, StubEncoder,
                      write_jsonl)
from utils import lineage as lineage_mod  # noqa: E402
from utils import transport  # noqa: E402
from utils.schemas import ConfigError  # noqa: E402


def run_once(tmp_path, records, *, overrides=None, encoder=None, name="input",
             media=None, raw_lines=None, unique_id="task_file", output_root=None,
             encoding="utf-8"):
    base = build_input_tree(tmp_path / name, records, media=media or default_media(),
                            raw_lines=raw_lines, encoding=encoding)
    root = output_root or (tmp_path / ("out_" + name))
    result = inference.run(str(base / "demo.jsonl"), unique_id,
                           config_path=str(tmp_path / "config.yaml"),
                           output_root=str(root), overrides=overrides, encoder=encoder)
    return result, root


def run_with_config(tmp_path, records, config_text, *, encoder=None, name="input",
                    overrides=None, unique_id="task_file"):
    config_path = tmp_path / "custom_config.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    base = build_input_tree(tmp_path / name, records, media=default_media())
    root = tmp_path / ("out_" + name)
    return inference.run(str(base / "demo.jsonl"), unique_id,
                         config_path=str(config_path), output_root=str(root),
                         overrides=overrides, encoder=encoder), root


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def test_default_config_is_route_disabled(config_file):
    config = inference.load_config(str(config_file))
    assert config.enabled is False
    # [2026-11-05 全量标定] 默认低分阈值 0.20
    assert config.low_score_threshold == pytest.approx(0.20)
    assert config.max_frames_per_segment == 8
    assert config.clip.model_name == "ViT-B-32"


def test_override_enables_route(config_file):
    config = inference.load_config(str(config_file),
                                  {"enabled": True, "max_frames_per_segment": 4})
    assert config.enabled is True
    assert config.max_frames_per_segment == 4


@pytest.mark.parametrize("overrides", [
    {"enabled": "yes"},
    {"max_frames_per_segment": 0},
    {"max_frames_per_segment": 9},
    {"max_frames_per_segment": "8"},
    {"low_score_threshold": 0.9},
    {"low_score_threshold": 0.05},
    {"allow_degraded": "true"},
    {"clip": {"model_name": "Chinese-CLIP-ViT-B-16"}},
    {"clip": {"library": "unknown-lib"}},
    {"clip": {"device": "tpu"}},
    {"clip": {"batch_size": 0}},
    {"unknown_key": 1},
])
def test_invalid_config_is_rejected(config_file, overrides):
    with pytest.raises(ConfigError):
        inference.load_config(str(config_file), overrides)


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        inference.load_config(str(tmp_path / "not_exist.yaml"))


def test_broken_config_file_raises(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("enabled: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        inference.load_config(str(path))


def test_config_snapshot_records_business_and_overrides(config_file):
    config = inference.load_config(str(config_file), {"enabled": True})
    snapshot = inference.config_snapshot(config, {"enabled": True})
    assert snapshot["business"]["enabled"] is True
    assert "enabled" in snapshot["overridden_keys"]
    assert snapshot["deployment"]["stage_id"] == "S3"


# --------------------------------------------------------------------------- #
# 关闭路线：原样透传，不加载模型、不写视文评分文件、不隔离
# --------------------------------------------------------------------------- #

def test_disabled_route_passes_through_without_model(tmp_path, config_file):
    records = [make_flat_record("s-%02d" % index, "描述文本", default_frames(2))
               for index in range(1, 4)]
    result, _root = run_once(tmp_path, records)
    stats = result["statistics"]
    assert result["status"] == "success"
    assert result["entrypoint"] == "14_passthrough.jsonl"
    assert stats["skipped_records"] == 3
    assert stats["passed_records"] == 0
    assert stats["sample_isolated"] == 0
    assert stats["models_available"] is False
    assert stats["model_source"] is None
    assert stats["conservation_ok"] is True

    rows = read_jsonl(result["outputFile"])
    assert len(rows) == 3
    for row in rows:
        node = row["processing"]["14"]
        assert node["status"] == 1
        assert node["process_status"] == "skipped"
        assert node["video_text_score"] is None
        # 关闭态回填本次配置生效值（默认 0.20），不是硬编码 0.25
        assert node["low_score_threshold"] == pytest.approx(0.20)
        assert node["max_frames_per_segment"] == 8
        assert node["frames_used"] == 0
    assert result["models"]["reason"] == "route_disabled"


def test_disabled_route_node_uses_configured_threshold(tmp_path, config_file):
    """关闭态阈值/帧预算输出必须与本次运行配置一致（开关两态口径一致）。"""
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records,
                             overrides={"low_score_threshold": 0.31,
                                        "max_frames_per_segment": 4})
    rows = read_jsonl(result["outputFile"])
    node = rows[0]["processing"]["14"]
    assert node["process_status"] == "skipped"
    assert node["low_score_threshold"] == pytest.approx(0.31)
    assert node["max_frames_per_segment"] == 4


# --------------------------------------------------------------------------- #
# 启用路线：真实分段判定
# --------------------------------------------------------------------------- #

def test_enabled_route_scores_and_splits(tmp_path, config_file):
    frames = [{"path": "frames/frame_0001.png", "timestamp": 0.0},
              {"path": "frames/frame_0002.png", "timestamp": 1.0},
              {"path": "frames/frame_0003.png", "timestamp": 2.0},
              {"path": "frames/frame_0003.png", "timestamp": 3.0}]
    segments = [{"start": 0.0, "end": 2.0, "text": "高分段"},
                {"start": 2.0, "end": 4.0, "text": "低分段"}]
    records = [make_flat_record("s-pass", "描述", frames[:2]),
               make_pair("pair-low", "整段描述", frames, segments=segments)]
    encoder = StubEncoder(scores={"低分段": 0.10}, default=0.80)
    result, _root = run_once(tmp_path, records, overrides={"enabled": True}, encoder=encoder)
    stats = result["statistics"]
    assert result["entrypoint"] == "14_video_text_scores.jsonl"
    assert stats["passed_records"] == 1
    assert stats["sample_isolated"] == 1
    assert stats["segment_below_threshold"] == 1
    assert stats["scored_records"] == 2
    assert stats["models_available"] is True
    assert stats["conservation_ok"] is True

    main = read_jsonl(result["outputFile"])
    assert len(main) == 1
    assert main[0]["video_text_score"]["passed"] is True
    assert main[0]["video_text_score"]["video_text_score"] == pytest.approx(0.80)
    assert main[0]["video_text_score"]["model"]["name"] == "CLIP-ViT/B-32"

    quarantine = read_jsonl(result["sampleQuarantineFile"])
    assert len(quarantine) == 1
    assert quarantine[0]["label"] == "样本-语义不通"
    assert quarantine[0]["destination"] == "样本隔离库"
    assert quarantine[0]["video_text"]["min_segment_score"] == pytest.approx(0.10)


def test_enabled_route_boundary_threshold_passes(tmp_path, config_file):
    records = [make_flat_record("s-bound", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records, overrides={"enabled": True},
                             encoder=StubEncoder(default=0.25))
    assert result["statistics"]["passed_records"] == 1
    assert result["statistics"]["sample_isolated"] == 0


def test_enabled_route_multi_segment_whole_video_cap(tmp_path, config_file):
    """端到端：3 段 ×10 候选、上限 8 → 输出 frames_used ≤ 8，视频分为全帧均值。"""
    segment_count, per_segment = 3, 10
    frames = [{"path": "frames/frame_%04d.png" % (index + 1), "timestamp": float(index)}
              for index in range(segment_count * per_segment)]
    media = default_media(["frames/frame_%04d.png" % (index + 1)
                           for index in range(segment_count * per_segment)])
    segments = [{"start": float(index * per_segment),
                 "end": float((index + 1) * per_segment),
                 "text": "段%d" % (index + 1)} for index in range(segment_count)]
    records = [make_flat_record("s-multi", "整段描述", frames, segments=segments)]
    encoder = StubEncoder(scores={"段1": 0.80, "段2": 0.60, "段3": 0.40},
                          default=0.50)
    result, _root = run_once(tmp_path, records, media=media,
                             overrides={"enabled": True}, encoder=encoder)
    assert result["statistics"]["passed_records"] == 1
    node = read_jsonl(result["outputFile"])[0]["video_text_score"]
    assert node["frames_used"] == 8
    assert node["frames_used"] <= node["max_frames_per_segment"] == 8
    assert sum(item["frame_count"] for item in node["segments"]) == 8
    all_scores = [frame["score"] for item in node["segments"]
                  for frame in item["frames"]]
    assert node["video_text_score"] == pytest.approx(
        sum(all_scores) / len(all_scores))


def test_frames_are_carried_into_run_dir(tmp_path, config_file):
    records = [make_flat_record("s-carry", "描述", default_frames(3))]
    result, _root = run_once(tmp_path, records, overrides={"enabled": True},
                             encoder=StubEncoder(default=0.6))
    stats = result["statistics"]
    assert stats["frames_carried"] == 3
    assert stats["media_carried"] >= 1
    run_dir = Path(result["runDir"])
    assert (run_dir / "frames" / "frame_0001.png").is_file()


# --------------------------------------------------------------------------- #
# 空文件 / 坏行 / 缺字段 / 重复标识 / 编码
# --------------------------------------------------------------------------- #

def test_empty_input_is_success_with_conservation(tmp_path, config_file):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    result = inference.run(str(empty), "task_file", config_path=str(config_file),
                           output_root=str(tmp_path / "out_empty"))
    stats = result["statistics"]
    assert result["status"] == "success"
    assert stats["input_records"] == 0
    assert stats["empty_input"] is True
    assert stats["conservation_ok"] is True
    assert read_jsonl(result["outputFile"]) == []


def test_bad_lines_go_to_rejects(tmp_path, config_file):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    raw_lines = [json.dumps(records[0], ensure_ascii=False),
                 "{这不是合法 JSON",
                 json.dumps([1, 2, 3], ensure_ascii=False),
                 ""]
    result, _root = run_once(tmp_path, records, raw_lines=raw_lines)
    stats = result["statistics"]
    assert stats["bad_lines"] == 2
    assert stats["blank_lines"] == 1
    assert stats["input_records"] == 3
    assert stats["conservation_ok"] is True
    rejects = read_jsonl(result["rejectsFile"])
    assert [item["reason"] for item in rejects] == ["json_parse_error", "record_not_object"]


def test_missing_identifier_goes_to_origin_quarantine(tmp_path, config_file):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    records[0].pop("sample_id")
    result, _root = run_once(tmp_path, records)
    stats = result["statistics"]
    assert stats["valid_records"] == 1
    assert stats["origin_quarantined"] == 1
    rejects = read_jsonl(result["rejectsFile"])
    assert rejects[0]["reason"] == "missing_field: sample_id"
    assert rejects[0]["label"] == "原始文件-缺字段"


def test_duplicate_identifier_goes_to_origin_quarantine(tmp_path, config_file):
    records = [make_flat_record("s-dup", "描述", default_frames(2)),
               make_flat_record("s-dup", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records)
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    rejects = read_jsonl(result["rejectsFile"])
    assert rejects[0]["reason"] == "duplicate_identifier"


def test_encoding_error_fails_whole_file(tmp_path, config_file):
    payload = b'{"sample_id": "s-01", "text": "\xff\xfe"}\n'
    source = tmp_path / "bad_encoding.jsonl"
    source.write_bytes(payload)
    result = inference.run(str(source), "task_file", config_path=str(config_file),
                           output_root=str(tmp_path / "out_enc"))
    stats = result["statistics"]
    assert result["status"] == "failed"
    assert stats["encoding_error"] is True
    assert stats["file_read_incomplete"] is True
    assert stats["bad_lines"] == 1
    assert stats["valid_records"] == 0
    assert stats["conservation_ok"] is True
    rejects = read_jsonl(result["rejectsFile"])
    assert rejects[0]["reason"] == "encoding_error"
    assert rejects[0]["label"] == "原始文件-编码异常"


# --------------------------------------------------------------------------- #
# 降级 / 失败
# --------------------------------------------------------------------------- #

def test_route_enabled_without_model_and_no_degrade_fails(tmp_path, config_file):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    base = build_input_tree(tmp_path / "input", records, media=default_media())
    with pytest.raises(inference.DependencyError):
        inference.run(str(base / "demo.jsonl"), "task_file",
                      config_path=str(config_file),
                      output_root=str(tmp_path / "out_strict"),
                      overrides={"enabled": True}, encoder=None)


def test_route_enabled_with_explicit_degrade_produces_no_scores(tmp_path, config_file):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records,
                             overrides={"enabled": True, "allow_degraded": True},
                             encoder=None)
    stats = result["statistics"]
    assert result["status"] == "degraded"
    assert stats["degraded"] is True
    assert stats["model_unavailable"] == 1
    assert stats["pending_records"] == 1
    assert stats["passed_records"] == 0
    assert stats["sample_isolated"] == 0
    assert stats["models_available"] is False
    assert stats["conservation_ok"] is True

    pending = read_jsonl(result["pendingFile"])
    assert pending[0]["video_text"]["video_text_score"] is None
    assert pending[0]["video_text"]["model"] is None
    assert pending[0]["destination"] == "产线待处理"


# --------------------------------------------------------------------------- #
# 任务隔离与路径安全
# --------------------------------------------------------------------------- #

def test_two_unique_ids_are_isolated(tmp_path, config_file):
    records_a = [make_flat_record("s-a", "描述A", default_frames(2))]
    records_b = [make_flat_record("s-b1", "描述B", default_frames(2)),
                 make_flat_record("s-b2", "描述B2", default_frames(2))]
    base = build_input_tree(tmp_path / "input", records_a, media=default_media())
    base_b = build_input_tree(tmp_path / "input_b", records_b, media=default_media())
    root = tmp_path / "output"
    first = inference.run(str(base / "demo.jsonl"), "task_a",
                          config_path=str(config_file), output_root=str(root))
    second = inference.run(str(base_b / "demo.jsonl"), "task_b",
                           config_path=str(config_file), output_root=str(root))
    assert first["statistics"]["input_records"] == 1
    assert second["statistics"]["input_records"] == 2
    assert Path(first["runDir"]).parts != Path(second["runDir"]).parts
    assert "task_a" in str(first["runDir"])
    assert "task_b" in str(second["runDir"])
    assert (root / "task_a").is_dir() and (root / "task_b").is_dir()


def test_input_file_outside_task_is_rejected(tmp_path, config_file):
    outside = tmp_path / "outside.jsonl"
    write_jsonl(outside, [make_flat_record("s-01", "描述", default_frames(2))])
    root = tmp_path / "output"
    inference.run(str(outside), "task_x", config_path=str(config_file),
                  output_root=str(root))  # allow_external_input=True（CLI 口径）
    with pytest.raises(inference.InputError):
        inference.run(str(outside), "task_x", config_path=str(config_file),
                      output_root=str(root), allow_external_input=False)


@pytest.mark.parametrize("unique_id", ["", "  ", "..", "a/b", "a\\b", "C:", "con", "a*"])
def test_invalid_unique_id_is_rejected(tmp_path, config_file, unique_id):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    base = build_input_tree(tmp_path / "input", records, media=default_media())
    with pytest.raises(ValueError):
        inference.run(str(base / "demo.jsonl"), unique_id,
                      config_path=str(config_file), output_root=str(tmp_path / "out"))


def test_missing_input_file_raises(tmp_path, config_file):
    with pytest.raises(inference.InputError):
        inference.run(str(tmp_path / "not_exist.jsonl"), "task_file",
                      config_path=str(config_file), output_root=str(tmp_path / "out"))


def test_reruns_do_not_overwrite_previous_run(tmp_path, config_file):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    first, _root = run_once(tmp_path, records)
    base = build_input_tree(tmp_path / "input2", records, media=default_media())
    second = inference.run(str(base / "demo.jsonl"), "task_file",
                           config_path=str(tmp_path / "config.yaml"),
                           output_root=str(tmp_path / "out_input"))
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()


# --------------------------------------------------------------------------- #
# 血缘与返回形态
# --------------------------------------------------------------------------- #

def test_run_result_has_no_lineage_outputs(tmp_path, config_file):
    """[v4.2] 血缘/阶段归档已停用：返回值不再含 lineage 键，也不落盘血缘文件。"""
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records, overrides={"enabled": True},
                             encoder=StubEncoder(default=0.7))
    for key in ("lineageFile", "stageLineage", "priorLineage", "contextPath"):
        assert key not in result
    run_dir = Path(result["runDir"])
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    assert result["statistics"]["passed_records"] == 1


def test_model_is_null_when_disabled(tmp_path, config_file):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records)
    assert result["model"] is None


def test_disabled_lineage_builders_return_empty():
    """[v4.2] 已停用的阶段形态函数保留存档，但不再构造血缘。"""
    result = {"stageLineage": [{"operator": "video_text_scoring"}]}
    assert inference.build_intermediate_lineage(result) == {}
    assert inference.build_stage_payload(result) == []


def test_stage_payload_is_disabled(tmp_path, config_file):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records)
    assert inference.build_stage_payload(result) == []


def test_stage_file_object_is_crossmodal_files_v1(tmp_path, config_file):
    """启用路线：阶段末文件对象必须自带入口、清单与评分用的抽帧。"""
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records, overrides={"enabled": True},
                             encoder=StubEncoder(default=0.7))
    obj = inference.stage_file_object(result)
    assert obj["format"] == "crossmodal-files-v1"
    assert obj["entrypoint"] == "14_video_text_scores.jsonl"
    paths = {item["path"] for item in obj["files"]}
    assert "14_video_text_scores.jsonl" in paths
    assert "manifest.json" in paths
    assert "14_sample_quarantine.jsonl" in paths
    assert any(path.startswith("frames/") for path in paths)

    restored = tmp_path / "restored"
    entry = transport.restore_file_object(obj, restored)
    assert entry.is_file()
    assert entry.name == "14_video_text_scores.jsonl"


def test_disabled_route_stage_file_object_has_no_frames(tmp_path, config_file):
    """关闭路线（表1）：不生成视文产物，因此不搬运抽帧，只透传记录与台账。"""
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records)
    obj = inference.stage_file_object(result)
    assert obj["entrypoint"] == "14_passthrough.jsonl"
    paths = {item["path"] for item in obj["files"]}
    assert "14_passthrough.jsonl" in paths
    assert "manifest.json" in paths
    assert not any(path.startswith("frames/") for path in paths)
    assert result["statistics"]["frames_carried"] == 0


def test_cli_data_shape_for_both_modes():
    # [v4.2] 两种形态统一 outputFile + result
    assert inference.cli_data_shape("success", False) == {
        "outputFile": "file-object", "result": "object"}
    assert inference.cli_data_shape("success", True) == {
        "outputFile": "file-object", "result": "object"}
    assert inference.cli_data_shape("failed", False) == {}


def test_stage_context_is_not_written(tmp_path, config_file):
    """[v4.2] 阶段归档已停用：不再写 stage_context.json。"""
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records)
    assert not (Path(result["runDir"]) / "stage_context.json").exists()


# --------------------------------------------------------------------------- #
# 统计守恒：逐分支
# --------------------------------------------------------------------------- #

def test_conservation_holds_across_all_branches(tmp_path, config_file):
    frames = [{"path": "frames/frame_0001.png", "timestamp": 0.0},
              {"path": "frames/frame_0002.png", "timestamp": 1.0}]
    segments = [{"start": 0.0, "end": 1.0, "text": "高分段"},
                {"start": 1.0, "end": 3.0, "text": "低分段"}]
    records = [
        make_flat_record("s-pass", "描述", frames),
        make_pair("pair-low", "描述", frames, segments=segments),
        make_flat_record("s-nodesc", "描述", default_frames(2)),
        make_flat_record("s-noframes", "描述", []),
    ]
    records[2]["text"] = None
    records[2]["metadata"] = {}
    records.append(make_flat_record("s-missing", "描述",
                                    [{"path": "frames/gone.png", "timestamp": 0.0}]))
    raw_lines = [json.dumps(item, ensure_ascii=False) for item in records]
    raw_lines.append("{broken")
    result, _root = run_once(tmp_path, records, raw_lines=raw_lines,
                             overrides={"enabled": True},
                             encoder=StubEncoder(scores={"低分段": 0.05}, default=0.9))
    stats = result["statistics"]
    assert stats["conservation_ok"] is True
    assert stats["input_records"] == stats["valid_records"] + stats["bad_lines"]
    assert stats["valid_records"] == (stats["origin_quarantined"] + stats["sample_isolated"]
                                      + stats["pending_records"] + stats["passed_records"]
                                      + stats["skipped_records"])
    assert stats["sample_isolated"] == stats["segment_below_threshold"]
    assert stats["scored_records"] == (stats["passed_records"] + stats["segment_below_threshold"])
    assert stats["pending_records"] == (stats["no_video_text_pair"] + stats["no_frames"]
                                       + stats["missing_description"]
                                       + stats["model_unavailable"]
                                       + stats["score_error_records"])
    assert stats["passed_records"] == 1
    assert stats["sample_isolated"] == 1
    assert stats["origin_quarantined"] == 1
    assert stats["pending_records"] == 2
    assert stats["bad_lines"] == 1


def test_statistics_are_not_file_sizes(tmp_path, config_file):
    records = [make_flat_record("s-01", "描述", default_frames(2))]
    result, _root = run_once(tmp_path, records)
    stats = result["statistics"]
    assert stats["input_records"] == 1
    assert not any(isinstance(value, int) and value > 1_000_000
                   for value in stats.values())
