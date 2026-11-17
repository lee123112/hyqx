"""#16 业务核心测试：阈值分档、人工三按钮、否决锁定、改描述重评分、守恒与异常路径。"""
from __future__ import annotations

import copy
import json

import pytest

import make_sample
from conftest import FailingEncoder, StubEncoder, decisions_of
from utils import alignment as al
from utils import backends, io as op_io


def _events(items) -> al.HumanEventSet:
    lines = [(index + 1, item, None) for index, item in enumerate(items)]
    return al.load_human_events(lines)


# ------------------------------------------------------------------ 配置

def test_default_thresholds_match_table1(raw_cfg):
    cfg = al.validate_config(raw_cfg)
    # [2026-11-05 全量标定] 隔离线 0.20（与 #12/#14 一致），自动档 0.32 不变
    assert al.thresholds(cfg) == (0.20, 0.32)


@pytest.mark.parametrize("isolation,auto", [(0.19, 0.32), (0.41, 0.32), (0.25, 0.24),
                                            (0.25, 0.51), (0.25, 0.25)])
def test_threshold_domain_and_order_rejected(raw_cfg, isolation, auto):
    cfg = copy.deepcopy(raw_cfg)
    cfg[al.SECTION]["isolation_threshold"] = isolation
    cfg[al.SECTION]["auto_align_threshold"] = auto
    with pytest.raises(al.ConfigError):
        al.validate_config(cfg)


@pytest.mark.parametrize("mutate", [
    lambda cfg: cfg[al.SECTION].__setitem__("enabled", False),
    lambda cfg: cfg[al.SECTION].__setitem__("rescore_modified_description", False),
    lambda cfg: cfg["model"].__setitem__("library", "magic"),
    lambda cfg: cfg["model"].__setitem__("device", "tpu"),
    lambda cfg: cfg["model"].__setitem__("allow_degraded", "yes"),
])
def test_invalid_config_rejected(raw_cfg, mutate):
    cfg = copy.deepcopy(raw_cfg)
    mutate(cfg)
    with pytest.raises(al.ConfigError):
        al.validate_config(cfg)


def test_request_override_cannot_change_model_or_identity(raw_cfg):
    with pytest.raises(al.ConfigError):
        al.apply_overrides(raw_cfg, {"weights": "/tmp/other.bin"})
    with pytest.raises(al.ConfigError):
        al.apply_overrides(raw_cfg, {"enabled": False})
    merged = al.apply_overrides(raw_cfg, {"auto_align_threshold": 0.30})
    assert merged[al.SECTION]["auto_align_threshold"] == 0.30
    assert raw_cfg[al.SECTION]["auto_align_threshold"] == 0.32


# ------------------------------------------------------------------ 阈值分档

@pytest.mark.parametrize("score,expected", [
    (0.10, al.DECISION_NOT_ALIGNED),          # 远低于不对齐阈值
    (0.24, al.DECISION_NOT_ALIGNED),          # 下阈值外
    (0.25, al.DECISION_PENDING_MANUAL_REVIEW),  # 下边界（含）→ 人工
    (0.28, al.DECISION_PENDING_MANUAL_REVIEW),
    (0.319999, al.DECISION_PENDING_MANUAL_REVIEW),
    (0.32, al.DECISION_AUTO_ALIGNED),         # 上边界（含）→ 自动
    (0.41, al.DECISION_AUTO_ALIGNED),
])
def test_threshold_buckets(plan, build_pair, score, expected):
    result = plan([build_pair(1, score)])
    assert result.stats["conservation_ok"] is True
    assert decisions_of(result)["pair-t01--i01"]["decision"] == expected


def test_review_queue_only_holds_middle_band_and_sampling(plan, build_pair):
    result = plan([build_pair(1, 0.28), build_pair(2, 0.24), build_pair(3, 0.55,
                                                                      sampling=True)])
    queued = {entry["pair_id"] for entry in result.review}
    assert queued == {"pair-t01--i01", "pair-t03--i03"}
    isolated = {entry["pair_id"] for entry in result.isolation}
    assert isolated == {"pair-t02--i02"}
    assert len(result.isolation) == 1 and result.stats["not_aligned"] == 1


