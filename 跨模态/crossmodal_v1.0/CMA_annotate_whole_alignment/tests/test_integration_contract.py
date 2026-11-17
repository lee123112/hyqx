"""History regressions against optimized #13/#14/#15 contracts; no model loading."""
import pytest

from conftest import decisions_of
from utils import alignment as al


def av_pair(modality="audio", node=None, *, placement="record"):
    number = "13" if modality == "audio" else "14"
    media = {"sample_id": "media", modality: {"path": "media/clip." +
             ("wav" if modality == "audio" else "mp4")}}
    pair = {"pair_id": "pair-av", "left": {"sample_id": "text", "record": {
        "sample_id": "text", "text": {"content": "人物正在讲话"}}},
        "right": {"sample_id": "media", "modalities": [modality], "record": media}}
    if node is not None:
        holder = pair if placement == "root" else media
        holder["processing"] = {number: node}
    return pair


def audio_node(**changes):
    node = {"route_enabled": True, "passed": True, "process_status": "passed",
            "edit_distance_ratio": 0.2222222222, "edit_distance_ratio_max": 0.30,
            "homophone_tolerance": False, "degraded": False}
    node.update(changes)
    return node


def video_node(**changes):
    node = {"route_enabled": True, "passed": True, "process_status": "passed",
            "video_text_score": 0.303645, "low_score_threshold": 0.20,
            "degraded": False}
    node.update(changes)
    return node


def whole(result):
    return decisions_of(result)["pair-av"]


@pytest.mark.parametrize("placement", ["root", "record"])
def test_audio_cer_consumes_actual_threshold_without_clip(plan, placement):
    item = av_pair(node=audio_node(), placement=placement)
    result = plan([item])
    value = whole(result)
    assert value["decision"] == al.DECISION_AUTO_ALIGNED
    assert value["clip_score"] is None
    assert value["score_metric"] == "CER"
    assert value["score_value"] == pytest.approx(0.222222, abs=1e-6)
    assert value["score_direction"] == "lower_is_better"
    assert value["score_threshold"] == 0.30
    assert value["human_verified"] is False


def test_audio_uses_optimized_final_homophone_cer(plan):
    node = audio_node(edit_distance_ratio=0.5625, edit_distance_ratio_homophone=0.0625,
                      homophone_tolerance=True, homophone_tolerated=True,
                      pinyin_dictionary_source="package", pinyin_table_entries=26703)
    value = whole(plan([av_pair(node=node)]))
    assert value["decision"] == al.DECISION_AUTO_ALIGNED
    assert value["score_value"] == 0.0625
    assert value["clip_score"] is None


