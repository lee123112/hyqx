# -*- coding: utf-8 -*-
"""09 优化建议生成（avp_suggest_optimization）。

把 008 的归因结论转成 1–3 条可执行建议：事实 / 证据强度 / 解释 / 验证方式。
**所有数字都来自 008 产物或本配置的系数，文案由固定模板回填**——本模块不调用大模型、
不生成自由文本、不引入任何未在产物里出现过的数字。
"""

from __future__ import annotations

import datetime as _dt
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import config as cfgmod
from common import iofs
from common import schemas
from common.context import OperatorContext

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
METRIC_LABEL = {"cer": "CER", "wer": "WER", "ser": "SER"}
ERROR_KEYS = ("sub", "del", "ins", "unk")
OVERALL = "__overall__"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _round_half_up(value: float, digits: int = 0) -> float:
    """四舍五入（half-up），与 007/008 口径一致。"""
    factor = 10 ** digits
    scaled = float(value) * factor
    if scaled >= 0:
        out = math.floor(scaled + 0.5)
    else:
        out = math.ceil(scaled - 0.5)
    return out / factor


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num_text(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return ("%.2f" % float(value)).rstrip("0").rstrip(".")


def _pp_text(value: Optional[float], cfg: Dict[str, Any]) -> str:
    if value is None:
        return "—"
    return ("%." + str(int(cfg["precision"]["pp_digits"])) + "f") % float(value)


def _pct_text(value: float, cfg: Dict[str, Any]) -> str:
    return ("%." + str(int(cfg["precision"]["pct_digits"])) + "f") % float(value)


def _metric_label(metric: str) -> str:
    key = str(metric).lower()
    return METRIC_LABEL.get(key, key.upper())


def _fit_title(text: str, cfg: Dict[str, Any], warnings: List[str]) -> str:
    max_len = int(cfg["io"]["max_title_len"])
    if len(text) <= max_len:
        return text
    fitted = text[: max_len - 1] + "…"
    warnings.append("建议标题超过 %d 字已截断：%s" % (max_len, fitted))
    return fitted


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_params(ctx: OperatorContext, cfg: Dict[str, Any]) -> Dict[str, Any]:
    params = ctx.params or {}
    checks = cfg["checks"]
    max_suggestions = schemas.as_int(
        params.get("max_suggestions", checks["max_suggestions"]), "max_suggestions", lo=1, hi=3
    )
    min_suggestions = schemas.as_int(
        params.get("min_suggestions", checks["min_suggestions"]), "min_suggestions", lo=1, hi=max_suggestions
    )
    p0_pp = schemas.as_float(params.get("p0_pp", checks["p0_pp"]), "p0_pp", lo=0.0, hi=100.0)
    p0_share = schemas.as_float(params.get("p0_share", checks["p0_share"]), "p0_share", lo=0.0, hi=1.0)
    p1_pp = schemas.as_float(params.get("p1_pp", checks["p1_pp"]), "p1_pp", lo=0.0, hi=100.0)
    if p1_pp > p0_pp:
        raise schemas.ParamError("p1_pp 不能大于 p0_pp（P1 区间为 p1_pp–p0_pp）")
    sample_plan_factor = schemas.as_float(
        params.get("sample_plan_factor", checks["sample_plan_factor"]), "sample_plan_factor", lo=0.01, hi=100.0
    )
    target_pp_drop = schemas.as_float(
        params.get("target_pp_drop", checks["target_pp_drop"]), "target_pp_drop", lo=0.0, hi=100.0
    )
    return {
        "max_suggestions": max_suggestions,
        "min_suggestions": min_suggestions,
        "p0_pp": p0_pp,
        "p0_share": p0_share,
        "p1_pp": p1_pp,
        "sample_plan_factor": sample_plan_factor,
        "target_pp_drop": target_pp_drop,
    }


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #
def _resolve_input(ctx: OperatorContext, cfg: Dict[str, Any]) -> Path:
    path = ctx.require_input()
    if not path.is_dir():
        return path
    preferred = path / str(cfg["io"]["input_default_name"])
    if preferred.is_file():
        return preferred
    candidates = [item for item in sorted(path.iterdir()) if item.is_file() and item.suffix.lower() in (".json", ".jsonl")]
    if not candidates:
        raise schemas.OpError("missing_file: 输入目录里没有 008 归因产物（.json/.jsonl）：%s" % path.name)
    return candidates[0]


def _load_attribution(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise schemas.OpError("missing_file: 找不到输入文件：%s" % path.name)
    payload = iofs.read_json(path)
    if not isinstance(payload, dict):
        raise schemas.OpError("invalid_jsonl: 输入不是 008 归因 JSON 对象：%s" % path.name)

    required = ("eval_id", "total_net_error", "metric", "factors")
    for key in required:
        if key not in payload:
            raise schemas.OpError("invalid_jsonl: 输入缺少字段 %s，无法生成建议：%s" % (key, path.name))
    if not isinstance(payload["factors"], list):
        raise schemas.OpError("invalid_jsonl: factors 必须是数组：%s" % path.name)
    total_net_error = _opt_int(payload["total_net_error"])
    if total_net_error is None:
        raise schemas.OpError("invalid_jsonl: total_net_error 不是整数：%r" % payload["total_net_error"])
    metric = str(payload["metric"]).strip()
    if not metric:
        raise schemas.OpError("invalid_jsonl: metric 不能为空")

    factors: List[Dict[str, Any]] = []
    for index, item in enumerate(payload["factors"]):
        if not isinstance(item, dict):
            raise schemas.OpError("invalid_jsonl: factors[%d] 不是对象" % index)
        for key in ("dimension", "value", "samples"):
            if key not in item:
                raise schemas.OpError("invalid_jsonl: factors[%d] 缺少字段 %s" % (index, key))
        dimension = str(item["dimension"]).strip()
        value = str(item["value"]).strip()
        if not dimension or not value:
            raise schemas.OpError("invalid_jsonl: factors[%d] 的 dimension/value 不能为空" % index)
        samples = _opt_int(item["samples"])
        if samples is None or samples < 0:
            raise schemas.OpError("invalid_jsonl: factors[%d].samples 非法：%r" % (index, item["samples"]))
        trace = item.get("sample_trace")
        factors.append(
            {
                "index": index,
                "dimension": dimension,
                "value": value,
                "samples": samples,
                "ratio": _opt_float(item.get("ratio")),
                "in_scope": bool(item.get("in_scope", False)),
                "comparable": bool(item.get("comparable", False)),
                "baseline_present": bool(item.get("baseline_present", False)),
                "diff_pp": _opt_float(item.get("diff_pp")),
                "affected": _opt_int(item.get("affected")),
                "share": _opt_float(item.get("share")),
                "confidence": item.get("confidence") if item.get("confidence") in ("低", "中", "高") else None,
                "trace": trace if isinstance(trace, dict) else None,
            }
        )

    baseline = payload.get("baseline") if isinstance(payload.get("baseline"), dict) else {}
    return {
        "payload": payload,
        "eval_id": str(payload.get("eval_id") or ""),
        "external_task_id": payload.get("external_task_id"),
        "baseline_eval_id": str(baseline.get("eval_id") or ""),
        "metric": metric,
        "metric_label": _metric_label(metric),
        "total_net_error": total_net_error,
        "sample_count": _opt_int(payload.get("sample_count")) or 0,
        "main_cause": payload.get("main_cause"),
        "share": _opt_float(payload.get("share")),
        "affected": _opt_int(payload.get("affected")),
        "confidence": payload.get("confidence") if payload.get("confidence") in ("低", "中", "高") else None,
        "error_mix": payload.get("error_mix") if isinstance(payload.get("error_mix"), dict) else {},
        "metrics": payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
        "baseline_metrics": payload.get("baseline_metrics")
        if isinstance(payload.get("baseline_metrics"), dict)
        else (baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) else {}),
        "alert_count": _opt_int(payload.get("alert_count")),
        "alert_threshold_pp": _opt_float(payload.get("alert_threshold_pp")),
        "factors": factors,
        "record_id": payload.get("record_id"),
        "record_attributable": payload.get("attributable", True),
        "warnings": list(payload.get("warnings") or []),
    }


def _load_alerts(input_path: Path, attribution: Dict[str, Any], cfg: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    """读取 008 的告警阈值；缺失时退化为配置默认值（只用于文案，不影响建议计算）。"""
    alerts_path = None
    if input_path.is_dir():
        candidate = input_path / str(cfg["io"]["alerts_default_name"])
        if candidate.is_file():
            alerts_path = candidate
    else:
        candidate = input_path.parent / str(cfg["io"]["alerts_default_name"])
        if candidate.is_file():
            alerts_path = candidate

    recorded_threshold = attribution.get("alert_threshold_pp")
    default_threshold = float(recorded_threshold) if recorded_threshold is not None else float(cfg["checks"]["alert_pp"])
    threshold = default_threshold
    count = attribution.get("alert_count") or 0
    used = False
    if alerts_path is not None:
        payload = iofs.read_json(alerts_path)
        if isinstance(payload, dict):
            used = True
            parsed = _opt_float(payload.get("threshold_pp"))
            if parsed is not None:
                threshold = parsed
            parsed_count = _opt_int(payload.get("count"))
            if parsed_count is not None:
                count = parsed_count
            if parsed is not None and abs(parsed - default_threshold) > 1e-9:
                warnings.append(
                    "008 告警阈值为 %spp，与本算子默认 %spp 不同；补样验证文案采用 008 的实际阈值"
                    % (_num_text(parsed), _num_text(default_threshold))
                )
    if alerts_path is None:
        if recorded_threshold is not None:
            warnings.append(
                "未读取到 008_alerts.json，补样验证文案采用 008 attribution 记录的告警阈值 %spp"
                % _num_text(float(recorded_threshold))
            )
        else:
            warnings.append(
                "未读取到 008_alerts.json，补样验证文案采用配置默认告警阈值 %spp"
                % _num_text(default_threshold)
            )
    if attribution.get("alert_count") is not None and attribution["alert_count"] != count:
        warnings.append(
            "008 attribution.alert_count=%s 与 alerts.count=%s 不一致，按告警文件为准"
            % (attribution["alert_count"], count)
        )
    return {"threshold_pp": threshold, "count": int(count or 0), "used": used, "path": alerts_path}


# --------------------------------------------------------------------------- #
# 选因子与优先级
# --------------------------------------------------------------------------- #
def _priority(diff_pp: float, share: Optional[float], params: Dict[str, Any]) -> str:
    share_value = share if share is not None else 0.0
    if diff_pp >= params["p0_pp"] or share_value >= params["p0_share"]:
        return "P0"
    if diff_pp >= params["p1_pp"]:
        return "P1"
    return "P2"


def _select_factors(
    attribution: Dict[str, Any], params: Dict[str, Any], cfg: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    min_samples = int(cfg["advice"]["min_samples_per_value"])
    degradation: List[Dict[str, Any]] = []
    coverage: List[Dict[str, Any]] = []
    for factor in attribution["factors"]:
        if not (factor["in_scope"] and factor["comparable"]):
            continue
        diff = factor["diff_pp"]
        if diff is not None and diff > 0:
            factor["priority"] = _priority(diff, factor["share"], params)
            degradation.append(factor)
        elif factor["samples"] < min_samples:
            factor["priority"] = "P2"
            coverage.append(factor)

    degradation.sort(
        key=lambda item: (
            PRIORITY_ORDER[item["priority"]],
            -float(item["diff_pp"]),
            -(item["share"] or 0.0),
            item["dimension"],
            item["value"],
        )
    )
    coverage.sort(
        key=lambda item: (
            item["ratio"] if item["ratio"] is not None else 1.0,
            -(item["diff_pp"] or 0.0),
            item["dimension"],
            item["value"],
        )
    )
    return degradation, coverage


# --------------------------------------------------------------------------- #
# 建议构造
# --------------------------------------------------------------------------- #
def _error_counts(factor: Dict[str, Any], attribution: Dict[str, Any]) -> Tuple[Dict[str, int], str]:
    trace = factor["trace"] or {}
    counts: Dict[str, int] = {}
    for key in ERROR_KEYS:
        counts[key] = int(trace.get("error_counts", {}).get(key, 0) or 0)
    if any(counts.values()):
        return counts, "sample_trace"
    fallback = attribution["error_mix"]
    for key in ERROR_KEYS:
        counts[key] = int(fallback.get(key, 0) or 0)
    return counts, "record_error_mix"


def _sample_plan(
    *,
    count: int,
    gap: int,
    factor: float,
    projected_samples: int,
    purpose: str,
    factor_row: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
    target_pp_drop: float,
) -> Dict[str, Any]:
    trace = (factor_row or {}).get("trace") or {}
    return {
        "count": int(count),
        "gap": int(gap),
        "factor": float(factor),
        "formula": "count = max(min_count, round_half_up(gap × factor))",
        "gap_definition": {
            "degradation_verification": "gap = max(min_count, 该维度影响条数 affected（多错样本规模））",
            "coverage_expansion": "gap = min_samples_per_value − 现有样本数",
            "overall_review": "gap = max(min_count, |total_net_error|)",
        }[purpose],
        "purpose": purpose,
        "projected_samples": int(projected_samples),
        "min_samples_per_value": int(cfg["advice"]["min_samples_per_value"]),
        "min_count": int(cfg["checks"]["sample_plan_min_count"]),
        "target_pp_drop": float(target_pp_drop),
        "dimension": (factor_row or {}).get("dimension"),
        "value": (factor_row or {}).get("value"),
        "snr_bucket": trace.get("top_snr_bucket"),
    }


def _source(attribution: Dict[str, Any], factor_row: Optional[Dict[str, Any]], purpose: str) -> Dict[str, Any]:
    return {
        "eval_id": attribution["eval_id"],
        "baseline_eval_id": attribution["baseline_eval_id"],
        "factor_index": (factor_row or {}).get("index"),
        "metric": attribution["metric"],
        "total_net_error": attribution["total_net_error"],
        "main_cause": attribution["main_cause"],
        "purpose": purpose,
    }


def _base_numbers(attribution: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "metric": attribution["metric_label"],
        "ci_level": int(cfg["stats"]["ci_level"]),
        "total_net_error": attribution["total_net_error"],
    }


def _build_degradation(
    factor: Dict[str, Any],
    attribution: Dict[str, Any],
    params: Dict[str, Any],
    cfg: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    templates = cfg["templates"]
    trace = factor["trace"] or {}
    counts, counts_source = _error_counts(factor, attribution)
    diff = float(factor["diff_pp"])
    affected = int(factor["affected"] or 0)
    share = float(factor["share"] or 0.0)
    samples = int(factor["samples"])
    min_count = int(cfg["checks"]["sample_plan_min_count"])
    gap = affected if affected > 0 else min_count
    count = max(min_count, int(_round_half_up(gap * params["sample_plan_factor"])))
    projected = samples + count

    numbers = _base_numbers(attribution, cfg)
    numbers.update(
        {
            "dimension": factor["dimension"],
            "value": factor["value"],
            "diff_pp": _pp_text(diff, cfg),
            "affected": affected,
            "share_pct": _pct_text(share * 100.0, cfg),
            "samples": samples,
            "confidence": factor["confidence"] or "未知",
            "error_samples": int(trace.get("error_samples") or 0),
            "error_rate_pct": _pct_text(float(trace.get("error_rate") or 0.0) * 100.0, cfg),
            "ci_half_pp": _pp_text(_opt_float(trace.get("ci_half_pp")), cfg),
            "sub": counts["sub"],
            "del": counts["del"],
            "ins": counts["ins"],
            "unk": counts["unk"],
            "top_snr_bucket": trace.get("top_snr_bucket") or "",
            "sample_count": count,
            "gap": gap,
            "factor": _num_text(params["sample_plan_factor"]),
            "target_pp": _pp_text(params["target_pp_drop"], cfg),
        }
    )

    title = _fit_title(templates["title_degradation"].format(**numbers), cfg, warnings)
    fact = templates["fact_degradation"].format(**numbers)
    has_ci = (
        trace.get("status") in ("exact", "partial")
        and trace.get("error_samples") is not None
        and trace.get("ci_half_pp") is not None
    )
    evidence = templates["evidence" if has_ci else "evidence_no_ci"].format(**numbers)
    if any(counts.values()):
        key = "explanation" if numbers["top_snr_bucket"] else "explanation_no_snr"
        explanation = templates[key].format(**numbers)
    else:
        explanation = templates["explanation_fallback"].format(**numbers)
    verification = templates["verification"].format(**numbers)

    return {
        "title": title,
        "dimension": factor["dimension"],
        "value": factor["value"],
        "priority": factor["priority"],
        "fact": fact,
        "evidence": evidence,
        "explanation": explanation,
        "verification": verification,
        "sample_plan": _sample_plan(
            count=count,
            gap=gap,
            factor=params["sample_plan_factor"],
            projected_samples=projected,
            purpose="degradation_verification",
            factor_row=factor,
            cfg=cfg,
            target_pp_drop=params["target_pp_drop"],
        ),
        "source": _source(attribution, factor, "degradation_verification"),
        "evidence_source": counts_source,
    }


def _build_coverage(
    factor: Dict[str, Any],
    attribution: Dict[str, Any],
    params: Dict[str, Any],
    cfg: Dict[str, Any],
    alert_pp: float,
    warnings: List[str],
) -> Dict[str, Any]:
    templates = cfg["templates"]
    samples = int(factor["samples"])
    min_samples = int(cfg["advice"]["min_samples_per_value"])
    min_count = int(cfg["checks"]["sample_plan_min_count"])
    gap = max(0, min_samples - samples)
    count = max(min_count, int(_round_half_up(gap * params["sample_plan_factor"])))
    projected = samples + count

    numbers = _base_numbers(attribution, cfg)
    numbers.update(
        {
            "dimension": factor["dimension"],
            "value": factor["value"],
            "samples": samples,
            "ratio_pct": _pct_text((factor["ratio"] or 0.0) * 100.0, cfg),
            "diff_pp": _pp_text(factor["diff_pp"], cfg),
            "min_samples": min_samples,
            "gap": gap,
            "projected_samples": projected,
            "sample_count": count,
            "factor": _num_text(params["sample_plan_factor"]),
            "alert_pp": _pp_text(alert_pp, cfg),
        }
    )

    return {
        "title": _fit_title(templates["title_coverage"].format(**numbers), cfg, warnings),
        "dimension": factor["dimension"],
        "value": factor["value"],
        "priority": "P2",
        "fact": templates["fact_coverage"].format(**numbers),
        "evidence": templates["evidence_coverage"].format(**numbers),
        "explanation": templates["explanation_coverage"].format(**numbers),
        "verification": templates["verification_coverage"].format(**numbers),
        "sample_plan": _sample_plan(
            count=count,
            gap=gap,
            factor=params["sample_plan_factor"],
            projected_samples=projected,
            purpose="coverage_expansion",
            factor_row=factor,
            cfg=cfg,
            target_pp_drop=params["target_pp_drop"],
        ),
        "source": _source(attribution, factor, "coverage_expansion"),
        "evidence_source": "sample_gap",
    }


def _metric_value(metrics: Dict[str, Any], metric: str) -> Optional[float]:
    if not isinstance(metrics, dict):
        return None
    value = metrics.get(metric)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_overall(
    attribution: Dict[str, Any], params: Dict[str, Any], cfg: Dict[str, Any], warnings: List[str]
) -> Dict[str, Any]:
    templates = cfg["templates"]
    metric = attribution["metric"]
    target_value = _metric_value(attribution["metrics"], metric)
    baseline_value = _metric_value(attribution["baseline_metrics"], metric)
    if target_value is None or baseline_value is None:
        raise schemas.OpError(
            "invalid_jsonl: 整体兜底建议需要目标/基准的整体 %s 指标，输入缺失（metrics/baseline_metrics）" % metric
        )
    diff = target_value - baseline_value
    abs_diff = abs(diff)
    trend = "升高" if diff > 0 else ("降低" if diff < 0 else "持平")
    samples = attribution["sample_count"]
    min_count = int(cfg["checks"]["sample_plan_min_count"])
    gap = max(min_count, abs(int(attribution["total_net_error"])))
    count = max(min_count, int(_round_half_up(gap * params["sample_plan_factor"])))
    projected = samples + count

    numbers = _base_numbers(attribution, cfg)
    numbers.update(
        {
            "trend": trend,
            "abs_diff_pp": _pp_text(abs_diff, cfg),
            "samples": samples,
            "sample_count": count,
            "gap": gap,
            "factor": _num_text(params["sample_plan_factor"]),
        }
    )
    priority = _priority(abs_diff, None, params)
    return {
        "title": _fit_title(templates["title_overall"].format(**numbers), cfg, warnings),
        "dimension": "overall",
        "value": None,
        "priority": priority,
        "fact": templates["fact_overall"].format(**numbers),
        "evidence": templates["evidence_overall"].format(**numbers),
        "explanation": templates["explanation_overall"].format(**numbers),
        "verification": templates["verification_overall"].format(**numbers),
        "sample_plan": _sample_plan(
            count=count,
            gap=gap,
            factor=params["sample_plan_factor"],
            projected_samples=projected,
            purpose="overall_review",
            factor_row=None,
            cfg=cfg,
            target_pp_drop=params["target_pp_drop"],
        ),
        "source": _source(attribution, None, "overall_review"),
        "evidence_source": "overall_metrics",
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> Dict[str, Any]:
    cfg = ctx.config
    if not isinstance(cfg, dict):
        cfg = cfgmod.load(Path(__file__).resolve().parent)
    ctx.log.start()

    params = _parse_params(ctx, cfg)
    input_path = _resolve_input(ctx, cfg)
    ctx.log.validated(input_path.name)

    attribution = _load_attribution(input_path)
    ctx.log.read(1, "条归因结果")
    warnings: List[str] = []
    for note in attribution["warnings"]:
        warnings.append("008 警告：%s" % note)
    if not attribution["record_attributable"]:
        warnings.append("输入记录的 attributable=false（版本已回滚）；008 应已跳过，009 仍按产物生成建议")
    if attribution["main_cause"] is None:
        reason = attribution["payload"].get("main_cause_reason")
        warnings.append("008 未给出主因%s，009 按维度因素降级生成建议"
                        % ("（%s）" % reason if reason else ""))

    alerts = _load_alerts(input_path, attribution, cfg, warnings)
    degradation, coverage = _select_factors(attribution, params, cfg)

    chosen: List[Any] = list(degradation[: params["max_suggestions"]])
    if len(chosen) < params["max_suggestions"]:
        remaining = params["max_suggestions"] - len(chosen)
        chosen.extend(coverage[:remaining])
    if len(chosen) < params["min_suggestions"]:
        chosen.append(OVERALL)
    if not chosen:
        chosen = [OVERALL]

    suggestions: List[Dict[str, Any]] = []
    for item in chosen:
        if item == OVERALL:
            suggestions.append(_build_overall(attribution, params, cfg, warnings))
        elif item.get("diff_pp") is not None and item["diff_pp"] > 0:
            suggestions.append(_build_degradation(item, attribution, params, cfg, warnings))
        else:
            suggestions.append(_build_coverage(item, attribution, params, cfg, alerts["threshold_pp"], warnings))

    priority_counts = {"P0": 0, "P1": 0, "P2": 0}
    for suggestion in suggestions:
        priority_counts[suggestion["priority"]] += 1

    advice = {
        "task_id": str(attribution["external_task_id"] or attribution["eval_id"]),
        "generated_at": _now_iso(),
        "suggestions": suggestions,
        # ---- 只增字段 ----
        "operator": "avp_suggest_optimization",
        "record_id": attribution["record_id"],
        "eval_id": attribution["eval_id"],
        "baseline_eval_id": attribution["baseline_eval_id"],
        "metric": attribution["metric"],
        "total_net_error": attribution["total_net_error"],
        "main_cause": attribution["main_cause"],
        "share": attribution["share"],
        "affected": attribution["affected"],
        "confidence": attribution["confidence"],
        "suggestion_count": len(suggestions),
        "priority_counts": priority_counts,
        "alerts": {"available": alerts["used"], "count": alerts["count"], "threshold_pp": alerts["threshold_pp"]},
        "source_file": input_path.name,
        "warnings": warnings,
    }

    out_dir = ctx.out_dir
    out_path = iofs.write_json(out_dir / str(cfg["io"]["output_advice"]), advice)

    statistics = {
        "input_files": 1,
        "factors_total": len(attribution["factors"]),
        "factors_in_scope": len([f for f in attribution["factors"] if f["in_scope"]]),
        "factors_comparable": len([f for f in attribution["factors"] if f["comparable"]]),
        "degradation_factors": len(degradation),
        "coverage_factors": len(coverage),
        "suggestions": len(suggestions),
        "p0": priority_counts["P0"],
        "p1": priority_counts["P1"],
        "p2": priority_counts["P2"],
        "alerts_available": alerts["used"],
        "overall_fallback": any(item.get("dimension") == "overall" for item in suggestions),
        "main_cause_found": attribution["main_cause"] is not None,
    }
    ctx.log.add(
        "处理",
        "建议生成完成",
        "共 %d 条（P0=%d, P1=%d, P2=%d），主因 %s"
        % (
            len(suggestions),
            priority_counts["P0"],
            priority_counts["P1"],
            priority_counts["P2"],
            attribution["main_cause"] or "未判定",
        ),
    )
    ctx.log.write_output(out_path.name)

    return {
        "outputs": {"advice": out_path},
        "statistics": statistics,
        "result": {
            "task_id": advice["task_id"],
            "eval_id": attribution["eval_id"],
            "suggestion_count": len(suggestions),
            "priorities": [suggestion["priority"] for suggestion in suggestions],
            "main_cause": attribution["main_cause"],
            "total_net_error": attribution["total_net_error"],
            "status": "success",
        },
        "status": "success",
    }
