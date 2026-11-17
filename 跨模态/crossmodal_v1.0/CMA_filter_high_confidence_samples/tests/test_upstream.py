"""上游结论解析层（``utils/upstream.py``）单元测试。

覆盖：#12–#18 命名空间取值优先级、开关/通过/待定/不适用判定、
成对记录扁平化、授权「可训练」归一化、时间轴存在性与上游隔离标记。
"""
from __future__ import annotations

from conftest import (make_pair, make_record, rec_12, rec_13, rec_14, rec_15,
                      rec_16, rec_17, rec_18, rec_license)

from utils import upstream


# --------------------------------------------------------------------------- #
# 授权结论
# --------------------------------------------------------------------------- #

def test_license_read_from_admission_first():
    record = make_record(rec_license("可训练", where="admission"),
                         {"metadata": {"license": "不可训练"}})
    value, source = upstream.read_license(record)
    assert value == "可训练"
    assert source == "admission.license"


def test_license_read_from_metadata_when_admission_absent():
    value, source = upstream.read_license(make_record(rec_license("可训练", where="metadata")))
    assert value == "可训练"
    assert source == "metadata.license"


def test_license_read_from_authorization_status_and_top_level():
    value, source = upstream.read_license(make_record(rec_license("可训练", where="authorization")))
    assert value == "可训练"
    assert source == "authorization.status"

    value, source = upstream.read_license(make_record(rec_license("可训练", where="top")))
    assert value == "可训练"
    assert source == "license"


def test_license_missing_returns_none():
    value, source = upstream.read_license(make_record(None))
    assert value is None
    assert source is None


def test_is_trainable_accepts_both_chinese_and_latin():
    assert upstream.is_trainable("可训练") is True
    assert upstream.is_trainable(" 可训练 ") is True
    assert upstream.is_trainable("trainable") is True
    assert upstream.is_trainable("TRAINABLE") is True


def test_is_trainable_rejects_other_values():
    for value in ("待确认", "不可训练", "unknown", "not_trainable", ""):
        assert upstream.is_trainable(value) is False, value
    assert upstream.is_trainable(None) is None


# --------------------------------------------------------------------------- #
# 整体对齐结论
# --------------------------------------------------------------------------- #

def test_whole_alignment_prefers_alignment_whole():
    record = make_record(rec_16("manual_aligned"),
                         {"processing": {"16": {"status": "not_aligned"}}})
    info = upstream.read_whole_alignment(record)
    assert info["decision"] == "manual_aligned"
    assert info["source"] == "alignment.whole.decision"


def test_whole_alignment_falls_back_to_processing_node():
    record = {"sample_id": "s", "processing": {"16": {"status": "not_aligned"}}}
    info = upstream.read_whole_alignment(record)
    assert info["decision"] == "not_aligned"
    assert info["source"] == "processing.16.status"


def test_whole_alignment_missing():
    info = upstream.read_whole_alignment(make_record(None))
    assert info["decision"] is None
    assert info["source"] is None


def test_whole_alignment_human_veto_locked_is_reported():
    record = make_record(rec_16("not_aligned", human_veto_locked=True))
    info = upstream.read_whole_alignment(record)
    assert info["human_veto_locked"] is True


# --------------------------------------------------------------------------- #
# 评分路线 #12 / #13 / #14
# --------------------------------------------------------------------------- #

def test_route12_scored_auto_aligned_is_enabled_and_passed():
    result = upstream.read_routes(make_record(rec_12("auto_aligned")))["12"]
    assert result["present"] is True
    assert result["enabled"] is True
    assert result["passed"] is True
    assert result["tier"] == "auto_aligned"


def test_route12_pending_manual_is_enabled_and_passed_at_route_level():
    """0.25–0.32 是「留样本池 + 人工」，路线级不算失败（由 #16 定整体结论）。"""
    result = upstream.read_routes(make_record(rec_12("pending_manual")))["12"]
    assert result["enabled"] is True
    assert result["passed"] is True


def test_route12_isolated_is_enabled_and_failed():
    result = upstream.read_routes(make_record(rec_12("isolated")))["12"]
    assert result["enabled"] is True
    assert result["passed"] is False


def test_route12_pending_model_is_pending():
    result = upstream.read_routes(make_record(rec_12(None, status="pending_model")))["12"]
    assert result["enabled"] is True
    assert result["passed"] is None
    assert result["reason"] == "pending_model"


def test_route13_route_enabled_passed():
    result = upstream.read_routes(make_record(rec_13(True, "passed")))["13"]
    assert (result["enabled"], result["passed"]) == (True, True)


def test_route13_route_enabled_failed():
    result = upstream.read_routes(make_record(rec_13(False, "failed")))["13"]
    assert (result["enabled"], result["passed"]) == (True, False)


def test_route13_skipped_means_not_enabled():
    result = upstream.read_routes(make_record(rec_13(None, "skipped", route_enabled=False)))["13"]
    assert result["enabled"] is False


def test_route13_pending_is_pending():
    result = upstream.read_routes(make_record(rec_13(None, "pending")))["13"]
    assert result["enabled"] is True
    assert result["passed"] is None


def test_route14_reads_top_level_alias():
    record = {"sample_id": "s", "video_text_score": {"status": "failed", "passed": False,
                                                     "route_enabled": True}}
    result = upstream.read_routes(record)["14"]
    assert result["present"] is True
    assert result["enabled"] is True
    assert result["passed"] is False


