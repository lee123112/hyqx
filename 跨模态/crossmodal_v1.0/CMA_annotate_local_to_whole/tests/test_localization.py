"""业务规则单元测试（表1 #17 冻结口径）。

重点：图像仅保留「通用类别 ∩ 与描述可对上」的框；音视频复用转写起止时间预填；
未人工确认只能是 ``pending_manual_review``（不可交付）；人工确认后才 ``confirmed``；
人工确认但标注不完整 → 样本隔离库（标签 ``样本-语义不通``）。
"""
from __future__ import annotations

import json

import pytest

from tests import fixtures
from utils import localization as loc
from utils.schemas import OperatorConfig


def _config(**overrides) -> OperatorConfig:
    payload = {
        "operator": {"number": 17, "name": "local_to_whole_annotation", "version": "v1.0.0"},
        "stage_id": "S3",
        "enabled": True,
        "detector_backend": "auto",
        "detector": {"weights": None, "device": "cpu"},
        "allow_degraded": True,
    }
    payload.update(overrides)
    return OperatorConfig.model_validate(payload)


AVAILABLE_DETECTOR = {"available": True, "degraded": False, "reason": None,
                      "identity": {"name": "YOLOv8n", "real_backend": True}}
DEGRADED_DETECTOR = {"available": False, "degraded": True,
                     "reason": "model_weights_not_configured", "identity": None}


# --------------------------------------------------------------------------- #
# 字段解析
# --------------------------------------------------------------------------- #

def test_record_sample_id_aliases():
    assert loc.record_sample_id({"sample_id": "a"}) == "a"
    assert loc.record_sample_id({"sampleId": "b"}) == "b"
    assert loc.record_sample_id({"id": 7}) == "7"
    assert loc.record_sample_id({"id": True}) is None
    assert loc.record_sample_id({}) is None


def test_resolve_description_priority_and_truncation():
    assert loc.resolve_description({"text": {"content": "a"}, "description": "b"}, 10) == "a"
    assert loc.resolve_description({"metadata": {"description": "c"}}, 10) == "c"
    assert loc.resolve_description({"description": "d"}, 10) == "d"
    assert loc.resolve_description({"text": {"content": "x" * 40}}, 5) == "xxxxx"
    assert loc.resolve_description({}, 10) is None
    assert loc.resolve_description({"text": {"content": "   "}}, 10) is None


def test_resolve_modality_collects_references():
    record = fixtures.make_record("s-1", image="media/a.png",
                                  images=["media/b.png"], video="media/v.mp4",
                                  audio="media/a.wav")
    modality = loc.resolve_modality(record)
    assert modality["has_image"] is True
    assert modality["has_audio"] is True
    assert modality["has_video"] is True
    assert modality["images"] == ["media/a.png", "media/b.png"]
    assert modality["videos"] == ["media/v.mp4"]
    assert modality["audios"] == ["media/a.wav"]

    text_only = loc.resolve_modality(fixtures.make_record("s-2", modality=["text"]))
    assert text_only["has_image"] is False
    assert text_only["applicable"] is False


def test_whole_alignment_negative_status_detected():
    assert loc.whole_alignment_not_aligned(
        {"alignment": {"whole": {"status": "not_aligned"}}}) is True
    assert loc.whole_alignment_not_aligned(
        {"alignment": {"overall": "mismatch"}}) is True
    assert loc.whole_alignment_not_aligned(
        {"alignment": {"whole": {"status": "aligned"}}}) is False
    assert loc.whole_alignment_not_aligned({}) is False


# --------------------------------------------------------------------------- #
# 转写与时间段
# --------------------------------------------------------------------------- #

def test_normalize_transcript_variants():
    entries = [
        {"id": "s0", "start": 0.0, "end": 1.5, "text": "第一段"},
        {"segment_id": "s1", "start_sec": "2.0", "end_sec": 3.5, "text": "第二段"},
    ]
    segments = loc.normalize_transcript(entries)
    assert [item["start_sec"] for item in segments] == [0.0, 2.0]
    assert [item["end_sec"] for item in segments] == [1.5, 3.5]
    assert segments[0]["segment_id"] == "s0"
    assert loc.normalize_transcript([]) == []


