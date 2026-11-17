"""#16 视文路线（video+text）测试：#14 取分、路线优先级、无分/无帧显式降级、图文回归。

这些用例只使用**上游构造的真实分数**与确定性编码器替身，不加载任何模型，
也不验证真实视频解码；VFR/帧数与 #6/#14 的单测已覆盖。
"""
from __future__ import annotations

import make_sample
import pytest

from conftest import decisions_of
from utils import alignment as al

VIDEO_MODEL = {"name": "CLIP-ViT/B-32", "version": "ViT-B-32", "library": "open_clip"}
TEXT = "样例描述：一只动物在户外场景中活动"


def _events(items) -> al.HumanEventSet:
    return al.load_human_events([(index + 1, item, None) for index, item in enumerate(items)])


def _video_node(score, *, tier=None):
    """构造 #14 视文节点（与 #14 主输出同构：processing["14"] == 顶层 video_text_score）。"""
    return {
        "operator": "video_text_scoring",
        "status": 1 if score is not None else 0,
        "process_status": "passed" if score is not None else "pending",
        "passed": (score >= 0.20) if score is not None else None,
        "video_text_score": score,
        "segment_mean": score,
        "min_segment_score": score,
        "model": VIDEO_MODEL if score is not None else None,
        "tier": tier,
        "segments": [],
        "scored_frames": 0,
    }


def _video_record(sample_id: str, *, frames=None, video_path="media/clip.mp4") -> dict:
    video = {"path": video_path, "duration": 2.0, "fps": 2.0, "frame_count": 0}
    if frames is not None:
        video["frames"] = [{"path": path, "timestamp": 0.5 * index}
                           for index, path in enumerate(frames)]
        video["segments"] = [{"start": 0.0, "end": 2.0, "frames": video["frames"]}]
    return {"sample_id": sample_id, "batch_id": "batch-16", "text": None, "image": None,
            "video": video, "audio": None, "processing": {},
            "alignment": {"whole": None, "local": []}, "qc": {},
            "metadata": {"batch_id": "batch-16"}}


def video_pair(index: int, score=0.41, *, source="top_level", frames=None,
               video_path="media/clip.mp4", tier=None) -> dict:
    """video+text 候选对；``source`` 决定 #14 节点挂在 pair 顶层还是记录侧。"""
    left_id, right_id = "t%02d" % index, "v%02d" % index
    left = make_sample.text_record(left_id, TEXT, "batch-16", "src-16")
    video_record = _video_record(right_id, frames=frames, video_path=video_path)
    right = {"sample_id": right_id, "modalities": ["video"], "record": video_record}
    node = None if source == "absent" else _video_node(score, tier=tier)
    item = {
        "pair_id": "pair-%s--%s" % (left_id, right_id),
        "group_id": "grp-src-src-16",
        "modality_pair": ["text", "video"],
        "evidence": {"type": "A", "criterion": "same_source_id", "evidence_source": "rule",
                     "value": {"source_id": "src-16"}},
        "evidence_priority": 1,
        "left": left,
        "right": right,
        "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0",
                     "uniqueId": "sample_task"},
    }
    if source == "top_level" and node is not None:
        item["video_text_score"] = node
        item["processing"] = {"14": node}
    elif source == "processing" and node is not None:
        item["processing"] = {"14": node}
    elif source == "record" and node is not None:
        video_record["video_text_score"] = node
        video_record["processing"] = {"14": node}
    return item


# ------------------------------------------------------------------ 路线判定

def test_resolve_orientation_video_cross():
    sides = [make_sample.text_record("t1", TEXT, "b", "s"),
             {"sample_id": "v1", "modalities": ["video"],
              "record": _video_record("v1", frames=["frames/f.png"])}]
    orientation = al.resolve_orientation(sides)
    assert orientation["kind"] == al.KIND_VIDEO_CROSS
    assert orientation["video_side"] == 1 and orientation["text_side"] == 0
    assert orientation["image_side"] is None
    assert orientation["video_record"]["sample_id"] == "v1"


