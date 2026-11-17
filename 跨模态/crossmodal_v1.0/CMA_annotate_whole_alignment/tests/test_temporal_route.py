"""#16 时序路线测试：消费 #15 的 ``processing["15"]`` 结论（2026-11-05 修复）。

背景：时序记录（自带 timeline/temporal_bindings/temporal_anchors）没有 #12/#14 的
CLIP 分，#14 已对其判 not_applicable。修复前 #16 只按 CLIP 分档，读不到分就把这些
样本全部押进人工队列（reason=no_score），#15 已经算好的时间轴结论被丢掉。
修复后：无 CLIP 分但有 #15 结论时，按 #15 分档，并写入
``bucketed_by="temporal_15"`` / ``evidence.temporal`` 供追溯。

这些用例不加载模型，只构造上游节点。
"""
from __future__ import annotations

import copy

import make_sample
import pytest

from conftest import decisions_of
from utils import alignment as al

TEXT = "样例描述：人物在画面中先挥手再转身"
TIMELINE = {"tracks": [{"id": "trk-1", "items": [{"start": 0.0, "end": 1.5,
                                                  "caption": "挥手"}]}]}
TEMPORAL_BINDINGS = [{"text": "先挥手", "start": 0.0, "end": 1.5}]
TEMPORAL_ANCHORS = [{"text": "再转身", "time": 2.0}]


def _temporal_node(process_status: str, *, reason_code="within_threshold",
                   operator="temporal_consistency") -> dict:
    """构造 #15 节点（与 #15 主输出同构：status 为 int，字符串结论在 process_status）。"""
    return {
        "operator": operator,
        "operator_version": "v1.0.0",
        "status": 1 if process_status == "passed" else 0,
        "process_status": process_status,
        "decision": process_status,
        "reason_code": reason_code,
        "label": "时序-时空一致" if process_status == "passed" else "时序-时空不一致",
        "review_required": process_status in ("pending_calibration", "skipped"),
    }


def temporal_pair(index: int, node=None, *, side="record", extra_temporal=True) -> dict:
    """video+text 时序候选对；#15 节点挂在视频侧记录或 pair 顶层。"""
    left_id, right_id = "t%02d" % index, "v%02d" % index
    left = make_sample.text_record(left_id, TEXT, "batch-16", "src-16")
    video = {"path": "media/clip.mp4", "duration": 2.0, "fps": 2.0, "frame_count": 0}
    record = {"sample_id": right_id, "batch_id": "batch-16", "text": None,
              "image": None, "video": video, "audio": None, "processing": {},
              "alignment": {"whole": None, "local": []}, "qc": {},
              "metadata": {"batch_id": "batch-16"}}
    if extra_temporal:
        record["timeline"] = copy.deepcopy(TIMELINE)
        record["temporal_bindings"] = copy.deepcopy(TEMPORAL_BINDINGS)
        record["temporal_anchors"] = copy.deepcopy(TEMPORAL_ANCHORS)
    if node is not None:
        if side == "record":
            record["processing"]["15"] = node
        else:
            # pair 顶层节点在下面挂
            pass
    right = {"sample_id": right_id, "modalities": ["video"], "record": record}
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
    if node is not None and side == "pair":
        item["processing"] = {"15": node}
    return item


def _cfg_with(route: bool, fresh_cfg: dict) -> dict:
    cfg = copy.deepcopy(fresh_cfg)
    cfg[al.SECTION]["temporal_route"] = route
    return cfg


# ------------------------------------------------------------------ 解析层

def test_temporal_upstream_reads_record_processing_15():
    pair = temporal_pair(1, _temporal_node("passed"))
    found = al.temporal_upstream(pair, sides=[pair["left"], pair["right"]])
    assert found is not None
    assert found["status"] == "passed"
    assert found["source"] == al.SOURCE_PROCESSING_15
    assert found["review_required"] is False


def test_temporal_upstream_reads_pair_level_and_prefers_it():
    pair = temporal_pair(2, _temporal_node("failed"), side="record")
    pair["processing"] = {"15": _temporal_node("passed")}
    found = al.temporal_upstream(pair, sides=[pair["right"]])
    assert found["status"] == "passed"


def test_temporal_upstream_ignores_foreign_nodes():
    """processing["15"] 不是 #15 节点（如别的算子误写）时不参与判定。"""
    pair = temporal_pair(3, _temporal_node("passed", operator="other_operator"))
    assert al.temporal_upstream(pair, sides=[pair["right"]]) is None
    pair = temporal_pair(4, {"clip_score": 0.9})
    assert al.temporal_upstream(pair, sides=[pair["right"]]) is None


def test_resolve_upstream_temporal_only_without_clip_score():
    pair = temporal_pair(5, _temporal_node("passed"))
    info = al.resolve_upstream(pair, sides=[pair["left"], pair["right"]])
    assert info["score"] is None
    assert info["origin"] == al.ORIGIN_UPSTREAM_15
    assert info["temporal"]["status"] == "passed"


