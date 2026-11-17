"""#19 时序路线兜底（``temporal_route_fallback``）单元测试。

背景：时序窗口的 #14 判 ``not_applicable``（不评分），#13 可能因转写不可靠也判
``not_applicable``，于是三条评分路线都未生效。此类记录 #15 已判时序通过、#16 已判
``auto_aligned``，若 route 门只认 #12/#13/#14，就会以 ``no_scoring_route_enabled``
留产线待人工。本组用例锁定兜底口径：

- 仅当**没有任何评分路线生效**时，才用 #15 的「通过」补 route 门；
- #15 失败不补 route（失败仍由 temporal 门以 ``temporal_failed`` 隔离）；
- ``not_applicable`` / ``pending_calibration`` 不构成证据；
- 关闭 ``temporal_route_fallback`` 复现旧行为。
"""
from __future__ import annotations

import json

from conftest import make_record, rec_15, rec_16, rec_license, write_jsonl

import inference
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
                                       event_time="2026-11-05T00:00:00+08:00")


def temporal_only_record(sample_id="hc-t15", status="passed", passed=True,
                         reason="within_threshold"):
    """只有 #15 + #16 + 授权、没有任何 #12/#13/#14 的时序窗口记录。"""
    return make_record(sample_id, rec_16("auto_aligned"),
                       rec_license("可训练"),
                       rec_15(status, passed, "within_threshold" if reason is None else reason))


# --------------------------------------------------------------------------- #
# 1. 兜底生效：无评分路线 + #15 通过 → 入池
# --------------------------------------------------------------------------- #

def test_temporal_pass_fills_route_when_no_scoring_route():
    outcome = evaluate(temporal_only_record())
    node = outcome["node"]
    assert outcome["action"] == pool_filter.ACTION_ADMIT
    assert node["reason_code"] == pool_filter.REASON_ALL_PASSED
    route = node["gates"]["route"]
    assert route["passed"] is True
    assert route["enabled_routes"] == ["15"]
    assert "processing[\"15\"]" in route["source"]
    assert route["route_results"]["15"]["passed"] is True
    assert node["evidence"]["enabled_routes"] == ["15"]


def test_temporal_pass_does_not_bypass_other_gates():
    """兜底只补 route 门：整体对齐待人工时仍然待人工。"""
    record = make_record("hc-t15b", rec_16("pending_manual_review"),
                         rec_license("可训练"), rec_15("passed", True))
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_WHOLE_PENDING
    assert outcome["node"]["gates"]["route"]["passed"] is True


# --------------------------------------------------------------------------- #
# 2. #15 失败 / 未定 / 不适用 → 不补 route
# --------------------------------------------------------------------------- #

def test_temporal_failure_is_isolated_by_temporal_gate():
    record = temporal_only_record(status="failed", passed=False,
                                  reason="max_delta_exceeded")
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    # 主因必须是 temporal_failed，而不是 route_failed
    assert outcome["node"]["reason_code"] == pool_filter.REASON_TEMPORAL_FAILED
    assert outcome["node"]["gates"]["route"]["passed"] is None
    assert outcome["node"]["gates"]["temporal"]["passed"] is False


def test_temporal_not_applicable_keeps_no_route_pending():
    record = temporal_only_record(status="not_applicable", passed=None,
                                  reason="no_timestamp_timeline")
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_NO_ROUTE
    assert outcome["node"]["gates"]["route"]["enabled_routes"] == []


def test_temporal_pending_calibration_keeps_no_route_pending():
    record = temporal_only_record(status="pending_calibration", passed=None)
    outcome = evaluate(record)
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_NO_ROUTE


# --------------------------------------------------------------------------- #
# 3. 关闭兜底 / 已有评分路线时不改变原判定
# --------------------------------------------------------------------------- #

def test_fallback_disabled_restores_pending():
    outcome = evaluate(temporal_only_record(), config(temporal_route_fallback=False))
    assert outcome["action"] == pool_filter.ACTION_PENDING
    assert outcome["node"]["reason_code"] == pool_filter.REASON_NO_ROUTE
    assert outcome["node"]["gates"]["route"]["enabled_routes"] == []


def test_scoring_route_precedes_temporal_fallback():
    """有 #12 分时 route 门只看 #12；#15 失败仍由 temporal 门隔离。"""
    record = make_record("hc-t15c", rec_12_like(), rec_16("auto_aligned"),
                         rec_license("可训练"),
                         rec_15("failed", False, "max_delta_exceeded"))
    outcome = evaluate(record)
    route = outcome["node"]["gates"]["route"]
    assert route["enabled_routes"] == ["12"]
    assert "15" not in route["route_results"]
    assert outcome["action"] == pool_filter.ACTION_ISOLATE
    assert outcome["node"]["reason_code"] == pool_filter.REASON_TEMPORAL_FAILED


def rec_12_like():
    from conftest import rec_12
    return rec_12("auto_aligned")


# --------------------------------------------------------------------------- #
# 4. 统计可见：admitted_via_temporal_route
# --------------------------------------------------------------------------- #

def test_statistics_count_temporal_route_admits(tmp_path, config_file, output_root):
    sample = write_jsonl(tmp_path / "temporal.jsonl", [
        temporal_only_record("hc-t15-1"),
        temporal_only_record("hc-t15-2", status="not_applicable", passed=None,
                             reason="no_timestamp_timeline"),
    ])
    result = inference.run(input_file=str(sample), unique_id="task_t15",
                           config_path=str(config_file),
                           output_root=str(output_root))
    stats = result["statistics"]
    assert stats["temporal_route_fallback"] is True
    assert stats["admitted_via_temporal_route"] == 1
    assert stats["admitted_records"] == 1
    assert stats["pending_records"] == 1
    rows = [json.loads(line) for line in
            open(result["outputFile"], encoding="utf-8").read().splitlines() if line.strip()]
    assert rows[0]["operator_19"]["enabled_routes"] == ["15"]