def test_resolve_orientation_image_still_wins_over_video():
    """video+image+text 仍走图像路线（image 优先，行为不变）。"""
    sides = [make_sample.text_record("t1", TEXT, "b", "s"),
             {"sample_id": "iv1", "modalities": ["video", "image"],
              "record": dict(_video_record("iv1", frames=["frames/f.png"]),
                             image={"path": "media/cat.png"})}]
    orientation = al.resolve_orientation(sides)
    assert orientation["kind"] in (al.KIND_CROSS, al.KIND_INTERNAL)
    assert orientation["image_side"] == 1
    assert orientation["video_side"] is None


def test_representative_frame_is_middle_frame():
    record = {"video": {"segments": [{"frames": [{"path": "f0.png"},
                                                  {"path": "f1.png"}]},
                                     {"frames": [{"path": "f2.png"}]}],
                        "frames": [{"path": "ignored.png"}]}}
    assert al.video_frames(record) == ["f0.png", "f1.png", "f2.png"]
    assert al.representative_frame_reference(record) == "f1.png"
    assert al.representative_frame_reference({"video": {"path": "clip.mp4"}}) is None


def test_representative_frame_falls_back_to_video_frames():
    record = {"video": {"frames": [{"path": "a.png"}, {"path": "b.png"},
                                   {"path": "c.png"}]}}
    assert al.representative_frame_reference(record) == "b.png"


# ------------------------------------------------------------------ 视文分档

def test_video_high_score_is_auto_aligned(plan):
    result = plan([video_pair(1, 0.41)])
    assert result.not_applicable == [] and result.stats["not_applicable"] == 0
    whole = decisions_of(result)["pair-t01--v01"]
    assert whole["decision"] == al.DECISION_AUTO_ALIGNED
    assert whole["clip_score"] == 0.41
    assert whole["score_source"] == al.SOURCE_VIDEO_TEXT_SCORE
    assert whole["model"] == "CLIP-ViT/B-32"
    assert whole["model_version"] == "ViT-B-32"
    assert result.stats["conservation_ok"] is True
    # 常规视文分档不读媒体、不加载本地 CLIP
    assert result.stats["media_missing"] == 0 and result.stats["models_available"] is False

    record = result.main[0]
    assert record["processing"]["16"]["score_source"] == al.SOURCE_VIDEO_TEXT_SCORE
    # 供下游 #19 的 route.14 / whole 门读取：原样保留
    assert record["video_text_score"]["video_text_score"] == 0.41
    assert record["processing"]["14"]["video_text_score"] == 0.41