def test_normalize_transcript_prefers_milliseconds_with_seconds_fallback():
    """#8 v1.0.1：有 start_ms/end_ms 时以毫秒为准；旧条目回落秒字段。"""
    entries = [
        {"index": 1, "start_ms": 0, "end_ms": 1235, "start": 0.0, "end": 1.23,
         "text": "一"},
        {"index": 2, "start": 1.2, "end": 2.0, "text": "二"},          # 旧格式
        {"index": 3, "start_ms": 5000, "end_ms": 5001, "text": "三"},  # 只有毫秒
    ]
    segments = loc.normalize_transcript(entries)
    assert segments[0]["start_sec"] == 0.0
    assert segments[0]["end_sec"] == pytest.approx(1.235)
    assert segments[0]["end_ms"] == 1235
    assert segments[1]["start_ms"] == 1200 and segments[1]["end_ms"] == 2000
    assert segments[2]["start_ms"] == 5000 and segments[2]["end_ms"] == 5001

    ranges = loc.build_time_ranges(segments, max_time_ranges=10, max_text_chars=10)
    assert [item["end_ms"] for item in ranges] == [1235, 2000, 5001]
    assert [item["start_sec"] for item in ranges] == [0.0, 1.2, 5.0]


@pytest.mark.parametrize("bad", [
    [{"start": 0.0}],
    [{"start": 1.0, "end": 0.5}],
    [{"start": 0.0, "end": "abc"}],
    [{"start": -1.0, "end": 2.0}],
])
def test_normalize_transcript_rejects_invalid(bad):
    with pytest.raises(loc.LocalizationError):
        loc.normalize_transcript(bad)


def test_build_time_ranges_caps_and_excerpts():
    segments = loc.normalize_transcript(fixtures.transcript_payload()["segments"])
    ranges = loc.build_time_ranges(segments, max_time_ranges=10, max_text_chars=4)
    assert len(ranges) == 2
    assert ranges[0]["source"] == "transcript"
    assert ranges[0]["start_sec"] == 0.0
    assert ranges[0]["duration_sec"] == pytest.approx(1.2, abs=1e-6)
    assert len(ranges[0]["text_excerpt"]) <= 4

    with pytest.raises(loc.LocalizationError):
        loc.build_time_ranges(segments, max_time_ranges=1, max_text_chars=10)


def test_read_sidecar_json_errors(tmp_path):
    payload_path = tmp_path / "ok.json"
    payload_path.write_text(json.dumps({"segments": []}), encoding="utf-8")
    assert loc.read_sidecar_json(payload_path) == {"segments": []}

    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    with pytest.raises(loc.LocalizationError):
        loc.read_sidecar_json(broken)

    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(loc.LocalizationError):
        loc.read_sidecar_json(binary)

    assert loc.read_sidecar_json(tmp_path / "absent.json") is None


# --------------------------------------------------------------------------- #
# 通用类别过滤（仅通用类别 ∩ 与描述可对上）
# --------------------------------------------------------------------------- #

