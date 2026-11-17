"""算子19 报告计算：记录分类、阶段统计校验、指标复算与报告组装。

本模块只做纯计算，不触碰文件系统；所有统计都必须来自真实输入计数。
"""
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from utils.schemas import STAGE_COUNTER_FIELDS

KNOWN_RECORD_TYPES = {"stage_statistics", "dataset_record", "delivery_summary"}
GROUP_DIMENSION_FIELDS = {
    "task_id": ("task_id",),
    "language_pair": ("language_pair",),
    "source_type": ("source_type", "source_batch"),
    "domain": ("domain",),
}


def reject_entry(lineno: int, record_type: str, reasons: List[str],
                 record: Any = None, stage: Optional[str] = None, **extra) -> dict:
    entry: Dict[str, Any] = {
        "line": lineno,
        "record_type": record_type,
        "stage": stage,
        "reasons": list(reasons),
        "record": record,
    }
    for key, value in extra.items():
        if value:
            entry[key] = value
    return entry


def classify_record(record: dict) -> str:
    """按内容判断记录类型；显式 record_type 优先，便于工程侧自定义清单。"""
    raw = record.get("record_type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if any(field in record for field in STAGE_COUNTER_FIELDS) or "stage" in record:
        return "stage_statistics"
    if "source" in record and "target" in record:
        return "dataset_record"
    if "delivery_records" in record or "record_count" in record:
        return "delivery_summary"
    return "unknown"


def normalize_unit_type(cfg, value) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    return cfg.unit_type_aliases.get(text, text)


def _is_unit_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_stage_record(cfg, record: dict, lineno: int
                       ) -> Tuple[Optional[dict], Optional[Dict[str, int]], List[str]]:
    """校验单条阶段统计记录，返回 (拒绝条目, 合格计数, 原因列表)。"""
    reasons: List[str] = []
    stage = record.get("stage")
    if not isinstance(stage, str) or not stage.strip():
        reasons.append("missing_field:stage")
        stage = None
    elif cfg.require_known_stage and stage not in cfg.stage_order:
        reasons.append("unknown_stage:%s" % stage)

    missing: List[str] = []
    invalid: List[str] = []
    counters: Dict[str, int] = {}
    for field in cfg.stage_required_fields:
        if record.get(field) is None:
            missing.append(field)
            reasons.append("missing_field:%s" % field)
            continue
        value = record[field]
        if not _is_unit_count(value):
            invalid.append(field)
            reasons.append("invalid_type:%s" % field)
            continue
        if value < 0:
            reasons.append("negative_value:%s" % field)
            continue
        counters[field] = value

    if cfg.stage_balance_check and not missing and not invalid:
        parts = sum(value for key, value in counters.items() if key != "input_units")
        if parts != counters.get("input_units"):
            reasons.append("stage_balance_mismatch:%s" % stage)

    if not reasons:
        return None, counters, []
    entry = reject_entry(lineno, "stage_statistics", reasons, record=record, stage=stage,
                         missing_fields=missing or None, invalid_fields=invalid or None)
    usable = counters if not missing and not invalid else None
    return entry, usable, reasons


def parse_delivery_summary(cfg, record: dict, lineno: int
                           ) -> Tuple[Optional[dict], Optional[dict], List[str]]:
    """校验交付汇总记录，返回 (拒绝条目, 汇总数据, 原因列表)。"""
    reasons: List[str] = []
    delivery = record.get("delivery_records")
    if delivery is None:
        delivery = record.get("record_count")
    if delivery is None:
        reasons.append("missing_field:delivery_records")
    elif not _is_unit_count(delivery) or delivery < 0:
        reasons.append("invalid_type:delivery_records")
        delivery = None

    counts: Dict[str, Dict[str, int]] = {}
    for name in ("grade_counts", "delivery_grade_counts", "formal_delivery_grade_counts"):
        value = record.get(name)
        if value is None:
            continue
        if not isinstance(value, dict) or not all(_is_unit_count(v) for v in value.values()):
            reasons.append("invalid_type:%s" % name)
            continue
        counts[name] = dict(value)

    summary = dict(record)
    summary["delivery_records"] = delivery
    if "formal_ai_ready_count" in record:
        value = record.get("formal_ai_ready_count")
        if not _is_unit_count(value) or value < 0:
            reasons.append("invalid_type:formal_ai_ready_count")
        else:
            summary["formal_ai_ready_count"] = value
    if "waiting_manual_count" in record:
        value = record.get("waiting_manual_count")
        if not _is_unit_count(value) or value < 0:
            reasons.append("invalid_type:waiting_manual_count")
        else:
            summary["waiting_manual_count"] = value
    summary.update(counts)
    if reasons:
        entry = reject_entry(lineno, "delivery_summary", reasons, record=record)
        return entry, (None if delivery is None else summary), reasons
    return None, summary, []


def stage_continuity_reasons(cfg, stages_seen: List[str]) -> List[str]:
    """阶段链中间缺环即血缘断裂。"""
    index = sorted(cfg.stage_order.index(stage) for stage in stages_seen
                   if stage in cfg.stage_order)
    if not index:
        return []
    reasons = []
    for position in range(index[0], index[-1] + 1):
        stage = cfg.stage_order[position]
        if stage not in stages_seen:
            reasons.append("lineage_broken:%s" % stage)
    return reasons


def compute_metrics(cfg, low_quality_input_count: Optional[int],
                    converted_ai_ready_count: Optional[int],
                    pre_final_high_quality_count: Optional[int],
                    retained_high_quality_count: Optional[int],
                    scope_blocking: Optional[List[str]] = None) -> dict:
    """固定公式复算；范围/粒度不一致时指标 BLOCKED，不参与阈值比较。"""
    scope_blocking = list(scope_blocking or [])
    blocking: List[str] = []
    if low_quality_input_count is None:
        blocking.append("missing_denominator_source:low_quality_input_count")
    if converted_ai_ready_count is None:
        blocking.append("missing_converted_count")
    if pre_final_high_quality_count is None:
        blocking.append("missing_high_quality_candidate_count")
    if retained_high_quality_count is None:
        blocking.append("missing_retained_high_quality_count")
    if low_quality_input_count == 0:
        blocking.append("denominator_zero:low_quality_conversion_rate")
    if pre_final_high_quality_count == 0:
        blocking.append("denominator_zero:high_quality_retention_rate")

    conversion: Optional[float] = None
    retention: Optional[float] = None
    if not blocking:
        if low_quality_input_count:
            conversion = round(converted_ai_ready_count / low_quality_input_count,
                               cfg.rate_precision)
        if pre_final_high_quality_count:
            retention = round(retained_high_quality_count / pre_final_high_quality_count,
                              cfg.rate_precision)

    blocking = list(dict.fromkeys(blocking + scope_blocking))
    if blocking:
        # 阻塞时不暴露一个可被误读为正式指标的数值。
        conversion = None
        retention = None
    conversion_status = "BLOCKED" if blocking else "COMPUTABLE"
    retention_status = "BLOCKED" if blocking else "COMPUTABLE"
    threshold: List[str] = []
    if conversion_status == "COMPUTABLE" and conversion is not None and conversion < cfg.low_quality_conversion_threshold:
        threshold.append("low_quality_conversion_below_threshold")
    if retention_status == "COMPUTABLE" and retention is not None and retention < cfg.high_quality_retention_threshold:
        threshold.append("high_quality_retention_below_threshold")

    passed = (not blocking and not threshold
              and conversion is not None and retention is not None)
    return {
        "low_quality_conversion_rate": conversion,
        "high_quality_retention_rate": retention,
        "blocking_reasons": blocking,
        "threshold_reasons": threshold,
        "passed": passed,
        "metric_status": {
            "low_quality_conversion": {
                "status": conversion_status, "value": conversion,
                "numerator": converted_ai_ready_count,
                "denominator": low_quality_input_count,
                "blocking_reasons": list(blocking),
                "threshold": cfg.low_quality_conversion_threshold,
            },
            "high_quality_retention": {
                "status": retention_status, "value": retention,
                "numerator": retained_high_quality_count,
                "denominator": pre_final_high_quality_count,
                "blocking_reasons": list(blocking),
                "threshold": cfg.high_quality_retention_threshold,
            },
        },
    }


def build_scope(cfg, sources: Dict[str, str], task_id: Optional[str],
                pipeline_version: Optional[str], unit_type: Optional[str],
                low_quality_input_count, converted_ai_ready_count,
                pre_final_high_quality_count, retained_high_quality_count) -> dict:
    """统计口径：分母取值、时点、统计单位与配置版本，全部写入报告。"""
    return {
        "unit_type": unit_type,
        "task_id": task_id,
        "pipeline_version": pipeline_version,
        "config_version": cfg.config_version,
        "report_version": cfg.report_version,
        "low_quality_input_count": {
            "value": low_quality_input_count,
            "source": sources.get("low_quality_input_count"),
            "time_point": "产线首个有效阶段的输入（进入产线的有效低质量单元总数）",
        },
        "converted_ai_ready_count": {
            "value": converted_ai_ready_count,
            "source": sources.get("converted_ai_ready_count"),
            "time_point": "数据集交付阶段（算子18）交付之后",
        },
        "pre_final_high_quality_count": {
            "value": pre_final_high_quality_count,
            "source": sources.get("pre_final_high_quality_count"),
            "time_point": "最终入库门禁与人工治理之前",
        },
        "retained_high_quality_count": {
            "value": retained_high_quality_count,
            "source": sources.get("retained_high_quality_count"),
            "time_point": "算子18 实际交付之后",
        },
        "high_quality_grades": list(cfg.high_quality_grades),
        "below_threshold_status": cfg.below_threshold_status,
        "group_dimensions": list(cfg.group_dimensions),
        "unit_type_aliases": dict(cfg.unit_type_aliases),
    }


def build_thresholds(cfg) -> dict:
    """报告 thresholds 固定只含两项指标阈值（0.85 / 0.90）。"""
    return {
        "low_quality_conversion_rate": cfg.low_quality_conversion_threshold,
        "high_quality_retention_rate": cfg.high_quality_retention_threshold,
    }


def most_common(counter: Counter, fallback=None):
    if not counter:
        return fallback
    return counter.most_common(1)[0][0]


def sum_grades(counts: Optional[dict], grades) -> Optional[int]:
    """只累加指定等级；counts 为 None 时返回 None（表示来源缺失，而不是 0）。"""
    if not isinstance(counts, dict) or not counts:
        return None
    wanted = {str(grade) for grade in grades}
    return sum(int(value) for key, value in counts.items() if str(key) in wanted)
