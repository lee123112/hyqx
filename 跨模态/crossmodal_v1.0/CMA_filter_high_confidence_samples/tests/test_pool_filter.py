"""业务规则层（``utils/pool_filter.py``）单元测试：表1 #19 四道必过项。

表1 冻结口径（逐字）：
> 评分开启且通过、整体自动/人工对齐、授权可训练、有时间戳样本时序通过，四项同时满足入候选池
> 不得用加权分把已隔离样本加回；局部标注不作为入池硬条件。
"""
from __future__ import annotations

import json

from conftest import (deep_merge, make_pair, make_record, rec_12, rec_13, rec_14,
                      rec_15, rec_16, rec_17, rec_18, rec_license)

from utils import pool_filter
from utils.schemas import OperatorConfig


def config(**overrides) -> OperatorConfig:
    base = {"operator": {"number": 19, "name": "high_confidence_pool",
                         "version": "v1.0.0"},
            "stage_id": "S3", "enabled": True,
            "required_alignment": "auto|human",
            "require_temporal_if_present": True,
            "training_authorization": "required",
            "min_enabled_routes": 1,
            "missing_upstream_policy": "waiting_manual"}
    base.update(overrides)
    return OperatorConfig(**base)


def evaluate(record, cfg=None):
    resolved = pool_filter.resolve_record(record)
    return pool_filter.evaluate_record(resolved, config=cfg or config(),
                                       event_time="2026-10-07T00:00:00+08:00")


# --------------------------------------------------------------------------- #
# 1. 四项同时满足 → 入池
# --------------------------------------------------------------------------- #

def test_all_four_gates_pass_admits():
    record = make_record("hc-0001", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    outcome = evaluate(record)
    node = outcome["node"]
    assert outcome["action"] == pool_filter.ACTION_ADMIT
    assert node["status"] == 1
    assert node["process_status"] == pool_filter.STATUS_ADMITTED
    assert node["reason_code"] == pool_filter.REASON_ALL_PASSED
    assert node["label"] == pool_filter.LABEL_ADMITTED
    assert node["destination"] == pool_filter.DEST_POOL
    assert node["action"] == pool_filter.ACTION_KEEP
    assert node["human_verified"] is False
    assert node["review_required"] is True
    assert node["gates"]["route"]["passed"] is True
    assert node["gates"]["whole"]["passed"] is True
    assert node["gates"]["license"]["passed"] is True


def test_admitted_output_keeps_original_fields_and_appends_namespace():
    record = make_record("hc-0001", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None),
                         {"modalities": ["image", "text"], "batch_id": "b-1"})
    outcome = evaluate(record)
    output = outcome["output"]
    assert output["sample_id"] == "hc-0001"
    assert output["batch_id"] == "b-1"
    assert output["modalities"] == ["image", "text"]
    assert output["processing"]["12"]["tier"] == "auto_aligned"
    assert output["processing"]["19"] == outcome["node"]
    assert output["operator_19"]["operator_number"] == 19
    assert output["operator_19"]["decision"] == "admit"
    assert output["operator_19"]["source_reference"] == "operator_19"