def test_sampling_hit_moves_high_score_to_review(plan, build_pair):
    result = plan([build_pair(2, 0.55, sampling=True)])
    whole = decisions_of(result)["pair-t02--i02"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["sampling_hit"] is True
    assert whole["evidence"]["reason"] == "sampling_hit"
    assert whole["evidence"]["sampling"]["source"] == "metadata.sampling_hit"


def test_low_score_goes_to_isolation_not_review_queue(plan, build_pair):
    result = plan([build_pair(7, 0.24)])
    assert result.review == []
    entry = result.isolation[0]
    assert entry["label"] == al.LABEL_NOT_ALIGNED
    assert entry["destination"] == "样本隔离库"
    assert entry["evidence"]["stage"] == "样本业务状态"


# ------------------------------------------------------------------ 上游分数消费

def test_no_upstream_score_never_fabricates(plan, build_pair):
    result = plan([build_pair(10, None, upstream_absent=True)])
    whole = decisions_of(result)["pair-t10--i10"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["clip_score"] is None
    assert whole["score_source"] == al.SOURCE_NONE
    assert whole["evidence"]["reason"] == "no_score"


def test_upstream_isolated_tier_followed_without_score(plan, build_pair):
    result = plan([build_pair(9, None, tier="isolated", model=None)])
    whole = decisions_of(result)["pair-t09--i09"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["clip_score"] is None
    assert whole["evidence"]["upstream"]["isolated"] is True
    assert result.review == []


def test_upstream_score_and_model_version_recorded(plan, build_pair):
    result = plan([build_pair(1, 0.41)])
    whole = decisions_of(result)["pair-t01--i01"]
    assert whole["clip_score"] == 0.41
    assert whole["score_source"] == "image_text_score"
    assert whole["model"] == "CLIP-ViT/B-32"
    assert whole["model_version"] == "ViT-B-32"
    assert whole["threshold_review"] == 0.25 and whole["threshold_auto"] == 0.32  # 跟随样例上游声明的线


def test_upstream_namespaces_are_not_overwritten(plan, build_pair):
    item = build_pair(1, 0.41)
    item["alignment"] = {"whole": None, "local": [{"span": [0, 4]}]}
    item["qc"] = {"checked": True}
    item["custom_extension"] = {"keep": 1}
    result = plan([item])
    record = result.main[0]
    assert record["alignment"]["local"] == [{"span": [0, 4]}]
    assert record["qc"] == {"checked": True}
    assert record["custom_extension"] == {"keep": 1}
    assert record["left"]["record"]["text"]["content"].startswith("样例描述")


def test_every_published_score_is_upstream_or_rescore(plan, build_pair):
    result = plan([build_pair(1, 0.41), build_pair(2, 0.24), build_pair(10, None,
                                                                      upstream_absent=True)])
    for record in result.main:
        whole = record["alignment"]["whole"]
        assert whole["score_source"] in (al.SOURCE_UPSTREAM, al.SOURCE_RESCORE,
                                         "image_text_score", "processing.12",
                                         "alignment.image_text", al.SOURCE_NONE)
        if whole["clip_score"] is not None:
            assert whole["score_source"] != al.SOURCE_NONE


# ------------------------------------------------------------------ 人工三按钮

def test_human_aligned_writes_manual_aligned(plan, build_pair):
    events = _events([{"pair_id": "pair-t11--i11", "decision": "aligned",
                       "reviewer_id": "ann-01",
                       "reviewed_at": "2026-10-07T09:10:00+08:00",
                       "previous_decision": "pending_manual_review",
                       "comment": "人工确认一致"}])
    result = plan([build_pair(11, 0.28)], events=events)
    whole = decisions_of(result)["pair-t11--i11"]
    assert whole["decision"] == al.DECISION_MANUAL_ALIGNED
    assert whole["confirmation_method"] == al.CONFIRM_HUMAN
    assert whole["human_verified"] is True
    assert whole["reviewer_id"] == "ann-01"
    assert whole["reviewed_at"] == "2026-10-07T09:10:00+08:00"
    assert whole["previous_decision"] == "pending_manual_review"
    assert whole["comment"] == "人工确认一致"
    assert whole["review_task_id"] == "16-test_task-pair-t11--i11"
    assert whole["evidence"]["label"] == al.LABEL_PASS
    assert result.main[0]["pair_id"] == "pair-t11--i11"


def test_human_not_aligned_locks_veto_against_high_score(plan, build_pair):
    events = _events([{"pair_id": "pair-t12--i12", "decision": "not_aligned",
                       "reviewer_id": "ann-02",
                       "reviewed_at": "2026-10-07T09:12:00+08:00"}])
    result = plan([build_pair(12, 0.41)], events=events)
    whole = result.isolation[0]["record"]["alignment"]["whole"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["human_veto_locked"] is True
    assert whole["human_verified"] is True
    assert whole["clip_score"] == 0.41          # 真实分数保留，但结论服从人工
    assert result.review == []
    assert result.stats["veto_locked"] == 1


def test_existing_veto_lock_survives_later_high_score(plan, build_pair):
    result = plan([build_pair(15, 0.55, veto_locked=True)])
    whole = result.isolation[0]["record"]["alignment"]["whole"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["human_veto_locked"] is True
    assert whole["evidence"]["reason"] == "human_veto_locked"


def test_human_uncertain_routes_to_25(plan, build_pair):
    events = _events([{"pair_id": "pair-t13--i13", "decision": "uncertain",
                       "reviewer_id": "ann-03",
                       "reviewed_at": "2026-10-07T09:20:00+08:00"}])
    result = plan([build_pair(13, 0.41)], events=events)
    whole = decisions_of(result)["pair-t13--i13"]
    assert whole["decision"] == al.DECISION_UNCERTAIN
    assert whole["human_verified"] is True
    entry = [item for item in result.review if item["pair_id"] == "pair-t13--i13"][0]
    assert entry["destination"] == "人工复核队列(#25)"
    assert entry["entry_kind"] == "human_queue"


def test_human_event_requires_reviewer_identity(plan, build_pair):
    events = _events([{"pair_id": "pair-t11--i11", "decision": "aligned",
                       "reviewed_at": "2026-10-07T09:10:00+08:00"}])
    result = plan([build_pair(11, 0.28)], events=events)
    assert decisions_of(result)["pair-t11--i11"]["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert events.rejected[0]["reason"] == "human_event_missing_reviewer"
    assert result.stats["human_events_rejected"] >= 1


@pytest.mark.parametrize("item,reason", [
    ({"pair_id": "p", "decision": "maybe", "reviewer_id": "a",
      "reviewed_at": "t"}, "human_event_unknown_decision"),
    ({"pair_id": "p", "reviewer_id": "a", "reviewed_at": "t"}, "human_event_no_action"),
    ({"pair_id": "p", "reviewer_id": "a", "reviewed_at": "t",
      "new_description": "  "}, "human_event_invalid_description"),
    ({"decision": "aligned", "reviewer_id": "a",
      "reviewed_at": "t"}, "human_event_missing_target"),
    ({"pair_id": "p", "decision": "aligned", "reviewed_at": "t"},
     "human_event_missing_reviewer"),
    ({"pair_id": "p", "decision": "aligned", "reviewer_id": "a"},
     "human_event_missing_reviewed_at"),
])
def test_human_event_validation_reasons(item, reason):
    events = _events([item])
    assert events.by_key == {}
    assert events.rejected[0]["reason"] == reason
    assert events.rejected[0]["label"] == al.LABEL_FLOW_INVALID_HUMAN_EVENT
    assert events.rejected[0]["destination"] == al.DEST_MANUAL_EVENT


def test_later_human_event_supersedes_earlier_one():
    events = _events([
        {"pair_id": "p", "decision": "aligned", "reviewer_id": "a",
         "reviewed_at": "2026-10-07T10:00:00+08:00"},
        {"pair_id": "p", "decision": "not_aligned", "reviewer_id": "b",
         "reviewed_at": "2026-10-07T11:00:00+08:00"},
    ])
    assert events.by_key["p"]["decision"] == "not_aligned"
    assert events.superseded[0]["kept_reviewed_at"] == "2026-10-07T11:00:00+08:00"


def test_unmatched_human_event_lands_in_audit(plan, build_pair):
    events = _events([{"pair_id": "pair-not-exist", "decision": "aligned",
                       "reviewer_id": "ann-09",
                       "reviewed_at": "2026-10-07T09:00:00+08:00"}])
    result = plan([build_pair(1, 0.41)], events=events)
    assert result.stats["human_events_unmatched"] == 1
    entry = [item for item in result.audit
             if item.get("reason") == "human_event_target_not_found"][0]
    assert entry["label"] == al.LABEL_FLOW_INVALID_HUMAN_EVENT


def test_inline_human_review_is_honoured(plan, build_pair):
    item = build_pair(1, 0.28)
    item["metadata"] = {"human_review": {"pair_id": "pair-t01--i01",
                                         "decision": "aligned",
                                         "reviewer_id": "ann-inline",
                                         "reviewed_at": "2026-10-07T09:00:00+08:00"}}
    result = plan([item])
    whole = decisions_of(result)["pair-t01--i01"]
    assert whole["decision"] == al.DECISION_MANUAL_ALIGNED
    assert whole["evidence"]["human_event"]["inline"] is True


# ------------------------------------------------------------------ 改描述重算

def test_rescore_uses_same_instance_and_rebuckets(plan, media, build_pair):
    encoder = StubEncoder(score=0.42)
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写：一只猫在室内沙发上休息"}])
    result = plan([build_pair(14, 0.18, media="media/cat.png")], encoder=encoder,
                  events=events)
    whole = decisions_of(result)["pair-t14--i14"]
    assert whole["decision"] == al.DECISION_AUTO_ALIGNED
    assert whole["rescored"] is True
    assert whole["score_source"] == al.SOURCE_RESCORE
    assert whole["clip_score"] == pytest.approx(0.42, abs=1e-9)
    assert encoder.text_calls == 1 and encoder.image_calls == 1
    edit = whole["description_edit"]
    assert edit["score_before"] == 0.18 and edit["score_after"] == pytest.approx(0.42)
    assert edit["new_description_ref"]["length"] > 0
    assert "sha256" in edit["new_description_ref"]
    assert result.main[0]["metadata"]["human_description"]["reviewer_id"] == "ann-04"


def test_rescore_overrides_low_old_score_but_keeps_old_score_in_evidence(plan, media,
                                                                       build_pair):
    encoder = StubEncoder(score=0.26)
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([build_pair(14, 0.90, media="media/cat.png")], encoder=encoder,
                  events=events)
    whole = decisions_of(result)["pair-t14--i14"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["clip_score"] == pytest.approx(0.26)
    assert whole["description_edit"]["score_before"] == 0.90


def test_rescore_rebuckets_even_when_event_carries_old_aligned_decision(plan, media,
                                                                        build_pair):
    """表1 #16 算法 4)：人改描述后「仍按 0.25/0.32 分档」，
    事件里携带的旧结论（aligned）不得覆盖由新分算出的档位。"""
    encoder = StubEncoder(score=0.26)
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "decision": "aligned",
                       "new_description": "人工改写后的描述"}])
    result = plan([build_pair(14, 0.90, media="media/cat.png")], encoder=encoder,
                  events=events)
    whole = decisions_of(result)["pair-t14--i14"]
    assert whole["bucketed_by"] == al.CONFIRM_RESCORE
    assert whole["human_rewritten"] is True
    assert whole["human_decision"] == "aligned"
    assert whole["clip_score"] == pytest.approx(0.26)
    assert whole["confirmation_method"] == al.CONFIRM_RESCORE
    # 0.26 落在 [0.25, 0.32) → 人工队列，而不是事件里写的 aligned
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["evidence"]["reason"] == "rescore_borderline_score"


def test_rescore_below_isolation_buckets_not_aligned(plan, media, build_pair):
    """表1 #16 算法 4) + 3)：人改描述后新分低于 0.25 → 隔离档，并沿用 #12 隔离口径。"""
    encoder = StubEncoder(score=0.18)
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([build_pair(14, 0.90, media="media/cat.png")], encoder=encoder,
                  events=events)
    whole = decisions_of(result)["pair-t14--i14"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["bucketed_by"] == al.CONFIRM_RESCORE
    assert whole["isolation_source"]["reused"] is True
    assert "rescore_below_isolation" in whole["evidence"]["notes"]


def test_below_isolation_reuses_upstream_isolation_record(plan, build_pair):
    """表1 #16 算法 3)：低于 0.25 沿用评分工具的隔离结果，不自造同级结论。"""
    result = plan([build_pair(7, 0.18)])
    whole = decisions_of(result)["pair-t07--i07"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["bucketed_by"] == "upstream_score"
    assert whole["isolation_source"]["reused"] is True
    assert whole["isolation_source"]["origin"] == "12"


def test_human_not_aligned_veto_survives_high_rescore(plan, media, build_pair):
    """边界：人工判不对齐后，即使改描述重算出高分也不能改回通过。"""
    encoder = StubEncoder(score=0.90)
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "decision": "not_aligned",
                       "new_description": "人工改写后的描述"}])
    result = plan([build_pair(14, 0.30, media="media/cat.png")], encoder=encoder,
                  events=events)
    whole = decisions_of(result)["pair-t14--i14"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["human_veto_locked"] is True
    assert whole["confirmation_method"] == al.CONFIRM_HUMAN
    assert whole["evidence"]["reason"] == "human_not_aligned"


def test_bucket_by_score_is_pure_thresholds():
    assert al.bucket_by_score(0.32, 0.25, 0.32)[0] == al.DECISION_AUTO_ALIGNED
    assert al.bucket_by_score(0.25, 0.25, 0.32)[0] == al.DECISION_PENDING_MANUAL_REVIEW
    assert al.bucket_by_score(0.24, 0.25, 0.32)[0] == al.DECISION_NOT_ALIGNED
    assert al.bucket_by_score(0.40, 0.25, 0.32, sampling=True)[0] == \
        al.DECISION_PENDING_MANUAL_REVIEW
    assert al.bucket_by_score(None, 0.25, 0.32) == (None, "no_score")


def test_rescore_deferred_is_degraded_not_faked(plan, media, build_pair):
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([build_pair(14, 0.30, media="media/cat.png")], encoder=None,
                  events=events)
    whole = decisions_of(result)["pair-t14--i14"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["rescored"] is False
    assert whole["score_source"] == "image_text_score"   # 仍然如实标明是上游旧分
    # [v4.2] status 双字段：int status=0（失败）+ 原字符串迁入 process_status
    assert whole["description_edit"]["process_status"] == al.REASON_MODEL_UNAVAILABLE
    assert whole["description_edit"]["status"] == 0
    assert whole["description_edit"]["score_after"] is None
    assert result.stats["degraded"] is True
    assert result.stats["rescore_deferred"] == 1


def test_rescore_without_model_fails_when_degradation_not_allowed(plan, media, build_pair,
                                                                 fresh_cfg):
    cfg = copy.deepcopy(fresh_cfg)
    cfg["model"]["allow_degraded"] = False
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    with pytest.raises(al.RescoreUnavailable):
        plan([build_pair(14, 0.30, media="media/cat.png")], encoder=None,
             events=events, cfg=cfg)


def test_rescore_inference_failure_does_not_emit_score(plan, media, build_pair):
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([build_pair(14, 0.30, media="media/cat.png")],
                  encoder=FailingEncoder(), events=events)
    whole = decisions_of(result)["pair-t14--i14"]
    assert whole["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert whole["rescored"] is False
    assert whole["description_edit"]["score_after"] is None
    assert str(whole["description_edit"]["process_status"]).startswith("inference_failed")
    assert whole["description_edit"]["status"] == 0   # [v4.2] int status（0 失败）
    assert result.stats["degraded"] is True


def test_rescore_requires_readable_media(plan, build_pair):
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "人工改写后的描述"}])
    result = plan([build_pair(14, 0.30, media="media/missing.png")],
                  encoder=StubEncoder(0.42), events=events)
    assert result.stats["structural_rejects"] == 1
    entry = result.rejects[0]
    assert entry["reason"] == "media_missing"
    assert entry["label"] == al.LABEL_ORIGIN_BROKEN
    assert entry["destination"] == al.DEST_ORIGIN


def test_missing_media_without_rescore_is_only_counted(plan, build_pair):
    result = plan([build_pair(6, 0.319999, media="media/missing.png")])
    assert decisions_of(result)["pair-t06--i06"]["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert result.rejects == []
    assert result.stats["media_missing"] == 1


def test_media_path_escape_is_origin_reject(plan, build_pair):
    events = _events([{"pair_id": "pair-t14--i14", "reviewer_id": "ann-04",
                       "reviewed_at": "2026-10-07T09:30:00+08:00",
                       "new_description": "改写"}])
    result = plan([build_pair(14, 0.30, media="../../etc/passwd")],
                  encoder=StubEncoder(0.42), events=events)
    assert result.rejects[0]["reason"] == "media_path_invalid"
    assert result.rejects[0]["label"] == al.LABEL_ORIGIN_UNPARSABLE


# ------------------------------------------------------------------ 结构/坏行/不适用

def test_bad_lines_and_missing_field_and_duplicate(plan, build_pair):
    raw_lines = ['{"pair_id": "broken",', json.dumps({"left": {}, "right": {}},
                                                     ensure_ascii=False)]
    result = plan([build_pair(1, 0.41), build_pair(1, 0.41)], raw_lines=raw_lines)
    reasons = sorted(item["reason"] for item in result.rejects)
    assert reasons == ["duplicate_pair_id", "json_parse_error", "missing_field: pair_id"]
    labels = {item["reason"]: item["label"] for item in result.rejects}
    assert labels["json_parse_error"] == al.LABEL_ORIGIN_UNPARSABLE
    assert labels["missing_field: pair_id"] == al.LABEL_ORIGIN_MISSING_FIELD
    assert labels["duplicate_pair_id"] == al.LABEL_ORIGIN_MISSING_FIELD


def test_encoding_error_and_blank_line_counted(tmp_path, raw_cfg):
    path = tmp_path / "mixed.jsonl"
    path.write_bytes(
        b"\n" + b'{"pair_id": "pair-t01--i01", "left": {}, "right": {}}\n'
        + b'\xff\xfe{"pair_id": "x"}\n' + b"\n")
    result = al.plan_alignment(op_io.read_jsonl(path),
                              al.validate_config(raw_cfg),
                              unique_id="t_f1", media_root=tmp_path,
                              now="2026-10-07T10:00:00+08:00")
    assert result.stats["blank_lines"] == 2
    assert result.stats["input"] == 2            # 坏行计入 input，空行不计
    assert result.stats["bad_lines"] == 1
    assert result.stats["structural_rejects"] == 0
    assert result.stats["not_applicable"] == 1   # 空左右侧无图文 → 不适用，不隔离
    assert result.rejects[0]["reason"] == "encoding_error"
    assert result.rejects[0]["label"] == al.LABEL_ORIGIN_ENCODING
    assert result.stats["conservation_ok"] is True


def test_audio_text_without_route_score_waits_for_real_evidence(plan, build_pair):
    result = plan([build_pair(16, 0.90, modality_pair=("audio", "text"),
                              upstream_absent=True)])
    assert len(result.main) == len(result.review) == 1
    assert result.isolation == [] and result.not_applicable == []
    assert result.main[0]["alignment"]["whole"]["decision"] == al.DECISION_PENDING_MANUAL_REVIEW
    assert result.main[0]["alignment"]["whole"]["clip_score"] is None


def test_flat_single_record_supported(plan):
    record = {
        "sample_id": "flat-01",
        "text": {"content": "一只猫在沙发上"},
        "image": {"path": "media/cat.png"},
        "image_text_score": {"clip_score": 0.41, "tier": "auto_aligned"},
    }
    result = plan([record])
    assert result.stats["applicable"] == 1
    assert result.stats["auto_aligned"] == 1
    assert result.main[0]["sample_id"] == "flat-01"
    assert result.main[0]["alignment"]["whole"]["decision"] == al.DECISION_AUTO_ALIGNED


def test_upstream_ledger_isolated_entry_followed(plan, build_pair):
    record = {"line": 3, "pair_id": "pair-t01--i01", "sample_id": "pair-t01--i01",
              "reason": "score_below_isolation", "label": al.LABEL_NOT_ALIGNED,
              "tier": "isolated", "action": "isolate", "destination": "样本隔离库",
              "record": build_pair(1, None, upstream_absent=True)}
    result = plan([record])
    whole = decisions_of(result)["pair-t01--i01"]
    assert whole["decision"] == al.DECISION_NOT_ALIGNED
    assert whole["evidence"]["upstream"]["reason"] == "upstream_ledger_isolated"


# ------------------------------------------------------------------ 统计守恒

def test_sample_dataset_conservation(plan, build_pair, tmp_path):
    records = [build_pair(index, score) for index, score in
               [(1, 0.41), (2, 0.55), (3, 0.28), (7, 0.24), (10, None)]]
    records[1]["metadata"] = {"sampling_hit": True}
    result = plan(records)
    stats = result.stats
    assert stats["conservation_ok"] is True, stats["conservation_failures"]
    assert stats["input"] == (len(result.main) + len(result.isolation)
                              + len(result.not_applicable) + len(result.rejects))
    assert stats["valid"] == stats["applicable"] + stats["not_applicable"]
    assert stats["applicable"] == (stats["auto_aligned"] + stats["manual_aligned"]
                                   + stats["pending_manual_review"] + stats["uncertain"]
                                   + stats["not_aligned"])
    assert stats["review_entries"] == stats["pending_manual_review"] + stats["uncertain"]
    assert len(result.isolation) == stats["not_aligned"]


def test_empty_input_is_success_with_conservation(plan):
    result = plan([])
    assert result.stats["input"] == 0
    assert result.stats["empty_input"] is True
    assert result.stats["conservation_ok"] is True
    assert result.main == [] and result.rejects == []


def test_conservation_detects_inconsistency(plan, build_pair):
    result = plan([build_pair(1, 0.41)])
    broken = copy.deepcopy(result.stats)
    broken["input"] = result.stats["input"] + 1
    al._check_conservation(broken, result.main, result.review, result.isolation,
                           result.not_applicable, result.rejects)
    assert broken["conservation_ok"] is False
    assert broken["conservation_failures"]


# ------------------------------------------------------------------ 重评分函数

def test_perform_rescore_reports_reasons():
    assert al.perform_rescore(None, None, "x")["status"] == al.REASON_MODEL_UNAVAILABLE
    failing = FailingEncoder()
    status = al.perform_rescore(failing, None, "x")["status"]
    assert status.startswith("inference_failed")


def test_cosine_similarity_is_real_math():
    assert backends.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert backends.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert backends.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    with pytest.raises(ValueError):
        backends.cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_description_ref_does_not_store_full_text():
    text = "很长的描述" * 50
    ref = al._description_ref(text)
    assert ref["length"] == len(text)
    assert len(ref["preview"]) <= 40
    assert text not in json.dumps(ref, ensure_ascii=False)
