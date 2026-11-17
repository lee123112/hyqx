"""#16 分档阈值来源测试（2026-11-05 修复）。

问题：#12 标定隔离线 0.22、#16 固定 0.25，同一份 CLIP 分会在一处判"通过"、
在另一处判"不对齐"并进隔离库（实测 800 条图文正样本里 33 条落在 0.22–0.25）。
修复：默认跟随上游节点自带的标定线（#12 ``thresholds`` / #14 ``low_score_threshold``），
上游没声明时才用本算子配置；``follow_upstream_thresholds=false`` 可复现旧行为。
"""
from __future__ import annotations

import copy

import make_sample
import pytest

from conftest import decisions_of
from utils import alignment as al

TEXT = "样例描述：一只动物在户外场景中活动"
MODEL = {"name": "CLIP-ViT/B-32", "version": "ViT-B-32"}


def image_pair(index: int, score: float, *, thresholds=None) -> dict:
    item = make_sample.pair(index, score)
    node = item.get("image_text_score")
    if isinstance(node, dict):
        node["model"] = MODEL
        if thresholds is not None:
            node["thresholds"] = thresholds
            # The sample builder retains its historical 0.25 fixture line;
            # construct a consistent #12 result for the line under test.
            low = thresholds.get("isolation", 0.20)
            high = thresholds.get("auto_align", 0.32)
            node["tier"] = "isolated" if score < low else ("auto_aligned" if score >= high else "pending_manual")
    return item


def video_pair(index: int, score: float, *, low=None) -> dict:
    left = make_sample.text_record("t%02d" % index, TEXT, "batch-16", "src-16")
    record = {"sample_id": "v%02d" % index, "batch_id": "batch-16", "text": None,
              "image": None, "audio": None, "processing": {},
              "video": {"path": "media/clip.mp4", "duration": 2.0, "fps": 2.0,
                        "frame_count": 0},
              "alignment": {"whole": None, "local": []}, "qc": {},
              "metadata": {"batch_id": "batch-16"}}
    node = {"operator": "video_text_scoring", "status": 1, "process_status": "passed",
            "passed": True, "video_text_score": score, "segment_mean": score,
            "model": MODEL}
    if low is not None:
        node["low_score_threshold"] = low
    record["video_text_score"] = node
    record["processing"] = {"14": node}
    return {"pair_id": "pair-t%02d--v%02d" % (index, index),
            "group_id": "grp-src-src-16", "modality_pair": ["text", "video"],
            "evidence": {"type": "A", "criterion": "same_source_id",
                         "evidence_source": "rule", "value": {"source_id": "src-16"}},
            "evidence_priority": 1,
            "left": left,
            "right": {"sample_id": "v%02d" % index, "modalities": ["video"],
                      "record": record},
            "producer": {"operator": "generate_candidate_pairs",
                         "operator_version": "v1.0.0", "uniqueId": "sample_task"}}


# ------------------------------------------------------------------ 解析

def test_node_thresholds_reads_12_and_14_shapes():
    assert al._node_thresholds({"thresholds": {"isolation": 0.22, "auto_align": 0.32}}) \
        == {"review": 0.22, "auto": 0.32}
    assert al._node_thresholds({"low_score_threshold": 0.23}) \
        == {"review": 0.23, "auto": None}
    assert al._node_thresholds({"thresholds": {"isolation": 0.3, "auto_align": 0.2}}) \
        == {"review": 0.3, "auto": None}
    assert al._node_thresholds({}) is None
    assert al._node_thresholds(None) is None


def test_effective_thresholds_follow_upstream(fresh_cfg):
    upstream = {"score": 0.23, "thresholds": {"review": 0.22, "auto": 0.32}}
    low, high, source = al.effective_thresholds(fresh_cfg, upstream)
    assert (low, high, source) == (0.22, 0.32, "upstream")


def test_effective_thresholds_config_when_disabled(fresh_cfg):
    cfg = copy.deepcopy(fresh_cfg)
    cfg[al.SECTION]["follow_upstream_thresholds"] = False
    upstream = {"score": 0.23, "thresholds": {"review": 0.22, "auto": 0.32}}
    low, high, source = al.effective_thresholds(cfg, upstream)
    assert (low, high, source) == (0.20, 0.32, "config")