def test_clip_score_still_wins_over_temporal():
    """只要存在 CLIP 分，#15 结论不参与（图文/视文路线行为完全不变）。"""
    pair = temporal_pair(6, _temporal_node("passed"))
    pair["image_text_score"] = {"clip_score": 0.28, "tier": "pending_manual"}
    info = al.resolve_upstream(pair, sides=[pair["left"], pair["right"]])
    assert info["score"] == 0.28
    assert info["temporal"] is None


# ------------------------------------------------------------------ 分档

def test_temporal_passed_is_auto_aligned(plan):
    result = plan([temporal_pair(7, _temporal_node("passed"))])
    whole = decisions_of(result)["pair-t07--v07"]
    assert whole["decision"] == al.DECISION_AUTO_ALIGNED
    assert whole["clip_score"] is None
    assert whole["score_source"] == al.SOURCE_PROCESSING_15
    assert whole["bucketed_by"] == al.CONFIRM_TEMPORAL
    assert whole["confirmation_method"] == al.CONFIRM_TEMPORAL
    assert whole["evidence"]["temporal"]["status"] == "passed"
    assert whole["evidence"]["upstream"]["origin"] == al.ORIGIN_UPSTREAM_15
    assert whole["evidence"]["reason"] == al.REASON_TEMPORAL_PASSED
    assert result.stats["conservation_ok"] is True

    record = result.main[0]
    node = record["processing"]["16"]
    assert node["bucketed_by"] == al.CONFIRM_TEMPORAL
    assert node["temporal"]["status"] == "passed"
    assert node["clip_score"] is None


def test_temporal_failed_is_isolated_and_reuses_upstream(plan):
    result = plan([temporal_pair(8, _temporal_node("failed"))])
    assert result.review == []
    entry = result.isolation[0]
    whole = entry["record"]["alignment"]["whole"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["evidence"]["reason"] == al.REASON_TEMPORAL_EXCEEDED
    assert whole["isolation_source"]["reused"] is True
    assert whole["isolation_source"]["temporal"]["status"] == "failed"
    assert whole["evidence"]["upstream"]["origin"] == al.ORIGIN_UPSTREAM_15
    assert result.stats["conservation_ok"] is True


@pytest.mark.parametrize("status", ["pending_calibration", "skipped", "not_applicable"])
def test_temporal_non_decisive_statuses_go_to_review(plan, status):
    result = plan([temporal_pair(9, _temporal_node(status))])
    whole = decisions_of(result)["pair-t09--v09"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["bucketed_by"] == al.CONFIRM_TEMPORAL
    assert len(result.review) == 1


def test_temporal_route_can_be_disabled_for_regression(plan, fresh_cfg):
    """temporal_route=false 复现旧行为：全部 pending/no_score（回归对比用）。"""
    cfg = _cfg_with(False, fresh_cfg)
    result = plan([temporal_pair(10, _temporal_node("passed"))], cfg=cfg)
    whole = decisions_of(result)["pair-t10--v10"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["evidence"]["reason"] == "no_score"
    assert whole["evidence"]["temporal"] is None
    assert whole["bucketed_by"] == "none"


def test_temporal_route_without_15_node_keeps_no_score(plan):
    """没有 #15 结论时保持原有语义：不猜结论，交人工。"""
    result = plan([temporal_pair(11, None)])
    whole = decisions_of(result)["pair-t11--v11"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["evidence"]["reason"] == "no_score"
    assert whole["evidence"]["temporal"] is None


def test_human_event_still_precedes_temporal(plan):
    """人工结论优先于 #15：改描述重评分仍按 CLIP 新分档（隔离 line 0.25）。"""
    events = al.load_human_events([(1, {
        "pair_id": "pair-t12--v12", "reviewer_id": "ann-04",
        "reviewed_at": "2026-11-05T09:30:00+08:00",
        "decision": "not_aligned"}, None)])
    result = plan([temporal_pair(12, _temporal_node("passed"))], events=events)
    whole = decisions_of(result)["pair-t12--v12"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["confirmation_method"] == al.CONFIRM_HUMAN


def test_temporal_route_keeps_identical_schema(plan):
    """时序路线与图文路线写出的键集合一致（协议 diff 0）。"""
    image_result = plan([make_sample.pair(1, 0.41)])
    temporal_result = plan([temporal_pair(13, _temporal_node("passed"))])
    image_whole = decisions_of(image_result)["pair-t01--i01"]
    temporal_whole = decisions_of(temporal_result)["pair-t13--v13"]
    assert set(temporal_whole) == set(image_whole)
    assert set(temporal_whole["evidence"]) == set(image_whole["evidence"])
    assert set(temporal_result.main[0]["processing"]["16"]) == \
        set(image_result.main[0]["processing"]["16"])


# ------------------------------------------------------------------ 配置

def test_temporal_route_defaults_true_and_rejects_non_bool():
    import inference

    cfg = al.validate_config(inference.load_config(None))
    assert cfg[al.SECTION]["temporal_route"] is True
    raw = inference.load_config(None)
    raw[al.SECTION]["temporal_route"] = "false"
    with pytest.raises(al.ConfigError):
        al.validate_config(raw)


def test_temporal_route_is_request_overridable(fresh_cfg):
    cfg = al.apply_overrides(fresh_cfg, {"temporal_route": False})
    assert cfg[al.SECTION]["temporal_route"] is False