def test_video_borderline_score_goes_to_review(plan):
    result = plan([video_pair(2, 0.28)])
    whole = decisions_of(result)["pair-t02--v02"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["evidence"]["reason"] == "borderline_score"
    assert len(result.review) == 1


def test_video_low_score_goes_to_isolation(plan):
    result = plan([video_pair(3, 0.18)])
    assert result.review == []
    entry = result.isolation[0]
    whole = entry["record"]["alignment"]["whole"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["bucketed_by"] == "upstream_score"
    assert whole["isolation_source"]["reused"] is True
    assert whole["isolation_source"]["origin"] == al.ORIGIN_UPSTREAM_14


def test_video_without_score_is_pending_not_fabricated(plan):
    """#14 节点存在但分数为 null（如 #14 降级）：显式待人工，不伪造分数。"""
    result = plan([video_pair(4, None)])
    whole = decisions_of(result)["pair-t04--v04"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["clip_score"] is None
    assert whole["score_source"] == al.SOURCE_NONE
    assert whole["evidence"]["reason"] == "no_score"
    assert whole["evidence"]["upstream"]["origin"] == al.ORIGIN_UPSTREAM_14
    assert whole["evidence"]["upstream"]["source"] is None
    assert result.stats["pending_manual_review"] == 1
    assert result.stats["conservation_ok"] is True


def test_video_without_any_14_node_is_pending(plan):
    result = plan([video_pair(5, source="absent")])
    whole = decisions_of(result)["pair-t05--v05"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["clip_score"] is None
    assert whole["score_source"] == al.SOURCE_NONE
    assert whole["evidence"]["upstream"]["origin"] is None


@pytest.mark.parametrize("where", ["pair", "video_side"])
def test_image_route_does_not_mask_current_video_score(plan, where):
    item = video_pair(30, 0.301592)
    holder = item if where == "pair" else item["right"]["record"]
    holder["image_text_score"] = {"clip_score": 0.98}
    holder.setdefault("processing", {})["12"] = {
        "route_enabled": False, "process_status": "not_applicable", "reason": "no_image"}
    value = decisions_of(plan([item]))[item["pair_id"]]
    assert value["clip_score"] == 0.301592
    assert value["evidence"]["upstream"]["origin"] == al.ORIGIN_UPSTREAM_14
    assert value["evidence"]["reason"] == "borderline_score"
    assert value["human_verified"] is False


def test_video_pending_does_not_take_stale_image_or_passed_time_score(plan):
    item = video_pair(31, None)
    item["clip_score"] = 0.95
    item["alignment"] = {"whole": {"clip_score": 0.99}}
    item["processing"]["15"] = {"operator": "temporal_consistency", "process_status": "passed"}
    value = decisions_of(plan([item]))[item["pair_id"]]
    assert value["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert value["clip_score"] is None and value["evidence"]["upstream"]["origin"] == "14"


def test_previous_whole_score_still_precedes_valid_video_despite_unrelated_image_na(plan):
    item = video_pair(32, 0.301592)
    item["processing"]["12"] = {"route_enabled": False, "process_status": "not_applicable"}
    item["alignment"] = {"whole": {"clip_score": 0.29}}
    value = decisions_of(plan([item]))[item["pair_id"]]
    assert value["clip_score"] == 0.29 and value["score_source"] == "alignment.whole"


@pytest.mark.parametrize("field,state", [
    ("processing.12", {"route_enabled": True, "passed": False, "process_status": "failed"}),
    ("image_text_score", {"route_enabled": True, "tier": "isolated"}),
])
def test_video_high_score_cannot_release_explicit_pair_image_isolation(plan, field, state):
    item = video_pair(33, 0.55)
    state["reason"] = "below_image_threshold"
    if field == "processing.12":
        item["processing"]["12"] = state
    else:
        item["alignment"] = {"whole": {"clip_score": 0.98}}
        item[field] = state
    result = plan([item])
    assert not result.main
    whole = result.isolation[0]["record"]["alignment"]["whole"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["clip_score"] is None
    assert whole["isolation_source"]["origin"] == "12"
    assert whole["evidence"]["reason"] == "below_image_threshold"


def test_processing_14_only_is_read_as_source(plan):
    result = plan([video_pair(6, 0.28, source="processing")])
    whole = decisions_of(result)["pair-t06--v06"]
    assert whole["clip_score"] == 0.28
    assert whole["score_source"] == al.SOURCE_PROCESSING_14
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW


def test_record_level_video_score_prefers_video_side(plan):
    """#14 分可以是记录级字段；视频侧优先，文本侧同名字段不得被误用。"""
    item = video_pair(7, 0.41, source="record")
    decoy = _video_node(0.95)
    item["left"]["record"]["video_text_score"] = decoy
    item["left"]["record"]["processing"] = {"14": decoy}
    result = plan([item])
    whole = decisions_of(result)["pair-t07--v07"]
    assert whole["clip_score"] == 0.41
    assert whole["score_source"] == al.SOURCE_VIDEO_TEXT_SCORE
    assert result.main[0]["right"]["record"]["video_text_score"]["video_text_score"] == 0.41
    assert {key for key in result.main[0]["right"]["record"]["processing"]} == {"14"}


# ------------------------------------------------------------------ 图文路线回归 / 协议

def test_image_route_still_wins_over_video_score(plan, media):
    """同一配对同时有 image+text 与视文分时，仍按 #12 图文分分档。"""
    item = video_pair(8, 0.55)
    item["right"]["record"]["image"] = {"path": "media/cat.png"}
    item["image_text_score"] = {"clip_score": 0.28, "tier": "pending_manual",
                                "model": VIDEO_MODEL}
    result = plan([item])
    whole = decisions_of(result)["pair-t08--v08"]
    assert whole["score_source"] == "image_text_score"
    assert whole["clip_score"] == 0.28
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW


def test_video_route_keeps_identical_whole_schema(plan):
    """协议字段 diff 0：视文与图文写出的 alignment.whole / processing.16 键集合一致。"""
    image_result = plan([make_sample.pair(1, 0.41)])
    video_result = plan([video_pair(2, 0.41)])
    image_whole = decisions_of(image_result)["pair-t01--i01"]
    video_whole = decisions_of(video_result)["pair-t02--v02"]
    assert set(video_whole) == set(image_whole)
    assert set(video_whole["evidence"]) == set(image_whole["evidence"])
    image_node = image_result.main[0]["processing"]["16"]
    video_node = video_result.main[0]["processing"]["16"]
    assert set(video_node) == set(image_node)


# ------------------------------------------------------------------ 代表帧重评分

def test_video_rescore_without_frames_is_pending(plan, stub_encoder):
    encoder = stub_encoder(0.42)
    events = _events([{"pair_id": "pair-t09--v09", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([video_pair(9, 0.41)], encoder=encoder, events=events)
    whole = decisions_of(result)["pair-t09--v09"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["rescored"] is False
    assert whole["description_edit"]["status"] == 0
    assert whole["description_edit"]["process_status"] == \
        al.REASON_NO_REPRESENTATIVE_FRAME
    assert whole["description_edit"]["score_after"] is None
    assert encoder.text_calls == 0 and encoder.image_calls == 0   # 无帧不调用模型
    assert result.stats["rescored"] == 0
    assert result.stats["rescore_deferred"] == 1
    assert result.stats["degraded"] is True
    assert result.stats["media_missing"] == 1


def test_video_rescore_uses_middle_frame(plan, media, stub_encoder):
    frames = ["frames/f0.png", "frames/f1.png", "frames/f2.png"]
    for name in frames:
        make_sample.write_png(media / name, 8, 6, (200, 160, 120))
    encoder = stub_encoder(0.42)
    events = _events([{"pair_id": "pair-t10--v10", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([video_pair(10, 0.18, frames=frames)], encoder=encoder, events=events)
    whole = decisions_of(result)["pair-t10--v10"]
    assert whole["decision"] == al.DECISION_AUTO_ALIGNED
    assert whole["score_source"] == al.SOURCE_RESCORE
    assert whole["clip_score"] == pytest.approx(0.42, abs=1e-9)
    assert whole["description_edit"]["score_before"] == 0.18
    assert encoder.text_calls == 1 and encoder.image_calls == 1
    assert encoder.image_paths == [str((media / "frames" / "f1.png").resolve())]
    record = result.main[0]
    assert "image_reference" not in record
    assert record["video_frame_reference"]["path"] == "frames/f1.png"
    assert record["video_frame_reference"]["side"] == "right"
    assert result.stats["_media_refs"] == frames + ["media/clip.mp4"]


def test_video_rescore_frame_falls_back_to_flat_frames(plan, media, stub_encoder):
    frames = ["frames/a.png", "frames/b.png"]
    for name in frames:
        make_sample.write_png(media / name, 8, 6, (120, 160, 200))
    item = video_pair(11, 0.18)
    video = item["right"]["record"]["video"]
    video["frames"] = [{"path": name} for name in frames]
    encoder = stub_encoder(0.35)
    events = _events([{"pair_id": "pair-t11--v11", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([item], encoder=encoder, events=events)
    whole = decisions_of(result)["pair-t11--v11"]
    assert whole["score_source"] == al.SOURCE_RESCORE
    assert encoder.image_paths == [str((media / "frames" / "b.png").resolve())]
    assert whole["clip_score"] == pytest.approx(0.35, abs=1e-9)


def test_video_rescore_missing_frame_file_never_scores(plan, stub_encoder):
    """帧引用存在但文件缺失：不得把路径交给编码器，也不得产出任何分数。"""
    encoder = stub_encoder(0.90)
    events = _events([{"pair_id": "pair-t12--v12", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([video_pair(12, 0.41, frames=["frames/missing.png"])],
                  encoder=encoder, events=events)
    whole = decisions_of(result)["pair-t12--v12"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["rescored"] is False
    assert whole["description_edit"]["status"] == 0
    assert whole["description_edit"]["score_after"] is None
    assert whole["description_edit"]["process_status"] in ("media_missing",
                                                           "no_representative_frame")
    assert encoder.text_calls == 0 and encoder.image_calls == 0
    assert result.stats["rescored"] == 0
    assert result.stats["media_missing"] == 1
    assert result.stats["degraded"] is True
