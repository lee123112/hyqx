"""业务入口测试：``inference.run`` 的路线、分流、守恒、隔离与失败路径。"""
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

from tests import fixtures
from utils import localization as loc
from utils.schemas import ConfigError


def _task(tmp_path, unique_id="task_1", records=None, media=None, **kwargs):
    records = records if records is not None else [
        fixtures.make_record("s-1", image="media/demo.png",
                             human=fixtures.human_evidence(boxes=[fixtures.human_box()])),
    ]
    media = media if media is not None else {"media/demo.png": fixtures.png_bytes()}
    return fixtures.write_task(tmp_path, unique_id, records, media=media, **kwargs)


def _run(tmp_path, unique_id="task_1", *, detector=None, overrides=None,
         config_path=None, **kwargs):
    return inference.run(input_file=None, unique_id=unique_id,
                         output_root=str(tmp_path), overrides=overrides,
                         config_path=config_path,
                         detector=detector, allow_external_input=False, **kwargs)


def _lineage(result):
    """读取阶段血缘数组，返回本算子本次运行的节点（最后一个本算子节点）。"""
    payload = json.loads(Path(result["lineageFile"]).read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload, "lineage file must be a non-empty array"
    nodes = [item for item in payload if item.get("operator") == loc.OPERATOR]
    assert nodes, "lineage array has no node for this operator"
    return nodes[-1]


def _stage_context(result):
    return json.loads(Path(result["stageContextFile"]).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 关闭路线（表1 默认）
# --------------------------------------------------------------------------- #

def test_disabled_route_passes_through_without_annotations(tmp_path):
    _task(tmp_path, records=[
        fixtures.make_record("s-1", image="media/demo.png"),
        fixtures.make_record("s-2", modality=["text"], description="纯文字"),
    ])
    result = _run(tmp_path)
    stats = result["statistics"]
    assert result["status"] == "success"
    assert result["entrypoint"] == loc.MAIN_NAME_DISABLED
    assert stats["skipped_records"] == 2
    assert stats["main_records"] == 2
    assert stats["boxes_prefilled"] == 0
    assert stats["time_ranges_prefilled"] == 0
    assert stats["detector_available"] is False
    assert stats["model"] is None
    assert stats["conservation_ok"] is True

    rows = fixtures.read_jsonl(result["outputFile"])
    # [v4.2] status 双字段：路线关闭属正常流程态（status=1），原字符串迁入 process_status
    assert all(row["processing"]["17"]["status"] == 1 for row in rows)
    assert all(row["processing"]["17"]["process_status"] == "skipped" for row in rows)
    assert all("local_annotations" not in row for row in rows)


def test_disabled_route_still_quarantines_bad_lines(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", modality=["text"])],
          raw_tail='{"sample_id": "broken", ')
    result = _run(tmp_path)
    stats = result["statistics"]
    assert stats["bad_lines"] == 1
    assert stats["origin_quarantined"] == 0
    assert stats["rejects_entries"] == 1
    assert stats["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 开启路线 + 注入检测后端
# --------------------------------------------------------------------------- #

def test_enabled_route_with_injected_detector(tmp_path):
    _task(tmp_path, records=[
        fixtures.make_record("s-confirmed", image="media/demo.png",
                             human=fixtures.human_evidence(boxes=[fixtures.human_box()])),
        fixtures.make_record("s-pending", image="media/demo.png"),
        fixtures.make_record("s-incomplete", image="media/demo.png",
                             human=fixtures.human_evidence(complete=False)),
        fixtures.make_record("s-na", modality=["text"], description="只有文字"),
    ])
    detector = fixtures.StubDetector()
    result = _run(tmp_path, overrides={"enabled": True}, detector=detector)
    stats = result["statistics"]

    assert result["status"] == "partial"
    assert result["entrypoint"] == loc.MAIN_NAME_ENABLED
    assert stats["confirmed_records"] == 1
    assert stats["pending_records"] == 1
    assert stats["incomplete_records"] == 1
    assert stats["not_applicable_records"] == 1
    assert stats["main_records"] == 3
    assert stats["detector_available"] is True
    assert stats["detector_degraded"] is False
    assert stats["boxes_prefilled"] == 3
    assert stats["boxes_dropped_unmatched"] == 3  # car 框对不上描述
    assert stats["boxes_confirmed"] == 1
    assert stats["pending_sidecar_entries"] == 1
    assert stats["conservation_ok"] is True
    assert stats["model"]["name"] == "YOLOv8n"
    assert detector.calls.count("demo.png") == 3

    rows = {row["sample_id"]: row for row in fixtures.read_jsonl(result["outputFile"])}
    assert rows["s-confirmed"]["local_annotations"]["deliverable"] is True
    assert rows["s-confirmed"]["local_annotations"]["human_verified"] is True
    assert rows["s-pending"]["local_annotations"]["deliverable"] is False
    # [v4.2] status 双字段：int status（待人工/不适用属正常流程态=1）+ process_status
    assert rows["s-pending"]["local_annotations"]["status"] == 1
    assert rows["s-pending"]["local_annotations"]["process_status"] == "pending_manual_review"
    assert rows["s-na"]["local_annotations"]["status"] == 1
    assert rows["s-na"]["local_annotations"]["process_status"] == "not_applicable"

    quarantine = fixtures.read_jsonl(result["sampleQuarantineFile"])
    assert [entry["sample_id"] for entry in quarantine] == ["s-incomplete"]
    assert quarantine[0]["label"] == "样本-语义不通"
    assert quarantine[0]["record"]["local_annotations"]["deliverable"] is False


def test_enabled_route_prefills_av_time_ranges_from_transcript(tmp_path):
    _task(tmp_path, records=[
        fixtures.make_record("av-1", modality=["audio", "text"], description="设备运转",
                             audio="media/clip.wav",
                             transcript=fixtures.transcript_payload()),
    ], media={"media/clip.wav": fixtures.wav_bytes()})
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["time_ranges_prefilled"] == 2
    assert stats["records_with_av"] == 1
    assert stats["media_carried"] >= 1

    row = fixtures.read_jsonl(result["outputFile"])[0]
    prefilled = row["local_annotations"]["prefilled"]["time_ranges"]
    assert [item["start_sec"] for item in prefilled] == [0.0, 1.2]
    assert row["local_annotations"]["process_status"] == "pending_manual_review"
    assert row["local_annotations"]["status"] == 1   # [v4.2] int status
    assert row["local_annotations"]["reason_code"] == loc.REASON_ANNOTATION_PENDING


def test_av_transcript_sidecar_is_carried(tmp_path):
    _task(tmp_path, records=[
        fixtures.make_record("av-2", modality=["audio", "text"], description="设备运转",
                             transcript_file="media/transcript.json"),
    ], media={"media/transcript.json": json.dumps(
        fixtures.transcript_payload(), ensure_ascii=False).encode("utf-8")})
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["time_ranges_prefilled"] == 2
    assert stats["media_carried"] >= 1
    assert stats["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 模型不可用：显式降级 / 显式失败（绝不伪造框）
# --------------------------------------------------------------------------- #

def test_detector_unavailable_degrades_without_fabricated_boxes(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-degraded", image="media/demo.png")])
    result = _run(tmp_path, overrides={"enabled": True, "allow_degraded": True})
    stats = result["statistics"]
    assert result["status"] == "degraded"
    assert stats["detector_available"] is False
    assert stats["detector_degraded"] is True
    assert stats["boxes_prefilled"] == 0
    assert stats["pending_records"] == 1
    assert stats["model"] is None
    assert stats["conservation_ok"] is True

    block = fixtures.read_jsonl(result["outputFile"])[0]["local_annotations"]
    assert block["prefilled"]["box_count"] == 0
    assert block["detector"]["degraded"] is True
    assert block["detector"]["real_backend"] is False
    assert block["reason_code"] == loc.REASON_DETECTOR_UNAVAILABLE


def test_detector_unavailable_without_degraded_fails(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/demo.png")])
    with pytest.raises(inference.DependencyError):
        _run(tmp_path, overrides={"enabled": True, "allow_degraded": False})


def test_forced_yolov8n_backend_without_weights_fails(tmp_path, config_writer):
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/demo.png")])
    path = config_writer(enabled=True, detector_backend="yolov8n")
    with pytest.raises(inference.DependencyError):
        _run(tmp_path, config_path=str(path))


def test_model_and_paths_cannot_be_overridden(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/demo.png")])
    for payload in ({"enabled": True, "detector_backend": "yolov8n"},
                    {"enabled": True, "detector": {"weights": "x.pt"}},
                    {"enabled": True, "model_name": "GroundingDINO"},
                    {"enabled": True, "stage_id": "S9"}):
        with pytest.raises(ConfigError):
            _run(tmp_path, overrides=payload)


def test_detector_runtime_failure_does_not_fabricate(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/demo.png")])
    detector = fixtures.StubDetector(fail_on={"demo.png"})
    with pytest.raises(inference.DependencyError):
        _run(tmp_path, overrides={"enabled": True}, detector=detector)


def test_grounding_dino_backend_is_not_supported(tmp_path, config_writer):
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/demo.png")])
    path = config_writer(enabled=True, detector_backend="grounding_dino")
    with pytest.raises(ConfigError):
        _run(tmp_path, config_path=str(path))


# --------------------------------------------------------------------------- #
# 异常与隔离
# --------------------------------------------------------------------------- #

def test_bad_lines_and_non_object_records_go_to_rejects(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", modality=["text"])],
          raw_tail='"not-an-object"')
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["bad_lines"] == 1
    assert stats["rejects_entries"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_RECORD_NOT_OBJECT
    assert entry["label"] == loc.LABEL_ORIGIN_UNPARSABLE


def test_missing_sample_id_is_origin_quarantine(tmp_path):
    _task(tmp_path, records=[fixtures.make_record(None, image="media/demo.png")])
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_MISSING_SAMPLE_ID
    assert entry["label"] == loc.LABEL_ORIGIN_MISSING_FIELD


def test_missing_image_is_origin_quarantine(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/absent.png")])
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_IMAGE_MISSING


def test_missing_media_is_quarantined_even_when_detector_degraded(tmp_path):
    """原始文件缺陷与模型可用性无关：降级时也必须隔离缺失媒体。"""
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/absent.png")])
    result = _run(tmp_path, overrides={"enabled": True, "allow_degraded": True})
    stats = result["statistics"]
    assert stats["detector_degraded"] is True
    assert stats["origin_quarantined"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_IMAGE_MISSING
    assert stats["conservation_ok"] is True


def test_unreadable_image_is_origin_quarantine(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/broken.png")],
          media={"media/broken.png": b"\xff\xfe\x00\x01"})
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    assert stats["images_unreadable"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_IMAGE_UNREADABLE
    assert entry["label"] == loc.LABEL_ORIGIN_DAMAGED


def test_broken_transcript_sidecar_is_origin_quarantine(tmp_path):
    _task(tmp_path, records=[
        fixtures.make_record("av-1", modality=["audio", "text"], description="声音",
                             transcript_file="media/transcript_broken.json"),
    ], media={"media/transcript_broken.json": b"{oops"})
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_TRANSCRIPT_UNREADABLE


def test_invalid_human_evidence_is_origin_quarantine(tmp_path):
    record = fixtures.make_record("s-1", image="media/demo.png",
                                  human={"human_verified": True, "boxes": "nope"})
    _task(tmp_path, records=[record])
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_HUMAN_ANNOTATION_INVALID


def test_media_path_escape_is_origin_quarantine(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", image="../escape.png")])
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_MEDIA_PATH_UNSAFE


def test_too_many_time_ranges_is_origin_quarantine(tmp_path):
    segments = [{"id": "s%d" % index, "start": float(index), "end": float(index) + 0.5}
                for index in range(5)]
    _task(tmp_path, records=[
        fixtures.make_record("av-1", modality=["audio", "text"], description="声音",
                             transcript={"segments": segments}),
    ])
    result = _run(tmp_path, overrides={"enabled": True, "max_time_ranges": 2},
                  detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    entry = fixtures.read_jsonl(result["rejectsFile"])[0]
    assert entry["reason_code"] == loc.REASON_TOO_MANY_TIME_RANGES


# --------------------------------------------------------------------------- #
# 空输入、编码错误、图片上限
# --------------------------------------------------------------------------- #

def test_empty_input_is_reported_not_hidden(tmp_path):
    _task(tmp_path, records=[])
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["empty_input"] is True
    assert stats["input_records"] == 0
    assert stats["conservation_ok"] is True


def test_non_utf8_input_is_flagged(tmp_path):
    entry = fixtures.write_task(tmp_path, "task_1", [fixtures.make_record("s-1")])
    entry.write_bytes(b'{"sample_id": "\xff\xfe"}\n')
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["encoding_error"] is True
    assert stats["file_read_incomplete"] is True
    assert result["status"] in ("partial", "failed")


def test_too_many_images_is_counted_not_silently_dropped(tmp_path):
    images = {"media/img_%d.png" % index: fixtures.png_bytes() for index in range(3)}
    record = fixtures.make_record(
        "s-1", images=["media/img_0.png", "media/img_1.png", "media/img_2.png"])
    _task(tmp_path, records=[record], media=images)
    result = _run(tmp_path, overrides={"enabled": True, "max_images_per_record": 1},
                  detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["images_processed"] == 1
    assert stats["images_over_limit"] == 2
    assert stats["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 任务隔离、run_id 与血缘
# --------------------------------------------------------------------------- #

def test_two_unique_ids_are_isolated(tmp_path):
    _task(tmp_path, "task_a", records=[fixtures.make_record("a-1", modality=["text"])])
    _task(tmp_path, "task_b", records=[fixtures.make_record("b-1", modality=["text"])])
    result_a = _run(tmp_path, "task_a", overrides={"enabled": True},
                    detector=fixtures.StubDetector())
    result_b = _run(tmp_path, "task_b", overrides={"enabled": True},
                    detector=fixtures.StubDetector())
    assert result_a["runDir"] != result_b["runDir"]
    assert "task_a" in result_a["runDir"]
    assert "task_b" in result_b["runDir"]
    assert fixtures.read_jsonl(result_a["outputFile"])[0]["sample_id"] == "a-1"
    assert fixtures.read_jsonl(result_b["outputFile"])[0]["sample_id"] == "b-1"


def test_second_run_uses_new_run_id(tmp_path):
    _task(tmp_path, "task_a", records=[fixtures.make_record("a-1", modality=["text"])])
    first = _run(tmp_path, "task_a", overrides={"enabled": True},
                 detector=fixtures.StubDetector())
    second = _run(tmp_path, "task_a", overrides={"enabled": True},
                  detector=fixtures.StubDetector())
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert first["runDir"] != second["runDir"]


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_lineage_contains_required_fields(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", modality=["text"])])
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    record = _lineage(result)
    for key in ("operator", "operator_version", "uniqueId", "stageId", "runId",
                "inputFile", "outputFile", "status", "startedAt", "finishedAt",
                "config", "statistics", "artifacts", "model"):
        assert key in record, key
    assert record["operator"] == loc.OPERATOR
    assert record["formalOperatorNumber"] == 17
    assert record["statistics"]["input_records"] == 1
    assert Path(result["lineageFile"]).exists()


def test_stage_file_object_shape(tmp_path):
    """[v4.2] 血缘中间/阶段形态已停用；仅保留 crossmodal-files-v1 文件对象校验。"""
    _task(tmp_path, records=[fixtures.make_record("s-1", modality=["text"])])
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())

    payload = inference.stage_file_object(result)
    assert payload["format"] == "crossmodal-files-v1"
    assert payload["entrypoint"] == result["entrypoint"]


def test_stage_control_files_not_packed(tmp_path):
    """[v4.2] 血缘文件已停用：不再产出 lineage.json / stage_context.json；
    run.log 仍落盘但不进 crossmodal-files-v1 文件内容对象。"""
    _task(tmp_path, records=[fixtures.make_record("s-1", modality=["text"])])
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    run_dir = Path(result["runDir"])

    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()

    log_text = Path(result["logFile"]).read_text(encoding="utf-8")
    assert "run_start" in log_text and "run_end" in log_text

    packed = {item["path"] for item in inference.stage_file_object(result)["files"]}
    assert "manifest.json" in packed
    for control in ("lineage.json", "stage_context.json", "run.log"):
        assert control not in packed, "%s 是本地控制文件，不应进文件内容对象" % control


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用")
def test_stage_lineage_appends_upstream_node(tmp_path):
    """同阶段上游节点必须按顺序追加，不能只保存当前一条。"""
    _task(tmp_path, records=[fixtures.make_record("s-1", modality=["text"])])
    upstream = {
        "operator": "whole_alignment_annotation",
        "operator_version": "v1.0.0",
        "stageId": "S3",
        "uniqueId": "task_1",
        "runId": "run001",
        "status": "success",
        "outputFile": "input/16_out.jsonl",
    }
    result = _run(tmp_path, overrides={"enabled": True},
                  detector=fixtures.StubDetector(), stage_lineage=[upstream])

    stored = json.loads(Path(result["lineageFile"]).read_text(encoding="utf-8"))
    assert [node["operator"] for node in stored] == [
        "whole_alignment_annotation", loc.OPERATOR]

    stage = inference.build_stage_payload(result)
    assert [node["operator"] for node in stage] == [
        "whole_alignment_annotation", loc.OPERATOR]
    assert all("outputFile" not in node for node in stage)

    context = _stage_context(result)
    assert [node["operator"] for node in context["stage_lineage"]] == [
        "whole_alignment_annotation", loc.OPERATOR]
    assert context["stageOperators"] == ["whole_alignment_annotation", loc.OPERATOR]
    assert result["stageStart"] is False
    assert result["statistics"]["stage_start"] is False


def test_processing_17_exposes_downstream_regions(tmp_path):
    """#18 需要 processing['17'].regions/boxes/candidate_boxes 与 paths.image。"""
    _task(tmp_path, records=[
        fixtures.make_record("s-1", image="media/demo.png",
                             human=fixtures.human_evidence(boxes=[fixtures.human_box()])),
        fixtures.make_record("s-2", image="media/demo.png"),
    ], media={"media/demo.png": fixtures.png_bytes()})
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    rows = fixtures.read_jsonl(result["mainFile"])
    by_id = {row["sample_id"]: row for row in rows}

    confirmed = by_id["s-1"]["processing"]["17"]
    assert confirmed["boxes_source"] == "human_confirmed"
    assert confirmed["confirmed_boxes"] and confirmed["regions"] == confirmed["confirmed_boxes"]
    assert confirmed["regions"][0]["bbox"]["coord"] == "xywh-normalized"
    assert confirmed["regions"][0]["human_verified"] is True
    assert confirmed["paths"]["image"] == "media/demo.png"
    assert confirmed["path"] == "media/demo.png"
    # 预填候选单列且绝不标人工确认
    assert confirmed["candidate_boxes"], "person 框应与描述对得上并作为候选"
    assert all(item["human_verified"] is False for item in confirmed["candidate_boxes"])

    pending = by_id["s-2"]["processing"]["17"]
    assert pending["boxes_source"] == "yolov8n_prefill"
    assert pending["regions"] == pending["candidate_boxes"]
    assert pending["confirmed_boxes"] == []


def test_skipped_route_exposes_empty_regions(tmp_path):
    """工具关闭：给下游明确空列表，而不是省略字段或留下路径。"""
    _task(tmp_path, records=[fixtures.make_record("s-1", image="media/demo.png")])
    result = _run(tmp_path, overrides={"enabled": False})
    row = fixtures.read_jsonl(result["mainFile"])[0]
    block = row["processing"]["17"]
    assert block["boxes_source"] == "none"
    assert block["regions"] == [] and block["candidate_boxes"] == []
    assert block["time_ranges"] == [] and block["confirmed_boxes"] == []
    assert block["paths"]["image"] == "media/demo.png"


def test_input_file_outside_task_is_rejected(tmp_path):
    _task(tmp_path, "task_a", records=[fixtures.make_record("a-1", modality=["text"])])
    _task(tmp_path, "task_b", records=[fixtures.make_record("b-1", modality=["text"])])
    outside = tmp_path / "task_b" / "input" / "demo.jsonl"
    with pytest.raises(ValueError):
        inference.run(input_file=str(outside), unique_id="task_a",
                      output_root=str(tmp_path), overrides={"enabled": True},
                      detector=fixtures.StubDetector(), allow_external_input=False)


def test_invalid_unique_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        _run(tmp_path, "../escape", overrides={"enabled": True})


@pytest.mark.skip(reason="v4.2 血缘/运输输出已停用：配置快照原内嵌于血缘节点")
def test_config_snapshot_records_overrides_and_identity(tmp_path):
    _task(tmp_path, records=[fixtures.make_record("s-1", modality=["text"])])
    result = _run(tmp_path, overrides={"enabled": True, "max_time_ranges": 7},
                  detector=fixtures.StubDetector())
    config = _lineage(result)["config"]
    assert config["overridden_keys"] == ["enabled", "max_time_ranges"]
    assert config["business"]["enabled"] is True
    assert config["business"]["max_time_ranges"] == 7
    assert config["deployment"]["operator"]["number"] == 17
    assert config["deployment"]["stage_id"] == "S3"


# --------------------------------------------------------------------------- #
# 无图视频：关键帧检测预填（v1.0.1）
# --------------------------------------------------------------------------- #

def test_video_frames_prefill_boxes_and_carry_frame_media(tmp_path):
    record = fixtures.make_record("vid-1", modality=["video", "text"],
                                  description="画面中有一名操作员在检查设备",
                                  video="media/vid/silent.mp4",
                                  visual_time_index=[
                                      {"index": 0, "start": 0.0, "end": 1.0,
                                       "text": None, "segment_path": None,
                                       "source": "visual_frame", "granularity": "frame",
                                       "frame_path": "media/vid/frame_000001.jpg"},
                                      {"index": 1, "start": 1.0, "end": 2.0,
                                       "text": None, "segment_path": None,
                                       "source": "visual_frame", "granularity": "frame",
                                       "frame_path": "media/vid/frame_000002.jpg"}])
    record["video"]["frames"] = [
        {"path": "media/vid/frame_000001.jpg", "timestamp": 0.0, "frame_index": 0},
        {"path": "media/vid/frame_000002.jpg", "timestamp": 1.0, "frame_index": 1}]
    _task(tmp_path, records=[record], media={
        "media/vid/silent.mp4": b"\x00\x00\x00\x18ftypmp42",
        "media/vid/frame_000001.jpg": fixtures.png_bytes(),
        "media/vid/frame_000002.jpg": fixtures.png_bytes()})
    detector = fixtures.StubDetector()

    result = _run(tmp_path, overrides={"enabled": True}, detector=detector)
    stats = result["statistics"]
    assert stats["records_with_frames"] == 1
    assert stats["frames_processed"] == 2
    assert stats["frames_missing"] == 0
    assert stats["boxes_prefilled"] == 2   # person × 每帧各一个
    assert stats["time_ranges_prefilled"] == 2
    assert stats["conservation_ok"] is True
    assert sorted(detector.calls) == ["frame_000001.jpg", "frame_000002.jpg"]

    row = fixtures.read_jsonl(result["outputFile"])[0]
    block = row["local_annotations"]
    assert block["process_status"] == "pending_manual_review"
    assert block["reason_code"] == loc.REASON_ANNOTATION_PENDING
    assert block["frame_count"] == 2 and block["frame_scoped_boxes"] == 2
    assert block["deliverable"] is False
    assert {item["frame_path"] for item in block["prefilled"]["boxes"]} == {
        "media/vid/frame_000001.jpg", "media/vid/frame_000002.jpg"}
    assert [item["source"] for item in block["prefilled"]["time_ranges"]] == [
        "visual_frame", "visual_frame"]
    # 帧文件随 run 目录搬运，下游 #18 能直接裁图
    run_dir = Path(result["outputFile"]).parent
    assert (run_dir / "media" / "vid" / "frame_000001.jpg").is_file()


def test_video_frames_missing_file_is_counted_not_quarantined(tmp_path):
    record = fixtures.make_record("vid-2", modality=["video", "text"],
                                  description="画面中有一辆汽车驶过",
                                  video="media/vid/silent.mp4")
    record["video"]["frames"] = [
        {"path": "media/vid/frame_000001.jpg", "timestamp": 0.0},
        {"path": "media/vid/absent.jpg", "timestamp": 1.0}]
    _task(tmp_path, records=[record], media={
        "media/vid/silent.mp4": b"\x00\x00\x00\x18ftypmp42",
        "media/vid/frame_000001.jpg": fixtures.png_bytes()})
    result = _run(tmp_path, overrides={"enabled": True}, detector=fixtures.StubDetector())
    stats = result["statistics"]
    assert stats["frames_processed"] == 1
    assert stats["frames_missing"] == 1
    assert stats["origin_quarantined"] == 0
    assert stats["boxes_prefilled"] == 1