def test_manual_alignment_is_accepted_by_default():
    record = make_record("hc-0002", rec_13(True, "passed"), rec_16("manual_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    assert evaluate(record)["action"] == pool_filter.ACTION_ADMIT


def test_audio_text_route_passing_admits():
    record = make_record("hc-0003", rec_13(True, "passed"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("passed", True, "within_threshold"))
    node = evaluate(record)["node"]
    assert node["status"] == 1
    assert node["process_status"] == pool_filter.STATUS_ADMITTED
    assert node["gates"]["route"]["enabled_routes"] == ["13"]


def test_video_text_route_passing_admits():
    record = make_record("hc-0004", rec_14(True, "passed"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    node = evaluate(record)["node"]
    assert node["status"] == 1
    assert node["process_status"] == pool_filter.STATUS_ADMITTED
    assert node["gates"]["route"]["enabled_routes"] == ["14"]


# --------------------------------------------------------------------------- #
# 2. 路线评分失败 / 待定
# --------------------------------------------------------------------------- #

def test_route_isolated_goes_to_sample_quarantine():
    record = make_record("hc-0005", rec_12("isolated"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_ROUTE_FAILED
    assert outcome["node"]["label"] == pool_filter.LABEL_ISOLATED
    assert outcome["node"]["destination"] == pool_filter.DEST_QUARANTINE


def test_isolated_record_cannot_be_pulled_back_by_other_gates():
    """表1：不得用加权分把已隔离样本加回。其余三项全通过、分数很高也不行。"""
    record = make_record("hc-0006", rec_12("isolated", clip_score=0.99),
                         rec_16("auto_aligned"), rec_license("可训练"),
                         rec_15("passed", True, "within_threshold"))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    # 判据是上游隔离结论本身，而不是任何合成分
    assert outcome["node"]["gates"]["route"]["passed"] is False


def test_multiple_routes_all_must_pass():
    record = make_record("hc-0007", rec_12("auto_aligned"), rec_13(False, "failed"),
                         rec_16("auto_aligned"), rec_license("可训练"),
                         rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_ROUTE_FAILED
    assert sorted(outcome["node"]["gates"]["route"]["enabled_routes"]) == ["12", "13"]


def test_route_pending_leaves_sample_in_pipeline():
    record = make_record("hc-0008", rec_13(None, "pending", reason="missing_transcript"),
                         rec_16("auto_aligned"), rec_license("可训练"),
                         rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_ROUTE_PENDING
    assert outcome["node"]["destination"] == pool_filter.DEST_WAIT


def test_no_scoring_route_enabled_is_pending_not_isolated():
    record = make_record("hc-0009", rec_16("auto_aligned"), rec_license("可训练"),
                         rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_NO_ROUTE


def test_min_enabled_routes_enforced():
    record = make_record("hc-0010", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    outcome = evaluate(record, config(min_enabled_routes=2))
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_INSUFFICIENT_ROUTES


# --------------------------------------------------------------------------- #
# 3. 整体对齐结论
# --------------------------------------------------------------------------- #

def test_whole_not_aligned_isolates_even_with_high_scores():
    record = make_record("hc-0011", rec_12("auto_aligned", clip_score=0.95),
                         rec_16("not_aligned"), rec_license("可训练"),
                         rec_15("passed", True))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_WHOLE_NOT_ALIGNED


def test_whole_uncertain_is_pending():
    record = make_record("hc-0012", rec_12("auto_aligned"), rec_16("uncertain"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_WHOLE_UNCERTAIN


def test_whole_pending_manual_review_is_pending():
    record = make_record("hc-0013", rec_12("pending_manual"),
                         rec_16("pending_manual_review"), rec_license("可训练"),
                         rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_WHOLE_PENDING


def test_whole_missing_is_pending():
    record = make_record("hc-0014", rec_12("auto_aligned"), rec_license("可训练"),
                         rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_WHOLE_MISSING


def test_required_alignment_auto_rejects_manual_aligned():
    record = make_record("hc-0015", rec_12("auto_aligned"), rec_16("manual_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    outcome = evaluate(record, config(required_alignment="auto"))
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_WHOLE_NOT_REQUIRED_TYPE


def test_required_alignment_human_rejects_auto_aligned():
    record = make_record("hc-0016", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    outcome = evaluate(record, config(required_alignment="human"))
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_WHOLE_NOT_REQUIRED_TYPE


# --------------------------------------------------------------------------- #
# 4. 授权可训练
# --------------------------------------------------------------------------- #

def test_license_not_trainable_isolates():
    record = make_record("hc-0017", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("不可训练"), rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_LICENSE_NOT_TRAINABLE
    assert outcome["node"]["label"] == pool_filter.LABEL_ISOLATED
    assert outcome["node"]["label_detail"] == pool_filter.LABEL_SENSITIVE


def test_license_missing_is_pending_not_isolated():
    record = make_record("hc-0018", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_LICENSE_MISSING


def test_license_trainable_latin_alias_works():
    record = make_record("hc-0019", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("trainable"), rec_15("not_applicable", None))
    assert evaluate(record)["action"] == pool_filter.ACTION_ADMIT


# --------------------------------------------------------------------------- #
# 5. 时序
# --------------------------------------------------------------------------- #

def test_temporal_failed_isolates():
    record = make_record("hc-0020", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("failed", False, "max_delta_exceeded"))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_TEMPORAL_FAILED


def test_temporal_not_applicable_admits_without_timeline():
    record = make_record("hc-0021", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    node = evaluate(record)["node"]
    assert node["status"] == 1
    assert node["process_status"] == pool_filter.STATUS_ADMITTED
    assert node["gates"]["temporal"]["applicable"] is False


def test_timeline_present_without_temporal_node_is_pending():
    record = make_record("hc-0022", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"),
                         {"time_index": [{"start": 0.0, "end": 1.0}]})
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_TEMPORAL_MISSING


def test_temporal_pending_calibration_is_pending():
    record = make_record("hc-0023", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"),
                         rec_15("pending_calibration", None, "calibration_unavailable"))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_TEMPORAL_PENDING


def test_temporal_requirement_can_be_relaxed_by_config():
    record = make_record("hc-0024", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("failed", False, "max_delta_exceeded"))
    outcome = evaluate(record, config(require_temporal_if_present=False))
    assert outcome["action"] == pool_filter.ACTION_ADMIT


def test_temporal_skipped_route_is_not_applicable():
    record = make_record("hc-0025", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"),
                         {"processing": {"15": {"status": "skipped",
                                                "reason_code": "route_disabled"}}})
    node = evaluate(record)["node"]
    assert node["status"] == 1
    assert node["process_status"] == pool_filter.STATUS_ADMITTED
    assert node["gates"]["temporal"]["applicable"] is False


# --------------------------------------------------------------------------- #
# 6. 局部标注不是硬条件，但上游隔离必须尊重
# --------------------------------------------------------------------------- #

def test_local_annotation_pending_does_not_block_admission():
    record = make_record("hc-0026", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None),
                         rec_17("pending_manual_review"), rec_18("pending_manual"))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ADMIT
    assert outcome["node"]["local_annotation_hard_requirement"] is False
    assert outcome["node"]["evidence"]["local_annotation"]["blocking"] is False


def test_local_annotation_missing_does_not_block_admission():
    record = make_record("hc-0027", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ADMIT
    assert outcome["node"]["evidence"]["local_annotation"]["operator17_status"] is None


def test_upstream_isolation_from_17_incomplete_is_respected():
    record = make_record("hc-0028", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None),
                         rec_17("incomplete"))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_UPSTREAM_ISOLATED


def test_upstream_isolation_from_18_isolated_is_respected():
    record = make_record("hc-0029", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None),
                         rec_18("isolated"))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_UPSTREAM_ISOLATED


# --------------------------------------------------------------------------- #
# 7. 成对记录、媒体路径与「不产出任何分数」
# --------------------------------------------------------------------------- #

def test_pair_record_admitted_with_pair_identifier():
    payload = make_pair("pair-0001",
                        left=make_record("s-text", rec_13(True, "passed"),
                                         rec_license("可训练")),
                        right=make_record("s-image", rec_12("auto_aligned"),
                                          rec_16("auto_aligned"),
                                          rec_15("not_applicable", None)))
    outcome = evaluate(payload)
    assert outcome["action"] == pool_filter.ACTION_ADMIT
    assert outcome["node"]["identifier"] == "pair-0001"
    assert outcome["node"]["pair_id"] == "pair-0001"


def test_unsafe_media_reference_goes_to_origin_quarantine():
    record = make_record("hc-0030", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None),
                         {"image": "/abs/escape.png"})
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ORIGIN
    assert outcome["node"]["reason_code"] == pool_filter.REASON_UNSAFE_MEDIA_PATH
    assert outcome["node"]["destination"] == pool_filter.DEST_ORIGIN


def test_missing_media_file_is_counted_but_not_blocking():
    record = make_record("hc-0031", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None),
                         {"image_reference": {"path": "media/not-there.png"}})
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ADMIT
    assert outcome["node"]["evidence"]["media_refs"] == 1


def test_no_score_or_weight_fields_are_produced():
    record = make_record("hc-0032", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("passed", True))
    node = evaluate(record)["node"]
    text = json.dumps(node, ensure_ascii=False).lower()
    for forbidden in ("clip_score", "weight", "final_score", "pool_score", "combined_score"):
        assert forbidden not in text, forbidden


def test_decisions_are_pure_rules_without_any_model():
    record = make_record("hc-0033", rec_12("auto_aligned"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    node = evaluate(record)["node"]
    assert node["model"] is None
    assert node["rule_based"] is True
    assert node["operator_number"] == 19
    assert node["operator_version"] == "v1.0.0"
    assert node["source_reference"] == "operator_19"


def test_resolve_record_without_identifier_is_flagged():
    resolved = pool_filter.resolve_record({"text": {"content": "x"},
                                           "processing": {"12": {"tier": "auto_aligned"}}})
    assert resolved["identifier"] is None
    outcome = pool_filter.evaluate_record(resolved, config=config(),
                                          event_time="2026-10-07T00:00:00+08:00")
    assert outcome["action"] == pool_filter.ACTION_ORIGIN
    assert outcome["node"]["reason_code"] == pool_filter.REASON_MISSING_IDENTIFIER


def test_gate_failure_order_is_deterministic():
    """四道门同时失败时，主因按表1 逐字顺序取「路线 → 整体 → 授权 → 时序」。"""
    record = make_record("hc-0034", rec_12("isolated"), rec_16("not_aligned"),
                         rec_license("不可训练"), rec_15("failed", False))
    node = evaluate(record)["node"]
    assert node["reason_code"] == pool_filter.REASON_ROUTE_FAILED
    failed = node["evidence"]["gates_failed"]
    assert failed == ["route", "whole", "license", "temporal"]


def test_ledger_entry_shape():
    record = make_record("hc-0035", rec_12("isolated"), rec_16("auto_aligned"),
                         rec_license("可训练"), rec_15("not_applicable", None))
    resolved = pool_filter.resolve_record(record)
    outcome = pool_filter.evaluate_record(resolved, config=config(),
                                          event_time="2026-10-07T00:00:00+08:00")
    entry = pool_filter.make_ledger_entry(line=7, spec=resolved, outcome=outcome,
                                          detail="route failed",
                                          detected_at="2026-10-07T01:00:00+08:00")
    assert entry["line"] == 7
    assert entry["identifier"] == "hc-0035"
    assert entry["operator_number"] == 19
    assert entry["reason"] == pool_filter.REASON_ROUTE_FAILED
    assert entry["stage_id"] == "S3"
    assert entry["source_reference"] == "operator_19"
    assert entry["detected_at"] == "2026-10-07T01:00:00+08:00"
    assert entry["human_verified"] is False
    assert entry["review_required"] is True


def test_empty_dict_record_is_origin():
    outcome = evaluate({})
    assert outcome["action"] == pool_filter.ACTION_ORIGIN
    assert outcome["node"]["reason_code"] == pool_filter.REASON_MISSING_IDENTIFIER


def test_deep_merge_helper_is_used_by_pair_flattening():
    merged = deep_merge({"a": {"x": 1}}, {"a": {"y": 2}})
    assert merged == {"a": {"x": 1, "y": 2}}
