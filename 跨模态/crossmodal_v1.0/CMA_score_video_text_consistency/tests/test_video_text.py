"""``utils.video_text`` 业务模块测试：字段解析、分段判定与台账路由。

使用 ``StubEncoder``（明确标注的测试替身）验证分段、均值、阈值与分流；
模型不可用路径只允许显式降级或显式失败，绝不产出分数。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fixtures import (build_input_tree, default_frames, default_media,  # noqa: E402
                      make_config, make_flat_record, make_pair, media_with_unreadable,
                      StubEncoder, video_object)
from utils import frames as frames_mod  # noqa: E402
from utils import video_text as vt  # noqa: E402
from utils.schemas import ConfigError  # noqa: E402


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def prepare(tmp_path, media=None, name="input"):
    """构造输入目录，返回媒体解析根。"""
    base = build_input_tree(tmp_path / name, [], media=media or default_media())
    return [base]


def spec_of(record, media_roots):
    spec = vt.resolve_pair(record)
    spec["record"] = record
    return spec


# --------------------------------------------------------------------------- #
# 多语义段构造（每段 per_segment 帧候选，供整段视频帧预算用例使用）
# --------------------------------------------------------------------------- #

def multi_segment_frames(segment_count: int = 3, per_segment: int = 10):
    return [{"path": "frames/frame_%04d.png" % (index + 1), "timestamp": float(index)}
            for index in range(segment_count * per_segment)]


def multi_segment_specs(segment_count: int = 3, per_segment: int = 10,
                        texts=None):
    texts = list(texts or ["段%d" % (index + 1) for index in range(segment_count)])
    return [{"start": float(index * per_segment),
             "end": float((index + 1) * per_segment),
             "text": texts[index]} for index in range(segment_count)]


def multi_segment_media(segment_count: int = 3, per_segment: int = 10):
    return default_media(["frames/frame_%04d.png" % (index + 1)
                         for index in range(segment_count * per_segment)])


# --------------------------------------------------------------------------- #
# 字段解析
# --------------------------------------------------------------------------- #

def test_resolve_description_prefers_text_content():
    result = vt.resolve_description({"text": {"content": "一只狗在跑"}})
    assert result == {"value": "一只狗在跑", "source": "text.content"}


def test_resolve_description_falls_back_to_metadata_and_processing():
    assert vt.resolve_description(
        {"metadata": {"description": "元数据描述"}})["value"] == "元数据描述"
    assert vt.resolve_description(
        {"processing": {"09": {"description": "上游描述"}}})["source"] == \
        "processing.09.description"


def test_resolve_description_returns_none_when_absent():
    assert vt.resolve_description({}) == {"value": None, "source": None}
    assert vt.resolve_description({"text": {"content": "   "}})["value"] is None


def test_resolve_video_reference_none_without_video():
    assert vt.resolve_video_reference({}) is None
    assert vt.resolve_video_reference({"video": None}) is None
    assert vt.resolve_video_reference({"video": "   "}) is None


def test_resolve_video_reference_accepts_object_and_string():
    obj = vt.resolve_video_reference(
        {"video": {"path": "video/a.mp4", "duration": 3.5,
                   "frames": [{"path": "frames/a.png", "timestamp": 0.0}]}})
    assert obj["path"] == "video/a.mp4"
    assert obj["duration"] == 3.5
    assert [item["path"] for item in obj["frames"]] == ["frames/a.png"]

    text = vt.resolve_video_reference({"video": "video/b.mp4"})
    assert text["path"] == "video/b.mp4"
    assert text["frames"] == []


def test_resolve_pair_picks_video_and_text_sides():
    record = make_pair("pair-01", "描述文本", default_frames(2))
    spec = vt.resolve_pair(record)
    assert spec["mode"] == "pair"
    assert spec["identifier"] == "pair-01"
    assert spec["video"]["path"] == "video/clip.mp4"
    assert spec["description"]["value"] == "描述文本"
    assert len(spec["frames"]) == 2


def test_resolve_pair_supports_flat_record():
    record = make_flat_record("s-01", "扁平描述", default_frames(2))
    spec = vt.resolve_pair(record)
    assert spec["mode"] == "flat"
    assert spec["identifier"] == "s-01"
    assert spec["description"]["value"] == "扁平描述"


def test_resolve_pair_handles_non_dict_record():
    spec = vt.resolve_pair("not-a-record")
    assert spec["identifier"] is None
    assert spec["video"] is None


# --------------------------------------------------------------------------- #
# 流程状态（不隔离）
# --------------------------------------------------------------------------- #

def test_missing_video_is_pending_not_isolated(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-01", "描述", default_frames(2))
    record["video"] = None
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pending"
    assert outcome["reason"] == vt.REASON_NO_VIDEO_TEXT_PAIR
    assert outcome["entry"]["destination"] == vt.DEST_PIPELINE
    assert outcome["entry"]["label"] == vt.LABEL_FLOW_NO_VIDEO_TEXT


def test_missing_frames_is_pending_not_isolated(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-02", "描述", [])
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pending"
    assert outcome["reason"] == vt.REASON_NO_FRAMES
    assert outcome["entry"]["destination"] == vt.DEST_PIPELINE


def test_missing_description_is_pending_not_isolated(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-03", "描述", default_frames(2))
    record["text"] = None
    record["metadata"] = {}
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pending"
    assert outcome["reason"] == vt.REASON_MISSING_DESCRIPTION
    assert outcome["entry"]["label"] == vt.LABEL_FLOW_PENDING_DESCRIPTION


# --------------------------------------------------------------------------- #
# 原始文件问题（原始隔离区）
# --------------------------------------------------------------------------- #

def test_missing_frame_file_goes_to_origin_quarantine(tmp_path):
    roots = prepare(tmp_path)
    frames = default_frames(2)
    frames.append({"path": "frames/not_exist.png", "timestamp": 5.0})
    record = make_flat_record("s-04", "描述", frames)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "origin"
    assert outcome["reason"] == vt.REASON_FRAME_MISSING
    assert outcome["entry"]["label"] == vt.LABEL_ORIGIN_UNPARSABLE
    assert outcome["entry"]["destination"] == vt.DEST_ORIGIN


def test_unsupported_frame_path_goes_to_origin_quarantine(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-05", "描述", [{"path": "../escape.png", "timestamp": 0.0}])
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "origin"
    assert outcome["reason"] == vt.REASON_UNSUPPORTED_MEDIA_PATH
    assert outcome["entry"]["destination"] == vt.DEST_ORIGIN


def test_unreadable_frame_goes_to_origin_quarantine(tmp_path):
    roots = prepare(tmp_path, media=media_with_unreadable(broken=("frame_0001.png",)))
    record = make_flat_record("s-06", "描述", default_frames(2))
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "origin"
    assert outcome["reason"] == vt.REASON_FRAME_UNREADABLE
    assert outcome["entry"]["label"] == vt.LABEL_ORIGIN_BROKEN


# --------------------------------------------------------------------------- #
# 模型不可用：显式失败或显式降级（绝不伪造分数）
# --------------------------------------------------------------------------- #

def test_encoder_unavailable_without_degrade_raises(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-07", "描述", default_frames(2))
    with pytest.raises(vt.DependencyError):
        vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                           config=make_config(allow_degraded=False), encoder=None,
                           event_time="2026-10-06T00:00:00+08:00")


def test_encoder_unavailable_with_degrade_is_pending_and_score_null(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-08", "描述", default_frames(2))
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(allow_degraded=True), encoder=None,
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pending"
    assert outcome["reason"] == vt.REASON_MODEL_UNAVAILABLE
    assert outcome["degraded"] is True
    node = outcome["node"]
    assert node["video_text_score"] is None
    assert node["segment_mean"] is None
    assert node["passed"] is None
    assert node["model"] is None
    assert outcome["entry"]["destination"] == vt.DEST_PIPELINE


def test_encoder_failure_is_score_error_pending(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-09", "描述", default_frames(2))
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(allow_degraded=True),
                                 encoder=StubEncoder(fail=True),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pending"
    assert outcome["reason"] == vt.REASON_SCORE_ERROR
    assert outcome["node"]["video_text_score"] is None


# --------------------------------------------------------------------------- #
# 判定：段均值 / 视文分数 / 最低段分
# --------------------------------------------------------------------------- #

def test_all_segments_pass(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-10", "描述", default_frames(3))
    encoder = StubEncoder(default=0.6)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pass"
    node = outcome["node"]
    assert node["passed"] is True
    assert node["video_text_score"] == pytest.approx(0.6)
    assert node["segment_mean"] == pytest.approx(0.6)
    assert node["min_segment_score"] == pytest.approx(0.6)
    assert node["segment_count"] == 1
    assert node["scored_frames"] == 3
    assert node["label"] == vt.LABEL_SCORE_PASS
    assert node["destination"] == vt.DEST_NEXT
    assert outcome["record"]["processing"]["14"]["status"] == 1
    assert outcome["record"]["processing"]["14"]["process_status"] == "passed"


def test_single_low_segment_fails_whole_sample(tmp_path):
    roots = prepare(tmp_path)
    frames = [{"path": "frames/frame_0001.png", "timestamp": 0.0},
              {"path": "frames/frame_0002.png", "timestamp": 1.0},
              {"path": "frames/frame_0003.png", "timestamp": 2.0},
              {"path": "frames/frame_0003.png", "timestamp": 3.0}]
    segments = [{"start": 0.0, "end": 2.0, "text": "高分段"},
                {"start": 2.0, "end": 4.0, "text": "低分段"}]
    record = make_flat_record("s-11", "整段描述", frames, segments=segments)
    encoder = StubEncoder(scores={"高分段": 0.80, "低分段": 0.10})
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "isolate"
    assert outcome["reason"] == vt.REASON_SEGMENT_BELOW
    assert outcome["entry"]["label"] == vt.LABEL_SAMPLE_SEMANTIC
    assert outcome["entry"]["destination"] == vt.DEST_SAMPLE
    node = outcome["node"]
    assert node["passed"] is False
    assert node["low_score_threshold"] == 0.25
    assert node["segment_count"] == 2
    assert node["min_segment_score"] == pytest.approx(0.10)
    # 每段 2 帧且段内同分：全帧均值恰好等于段均值均值
    frame_scores = [frame["score"] for item in node["segments"] for frame in item["frames"]]
    assert node["video_text_score"] == pytest.approx(
        sum(frame_scores) / len(frame_scores))
    assert node["video_text_score"] == pytest.approx((0.80 + 0.10) / 2.0)
    assert node["frames_used"] == 4
    assert node["frames_within_budget"] is True
    assert [item["segment_mean"] for item in node["segments"]] == \
        [pytest.approx(0.80), pytest.approx(0.10)]
    assert [item["passed"] for item in node["segments"]] == [True, False]
    assert encoder.text_calls == ["高分段", "低分段"]


def test_segment_boundary_equal_to_threshold_passes(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-12", "描述", default_frames(2))
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(default=0.25),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pass"
    assert outcome["node"]["min_segment_score"] == pytest.approx(0.25)


def test_segment_means_are_averaged_over_frames(tmp_path):
    """段均值 = 段内各帧 CLIP 余弦的均值。"""
    roots = prepare(tmp_path)
    frames = [{"path": "frames/frame_0001.png", "timestamp": 0.0},
              {"path": "frames/frame_0002.png", "timestamp": 1.0}]
    record = make_flat_record("s-13", "描述", frames)
    encoder = StubEncoder(default=0.5)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    node = outcome["node"]
    assert node["segments"][0]["frame_count"] == 2
    assert [item["score"] for item in node["segments"][0]["frames"]] == [0.5, 0.5]
    assert node["segments"][0]["segment_mean"] == pytest.approx(0.5)


def test_single_segment_frame_cap_is_eight(tmp_path):
    """单语义段：整段视频合计 8 帧（与旧行为等价），逐帧评分后取均值。"""
    roots = prepare(tmp_path)
    frames = [{"path": "frames/frame_%04d.png" % (index + 1), "timestamp": float(index)}
              for index in range(12)]
    media = default_media(["frames/frame_%04d.png" % (index + 1) for index in range(12)])
    roots = prepare(tmp_path, media=media, name="input_cap")
    record = make_flat_record("s-14", "描述", frames)
    encoder = StubEncoder(default=0.7)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    node = outcome["node"]
    assert node["scored_frames"] == 8
    assert node["frames_used"] == 8
    assert node["max_frames_per_segment"] == 8
    assert node["frames_within_budget"] is True
    assert node["segment_mean"] == pytest.approx(0.7)
    assert len(encoder.image_calls) == 8


def test_config_max_frames_limits_scoring(tmp_path):
    roots = prepare(tmp_path)
    media = default_media(["frames/frame_%04d.png" % (index + 1) for index in range(12)])
    roots = prepare(tmp_path, media=media, name="input_cap2")
    frames = [{"path": "frames/frame_%04d.png" % (index + 1), "timestamp": float(index)}
              for index in range(12)]
    record = make_flat_record("s-15", "描述", frames)
    encoder = StubEncoder(default=0.7)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(max_frames_per_segment=3),
                                 encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["node"]["scored_frames"] == 3
    assert outcome["node"]["frames_used"] == 3
    assert outcome["node"]["max_frames_per_segment"] == 3


# --------------------------------------------------------------------------- #
# 多语义段：整段视频帧预算、段分/视频分与隔离判定
# --------------------------------------------------------------------------- #

def test_multi_segment_uses_whole_video_frame_budget(tmp_path):
    """3 段 ×10 候选、上限 8：总帧数 ≤8，段分为段内帧均值、视频分为全帧均值。"""
    roots = prepare(tmp_path, media=multi_segment_media(), name="input_multi")
    record = make_flat_record("s-multi", "整段描述", multi_segment_frames(),
                              segments=multi_segment_specs())
    encoder = StubEncoder(scores={"段1": 0.80, "段2": 0.60, "段3": 0.40},
                          default=0.50)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    node = outcome["node"]
    assert outcome["action"] == "pass"
    assert node["frames_used"] == 8
    assert node["max_frames_per_segment"] == 8
    assert node["frames_used"] <= node["max_frames_per_segment"]
    assert node["frames_within_budget"] is True
    assert sum(item["frame_count"] for item in node["segments"]) == node["frames_used"]
    assert all(item["has_evidence"] for item in node["segments"])

    for segment in node["segments"]:
        scores = [frame["score"] for frame in segment["frames"]]
        assert segment["segment_mean"] == pytest.approx(sum(scores) / len(scores))

    all_scores = [frame["score"] for segment in node["segments"]
                  for frame in segment["frames"]]
    assert node["video_text_score"] == pytest.approx(
        sum(all_scores) / len(all_scores))
    segment_means = [item["segment_mean"] for item in node["segments"]]
    # 不再是「段均值的均值」（各段帧数不等时两者不同）
    assert node["video_text_score"] != pytest.approx(
        sum(segment_means) / len(segment_means))
    assert len(encoder.text_calls) == 3


def test_multi_segment_low_segment_isolates_whole_video(tmp_path):
    """任一段段分低于阈值 → 整段视频不通过（隔离）。"""
    roots = prepare(tmp_path, media=multi_segment_media(), name="input_multi_low")
    record = make_flat_record("s-multi-low", "整段描述", multi_segment_frames(),
                              segments=multi_segment_specs())
    encoder = StubEncoder(scores={"段3": 0.10}, default=0.80)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "isolate"
    assert outcome["reason"] == vt.REASON_SEGMENT_BELOW
    node = outcome["node"]
    assert node["passed"] is False
    assert node["failing_segments"] == [2]
    assert node["min_segment_score"] == pytest.approx(0.10)
    assert node["frames_used"] <= node["max_frames_per_segment"]
    assert node["segments"][2]["passed"] is False


def test_more_segments_than_cap_marks_zero_frame_segment_no_evidence(tmp_path):
    """3 段、上限 2：按段均匀覆盖（段 2 为 0 帧），0 帧段记无证据且不因其隔离。"""
    roots = prepare(tmp_path, media=multi_segment_media(), name="input_multi_cap2")
    record = make_flat_record("s-skip", "整段描述", multi_segment_frames(),
                              segments=multi_segment_specs())
    # 段 2 的文本分值极低：若它被评分必然隔离；0 帧时只记无证据、不参与判定
    encoder = StubEncoder(scores={"段2": 0.05}, default=0.80)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(max_frames_per_segment=2),
                                 encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pass"
    node = outcome["node"]
    assert node["frames_used"] == 2
    assert node["max_frames_per_segment"] == 2
    assert node["segments_without_frames"] == [1]
    assert "failing_segments" not in node
    skipped = node["segments"][1]
    assert skipped["frame_count"] == 0
    assert skipped["segment_mean"] is None
    assert skipped["passed"] is None
    assert skipped["has_evidence"] is False
    assert skipped["no_evidence_reason"] == "frame_quota_zero"
    assert "无证据" in node["frame_budget_note"]
    assert "段2" not in encoder.text_calls
    assert len(encoder.image_calls) == 2


def test_segment_without_candidate_frames_is_no_evidence(tmp_path):
    """段 2 无候选帧（整视频无 frames）：记无证据、不因该段隔离。"""
    roots = prepare(tmp_path)
    own = [{"path": "frames/frame_0001.png", "timestamp": 0.0},
           {"path": "frames/frame_0002.png", "timestamp": 1.0}]
    segments = [
        {"start": 0.0, "end": 2.0, "text": "有帧段", "frames": own},
        {"start": 2.0, "end": 4.0, "text": "无帧段"},
    ]
    record = make_flat_record("s-no-cand", "整段描述", [], segments=segments)
    encoder = StubEncoder(default=0.80)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "pass"
    node = outcome["node"]
    assert node["frames_used"] == 2
    assert node["segments_without_frames"] == [1]
    empty = node["segments"][1]
    assert empty["frame_count"] == 0
    assert empty["segment_mean"] is None
    assert empty["passed"] is None
    assert empty["has_evidence"] is False
    assert empty["no_evidence_reason"] == "no_candidate_frames"
    assert "无帧段" not in encoder.text_calls


@pytest.mark.parametrize("limit", [1, 2, 3, 8])
def test_frames_used_never_exceeds_config_cap(tmp_path, limit):
    roots = prepare(tmp_path, media=multi_segment_media(),
                    name="input_cap_%d" % limit)
    record = make_flat_record("s-cap-%d" % limit, "整段描述", multi_segment_frames(),
                              segments=multi_segment_specs())
    encoder = StubEncoder(default=0.70)
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(max_frames_per_segment=limit),
                                 encoder=encoder,
                                 event_time="2026-10-06T00:00:00+08:00")
    node = outcome["node"]
    assert node["frames_used"] <= limit
    assert node["frames_within_budget"] is True
    assert len(encoder.image_calls) == node["frames_used"]
    assert sum(item["frame_count"] for item in node["segments"]) == node["frames_used"]


# --------------------------------------------------------------------------- #
# 台账与节点
# --------------------------------------------------------------------------- #

def test_ledger_entry_has_required_ledger_fields():
    entry = vt.make_ledger_entry(line=3, reason="r", label="l", action="a",
                                 destination="d", event_time="t", sample_id="s-01",
                                 pair_id="pair-01", batch_id="batch-01", retry_count=1)
    for key in ("line", "sample_id", "pair_id", "reason", "label", "stage", "operator",
                "action", "destination", "event_time", "batch_id", "retry_count"):
        assert key in entry
    assert entry["stage"] == "14"
    assert entry["operator"] == vt.OPERATOR


def test_skipped_node_declares_no_scores():
    node = vt.skipped_node()
    assert node["status"] == 1
    assert node["process_status"] == "skipped"
    assert node["route_enabled"] is False
    assert node["video_text_score"] is None
    assert node["passed"] is None
    assert node["frames_used"] == 0
    assert node["low_score_threshold"] == vt.DEFAULT_THRESHOLD
    assert node["max_frames_per_segment"] == frames_mod.MAX_FRAMES_PER_SEGMENT
    assert node["label"] == vt.LABEL_FLOW_ROUTE_DISABLED


def test_skipped_node_uses_effective_config_values():
    """关闭态回填本次配置生效值（阈值/帧预算），不再硬编码默认阈值。"""
    config = make_config(low_score_threshold=0.31, max_frames_per_segment=5)
    node = vt.skipped_node(config)
    assert node["low_score_threshold"] == pytest.approx(0.31)
    assert node["max_frames_per_segment"] == 5
    assert node["route_enabled"] is False
    assert node["video_text_score"] is None
    assert node["frames_used"] == 0


def test_isolate_entry_carries_node_and_record(tmp_path):
    roots = prepare(tmp_path)
    record = make_flat_record("s-16", "描述", default_frames(2))
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(default=0.05),
                                 event_time="2026-10-06T00:00:00+08:00")
    assert outcome["action"] == "isolate"
    assert outcome["entry"]["video_text"]["reason"] == vt.REASON_SEGMENT_BELOW
    assert outcome["entry"]["record"]["sample_id"] == "s-16"


def test_constants_match_table1():
    assert vt.FORMAL_OPERATOR_NUMBER == 14
    assert vt.STAGE_ID == "S3"
    assert vt.MAIN_NAME_ENABLED == "14_video_text_scores.jsonl"
    assert vt.MAIN_NAME_DISABLED == "14_passthrough.jsonl"
    assert vt.CLIP_NAME == "CLIP-ViT/B-32"