def test_match_general_boxes_filters_by_class_and_description():
    boxes = [
        {"class_id": 0, "class_name": "person", "confidence": 0.9,
         "x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.6},
        {"class_id": 2, "class_name": "car", "confidence": 0.8,
         "x_min": 0.5, "y_min": 0.1, "x_max": 0.9, "y_max": 0.6},
        {"class_id": 0, "class_name": "person", "confidence": 0.5,
         "x_min": 0.1, "y_min": 0.7, "x_max": 0.3, "y_max": 0.9},
    ]
    matched, dropped = loc.match_general_boxes(boxes, "一名操作员在车间里工作")
    assert len(matched) == 2
    assert dropped == 1
    assert all(item["class_name"] == "person" for item in matched)
    assert all(item["source"] == "yolov8n" for item in matched)
    assert all(item["human_verified"] is False for item in matched)
    assert matched[0]["matched_alias"] == "操作员"


def test_match_general_boxes_drops_non_general_classes():
    boxes = [{"class_id": 99, "class_name": "excavator", "confidence": 0.9,
              "x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.6}]
    matched, dropped = loc.match_general_boxes(boxes, "工地上有一台挖掘机")
    assert matched == []
    assert dropped == 1


def test_match_general_boxes_without_description_matches_nothing():
    boxes = fixtures.DEFAULT_DETECTIONS
    matched, dropped = loc.match_general_boxes(boxes, None)
    assert matched == []
    assert dropped == len(boxes)


# --------------------------------------------------------------------------- #
# 人工确认证据解析
# --------------------------------------------------------------------------- #

def test_human_boxes_and_ranges_parsed():
    parsed = loc.parse_human_evidence(fixtures.human_evidence(
        boxes=[fixtures.human_box()], time_ranges=[fixtures.human_range()]))
    assert parsed["human_verified"] is True
    assert parsed["complete"] is True
    assert parsed["annotator"] == "annotator-01"
    assert parsed["boxes"][0]["label"] == "person"
    assert parsed["boxes"][0]["source"] == "human"
    assert parsed["boxes"][0]["human_verified"] is True
    assert parsed["time_ranges"][0]["start_sec"] == 0.0
    assert parsed["time_ranges"][0]["source"] == "human"


def test_human_evidence_absent_returns_none():
    assert loc.parse_human_evidence({}) is None


@pytest.mark.parametrize("payload", [
    "not-a-dict",
    {"human_verified": "yes"},
    {"human_verified": True, "complete": "no"},
    {"human_verified": True, "boxes": "nope"},
    {"human_verified": True, "boxes": [{"bbox": [0.1, 0.2, 0.3]}]},
    {"human_verified": True, "boxes": [{"bbox": [0.9, 0.2, 0.3, 0.5]}]},
    {"human_verified": True, "boxes": [{"bbox": [0.1, 0.2, "x", 0.5]}]},
    {"human_verified": True, "time_ranges": [{"start_sec": 1.0, "end_sec": 0.5}]},
    {"human_verified": True, "time_ranges": [{"start_sec": 1.0}]},
])
def test_human_evidence_invalid_raises(payload):
    with pytest.raises(loc.LocalizationError):
        loc.parse_human_evidence({"human_annotations": payload})


# --------------------------------------------------------------------------- #
# 单条判定
# --------------------------------------------------------------------------- #

def _spec(record, media_roots=()):
    return loc.resolve_input_spec(record, list(media_roots))


def test_evaluate_confirmed_with_human_boxes():
    record = fixtures.make_record("s-1", image="media/demo.png",
                                  human=fixtures.human_evidence(boxes=[fixtures.human_box()]))
    outcome = loc.evaluate_record(_spec(record), line=1, config=_config(),
                                  event_time="2026-10-07T10:00:00+08:00",
                                  boxes=fixtures.DEFAULT_DETECTIONS,
                                  detector_state=AVAILABLE_DETECTOR)
    assert outcome["action"] == "confirm"
    block = outcome["record"]["local_annotations"]
    # [v4.2] status 双字段：int status（confirmed=1）+ 原字符串迁入 process_status
    assert block["status"] == 1
    assert block["process_status"] == "confirmed"
    assert block["deliverable"] is True
    assert block["human_verified"] is True
    assert block["confirmed_count"] == 1
    assert block["candidate_count"] == 1  # 只有 person 命中描述
    # 已确认框只放人工框；预填候选单列，绝不与已确认混用
    assert len(block["boxes"]) == 1
    assert block["boxes"][0]["source"] == "human"
    assert block["boxes"][0]["human_verified"] is True
    assert block["prefilled"]["box_count"] == 1
    assert block["prefilled"]["boxes"][0]["source"] == "yolov8n"
    assert block["prefilled"]["boxes"][0]["human_verified"] is False
    assert block["label"] == loc.LABEL_CONFIRMED
    assert outcome["record"]["operator_17"]["status"] == 1   # [v4.2] int status
    assert outcome["record"]["operator_17"]["process_status"] == "confirmed"
    assert outcome["record"]["processing"]["17"]["label"] == loc.LABEL_CONFIRMED


def test_evaluate_pending_without_human_confirmation():
    record = fixtures.make_record("s-2", image="media/demo.png")
    outcome = loc.evaluate_record(_spec(record), line=2, config=_config(),
                                  event_time="t", boxes=fixtures.DEFAULT_DETECTIONS,
                                  detector_state=AVAILABLE_DETECTOR)
    assert outcome["action"] == "pending"
    block = outcome["record"]["local_annotations"]
    assert block["status"] == 1   # [v4.2] int status（待人工属正常流程态）
    assert block["process_status"] == "pending_manual_review"
    assert block["deliverable"] is False
    assert block["human_verified"] is False
    assert block["candidate_count"] == 1
    assert block["confirmed_count"] == 0
    assert block["boxes"] == []
    assert block["prefilled"]["box_count"] == 1
    assert block["label"] == loc.LABEL_PENDING
    assert block["review_required"] is True
    assert outcome["entry"]["action"] == loc.ACTION_WAITING_MANUAL
    assert outcome["entry"]["destination"] == loc.DEST_MANUAL_QUEUE


def test_evaluate_pending_when_human_not_verified_yet():
    record = fixtures.make_record("s-3", image="media/demo.png",
                                  human=fixtures.human_evidence(boxes=[], verified=False))
    outcome = loc.evaluate_record(_spec(record), line=3, config=_config(),
                                  event_time="t", boxes=fixtures.DEFAULT_DETECTIONS,
                                  detector_state=AVAILABLE_DETECTOR)
    assert outcome["action"] == "pending"
    assert outcome["record"]["local_annotations"]["human_verified"] is False


def test_evaluate_incomplete_goes_to_sample_quarantine():
    record = fixtures.make_record("s-4", image="media/demo.png",
                                  human=fixtures.human_evidence(complete=False))
    outcome = loc.evaluate_record(_spec(record), line=4, config=_config(),
                                  event_time="t", boxes=fixtures.DEFAULT_DETECTIONS,
                                  detector_state=AVAILABLE_DETECTOR)
    assert outcome["action"] == "incomplete"
    block = outcome["record"]["local_annotations"]
    assert block["deliverable"] is False
    assert block["label"] == loc.LABEL_INCOMPLETE
    assert block["destination"] == loc.DEST_SAMPLE_QUARANTINE
    assert outcome["entry"]["action"] == loc.ACTION_ISOLATE
    assert outcome["entry"]["label"] == "样本-语义不通"


def test_evaluate_incomplete_when_verified_but_no_required_annotation():
    record = fixtures.make_record("s-5", image="media/demo.png",
                                  human=fixtures.human_evidence(boxes=[]))
    outcome = loc.evaluate_record(_spec(record), line=5, config=_config(),
                                  event_time="t", boxes=fixtures.DEFAULT_DETECTIONS,
                                  detector_state=AVAILABLE_DETECTOR)
    assert outcome["action"] == "incomplete"
    assert outcome["reason"] == loc.REASON_ANNOTATION_INCOMPLETE


def test_evaluate_not_applicable_for_text_only():
    record = fixtures.make_record("s-6", modality=["text"], description="纯文字")
    outcome = loc.evaluate_record(_spec(record), line=6, config=_config(),
                                  event_time="t", boxes=None,
                                  detector_state=DEGRADED_DETECTOR)
    assert outcome["action"] == "not_applicable"
    assert outcome["reason"] == loc.REASON_NOT_APPLICABLE_MODALITY
    assert outcome["record"]["local_annotations"]["status"] == 1   # [v4.2] int status
    assert outcome["record"]["local_annotations"]["process_status"] == "not_applicable"
    assert outcome["record"]["local_annotations"]["deliverable"] is False


def test_evaluate_not_applicable_when_whole_alignment_negative():
    record = fixtures.make_record("s-7", image="media/demo.png",
                                  alignment={"whole": {"status": "not_aligned"}})
    outcome = loc.evaluate_record(_spec(record), line=7, config=_config(),
                                  event_time="t", boxes=fixtures.DEFAULT_DETECTIONS,
                                  detector_state=AVAILABLE_DETECTOR)
    assert outcome["action"] == "not_applicable"
    assert outcome["reason"] == loc.REASON_WHOLE_NOT_ALIGNED


def test_evaluate_pending_when_detector_degraded_and_no_fabricated_boxes():
    record = fixtures.make_record("s-8", image="media/demo.png")
    outcome = loc.evaluate_record(_spec(record), line=8, config=_config(),
                                  event_time="t", boxes=None,
                                  detector_state=DEGRADED_DETECTOR)
    assert outcome["action"] == "pending"
    block = outcome["record"]["local_annotations"]
    assert block["candidate_count"] == 0
    assert block["boxes"] == []
    assert block["prefilled"]["box_count"] == 0
    assert block["detector"]["degraded"] is True
    assert block["reason_code"] == loc.REASON_DETECTOR_UNAVAILABLE
    assert block["deliverable"] is False


def test_evaluate_audio_video_prefill_uses_transcript():
    record = fixtures.make_record(
        "s-9", modality=["audio", "text"], description="设备运转声音",
        transcript=fixtures.transcript_payload())
    outcome = loc.evaluate_record(_spec(record), line=9, config=_config(),
                                  event_time="t", boxes=None,
                                  detector_state=DEGRADED_DETECTOR)
    block = outcome["record"]["local_annotations"]
    assert outcome["action"] == "pending"
    assert block["time_ranges"] == []
    assert block["prefilled"]["time_range_count"] == 2
    assert block["prefilled"]["time_ranges"][0]["source"] == "transcript"
    assert block["prefilled"]["time_ranges"][0]["start_sec"] == 0.0
    assert block["reason_code"] == loc.REASON_ANNOTATION_PENDING


def test_evaluate_audio_confirmed_by_human_time_ranges():
    record = fixtures.make_record(
        "s-10", modality=["audio", "text"], description="设备运转声音",
        transcript=fixtures.transcript_payload(),
        human=fixtures.human_evidence(time_ranges=[fixtures.human_range()]))
    outcome = loc.evaluate_record(_spec(record), line=10, config=_config(),
                                  event_time="t", boxes=None,
                                  detector_state=DEGRADED_DETECTOR)
    block = outcome["record"]["local_annotations"]
    assert outcome["action"] == "confirm"
    assert block["confirmed_count"] == 1
    assert block["time_ranges"][0]["source"] == "human"
    assert block["prefilled"]["time_range_count"] == 2
    assert block["deliverable"] is True


def test_evaluate_pending_when_no_transcript_for_av():
    record = fixtures.make_record("s-11", modality=["audio", "text"], description="声音")
    outcome = loc.evaluate_record(_spec(record), line=11, config=_config(),
                                  event_time="t", boxes=None,
                                  detector_state=DEGRADED_DETECTOR)
    assert outcome["action"] == "pending"
    assert outcome["reason"] == loc.REASON_NO_TRANSCRIPT


def test_evaluate_does_not_map_words_to_regions():
    """表1 明确要求「不做词-区域一一对应」：输出不得包含词到框的映射字段。"""
    record = fixtures.make_record("s-12", image="media/demo.png",
                                  human=fixtures.human_evidence(boxes=[fixtures.human_box()]))
    outcome = loc.evaluate_record(_spec(record), line=12, config=_config(),
                                  event_time="t", boxes=fixtures.DEFAULT_DETECTIONS,
                                  detector_state=AVAILABLE_DETECTOR)
    block = outcome["record"]["local_annotations"]
    assert block["annotation_type"] == "local-to-whole"
    assert "word_box_mapping" not in block
    assert all("words" not in item for item in block["boxes"])
    assert block["algorithm"].startswith("yolov8n_general_prefill_v1")


def test_require_whole_aligned_false_keeps_record():
    record = fixtures.make_record("s-13", image="media/demo.png",
                                  alignment={"whole": {"status": "not_aligned"}})
    config = _config(require_whole_aligned=False)
    outcome = loc.evaluate_record(_spec(record), line=13, config=config,
                                  event_time="t", boxes=fixtures.DEFAULT_DETECTIONS,
                                  detector_state=AVAILABLE_DETECTOR)
    assert outcome["action"] == "pending"


def test_apply_skipped_only_writes_namespace():
    record = fixtures.make_record("s-14", image="media/demo.png")
    loc.apply_skipped(record)
    # [v4.2] status 双字段：int status（skipped=1 正常流程态）+ process_status
    assert record["processing"]["17"]["status"] == 1
    assert record["processing"]["17"]["process_status"] == "skipped"
    assert record["processing"]["17"]["reason_code"] == loc.REASON_ROUTE_DISABLED
    assert "local_annotations" not in record


def test_make_ledger_entry_carries_table1_fields():
    record = fixtures.make_record("s-15", batch_id="batch-a", metadata={"retry_count": 2})
    entry = loc.make_ledger_entry(line=3, reason="x", label="y", action="keep",
                                  destination="sample_pool", event_time="t",
                                  sample_id="s-15", record=record)
    assert entry["label"] == "y"
    assert entry["stage"] == "S3"
    assert entry["operator"] == 17
    assert entry["operator_name"] == loc.OPERATOR
    assert entry["batch_id"] == "batch-a"
    assert entry["retry_count"] == 2
    assert entry["sample_id"] == "s-15"
    assert entry["line_number"] == 3


def test_resolve_input_spec_reports_missing_media_reference():
    record = fixtures.make_record("s-16", modality=["image", "text"], description="有描述")
    spec = loc.resolve_input_spec(record, [])
    assert spec["images"] == []
    assert spec["missing_image_reference"] is True


def test_resolve_input_spec_rejects_bad_media_path():
    record = fixtures.make_record("s-17", image="../escape.png")
    with pytest.raises(loc.LocalizationError):
        loc.resolve_input_spec(record, [])


# --------------------------------------------------------------------------- #
# M08 时间索引（规范 §4.17：音视频引用 M08 时间索引）
# --------------------------------------------------------------------------- #

def test_resolve_transcript_reads_m08_time_index():
    record = {"sample_id": "s-m08",
              "time_index": [{"index": 0, "start": 0.0, "end": 1.5, "text": "第一段"},
                             {"index": 1, "start": 2.0, "end": 3.5, "text": "第二段"}]}
    segments, source, ref = loc.resolve_transcript(record, [])
    assert source == "m08_time_index"
    assert ref is None
    assert [item["start_sec"] for item in segments] == [0.0, 2.0]
    assert [item["end_sec"] for item in segments] == [1.5, 3.5]
    ranges = loc.build_time_ranges(segments, max_time_ranges=10, max_text_chars=50)
    assert len(ranges) == 2 and ranges[0]["source"] == "transcript"


def test_resolve_transcript_prefers_m08_index_over_inline():
    record = {"sample_id": "s-both",
              "time_index": [{"start": 0.0, "end": 1.0, "text": "索引"}],
              "transcript": [{"start": 0.0, "end": 9.0, "text": "内联"}]}
    segments, source, _ = loc.resolve_transcript(record, [])
    assert source == "m08_time_index"
    assert segments[0]["end_sec"] == 1.0


def test_resolve_transcript_falls_back_when_time_index_empty():
    record = {"sample_id": "s-empty-index", "time_index": [],
              "transcript": [{"start": 0.0, "end": 1.0, "text": "内联"}]}
    segments, source, _ = loc.resolve_transcript(record, [])
    assert source == "inline"
    assert len(segments) == 1


def test_resolve_transcript_without_any_source_is_empty():
    assert loc.resolve_transcript({"sample_id": "s-none"}, []) == ([], None, None)


def test_resolve_transcript_falls_back_to_m08_visual_index():
    """无声视频：M08 语音时间索引为空时，退回视觉时间索引（来源显式标注）。"""
    record = {"sample_id": "s-silent",
              "time_index": [],
              "visual_time_index": [
                  {"index": 0, "start": 0.0, "end": 6.4, "text": None,
                   "segment_path": None, "source": "visual_segment"}]}
    segments, source, ref = loc.resolve_transcript(record, [])
    assert source == "m08_visual_index"
    assert ref is None
    assert [item["start_sec"] for item in segments] == [0.0]
    assert [item["end_sec"] for item in segments] == [6.4]
    ranges = loc.build_time_ranges(segments, max_time_ranges=10, max_text_chars=50,
                                   source="visual_segment")
    assert ranges[0]["source"] == "visual_segment"
    assert ranges[0]["text_excerpt"] is None


def test_resolve_transcript_prefers_visual_index_over_inline():
    record = {"sample_id": "s-silent-both",
              "visual_time_index": [{"start": 0.0, "end": 2.0}],
              "transcript": [{"start": 0.0, "end": 9.0, "text": "内联"}]}
    segments, source, _ = loc.resolve_transcript(record, [])
    assert source == "m08_visual_index"
    assert segments[0]["end_sec"] == 2.0


def test_evaluate_silent_video_prefills_from_visual_index():
    """端到端：视觉时间索引可预填时间段，但仍需人工确认才可交付。"""
    record = fixtures.make_record(
        "s-silent-eval", modality=["video", "text"],
        description="画面中有一名操作员在检查设备",
        video="media/silent.mp4",
        visual_time_index=[{"index": 0, "start": 0.0, "end": 6.4,
                            "text": None, "segment_path": None,
                            "source": "visual_segment"}])
    outcome = loc.evaluate_record(_spec(record), line=11, config=_config(),
                                  event_time="t", boxes=None,
                                  detector_state=DEGRADED_DETECTOR)
    block = outcome["record"]["local_annotations"]
    assert outcome["action"] == "pending"
    assert block["reason_code"] == loc.REASON_ANNOTATION_PENDING
    assert block["prefilled"]["time_range_count"] == 1
    assert block["prefilled"]["time_ranges"][0]["source"] == "visual_segment"
    assert block["transcript_source"] == "m08_visual_index"
    assert block["deliverable"] is False


# --------------------------------------------------------------------------- #
# 无图视频：关键帧检测预填（v1.0.1）
# --------------------------------------------------------------------------- #

def _video_with_frames(sample_id: str = "s-vid-frames", **kwargs) -> dict:
    record = fixtures.make_record(sample_id, modality=["video", "text"],
                                  description="画面中有一名操作员在检查设备",
                                  video="media/vid/silent.mp4", **kwargs)
    record["video"]["frames"] = [
        {"path": "media/vid/frame_000001.jpg", "timestamp": 0.0, "frame_index": 0},
        {"path": "media/vid/frame_000002.jpg", "timestamp": 1.0, "frame_index": 1},
        {"path": "media/vid/silent.mp4", "timestamp": 2.0, "frame_index": 2},
    ]
    return record


def test_video_frame_refs_prefer_top_level_frames_and_filter_non_images():
    record = _video_with_frames()
    frames = loc.video_frame_refs(record)
    assert [item["path"] for item in frames] == ["media/vid/frame_000001.jpg",
                                                 "media/vid/frame_000002.jpg"]
    assert [item["timestamp"] for item in frames] == [0.0, 1.0]
    assert loc.video_frame_refs({"sample_id": "x"}) == []


def test_video_frame_refs_fall_back_to_segment_frames_and_dedupe():
    record = {"sample_id": "s-seg-frames", "video": {
        "path": "media/vid/silent.mp4",
        "segments": [{"start": 0.0, "end": 2.0, "frames": [
            {"path": "media/vid/f2.jpg", "timestamp": 1.0},
            {"path": "media/vid/f1.jpg", "timestamp": 0.0},
            {"path": "media/vid/f1.jpg", "timestamp": 0.0},
        ]}]}}
    frames = loc.video_frame_refs(record)
    assert [item["path"] for item in frames] == ["media/vid/f1.jpg", "media/vid/f2.jpg"]


def test_evaluate_video_frames_prefill_boxes_with_frame_path():
    """视频帧框 = 通用类别 ∩ 描述；框带 frame_path，仍判待人工、不可交付。"""
    record = _video_with_frames()
    detections = [dict(fixtures.DEFAULT_DETECTIONS[0],
                       frame_path="media/vid/frame_000001.jpg", frame_timestamp=0.0)]
    outcome = loc.evaluate_record(_spec(record), line=12, config=_config(),
                                  event_time="t", boxes=detections,
                                  detector_state=AVAILABLE_DETECTOR)
    block = outcome["record"]["local_annotations"]
    assert outcome["action"] == "pending"
    assert block["reason_code"] == loc.REASON_ANNOTATION_PENDING
    assert block["prefilled"]["box_count"] == 1
    assert block["frame_count"] == 2
    assert block["frame_scoped_boxes"] == 1
    assert block["deliverable"] is False and block["human_verified"] is False

    downstream = record["processing"]["17"]
    assert downstream["boxes_source"] == "yolov8n_frame_prefill"
    assert downstream["regions"][0]["frame_path"] == "media/vid/frame_000001.jpg"
    assert [item["path"] for item in downstream["video_frames"]] == [
        "media/vid/frame_000001.jpg", "media/vid/frame_000002.jpg"]


def test_evaluate_video_frame_detector_unavailable_reason():
    record = _video_with_frames()
    outcome = loc.evaluate_record(_spec(record), line=13, config=_config(),
                                  event_time="t", boxes=None,
                                  detector_state=DEGRADED_DETECTOR)
    block = outcome["record"]["local_annotations"]
    assert block["reason_code"] == loc.REASON_DETECTOR_UNAVAILABLE
    assert block["prefilled"]["box_count"] == 0


def test_build_time_ranges_carries_visual_frame_source():
    index = [{"index": 0, "start": 0.0, "end": 1.0, "text": None,
              "segment_path": None, "source": "visual_frame", "granularity": "frame",
              "frame_path": "media/vid/frame_000001.jpg", "frame_index": 0,
              "timestamp": 0.0},
             {"index": 1, "start": 1.0, "end": 2.0, "text": None,
              "segment_path": None, "source": "visual_frame", "granularity": "frame",
              "frame_path": "media/vid/frame_000002.jpg", "frame_index": 1,
              "timestamp": 1.0}]
    record = _video_with_frames(visual_time_index=index)
    segments, source, _ = loc.resolve_transcript(record, [])
    assert source == "m08_visual_index"
    ranges = loc.build_time_ranges(segments, max_time_ranges=10, max_text_chars=50,
                                   source="visual_segment")
    assert [item["source"] for item in ranges] == ["visual_frame", "visual_frame"]
    assert [item["frame_path"] for item in ranges] == [
        "media/vid/frame_000001.jpg", "media/vid/frame_000002.jpg"]
    assert [item["start_sec"] for item in ranges] == [0.0, 1.0]