@pytest.mark.parametrize("changes", [
    {"route_enabled": False}, {"passed": None}, {"degraded": True},
    {"edit_distance_ratio": float("nan")}, {"edit_distance_ratio": float("inf")},
    {"edit_distance_ratio": -0.1}, {"edit_distance_ratio_max": None},
    {"edit_distance_ratio_max": float("nan")}, {"edit_distance_ratio_max": True},
    {"edit_distance_ratio_max": 1.1}, {"process_status": "not_applicable"},
])
def test_audio_incomplete_or_degraded_never_auto(plan, changes):
    value = whole(plan([av_pair(node=audio_node(**changes))]))
    assert value["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert value["human_verified"] is False


def test_audio_missing_score_node_stays_pending(plan):
    assert whole(plan([av_pair()]))["decision"] == al.DECISION_PENDING_MANUAL_REVIEW


@pytest.mark.parametrize("node", [audio_node(passed=False, edit_distance_ratio=0.8),
                                  audio_node(edit_distance_ratio=0.4)])
def test_audio_real_failure_or_cer_above_actual_threshold_isolates(plan, node):
    value = whole(plan([av_pair(node=node)]))
    assert value["decision"] == al.DECISION_NOT_ALIGNED
    assert value["clip_score"] is None


def test_temporal_route_precedes_new_audio_fallback(plan):
    item = av_pair(node=audio_node(passed=False, edit_distance_ratio=0.8))
    item["processing"] = {"15": {"operator": "temporal_consistency",
                                   "process_status": "passed", "status": 1}}
    value = whole(plan([item]))
    assert value["decision"] == al.DECISION_AUTO_ALIGNED
    assert value["bucketed_by"] == al.CONFIRM_TEMPORAL


def test_video_passed_scoring_still_requires_auto_threshold(plan):
    value = whole(plan([av_pair("video", video_node())]))
    assert value["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert value["clip_score"] == 0.303645
    assert value["threshold_auto"] == 0.32
    assert value["threshold_review"] == 0.20
    assert value["human_verified"] is False


@pytest.mark.parametrize("changes,expected", [
    ({"passed": False}, al.DECISION_NOT_ALIGNED),
    ({"route_enabled": False}, al.DECISION_PENDING_MANUAL_REVIEW),
    ({"passed": None}, al.DECISION_PENDING_MANUAL_REVIEW),
    ({"degraded": True}, al.DECISION_PENDING_MANUAL_REVIEW),
    ({"video_text_score": 5.0}, al.DECISION_PENDING_MANUAL_REVIEW),
])
def test_video_flags_block_high_score(plan, changes, expected):
    node = video_node(video_text_score=0.91)
    node.update(changes)
    assert whole(plan([av_pair("video", node)]))["decision"] == expected


@pytest.mark.parametrize("frames", [42, [], None])
def test_video_integer_frame_count_and_keyframes(frames):
    record = {"video": {"frames": frames, "keyframes": [{"path": "frames/mid.png"}],
                        "segments": [{"frames": 20}]}}
    assert al.video_frames(record) == ["frames/mid.png"]


def test_media_collection_keeps_all_av_and_frames(plan):
    item = av_pair("video", video_node())
    item["right"]["record"]["audio"] = {"path": "media/track.wav"}
    item["right"]["record"]["video"].update(
        frames=42, keyframes=[{"path": "frames/mid.png"}])
    result = plan([item])
    assert result.stats["_media_refs"] == ["frames/mid.png", "media/clip.mp4", "media/track.wav"]
    assert "image_reference" not in result.main[0]


def test_image_reference_and_nested_m11_score_are_read(plan, media, build_pair):
    item = build_pair(1, 0.41)
    node = item.pop("image_text_score")
    item.pop("processing", None)
    item["right"]["record"]["image_reference"] = {"path": "media/cat.png"}
    item["right"]["record"]["image"] = None
    item["right"]["record"]["processing"] = {"12": node}
    result = plan([item])
    assert decisions_of(result)[item["pair_id"]]["decision"] == al.DECISION_AUTO_ALIGNED


def test_audio_edit_requires_route_rescore_and_veto_remains_locked(plan, stub_encoder):
    item = av_pair(node=audio_node())
    event = {"pair_id": "pair-av", "reviewer_id": "real-reviewer",
             "reviewed_at": "2026-11-09T10:00:00+08:00", "new_description": "新描述"}
    events = al.load_human_events([(1, event, None)])
    encoder = stub_encoder(0.99)
    value = whole(plan([item], events=events, encoder=encoder))
    assert value["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert value["rescored"] is False and value["clip_score"] is None
    assert encoder.image_calls == encoder.text_calls == 0
    item["alignment"] = {"whole": {"decision": "not_aligned", "confirmation_method": "human",
                                      "human_veto_locked": True}}
    assert whole(plan([item]))["decision"] == al.DECISION_NOT_ALIGNED


def test_side_veto_is_not_lost_on_m11_pair(plan):
    item = av_pair("video", video_node(video_text_score=0.9))
    item["right"]["record"]["alignment"] = {"whole": {
        "human_veto_locked": True, "decision": "not_aligned", "confirmation_method": "human"}}
    assert whole(plan([item]))["decision"] == al.DECISION_NOT_ALIGNED


def test_explicit_final_cer_precedes_diagnostic_strict_and_homophone(plan):
    node = audio_node(final_cer=0.25, edit_distance_ratio=0.7,
                      edit_distance_ratio_homophone=0.01, homophone_tolerance=True)
    value = whole(plan([av_pair(node=node)]))
    assert value["score_value"] == 0.25
    assert value["score_field"] == "final_cer"


def test_actual_temporal_not_applicable_14_does_not_mask_15(plan):
    item = av_pair("video", {"route_enabled": True, "passed": None,
                              "process_status": "not_applicable", "route": "temporal"})
    item["right"]["record"]["processing"]["15"] = {
        "operator": "temporal_consistency", "process_status": "passed", "status": 1}
    value = whole(plan([item]))
    assert value["decision"] == al.DECISION_AUTO_ALIGNED
    assert value["bucketed_by"] == al.CONFIRM_TEMPORAL


def test_authoritative_14_failure_blocks_scalar_alias(plan):
    item = av_pair("video", video_node(passed=False, process_status="isolated"), placement="root")
    item["video_text_score"] = 0.95
    assert whole(plan([item]))["decision"] == al.DECISION_NOT_ALIGNED


def test_audio_score_cannot_be_rescored_with_video_frame_clip(plan, media, stub_encoder):
    import make_sample
    item = av_pair("video", None)
    record = item["right"]["record"]
    record["audio"] = {"path": "media/audio.wav"}
    record["video"]["frames"] = [{"path": "frames/frame.png"}]
    make_sample.write_png(media / "frames/frame.png", 8, 6, (1, 2, 3))
    record["processing"] = {"13": audio_node()}
    events = al.load_human_events([(1, {"pair_id": "pair-av", "reviewer_id": "real-reviewer",
                                        "reviewed_at": "2026-11-09", "new_description": "新描述"}, None)])
    encoder = stub_encoder(0.9)
    value = whole(plan([item], events=events, encoder=encoder))
    assert value["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert value["clip_score"] is None and value["rescored"] is False
    assert encoder.image_calls == 0


def test_run_carries_video_audio_and_keyframes_without_proxy(tmp_path, monkeypatch):
    import json
    import inference
    item = av_pair("video", video_node())
    item["right"]["record"]["video"].update(frames=10, keyframes=[{"path": "frames/frame.png"}])
    item["right"]["record"]["audio"] = {"path": "media/audio.wav"}
    for relative in ("media/clip.mp4", "frames/frame.png", "media/audio.wav"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test transport only")
    source = tmp_path / "input.jsonl"
    source.write_text(json.dumps(item), encoding="utf-8")
    monkeypatch.setattr(inference, "build_encoder", lambda cfg: (None, "test_no_model"))
    result = inference.run(str(source), "av_carriage", output_root=str(tmp_path / "output"))
    assert result["manifest"]["media"]["missing"] == []
    assert result["manifest"]["media"]["carried"] == ["frames/frame.png", "media/audio.wav", "media/clip.mp4"]
    from pathlib import Path
    for relative in result["manifest"]["media"]["carried"]:
        assert (Path(result["runDir"]) / relative).read_bytes() == (tmp_path / relative).read_bytes()


@pytest.mark.parametrize("source", ["image_text_score", "processing.12", "alignment.image_text"])
@pytest.mark.parametrize("changes,expected", [
    ({"route_enabled": False}, al.DECISION_PENDING_MANUAL_REVIEW),
    ({"passed": False}, al.DECISION_NOT_ALIGNED),
    ({"degraded": True}, al.DECISION_PENDING_MANUAL_REVIEW),
])
def test_m12_explicit_route_state_blocks_high_score(plan, media, build_pair, source, changes, expected):
    item = build_pair(1, 0.9)
    node = item.pop("image_text_score")
    item.pop("processing", None)
    node.update(changes)
    if source == "processing.12":
        item["processing"] = {"12": node}
    elif source == "alignment.image_text":
        item["alignment"] = {"image_text": node}
    else:
        item[source] = node
    assert decisions_of(plan([item]))[item["pair_id"]]["decision"] == expected


def test_audio_does_not_borrow_image_clip_score(plan):
    item = av_pair()
    item["clip_score"] = 0.99
    assert whole(plan([item]))["decision"] == al.DECISION_PENDING_MANUAL_REVIEW


def test_current_video_degradation_cannot_reuse_old_whole_score(plan):
    item = av_pair("video", video_node(degraded=True, video_text_score=0.9))
    item["alignment"] = {"whole": {"clip_score": 0.9, "decision": "auto_aligned"}}
    assert whole(plan([item]))["decision"] == al.DECISION_PENDING_MANUAL_REVIEW


def test_audio_no_timestamp_15_does_not_mask_real_13(plan):
    item = av_pair(node=audio_node())
    item["right"]["record"]["processing"]["15"] = {
        "operator": "temporal_consistency", "process_status": "not_applicable", "status": 1,
        "reason_code": "no_timestamp_timeline", "review_required": False}
    value = whole(plan([item]))
    assert value["decision"] == al.DECISION_AUTO_ALIGNED
    assert value["bucketed_by"] == "audio_13"
    assert value["clip_score"] is None


def test_current_13_cer_alias_is_strict_before_homophone_final_decision(plan):
    node = audio_node(cer=0.5625, edit_distance_ratio=0.5625,
                      edit_distance_ratio_homophone=0.0625, homophone_tolerance=True,
                      homophone_tolerated=True, pinyin_dictionary_source="package")
    value = whole(plan([av_pair(node=node)]))
    assert value["score_value"] == 0.0625
    assert value["score_field"] == "edit_distance_ratio_homophone"
    assert value["decision"] == al.DECISION_AUTO_ALIGNED


def test_authoritative_12_failure_is_not_hidden_by_dict_alias(plan, media, build_pair):
    item = build_pair(1, 0.9)
    item["processing"] = {"12": {"clip_score": 0.9, "passed": False}}
    assert decisions_of(plan([item]))[item["pair_id"]]["decision"] == al.DECISION_NOT_ALIGNED


@pytest.mark.parametrize("flags,expected,bucket", [
    ({"passed": False}, al.DECISION_NOT_ALIGNED, al.CONFIRM_TEMPORAL),
    ({"degraded": True}, al.DECISION_PENDING_MANUAL_REVIEW, al.CONFIRM_TEMPORAL),
    ({"passed": None}, al.DECISION_PENDING_MANUAL_REVIEW, al.CONFIRM_TEMPORAL),
    ({"route_enabled": False}, al.DECISION_AUTO_ALIGNED, "audio_13"),
    ({"passed": False, "route_enabled": False}, al.DECISION_NOT_ALIGNED, al.CONFIRM_TEMPORAL),
])
def test_temporal_explicit_flags_are_not_overwritten_by_passed_status(plan, flags, expected, bucket):
    item = av_pair(node=audio_node())
    item["processing"] = {"15": dict({"operator": "temporal_consistency", "status": 1,
                                       "process_status": "passed"}, **flags)}
    value = whole(plan([item]))
    assert value["decision"] == expected
    assert value["bucketed_by"] == bucket
    if bucket == al.CONFIRM_TEMPORAL:
        for key, flag in flags.items():
            assert value["evidence"]["temporal"][key] == flag


def test_disabled_temporal_without_other_route_stays_pending(plan):
    item = av_pair()
    item["processing"] = {"15": {"operator": "temporal_consistency", "status": 1,
                                   "process_status": "passed", "route_enabled": False}}
    value = whole(plan([item]))
    assert value["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
