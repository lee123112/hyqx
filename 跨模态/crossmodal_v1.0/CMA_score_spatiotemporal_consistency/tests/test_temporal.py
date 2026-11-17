"""业务规则单测：线性插值校准、时间轴解析、判定分支与台账字段。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import fixtures
from utils import temporal
from utils.schemas import OperatorConfig


def _config(**overrides) -> OperatorConfig:
    payload = {"enabled": True}
    payload.update(overrides)
    return OperatorConfig.model_validate(payload)


# --------------------------------------------------------------------------- #
# 数值解析
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [(0, 0.0), (1.5, 1.5), ("2.25", 2.25), (" 3 ", 3.0)])
def test_to_seconds_valid(value, expected):
    assert temporal.to_seconds(value, "t") == expected


@pytest.mark.parametrize("value", [True, False, None, "abc", [], {}, float("nan"),
                                   float("inf"), float("-inf"), -1, "-0.5"])
def test_to_seconds_rejects_invalid(value):
    with pytest.raises(temporal.TimelineError):
        temporal.to_seconds(value, "t")


# --------------------------------------------------------------------------- #
# 时间轴
# --------------------------------------------------------------------------- #

def test_normalize_timeline_aliases_and_sorting():
    entries = [
        {"segment_id": "later", "begin_sec": 2.0, "end_sec": 2.9},
        {"segment_id": "earlier", "start": 0.5, "end": 1.2},
    ]
    timeline = temporal.normalize_timeline(entries, "audio")
    assert [point["key"] for point in timeline.points] == ["earlier", "later"]
    assert timeline.get("later", binding_id="b")["time"] == 2.0
    assert timeline.get(1, binding_id="b")["key"] == "later"
    assert timeline.get("#0", binding_id="b")["key"] == "earlier"
    with pytest.raises(temporal.TimelineError):
        timeline.get(2, binding_id="b")


def test_normalize_timeline_accepts_bare_numbers():
    timeline = temporal.normalize_timeline([0.0, 1.0], "video")
    assert timeline.get("#1", binding_id="b")["time"] == 1.0


@pytest.mark.parametrize("entries", [None, []])
def test_normalize_timeline_empty(entries):
    assert temporal.normalize_timeline(entries, "video") is None


def test_normalize_timeline_rejects_bad_shapes():
    with pytest.raises(temporal.TimelineError):
        temporal.normalize_timeline("not-a-list", "video")
    with pytest.raises(temporal.TimelineError):
        temporal.normalize_timeline({}, "video")
    with pytest.raises(temporal.TimelineError):
        temporal.normalize_timeline([{"id": "f0"}], "video")
    with pytest.raises(temporal.TimelineError):
        temporal.normalize_timeline([{"start_sec": 2.0, "end_sec": 1.0}], "audio")
    with pytest.raises(temporal.TimelineError):
        temporal.normalize_timeline([None], "audio")


def test_timeline_get_unknown_reference():
    timeline = temporal.normalize_timeline([{"id": "a0", "start_sec": 0.0}], "audio")
    with pytest.raises(temporal.TimelineError):
        timeline.get("missing", binding_id="b0")
    with pytest.raises(temporal.TimelineError):
        timeline.get(5, binding_id="b0")
    with pytest.raises(temporal.TimelineError):
        timeline.get(True, binding_id="b0")


# --------------------------------------------------------------------------- #
# 线性插值
# --------------------------------------------------------------------------- #

def test_time_map_not_applied_without_enough_anchors():
    time_map = temporal.build_time_map([], method="piecewise_linear", min_anchors=2)
    assert time_map.applied is False
    assert time_map.method == "identity"
    assert time_map.map(3.7) == 3.7
    described = time_map.describe()
    assert described["applied"] is False
    assert described["method"] == "identity"
    assert described["type"] == "linear"


def test_time_map_piecewise_matches_anchors_and_interpolates():
    time_map = temporal.build_time_map(
        [{"video_sec": 0.0, "source_sec": 0.0}, {"video_sec": 2.0, "source_sec": 2.0},
         {"video_sec": 4.0, "source_sec": 8.0}],
        method="piecewise_linear", min_anchors=2)
    assert time_map.applied is True
    assert time_map.map(0.0) == 0.0
    assert time_map.map(2.0) == 2.0
    assert time_map.map(8.0) == 4.0
    # 区间 [2,8] 中点的线性插值
    assert time_map.map(5.0) == pytest.approx(3.0)
    # 区间外按边界段斜率外推
    assert time_map.map(-2.0) == pytest.approx(-2.0)
    assert time_map.map(10.0) == pytest.approx(4.0 + (10.0 - 8.0) * (4.0 - 2.0) / (8.0 - 2.0))
    assert time_map.max_residual_sec == 0.0


def test_time_map_global_linear_least_squares():
    time_map = temporal.build_time_map(
        [{"video_sec": 0.0, "source_sec": 1.0}, {"video_sec": 2.0, "source_sec": 3.0},
         {"video_sec": 4.0, "source_sec": 5.0}],
        method="global_linear", min_anchors=2)
    assert time_map.method == "global_linear"
    assert time_map.map(3.0) == pytest.approx(2.0)
    assert time_map.max_residual_sec == pytest.approx(0.0)


def test_time_map_global_linear_handles_scale():
    time_map = temporal.build_time_map(
        [{"video_sec": 0.0, "source_sec": 0.0}, {"video_sec": 4.0, "source_sec": 2.0}],
        method="global_linear", min_anchors=2)
    assert time_map.map(1.0) == pytest.approx(2.0)


def test_build_time_map_alias_and_errors():
    time_map = temporal.build_time_map(
        [{"video_sec": 0.0, "audio_sec": 0.5}, {"video_sec": 2.0, "audio_sec": 2.5}],
        method="piecewise_linear", min_anchors=2)
    assert time_map.map(0.5) == pytest.approx(0.0)
    with pytest.raises(temporal.TimelineError):
        temporal.build_time_map(["bad"], method="piecewise_linear", min_anchors=2)
    with pytest.raises(temporal.TimelineError):
        temporal.build_time_map([{"video_sec": 1.0}], method="piecewise_linear", min_anchors=2)
    with pytest.raises(temporal.TimelineError):
        temporal.build_time_map([{"video_sec": 1.0, "source_sec": "x"}],
                                method="piecewise_linear", min_anchors=2)


# --------------------------------------------------------------------------- #
# 记录解析
# --------------------------------------------------------------------------- #

def test_record_sample_id_variants():
    assert temporal.record_sample_id({"sample_id": " a "}) == "a"
    assert temporal.record_sample_id({"sampleId": "b"}) == "b"
    assert temporal.record_sample_id({"sample_ID": "c"}) == "c"
    assert temporal.record_sample_id({"id": 7}) == "7"
    assert temporal.record_sample_id({"sample_id": "  "}) is None
    assert temporal.record_sample_id({}) is None


def test_resolve_spec_inline_and_media_refs(tmp_path):
    record = fixtures.make_record("s", media=["media/a.json", {"path": "media/b.mp4"}])
    spec = temporal.resolve_spec(record, [tmp_path])
    assert spec["sample_id"] == "s"
    assert all(spec["timelines"][kind] is not None for kind in temporal.TIMELINE_KINDS)
    assert spec["media_refs"] == ["media/a.json", "media/b.mp4"]


def test_resolve_spec_scattered_lists(tmp_path):
    record = {
        "sample_id": "s",
        "video_timeline": [{"id": "f0", "timestamp_sec": 0.0}],
        "audio_timeline": [{"id": "a0", "start_sec": 0.1}],
        "subtitle_timeline": [{"id": "t0", "start_sec": 0.05}],
        "temporal_bindings": [{"binding_id": "b0", "video": "f0", "audio": "a0", "text": "t0"}],
    }
    spec = temporal.resolve_spec(record, [tmp_path])
    assert all(spec["timelines"][kind] is not None for kind in temporal.TIMELINE_KINDS)


def test_resolve_spec_media_refs_are_not_timelines(tmp_path):
    """顶层 ``video`` 是媒体引用对象时，不能被当成时间轴。"""
    record = fixtures.make_record("s", anchor_offset=None, with_bindings=False,
                                  with_timeline=False)
    record["video"] = {"path": "media/clip.mp4"}
    record["audio"] = {"path": "media/clip.wav"}
    spec = temporal.resolve_spec(record, [tmp_path])
    assert spec["timelines"]["video"] is None
    assert "media/clip.mp4" in spec["media_refs"]


def test_resolve_spec_sidecar_ok(tmp_path):
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "tl.json").write_text(
        __import__("json").dumps({
            "video": [{"id": "f0", "timestamp_sec": 0.0}],
            "audio": [{"id": "a0", "start_sec": 0.0}],
            "text": [{"id": "t0", "start_sec": 0.0}],
            "temporal_bindings": [{"binding_id": "b0", "video": "f0",
                                   "audio": "a0", "text": "t0"}],
        }, ensure_ascii=False), encoding="utf-8")
    record = {"sample_id": "s", "timeline_file": "media/tl.json"}
    spec = temporal.resolve_spec(record, [tmp_path])
    assert spec["bindings"]
    assert "media/tl.json" in spec["media_refs"]


def test_resolve_spec_sidecar_missing_and_broken(tmp_path):
    with pytest.raises(temporal.TimelineUnreadable):
        temporal.resolve_spec({"sample_id": "s", "timeline_file": "media/none.json"},
                              [tmp_path])
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "bad.json").write_bytes(b"{oops")
    with pytest.raises(temporal.TimelineUnreadable):
        temporal.resolve_spec({"sample_id": "s", "timeline_file": "media/bad.json"},
                              [tmp_path])


def test_resolve_spec_rejects_bad_bindings_shape(tmp_path):
    record = fixtures.make_record("s")
    record["temporal_bindings"] = {"b0": "f0"}
    with pytest.raises(temporal.TimelineError):
        temporal.resolve_spec(record, [tmp_path])
    record["temporal_bindings"] = []
    record["temporal_anchors"] = "x"
    with pytest.raises(temporal.TimelineError):
        temporal.resolve_spec(record, [tmp_path])


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #

def _spec(record, tmp_path=Path(".")):
    return temporal.resolve_spec(record, [tmp_path])


def test_evaluate_passed_uses_real_deltas(tmp_path):
    record = fixtures.make_record("s", video=(0.0, 1.0, 2.0),
                                  audio=(0.05, 1.02, 2.03), text=(0.02, 1.0, 2.0))
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="2026-10-06T10:00:00+08:00")
    assert outcome["action"] == "pass"
    assert outcome["calibrated"] is True
    block = outcome["record"]["temporal_alignment"]
    # [v4.2] status 双字段：int status=1（通过）+ process_status 保留原字符串
    assert block["status"] == 1
    assert block["process_status"] == "passed"
    assert block["passed"] is True
    assert block["max_delta_sec"] == pytest.approx(0.05)
    assert block["avg_delta_sec"] == pytest.approx(0.033333)
    assert block["threshold_sec"] == 0.5
    assert block["binding_count"] == 3
    assert block["label"] == temporal.LABEL_PASS
    assert block["action"] == temporal.ACTION_KEEP
    assert block["destination"] == temporal.DEST_SAMPLE_POOL
    assert block["evidence"][0]["binding_id"] == "b0"
    assert block["reason_code"] == "within_threshold"


def test_evaluate_boundary_equal_to_threshold_passes(tmp_path):
    record = fixtures.make_record("s", video=(0.0, 1.0, 2.0), audio=(0.5, 1.5, 2.5),
                                  text=(0.0, 1.0, 2.0))
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["record"]["temporal_alignment"]["max_delta_sec"] == 0.5
    assert outcome["action"] == "pass"


def test_evaluate_boundary_just_over_threshold_isolates(tmp_path):
    """真实差值 0.5000004s 不得被 round(6) 舍成 0.5 而误判通过。"""
    record = fixtures.make_record("s", video=(0.0,), audio=(0.5000004,), text=(0.0,),
                                  anchor_offset=None)
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "isolate"
    block = outcome["entry"]["record"]["temporal_alignment"]
    assert block["passed"] is False
    # 展示字段仍保留 6 位小数，判定用未取整值
    assert block["max_delta_sec"] == pytest.approx(0.5)


def test_evaluate_boundary_just_under_threshold_passes(tmp_path):
    record = fixtures.make_record("s", video=(0.0,), audio=(0.4999996,), text=(0.0,),
                                  anchor_offset=None)
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "pass"


def test_evaluate_over_threshold_isolates(tmp_path):
    record = fixtures.make_record("s", video=(0.0, 1.0, 2.0), audio=(2.0, 3.0, 4.0),
                                  text=(0.0, 1.0, 2.0), batch_id="batch-x")
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=9,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "isolate"
    entry = outcome["entry"]
    assert entry["label"] == temporal.LABEL_FAIL
    assert entry["action"] == temporal.ACTION_ISOLATE
    assert entry["destination"] == temporal.DEST_SAMPLE_QUARANTINE
    assert entry["line_number"] == 9
    assert entry["batch_id"] == "batch-x"
    assert entry["sample_id"] == "s"
    assert entry["retry_count"] == 0
    assert entry["stage"] == temporal.STAGE_ID
    assert entry["operator"] == temporal.FORMAL_OPERATOR_NUMBER
    assert entry["record"]["temporal_alignment"]["status"] == 0
    assert entry["record"]["temporal_alignment"]["process_status"] == "failed"
    assert entry["record"]["temporal_alignment"]["passed"] is False


def test_evaluate_calibration_changes_verdict(tmp_path):
    """时钟整体偏移 0.9s：未校准会失败，线性插值校准后通过。"""
    record = fixtures.make_record("s", video=(0.0, 1.0, 2.0), audio=(0.9, 1.9, 2.9),
                                  text=(0.88, 1.88, 2.88), anchor_offset=0.9)
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "pass"
    assert outcome["record"]["temporal_alignment"]["max_delta_sec"] == pytest.approx(0.02)

    uncalibrated = dict(record)
    uncalibrated.pop("temporal_anchors")
    outcome2 = temporal.evaluate_record(_spec(uncalibrated, tmp_path), line=1,
                                        config=_config(), event_time="now")
    assert outcome2["action"] == "isolate"
    assert outcome2["calibrated"] is False
    assert outcome2["entry"]["record"]["temporal_alignment"]["interpolation"]["applied"] \
        is False


def test_evaluate_uncalibrated_flags_applied_false(tmp_path):
    record = fixtures.make_record("s", video=(0.0, 1.0), audio=(0.1, 1.1), text=(0.1, 1.1),
                                  anchor_offset=None)
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "pass"
    assert outcome["record"]["temporal_alignment"]["interpolation"]["applied"] is False
    assert outcome["calibrated"] is False


@pytest.mark.parametrize("dropped,reason", [
    ("video", temporal.REASON_MISSING_VIDEO_TIMELINE),
    ("audio", temporal.REASON_MISSING_AUDIO_TIMELINE),
    ("text", temporal.REASON_MISSING_TEXT_TIMELINE),
])
def test_evaluate_not_applicable_missing_timeline(tmp_path, dropped, reason):
    record = fixtures.make_record("s")
    del record["timeline"][dropped]
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "not_applicable"
    block = outcome["record"]["temporal_alignment"]
    assert block["status"] == 1
    assert block["process_status"] == "not_applicable"
    assert block["passed"] is None          # 绝不能写成“通过”
    assert block["reason_code"] == reason
    assert block["label"] == temporal.LABEL_NOT_APPLICABLE
    assert block["action"] == temporal.ACTION_KEEP
    assert block["destination"] == temporal.DEST_SAMPLE_POOL
    assert block["max_delta_sec"] is None


def test_evaluate_image_text_not_applicable(tmp_path):
    record = {"sample_id": "s", "modality": ["image", "text"]}
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "not_applicable"
    assert outcome["reason"] == temporal.REASON_NO_TIMESTAMP
    assert outcome["record"]["temporal_alignment"]["passed"] is None


def test_evaluate_no_binding_not_applicable(tmp_path):
    record = fixtures.make_record("s", with_bindings=False)
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "not_applicable"
    assert outcome["reason"] == temporal.REASON_NO_BINDING


def test_evaluate_invalid_binding_reference(tmp_path):
    record = fixtures.make_record("s")
    record["temporal_bindings"] = [{"binding_id": "b0", "video": "nope", "audio": "a0",
                                    "text": "t0"}]
    with pytest.raises(temporal.TimelineError):
        temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                 config=_config(), event_time="now")


def test_evaluate_binding_must_have_three_refs(tmp_path):
    record = fixtures.make_record("s")
    record["temporal_bindings"] = [{"binding_id": "b0", "video": "f0", "audio": "a0"}]
    with pytest.raises(temporal.TimelineError):
        temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                 config=_config(), event_time="now")
    record["temporal_bindings"] = ["not-a-binding"]
    with pytest.raises(temporal.TimelineError):
        temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                 config=_config(), event_time="now")


def test_evaluate_binding_index_form(tmp_path):
    record = fixtures.make_record("s", video=(0.0, 1.0), audio=(0.01, 1.01),
                                  text=(0.01, 1.01), anchor_offset=None)
    record["temporal_bindings"] = [["f0", "a0", "t0"], [1, 1, 1]]
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["action"] == "pass"
    assert outcome["record"]["temporal_alignment"]["binding_count"] == 2


def test_evaluate_binding_limit(tmp_path):
    record = fixtures.make_record("s", video=(0.0, 1.0), audio=(0.0, 1.0), text=(0.0, 1.0))
    with pytest.raises(temporal.BindingLimitExceeded):
        temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                 config=_config(max_bindings=1), event_time="now")


def test_evaluate_require_calibration_pending(tmp_path):
    record = fixtures.make_record("s", anchor_offset=None)
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=4,
                                       config=_config(require_calibration=True),
                                       event_time="now")
    assert outcome["action"] == "pending"
    entry = outcome["entry"]
    assert entry["label"] == temporal.LABEL_PENDING_CALIBRATION
    assert entry["action"] == temporal.ACTION_WAITING_MANUAL
    assert entry["destination"] == temporal.DEST_PIPELINE_WAIT
    assert entry["line_number"] == 4
    # 待人工校准：既不判通过，也不进样本隔离库
    assert entry["record"]["temporal_alignment"]["passed"] is None
    # [v4.2] 待人工校准：status=1（不是不通过）+ process_status 保留原流程态
    assert entry["record"]["temporal_alignment"]["status"] == 1
    assert entry["record"]["temporal_alignment"]["process_status"] == \
        temporal.STATUS_PENDING_CALIBRATION


def test_evaluate_evidence_truncated(tmp_path):
    count = 5
    record = fixtures.make_record("s", video=tuple(float(i) for i in range(count)),
                                  audio=tuple(float(i) + 0.01 for i in range(count)),
                                  text=tuple(float(i) + 0.01 for i in range(count)),
                                  anchor_offset=None)
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(max_evidence_bindings=2),
                                       event_time="now")
    block = outcome["record"]["temporal_alignment"]
    assert block["binding_count"] == count
    assert len(block["evidence"]) == 2
    assert block["evidence_truncated"] is True


def test_apply_block_preserves_upstream_fields(tmp_path):
    record = fixtures.make_record("s", processing={"12": {"status": "passed"}},
                                  alignment={"overall": "aligned"}, license="可训练")
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="2026-10-06T10:00:00+08:00")
    augmented = outcome["record"]
    assert augmented["processing"]["12"] == {"status": "passed"}
    assert augmented["processing"]["15"]["operator"] == temporal.OPERATOR
    assert augmented["processing"]["15"]["status"] == 1
    assert augmented["processing"]["15"]["process_status"] == "passed"
    assert augmented["alignment"] == {"overall": "aligned"}
    assert augmented["license"] == "可训练"
    assert augmented["operator_15"]["operator_number"] == 15
    assert augmented["operator_15"]["evidence"]["interpolation"]["applied"] is True
    assert augmented["operator_15"]["evidence"]["threshold_sec"] == 0.5
    assert augmented["operator_15"]["source_reference"] == temporal.SOURCE_REFERENCE


def test_apply_block_handles_non_dict_processing(tmp_path):
    record = fixtures.make_record("s")
    record["processing"] = "legacy-string"
    outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                       config=_config(), event_time="now")
    assert outcome["record"]["processing"]["15"]["status"] == 1
    assert outcome["record"]["processing"]["15"]["process_status"] == "passed"


def test_apply_skipped_marks_route_disabled():
    record = {"sample_id": "s"}
    temporal.apply_skipped(record)
    node = record["processing"]["15"]
    assert node["status"] == 1
    assert node["process_status"] == temporal.STATUS_SKIPPED
    assert node["reason_code"] == temporal.REASON_ROUTE_DISABLED
    assert "temporal_alignment" not in record


# ------------------------------------------------ 0.3/0.5/0.7s 中间漂移阈值标定（2026-11-05）

def test_mid_range_drift_calibration_curve(tmp_path):
    """与 03_m15_drift_calibration.jsonl 同构：0.3/0.5s 通过，0.7s 隔离。

    视频固定 0.5/2.5s（二进制可精确表示），音频与字幕整体后移 drift 秒，
    使 0.5s 边界命中真实差值恰为 0.5 的情形（比较为 ``<=``，闭区间通过）。
    """
    expectation = {
        0.30: ("pass", "within_threshold", True),
        0.50: ("pass", "within_threshold", True),
        0.70: ("isolate", "max_delta_exceeded", False),
    }
    for drift, (action, reason, passed) in expectation.items():
        shifted = (round(0.5 + drift, 6), round(2.5 + drift, 6))
        record = fixtures.make_record(
            "drift-%03d" % round(drift * 100),
            video=(0.5, 2.5), audio=shifted, text=shifted,
            anchor_offset=None)
        outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                           config=_config(), event_time="now")
        assert outcome["action"] == action
        block = ((outcome.get("record") or outcome["entry"]["record"])
                 ["temporal_alignment"])
        assert block["threshold_sec"] == 0.5
        assert block["max_delta_sec"] == pytest.approx(drift, abs=1e-9)
        assert block["passed"] is passed
        assert block["reason_code"] == reason


def test_jitter_within_one_and_two_frames_passes(tmp_path):
    """帧级抖动（24fps，1 帧≈41.7ms）不得误杀：±1/±2 帧全部通过。

    对应 `04_m15_jitter_mismatch.jsonl` 的 16 条 jitter 样本：视频/音频固定，
    只把字幕起止按 ±1/±2 帧平移；max_delta 应为 1/24、2/24 秒。
    """
    frame = 1.0 / 24.0
    for frames in (-2, -1, 1, 2):
        jitter = frames * frame
        record = fixtures.make_record(
            "jitter-%+d" % frames,
            video=(0.5, 2.5), audio=(0.5, 2.5),
            text=(round(0.5 + jitter, 6), round(2.5 + jitter, 6)),
            anchor_offset=None)
        outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                           config=_config(), event_time="now")
        assert outcome["action"] == "pass"
        block = ((outcome.get("record") or outcome["entry"]["record"])
                 ["temporal_alignment"])
        assert block["threshold_sec"] == 0.5
        assert block["max_delta_sec"] == pytest.approx(abs(jitter), abs=1e-6)
        assert block["passed"] is True
        assert block["reason_code"] == "within_threshold"


def test_segment_mismatch_isolates(tmp_path):
    """ subtitle/音频错位一个分段（s0↔s1）→ max_delta=2.0s，超阈值隔离。

    对应 `04_m15_jitter_mismatch.jsonl` 的 8 条 mismatch 样本。
    """
    for kind in ("text", "audio"):
        timeline = {"video": (0.5, 2.5), "audio": (0.5, 2.5), "text": (0.5, 2.5)}
        timeline[kind] = (2.5, 0.5)
        record = fixtures.make_record("mismatch-%s" % kind, video=timeline["video"],
                                      audio=timeline["audio"], text=timeline["text"],
                                      anchor_offset=None)
        outcome = temporal.evaluate_record(_spec(record, tmp_path), line=1,
                                           config=_config(), event_time="now")
        assert outcome["action"] == "isolate"
        block = ((outcome.get("record") or outcome["entry"]["record"])
                 ["temporal_alignment"])
        assert block["max_delta_sec"] == pytest.approx(2.0, abs=1e-9)
        assert block["passed"] is False
        assert block["reason_code"] == temporal.REASON_MAX_DELTA_EXCEEDED
        assert block["destination"] == temporal.DEST_SAMPLE_QUARANTINE