def test_effective_thresholds_need_a_score(fresh_cfg):
    """上游只声明阈值没有分数时不跟随（无分走 pending/no_score 分支）。"""
    low, high, source = al.effective_thresholds(
        fresh_cfg, {"score": None, "thresholds": {"review": 0.22, "auto": 0.32}})
    assert (low, high, source) == (0.20, 0.32, "config")


def test_effective_thresholds_keeps_auto_when_upstream_lacks_it(fresh_cfg):
    low, high, source = al.effective_thresholds(
        fresh_cfg, {"score": 0.26, "thresholds": {"review": 0.23, "auto": None}})
    assert (low, high, source) == (0.23, 0.32, "upstream")


# ------------------------------------------------------------------ 端到端

def test_borderline_score_is_not_reisolated_by_16(plan):
    """0.23 在 #12 的 0.22 线上是"通过"，#16 不得用 0.25 把它判成不对齐。"""
    result = plan([image_pair(1, 0.23, thresholds={"isolation": 0.22, "auto_align": 0.32})])
    whole = decisions_of(result)["pair-t01--i01"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["threshold_review"] == 0.22
    assert whole["threshold_source"] == "upstream"
    assert result.isolation == []


def test_follow_upstream_can_be_disabled_to_reproduce_old_behaviour(plan, fresh_cfg):
    cfg = copy.deepcopy(fresh_cfg)
    cfg[al.SECTION]["follow_upstream_thresholds"] = False
    # 0.19 < 本算子配置 0.20 → 不跟随上游 0.22 时会判不对齐（旧行为）
    result = plan([image_pair(2, 0.19, thresholds={"isolation": 0.22, "auto_align": 0.32})],
                  cfg=cfg)
    whole = decisions_of(result)["pair-t02--i02"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["threshold_review"] == 0.20
    assert whole["threshold_source"] == "config"


def test_upstream_thresholds_do_not_affect_rescore(plan, media, stub_encoder):
    """人工改描述后的重评分按上游线分档（重评分仍是 CLIP 分）。"""
    make_sample.write_png(media / "media" / "cat.png", 8, 6, (200, 160, 120))
    events = al.load_human_events([(1, {
        "pair_id": "pair-t03--i03", "reviewer_id": "ann-01",
        "reviewed_at": "2026-11-05T09:30:00+08:00",
        "new_description": "人工改写后的描述"}, None)])
    result = plan([image_pair(3, 0.21, thresholds={"isolation": 0.22, "auto_align": 0.30})],
                  encoder=stub_encoder(0.28), events=events)
    whole = decisions_of(result)["pair-t03--i03"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW   # 0.22 ≤ 0.28 < 0.30
    assert whole["bucketed_by"] == al.CONFIRM_RESCORE
    assert whole["threshold_review"] == 0.22


def test_video_route_follows_14_threshold(plan):
    result = plan([video_pair(4, 0.24, low=0.21)])
    whole = decisions_of(result)["pair-t04--v04"]
    assert whole["threshold_review"] == 0.21
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert result.isolation == []


def test_no_upstream_threshold_keeps_config_line(plan):
    item = image_pair(5, 0.19)
    item["image_text_score"].pop("thresholds", None)
    result = plan([item])
    whole = decisions_of(result)["pair-t05--i05"]
    assert whole["threshold_source"] == "config"
    assert whole["threshold_review"] == 0.20
    assert whole["decision"] == al.DECISION_NOT_ALIGNED


# ------------------------------------------------------------------ 配置

def test_follow_upstream_defaults_true_and_rejects_non_bool():
    import inference

    cfg = al.validate_config(inference.load_config(None))
    assert cfg[al.SECTION]["follow_upstream_thresholds"] is True
    raw = inference.load_config(None)
    raw[al.SECTION]["follow_upstream_thresholds"] = "false"
    with pytest.raises(al.ConfigError):
        al.validate_config(raw)


def test_follow_upstream_is_request_overridable(fresh_cfg):
    cfg = al.apply_overrides(fresh_cfg, {"follow_upstream_thresholds": False})
    assert cfg[al.SECTION]["follow_upstream_thresholds"] is False