def test_routes_absent_when_no_namespace():
    routes = upstream.read_routes(make_record(None))
    assert all(info["present"] is False for info in routes.values())
    assert upstream.enabled_routes(routes) == []


# --------------------------------------------------------------------------- #
# 时序结论与时间轴存在性
# --------------------------------------------------------------------------- #

def test_temporal_passed():
    info = upstream.read_temporal(make_record(rec_15("passed", True, "within_threshold")))
    assert info["present"] is True
    assert info["status"] == "passed"
    assert info["passed"] is True


def test_temporal_failed():
    info = upstream.read_temporal(make_record(rec_15("failed", False, "max_delta_exceeded")))
    assert info["passed"] is False


def test_temporal_not_applicable_keeps_passed_none():
    info = upstream.read_temporal(make_record(rec_15("not_applicable", None)))
    assert info["status"] == "not_applicable"
    assert info["passed"] is None
    assert info["applicable"] is False


def test_temporal_skipped_is_not_applicable():
    info = upstream.read_temporal(make_record(
        {"processing": {"15": {"status": "skipped", "reason_code": "route_disabled"}}}))
    assert info["status"] == "skipped"
    assert info["applicable"] is False


def test_temporal_absent_is_present_false():
    info = upstream.read_temporal(make_record(None))
    assert info["present"] is False
    assert info["applicable"] is False


def test_timeline_present_detects_inline_timestamps():
    assert upstream.timeline_present({"sample_id": "s", "time_index": [{"start": 0, "end": 1}]}) is True
    assert upstream.timeline_present(
        {"sample_id": "s", "video": {"frames": [{"timestamp": 0.5}]}}) is True
    assert upstream.timeline_present({"sample_id": "s", "timeline": {"bindings": 1}}) is True
    assert upstream.timeline_present({"sample_id": "s", "text": {"content": "hi"}}) is False


def test_timeline_present_true_when_temporal_node_decides():
    assert upstream.timeline_present(make_record(rec_15("failed", False))) is True
    assert upstream.timeline_present(make_record(rec_15("not_applicable", None))) is False


# --------------------------------------------------------------------------- #
# 局部标注与上游隔离
# --------------------------------------------------------------------------- #

def test_local_annotation_reads_17_and_18():
    record = make_record(rec_17("pending_manual_review"), rec_18("pending_manual"))
    info = upstream.read_local_annotation(record)
    assert info["operator17_status"] == "pending_manual_review"
    assert info["operator18_status"] == "pending_manual"
    assert info["blocking"] is False


def test_local_annotation_incomplete_is_upstream_isolation():
    record = make_record(rec_17("incomplete"))
    isolation = upstream.read_upstream_isolation(record)
    assert isolation["isolated"] is True
    assert "17" in isolation["source"]


def test_local_annotation_18_isolated_is_upstream_isolation():
    record = make_record(rec_18("isolated"))
    isolation = upstream.read_upstream_isolation(record)
    assert isolation["isolated"] is True
    assert "18" in isolation["source"]


def test_local_annotation_pending_is_not_blocking():
    record = make_record(rec_17("not_applicable"), rec_18("skipped"))
    assert upstream.read_upstream_isolation(record)["isolated"] is False
    assert upstream.read_local_annotation(record)["blocking"] is False


def test_explicit_operator21_isolation_marker():
    record = make_record({"processing": {"21": {"status": "isolated"}}})
    assert upstream.read_upstream_isolation(record)["isolated"] is True


# --------------------------------------------------------------------------- #
# 成对记录扁平化与标识
# --------------------------------------------------------------------------- #

def test_flatten_pair_merges_processing_namespaces():
    payload = make_pair("pair-9",
                        left=make_record("s-text", rec_13(True, "passed")),
                        right=make_record("s-image", rec_12("auto_aligned")))
    flat = upstream.flatten_record(payload)
    assert flat["processing"]["13"]["passed"] is True
    assert flat["processing"]["12"]["tier"] == "auto_aligned"
    assert "left" not in flat and "right" not in flat
    assert flat["pair_id"] == "pair-9"


def test_resolve_identifier_prefers_sample_id_then_pair_id():
    sample_id, pair_id = upstream.resolve_identifier({"sample_id": "a", "pair_id": "p"})
    assert (sample_id, pair_id) == ("a", "p")

    sample_id, pair_id = upstream.resolve_identifier({"pair_id": "p"})
    assert (sample_id, pair_id) == (None, "p")

    sample_id, pair_id = upstream.resolve_identifier(
        make_pair("pair-3", left={"sample_id": "L"}, right={"sample_id": "R"}))
    assert sample_id == "L" or sample_id == "R"
    assert pair_id == "pair-3"


def test_resolve_identifier_none_when_absent():
    assert upstream.resolve_identifier({"text": {"content": "x"}}) == (None, None)


def test_collect_media_refs_gathers_declared_paths():
    record = make_record(None, {"image": "media/a.png",
                               "image_reference": {"path": "media/b.png"},
                               "text": {"content": "x"}})
    refs = upstream.collect_media_refs(record)
    assert "media/a.png" in refs
    assert "media/b.png" in refs


def test_collect_media_refs_ignores_absolute_and_traversal():
    record = make_record(None, {"image": "/abs/a.png",
                               "image_reference": {"path": "../escape.png"},
                               "video": {"path": "http://x/y.mp4"}})
    unsafe = upstream.unsafe_media_refs(record)
    assert "/abs/a.png" in unsafe
    assert "../escape.png" in unsafe
    assert "http://x/y.mp4" in unsafe
