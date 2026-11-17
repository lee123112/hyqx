# -*- coding: utf-8 -*-
"""08 差异归因（avp_attribute_gap）。

职责
----
读取 007 落库的评测结果，把「目标（受测模型 / 新版本）」与「基准（对比模型 / 上一版本）」
按维度、错误形态、样本逐层对齐，算出整体净多错条数、每个维度的差值（pp）、影响条数、
占比与置信度，给出主因与告警，并保留可追溯到样本 ID 的归因链。

业务硬口径（规范 §11.9，不要改）
-------------------------------
- 主因 = 占比最高且 **≥35%**（``checks.main_cause_share``）；最高占比并列或低于阈值 →
  ``main_cause=null`` 并写原因，不硬选；
- 影响条数 = 四舍五入((目标指标 − 基准指标) / 100 × 该维度样本数)，用 half-up，
  不用 Python ``round`` 的银行家舍入；
- 总净多错 = 四舍五入((目标指标 − 基准指标) / 100 × 目标样本数)；
- 占比 ``share`` = 影响条数 / 总净多错；总净多错 ≤ 0 时占比记 0、不设主因；
- 置信度 = 主因样本 <100 低 / 100–200 中 / >200 高；
- 告警 = 单维度差值 **严格大于** 1.5pp（``checks.alert_pp``）；
- ``attributable=false``（版本已回滚）的记录不参与归因。

技术方案
--------
1. **基线解析**：``baseline_type=model`` 找同版本的其他模型；``baseline_type=version`` 找指定
   基准版本，未给 ``baseline_id`` 时按 ``version_id`` 字典序取上一版本。找不到就报参数错误，
   不猜、不拿自己跟自己比；
2. **维度对齐**：按 ``(dimension, value)`` 内连接；只对可比因素算差值，缺基准取值的因素保留在
   ``factors`` 里但不参与主因排名；
3. **硬口径计算**：影响条数、占比、置信度、告警全部按上面的口径算，阈值/系数只来自配置；
4. **样本级证据**：从 ``sample_results`` 的错误样本按 ``perturb`` 快照回溯到维度分组，统计错误
   形态构成、SNR 分桶集中度与 95% 置信区间半宽；样本无法完整回溯时明确标 ``unavailable``，
   不编造数字；
5. **归因链**：record → baseline → dimension → error_type → sample，逐层可追，样本 ID 截断
   （``max_trace_samples``）但总数保留。
"""

from __future__ import annotations

import datetime as _dt
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # 单目录跑测试/单跑时也能 import common
    sys.path.insert(0, str(_ROOT))

from common import iofs  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402

METRIC_NAMES = ("cer", "wer", "ser")
ERROR_TYPES = ("sub", "del", "ins", "unk")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _round_half_up(value: float, digits: int = 0) -> float:
    """四舍五入（half-up），避免 Python ``round`` 的银行家舍入（2.5 → 3）。"""
    factor = 10 ** digits
    scaled = float(value) * factor
    if scaled >= 0:
        out = math.floor(scaled + 0.5)
    else:
        out = math.ceil(scaled - 0.5)
    return out / factor


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _pos_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0 and value == int(value):
        return int(value)
    return None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _model_eq(left: Any, right: Any) -> bool:
    a, b = _text(left), _text(right)
    return bool(a) and bool(b) and a.casefold() == b.casefold()


def _metrics_of(record: Dict[str, Any]) -> Dict[str, float]:
    raw = record.get("metrics")
    out: Dict[str, float] = {}
    if isinstance(raw, dict):
        for name in METRIC_NAMES:
            value = _num(raw.get(name))
            if value is not None:
                out[name] = value
    return out


def _sample_count(record: Dict[str, Any]) -> int:
    """样本总数：优先 sample_count（007 归一化字段），退化到维度合计 / 样本条数。"""
    explicit = _pos_int(record.get("sample_count"))
    if explicit is not None:
        return explicit
    total = 0
    for dim in record.get("dimensions") or []:
        if isinstance(dim, dict):
            count = _pos_int(dim.get("samples"))
            if count:
                total += count
    if total:
        return total
    samples = record.get("sample_results")
    return len(samples) if isinstance(samples, list) else 0


def _sample_list(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = record.get("sample_results")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _error_type_of(sample: Dict[str, Any]) -> Optional[str]:
    value = sample.get("error_type")
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value not in ERROR_TYPES:
        return None
    return value


def _perturb_dimension(sample: Dict[str, Any]) -> str:
    perturb = sample.get("perturb")
    if not isinstance(perturb, dict):
        return ""
    return _text(perturb.get("dimension"))


def _perturb_matches(sample: Dict[str, Any], dimension: str, value: str) -> bool:
    perturb = sample.get("perturb")
    if not isinstance(perturb, dict):
        return False
    return _text(perturb.get("dimension")) == dimension and _text(perturb.get("value")) == value


def _snr_bucket_label(snr: float, bounds: Sequence[float]) -> str:
    if not bounds:
        return "all"
    if snr < bounds[0]:
        return "<%g" % bounds[0]
    for left, right in zip(bounds[:-1], bounds[1:]):
        if left <= snr < right:
            return "%g–%g" % (left, right)
    return ">=%g" % bounds[-1]


def _confidence_of(samples: int, bands: Dict[str, int]) -> str:
    if samples < int(bands["low"]):
        return "低"
    if samples <= int(bands["high"]):
        return "中"
    return "高"


# --------------------------------------------------------------------------- #
# 参数 / 输入
# --------------------------------------------------------------------------- #
def _parse_params(params: Dict[str, Any], ctx: OperatorContext, cfg: Dict[str, Any]) -> Dict[str, Any]:
    attribution = cfg["attribution"]
    checks = cfg["checks"]

    baseline_type = _text(params.get("baseline_type", attribution["baseline_type"])).lower()
    if baseline_type not in ("model", "version"):
        raise schemas.ParamError("baseline_type 只能是 model 或 version，收到 %r" % params.get("baseline_type"))

    baseline_id: Optional[str] = None
    raw_baseline = params.get("baseline_id")
    if raw_baseline not in (None, ""):
        baseline_id = schemas.as_str(raw_baseline, "baseline_id", max_len=128)

    dimensions_raw = params.get("dimensions")
    if dimensions_raw is None:
        dimensions = [str(item).strip() for item in attribution["dimensions"] if str(item).strip()]
    else:
        items = schemas.as_list(dimensions_raw, "dimensions")
        dimensions = [schemas.as_str(item, "dimensions[]", max_len=64) for item in items]
    if not dimensions:
        raise schemas.ParamError("dimensions 不能为空数组")

    alert_pp = schemas.as_float(params.get("alert_pp", checks["alert_pp"]), "alert_pp", lo=0.0)
    main_cause_share = schemas.as_float(
        params.get("main_cause_share", checks["main_cause_share"]), "main_cause_share", lo=0.0, hi=1.0
    )

    bands_raw = params.get("confidence_bands", checks["confidence_bands"])
    if not isinstance(bands_raw, dict):
        raise schemas.ParamError("confidence_bands 必须是对象 {low, high}")
    default_bands = checks["confidence_bands"]
    low = schemas.as_int(bands_raw.get("low", default_bands["low"]), "confidence_bands.low", lo=0)
    high = schemas.as_int(bands_raw.get("high", default_bands["high"]), "confidence_bands.high", lo=0)
    if low >= high:
        raise schemas.ParamError("confidence_bands.low 必须小于 confidence_bands.high")

    metric = _text(attribution["primary_metric"]).lower()
    if metric not in METRIC_NAMES:
        raise schemas.OpError("invalid_params: 配置 attribution.primary_metric 非法：%s" % metric)

    return {
        "baseline_type": baseline_type,
        "baseline_id": baseline_id,
        "dimensions": dimensions,
        "alert_pp": alert_pp,
        "main_cause_share": main_cause_share,
        "confidence_bands": {"low": low, "high": high},
        "metric": metric,
        "scope_fallback_all": bool(attribution.get("scope_fallback_all", True)),
        "max_trace_samples": max(1, int(attribution.get("max_trace_samples", 50))),
        "snr_buckets": [float(x) for x in attribution.get("snr_buckets") or []],
        "z_95": float(cfg["stats"]["z_95"]),
        "diff_digits": int(cfg["precision"]["diff_pp_digits"]),
        "share_digits": int(cfg["precision"]["share_digits"]),
        "pct_digits": int(cfg["precision"]["pct_digits"]),
        "ci_digits": int(cfg["precision"]["ci_digits"]),
    }


def _resolve_input(ctx: OperatorContext, cfg: Dict[str, Any]) -> Path:
    path = ctx.require_input()
    if not path.is_dir():
        return path
    preferred = path / str(cfg["io"]["input_default_name"])
    if preferred.is_file():
        return preferred
    candidates = [
        item for item in sorted(path.iterdir()) if item.is_file() and item.suffix.lower() in (".jsonl", ".json")
    ]
    if not candidates:
        raise schemas.OpError("missing_file: 输入目录里没有 .jsonl/.json 文件：%s" % path.name)
    return candidates[0]


def _load_records(path: Path) -> List[Dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = iofs.read_json(path)
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = None
            for key in ("records", "items", "results", "data"):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
            if rows is None:
                rows = [payload]
        else:
            raise schemas.OpError("invalid_jsonl: 输入 JSON 既不是对象也不是数组：%s" % path.name)
    else:
        rows = iofs.read_jsonl(path)
    records = [row for row in rows if isinstance(row, dict)]
    if not records:
        raise schemas.OpError("invalid_jsonl: 输入没有可用的评测结果记录：%s" % path.name)
    return records


_REQUIRED_FIELDS = ("eval_id", "version_id", "metrics", "dimensions", "error_mix", "sample_results")


def _check_record(record: Dict[str, Any], index: int) -> None:
    missing = [name for name in _REQUIRED_FIELDS if name not in record or record[name] in (None, "")]
    if missing:
        raise schemas.OpError(
            "invalid_jsonl: 第 %d 条不是 007 评测结果产物，缺少字段：%s（请先用 007 落库）"
            % (index + 1, ", ".join(missing))
        )
    if not _metrics_of(record):
        raise schemas.OpError("invalid_jsonl: 第 %d 条 metrics 里没有 cer/wer/ser 指标" % (index + 1))


# --------------------------------------------------------------------------- #
# 基线与目标选择
# --------------------------------------------------------------------------- #
def _pick_record(pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    """同一版本/模型可能有多条评测记录：取样本最多的一条，同数按 eval_id 稳定排序。"""
    return max(pool, key=lambda item: (_sample_count(item), _text(item.get("eval_id"))))


def _select_pair(eligible: List[Dict[str, Any]], params: Dict[str, Any]) -> Dict[str, Any]:
    baseline_type = params["baseline_type"]
    baseline_id = params["baseline_id"]
    warnings: List[str] = []

    if baseline_type == "model":
        if not baseline_id:
            raise schemas.ParamError(
                "baseline_type=model 时必须提供 baseline_id（对比对象模型名），否则无法确定归因基准"
            )
        baseline_pool = [item for item in eligible if _model_eq(item.get("model"), baseline_id)]
        target_pool = [item for item in eligible if not _model_eq(item.get("model"), baseline_id)]
        if not baseline_pool:
            raise schemas.ParamError(
                "未找到对比模型「%s」的可归因评测记录（attributable=true）；请确认 baseline_id 或先完成该模型评测"
                % baseline_id
            )
        if not target_pool:
            raise schemas.ParamError("除对比模型「%s」外没有可归因的受测模型记录" % baseline_id)
        target = _pick_record(target_pool)
        same_version = [item for item in baseline_pool if item.get("version_id") == target.get("version_id")]
        baseline = _pick_record(same_version or baseline_pool)
        if not same_version:
            warnings.append(
                "对比模型没有与目标同版本（%s）的记录，已用版本 %s 的基准记录，跨版本差异会混入对比"
                % (target.get("version_id"), baseline.get("version_id"))
            )
        if len(target_pool) > 1:
            warnings.append(
                "存在 %d 条非对比模型记录，已选样本最多的一条：%s" % (len(target_pool), target.get("eval_id"))
            )
        baseline_label = baseline_id
    else:
        versions = sorted({_text(item.get("version_id")) for item in eligible if _text(item.get("version_id"))})
        if not versions:
            raise schemas.OpError("invalid_jsonl: 可归因记录没有 version_id")
        if baseline_id:
            if baseline_id not in versions:
                raise schemas.ParamError(
                    "基准版本「%s」不在可归因记录里（现有：%s）" % (baseline_id, "、".join(versions))
                )
            target_versions = [item for item in versions if item != baseline_id]
            if not target_versions:
                raise schemas.ParamError("除基准版本「%s」外没有其它版本，无法做跨版本对比" % baseline_id)
            baseline_version = baseline_id
            target_version = target_versions[-1]
        else:
            if len(versions) < 2:
                raise schemas.ParamError(
                    "只有一个可归因版本（%s），无法做跨版本对比；请提供 baseline_id 或补充上一版本评测结果"
                    % versions[0]
                )
            baseline_version = versions[-2]
            target_version = versions[-1]
            warnings.append(
                "未提供 baseline_id，已按 version_id 字典序取上一版本：%s（目标 %s）" % (baseline_version, target_version)
            )
        target_pool = [item for item in eligible if _text(item.get("version_id")) == target_version]
        baseline_pool = [item for item in eligible if _text(item.get("version_id")) == baseline_version]
        target = _pick_record(target_pool)
        baseline = _pick_record(baseline_pool)
        if len(target_pool) > 1:
            warnings.append(
                "目标版本 %s 有 %d 条可归因记录，已选样本最多的一条：%s"
                % (target_version, len(target_pool), target.get("eval_id"))
            )
        baseline_label = baseline_version

    return {"target": target, "baseline": baseline, "baseline_label": baseline_label, "warnings": warnings}


# --------------------------------------------------------------------------- #
# 样本级证据
# --------------------------------------------------------------------------- #
def _trace_factor(
    record: Dict[str, Any],
    dimension: str,
    value: str,
    target_samples: int,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """把一个维度取值回溯到样本级：错误条数、形态构成、SNR 分桶、95% CI 半宽。"""
    samples = _sample_list(record)
    mode = _text(record.get("sample_results_mode")) or "full"
    all_errors = [item for item in samples if _error_type_of(item)]
    tagged = [item for item in samples if _perturb_matches(item, dimension, value)]
    tagged_errors = [item for item in tagged if _error_type_of(item)]
    ungrouped_errors = [item for item in all_errors if not _perturb_dimension(item)]
    coverage_ok = bool(all_errors) and not ungrouped_errors

    if mode == "subset":
        exact = coverage_ok
    else:
        exact = coverage_ok and target_samples > 0 and len(tagged) == target_samples

    counts = {name: 0 for name in ERROR_TYPES}
    for item in tagged_errors:
        counts[_error_type_of(item) or "sub"] += 1

    error_samples: Optional[int] = len(tagged_errors) if exact else None
    n = target_samples if mode == "subset" else len(tagged)
    error_rate: Optional[float] = None
    ci_half_pp: Optional[float] = None
    if error_samples is not None and n > 0:
        p = error_samples / float(n)
        error_rate = _round_half_up(p, params["diff_digits"])
        ci_half_pp = _round_half_up(
            params["z_95"] * math.sqrt(p * (1.0 - p) / float(n)) * 100.0, params["ci_digits"]
        )

    bounds = list(params["snr_buckets"])
    bucket_counts: Dict[str, int] = {}
    snr_values: List[float] = []
    for item in tagged_errors:
        perturb = item.get("perturb") if isinstance(item.get("perturb"), dict) else {}
        snr = _num(perturb.get("snr_db")) if isinstance(perturb, dict) else None
        if snr is None:
            continue
        snr_values.append(snr)
        label = _snr_bucket_label(snr, bounds)
        bucket_counts[label] = bucket_counts.get(label, 0) + 1

    buckets: List[Dict[str, Any]] = []
    if bounds:
        labels: List[str] = []
        labels.append("<%g" % bounds[0])
        for left, right in zip(bounds[:-1], bounds[1:]):
            labels.append("%g–%g" % (left, right))
        labels.append(">=%g" % bounds[-1])
        for label in labels:
            buckets.append({"label": label, "count": bucket_counts.get(label, 0)})
    top_bucket: Optional[str] = None
    if bucket_counts:
        top_bucket = max(bucket_counts.items(), key=lambda kv: kv[1])[0]

    if exact:
        status = "exact"
        note = None
    elif mode == "subset":
        status = "partial"
        note = "样本级回传为错误样本子集，且存在无法归到维度的错误样本，错误条数按已回传样本统计"
    else:
        status = "unavailable"
        note = "样本级结果未覆盖该维度全部样本（或存在无 perturb 的错误样本），仅用汇总口径，不估算置信区间"

    trace = {
        "status": status,
        "sample_results_mode": mode,
        "listed_samples": len(samples),
        "tagged_samples": len(tagged),
        "target_samples": target_samples,
        "error_samples": error_samples,
        "error_rate": error_rate,
        "ci_half_pp": ci_half_pp,
        "error_counts": counts,
        "snr_buckets": buckets,
        "top_snr_bucket": top_bucket,
        "snr_values_count": len(snr_values),
        "sample_ids": [str(item.get("sample_id")) for item in tagged_errors][: params["max_trace_samples"]],
        "sample_ids_truncated": max(0, len(tagged_errors) - params["max_trace_samples"]),
        "note": note,
    }
    return trace


# --------------------------------------------------------------------------- #
# 归因链
# --------------------------------------------------------------------------- #
def _build_chain(
    target: Dict[str, Any],
    baseline: Dict[str, Any],
    factor: Optional[Dict[str, Any]],
    trace: Optional[Dict[str, Any]],
    metric: str,
    params: Dict[str, Any],
    main_cause: Optional[str],
    reason: str,
    trace_for: Optional[str] = None,
) -> List[Dict[str, Any]]:
    chain: List[Dict[str, Any]] = [
        {
            "level": "record",
            "ref": {
                "eval_id": target.get("eval_id"),
                "external_task_id": target.get("external_task_id"),
                "version_id": target.get("version_id"),
                "model": target.get("model"),
                "sample_count": _sample_count(target),
                "sample_results_mode": target.get("sample_results_mode"),
                "metric": metric,
                "value": _metrics_of(target).get(metric),
            },
        },
        {
            "level": "baseline",
            "ref": {
                "type": baseline.get("baseline_type") if isinstance(baseline, dict) else None,
                "eval_id": baseline.get("eval_id"),
                "external_task_id": baseline.get("external_task_id"),
                "version_id": baseline.get("version_id"),
                "model": baseline.get("model"),
                "sample_count": _sample_count(baseline),
                "metric": metric,
                "value": _metrics_of(baseline).get(metric),
            },
        },
    ]
    if factor is None:
        chain.append({"level": "summary", "ref": {"main_cause": None, "reason": reason}})
        return chain

    if main_cause is None:
        summary = {"main_cause": None, "reason": reason}
        if trace_for:
            summary["trace_for"] = trace_for
        chain.append({"level": "summary", "ref": summary})

    chain.append(
        {
            "level": "dimension",
            "ref": {
                "dimension": factor["dimension"],
                "value": factor["value"],
                "samples": factor["samples"],
                "ratio": factor["ratio"],
                "target_metric": factor["target_metric"],
                "baseline_metric": factor["baseline_metric"],
                "diff_pp": factor["diff_pp"],
                "affected": factor["affected"],
                "share": factor["share"],
                "confidence": factor["confidence"],
                "in_scope": factor["in_scope"],
            },
        }
    )

    trace = trace or {}
    error_samples = trace.get("error_samples")
    source = "sample_results" if error_samples is not None else "record.error_mix"
    if error_samples is not None:
        error_counts = dict(trace.get("error_counts") or {})
    else:
        raw_mix = target.get("error_mix") if isinstance(target.get("error_mix"), dict) else {}
        error_counts = {name: int(raw_mix.get(name) or 0) for name in ERROR_TYPES}
    chain.append(
        {
            "level": "error_type",
            "ref": {
                "source": source,
                "error_samples": error_samples,
                "error_counts": error_counts,
                "ci_half_pp": trace.get("ci_half_pp"),
                "top_snr_bucket": trace.get("top_snr_bucket"),
            },
        }
    )
    chain.append(
        {
            "level": "sample",
            "ref": {
                "count": error_samples if error_samples is not None else sum(error_counts.values()),
                "sample_ids": list(trace.get("sample_ids") or []) if error_samples is not None else [],
                "truncated": int(trace.get("sample_ids_truncated") or 0),
                "note": "错误样本 ID 链路（按 sample_results 顺序，最多 %d 条）" % params["max_trace_samples"],
            },
        }
    )
    return chain


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(ctx: OperatorContext) -> dict:
    cfg = ctx.config or {}
    params = _parse_params(ctx.params or {}, ctx, cfg)
    metric = params["metric"]

    input_path = _resolve_input(ctx, cfg)
    records = _load_records(input_path)
    ctx.log.read(len(records), "条评测结果")

    eligible: List[Dict[str, Any]] = []
    skipped = 0
    for index, record in enumerate(records):
        _check_record(record, index)
        if record.get("attributable") is False:
            skipped += 1
            continue
        eligible.append(record)
    if not eligible:
        raise schemas.OpError(
            "invalid_jsonl: 输入里没有 attributable=true 的评测记录（共 %d 条，已回滚 %d 条）；已回滚版本不参与归因"
            % (len(records), skipped)
        )
    if skipped:
        ctx.log.warning("跳过 %d 条 attributable=false（版本已回滚）的评测记录，不参与归因" % skipped)

    picked = _select_pair(eligible, params)
    target = picked["target"]
    baseline = picked["baseline"]
    warnings: List[str] = list(picked["warnings"])

    target_metrics = _metrics_of(target)
    baseline_metrics = _metrics_of(baseline)
    if metric not in target_metrics:
        raise schemas.OpError("invalid_jsonl: 目标记录缺少 %s 指标：%s" % (metric, target.get("eval_id")))
    if metric not in baseline_metrics:
        raise schemas.OpError("invalid_jsonl: 基准记录缺少 %s 指标：%s" % (metric, baseline.get("eval_id")))
    target_samples = _sample_count(target)
    baseline_samples = _sample_count(baseline)
    target_value = target_metrics[metric]
    baseline_value = baseline_metrics[metric]

    # 总净多错：整体指标差 × 目标样本数（pp → 条数要 /100，四舍五入）
    total_net_error = int(_round_half_up((target_value - baseline_value) / 100.0 * target_samples, 0))

    # 维度白名单：与数据完全对不上时按配置退化为全维度（并标记），不退化为「无归因」
    target_dims = [item for item in target.get("dimensions") or [] if isinstance(item, dict)]
    present_dims = {_text(item.get("dimension")) for item in target_dims if _text(item.get("dimension"))}
    scope_set = set(params["dimensions"])
    scope_fallback = False
    if scope_set & present_dims:
        scope_names = scope_set & present_dims
    elif params["scope_fallback_all"] and present_dims:
        scope_names = present_dims
        scope_fallback = True
        warnings.append(
            "维度白名单 %s 与评测结果维度 %s 完全不匹配，已退化为全部维度参与归因"
            % ("/".join(sorted(scope_set)), "/".join(sorted(present_dims)))
        )
    else:
        scope_names = set()

    baseline_dims: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in baseline.get("dimensions") or []:
        if not isinstance(item, dict):
            continue
        dim, val = _text(item.get("dimension")), _text(item.get("value"))
        if dim and val:
            baseline_dims[(dim, val)] = item

    factors: List[Dict[str, Any]] = []
    for dim_entry in target_dims:
        dimension = _text(dim_entry.get("dimension"))
        value = _text(dim_entry.get("value"))
        if not dimension or not value:
            continue
        in_scope = dimension in scope_names
        dim_samples = _pos_int(dim_entry.get("samples")) or 0
        ratio = _num(dim_entry.get("ratio"))
        target_dim_metrics = {
            name: _num(dim_entry.get(name)) for name in METRIC_NAMES if _num(dim_entry.get(name)) is not None
        }
        base_entry = baseline_dims.get((dimension, value))
        base_dim_metrics = (
            {name: _num(base_entry.get(name)) for name in METRIC_NAMES if _num(base_entry.get(name)) is not None}
            if base_entry
            else {}
        )
        comparable = bool(
            base_entry
            and metric in target_dim_metrics
            and metric in base_dim_metrics
            and dim_samples > 0
        )
        target_metric = target_dim_metrics.get(metric)
        baseline_metric = base_dim_metrics.get(metric)
        diff_pp: Optional[float] = None
        deltas: Dict[str, float] = {}
        affected: Optional[int] = None
        if comparable:
            diff_pp = _round_half_up(float(target_metric) - float(baseline_metric), params["diff_digits"])
            # 影响条数 = 差值(pp) / 100 × 样本数，四舍五入
            affected = int(_round_half_up(float(diff_pp) / 100.0 * dim_samples, 0))
            for name in METRIC_NAMES:
                if name in target_dim_metrics and name in base_dim_metrics:
                    deltas[name] = _round_half_up(
                        float(target_dim_metrics[name]) - float(base_dim_metrics[name]), params["diff_digits"]
                    )

        share = 0.0
        if total_net_error > 0 and affected is not None:
            share = _round_half_up(affected / float(total_net_error), params["share_digits"])

        trace = _trace_factor(target, dimension, value, dim_samples, params) if dim_samples else None
        factors.append(
            {
                "dimension": dimension,
                "value": value,
                "in_scope": in_scope,
                "samples": dim_samples,
                "ratio": _num(dim_entry.get("ratio")),
                "target_metric": target_metric,
                "baseline_metric": baseline_metric,
                "target_metrics": target_dim_metrics,
                "baseline_metrics": base_dim_metrics,
                "baseline_samples": _pos_int(base_entry.get("samples")) if base_entry else None,
                "baseline_ratio": _num(base_entry.get("ratio")) if base_entry else None,
                "diff_pp": diff_pp,
                "deltas_pp": deltas,
                "affected": affected,
                "share": share,
                "confidence": _confidence_of(dim_samples, params["confidence_bands"]) if in_scope else None,
                "comparable": comparable,
                "baseline_present": base_entry is not None,
                "sample_trace": trace,
            }
        )

    # 主因：范围内、可比、影响条数为正，占比最高且 ≥ 阈值；并列/不足 → 不设主因
    main_factor: Optional[Dict[str, Any]] = None
    main_cause: Optional[str] = None
    main_reason = ""
    main_share: Optional[float] = None
    main_affected: Optional[int] = None
    main_confidence: Optional[str] = None

    comparable_in_scope = [item for item in factors if item["in_scope"] and item["comparable"]]
    candidates = [item for item in comparable_in_scope if item["affected"] is not None and item["affected"] > 0]
    if total_net_error <= 0:
        main_reason = "整体无净退化：%s 差值 %.2fpp，总净多错 %d 条，不设主因" % (
            metric.upper(),
            _round_half_up(target_value - baseline_value, params["pct_digits"]),
            total_net_error,
        )
    elif not comparable_in_scope:
        main_reason = "无可比维度：范围内没有同时具备目标与基准 %s 的维度取值" % metric.upper()
    elif not candidates:
        main_reason = "无显著主因：范围内没有影响条数为正的维度（含缺基准取值或差值≤0）"
    else:
        best_share = max(item["share"] for item in candidates)
        tops = [item for item in candidates if abs(item["share"] - best_share) <= 1e-9]
        if len(tops) > 1:
            names = "、".join("%s/%s" % (item["dimension"], item["value"]) for item in tops)
            main_reason = "无显著主因：最高占比并列 %.2f%%（%s），无法唯一确定主因" % (
                best_share * 100.0,
                names,
            )
        elif best_share < params["main_cause_share"]:
            main_reason = "无显著主因：最高占比 %.2f%% < 阈值 %.2f%%（%s/%s）" % (
                best_share * 100.0,
                params["main_cause_share"] * 100.0,
                tops[0]["dimension"],
                tops[0]["value"],
            )
        else:
            main_factor = tops[0]
            main_cause = "%s/%s" % (main_factor["dimension"], main_factor["value"])
            main_share = main_factor["share"]
            main_affected = main_factor["affected"]
            main_confidence = _confidence_of(main_factor["samples"], params["confidence_bands"])
            main_reason = "主因：%s 影响 %d 条，占比 %.2f%% ≥ %.2f%%，置信度 %s" % (
                main_cause,
                main_affected,
                main_share * 100.0,
                params["main_cause_share"] * 100.0,
                main_confidence,
            )

    # 告警：单维度差值严格大于阈值（可比维度，不限于白名单，便于发现白名单外的退化）
    alerts: List[Dict[str, Any]] = []
    for item in factors:
        if not item["comparable"] or item["diff_pp"] is None:
            continue
        if item["diff_pp"] > params["alert_pp"]:
            alerts.append(
                {
                    "code": "dimension_diff_gt_threshold",
                    "dimension": item["dimension"],
                    "value": item["value"],
                    "metric": metric,
                    "diff_pp": item["diff_pp"],
                    "threshold_pp": params["alert_pp"],
                    "samples": item["samples"],
                    "affected": item["affected"],
                    "in_scope": item["in_scope"],
                    "message": "%s/%s %s 差值 %.2fpp > 告警阈值 %.2fpp"
                    % (
                        item["dimension"],
                        item["value"],
                        metric.upper(),
                        item["diff_pp"],
                        params["alert_pp"],
                    ),
                }
            )

    # 归因链：有主因追主因；无主因时追「占比最高的可比维度」，仍要能落到样本级
    trace_factor = main_factor
    trace_for: Optional[str] = None
    if trace_factor is None:
        fallback_pool = [item for item in comparable_in_scope if item["affected"] is not None and item["affected"] > 0]
        if not fallback_pool:
            fallback_pool = [item for item in comparable_in_scope]
        if fallback_pool:
            trace_factor = sorted(
                fallback_pool,
                key=lambda item: (
                    -item["share"],
                    -float(item["diff_pp"]),
                    item["dimension"],
                    item["value"],
                ),
            )[0]
            trace_for = "top_factor"
    chain = _build_chain(
        target,
        {"baseline_type": params["baseline_type"], **baseline},
        trace_factor,
        trace_factor["sample_trace"] if trace_factor else None,
        metric,
        params,
        main_cause,
        main_reason,
        trace_for=trace_for,
    )

    record_id = "ATTR-%s-%s" % (_text(target.get("eval_id")), _text(baseline.get("eval_id")))
    generated_at = _now_iso()

    attribution: Dict[str, Any] = {
        "record_id": record_id,
        "eval_id": target.get("eval_id"),
        "baseline": {
            "type": params["baseline_type"],
            "id": picked["baseline_label"],
            "eval_id": baseline.get("eval_id"),
            "external_task_id": baseline.get("external_task_id"),
            "version_id": baseline.get("version_id"),
            "model": baseline.get("model"),
            "metrics": baseline_metrics,
            "sample_count": baseline_samples,
        },
        "total_net_error": total_net_error,
        "main_cause": main_cause,
        "share": main_share,
        "affected": main_affected,
        "confidence": main_confidence,
        "factors": factors,
        "chain": chain,
        # -- 只增不改的补充字段（009 / 011 消费） ------------------------------
        "generated_at": generated_at,
        "metric": metric,
        "external_task_id": target.get("external_task_id"),
        "version_id": target.get("version_id"),
        "model": target.get("model"),
        "sample_count": target_samples,
        "sample_results_mode": target.get("sample_results_mode"),
        "metrics": target_metrics,
        "baseline_metrics": baseline_metrics,
        "error_mix": target.get("error_mix") if isinstance(target.get("error_mix"), dict) else {},
        "main_cause_dimension": main_factor["dimension"] if main_factor else None,
        "main_cause_value": main_factor["value"] if main_factor else None,
        "main_cause_reason": main_reason,
        "main_cause_detail": (
            {
                "dimension": main_factor["dimension"],
                "value": main_factor["value"],
                "samples": main_factor["samples"],
                "diff_pp": main_factor["diff_pp"],
                "affected": main_factor["affected"],
                "share": main_factor["share"],
                "confidence": main_confidence,
                "error_counts": (main_factor.get("sample_trace") or {}).get("error_counts"),
                "ci_half_pp": (main_factor.get("sample_trace") or {}).get("ci_half_pp"),
                "top_snr_bucket": (main_factor.get("sample_trace") or {}).get("top_snr_bucket"),
                "sample_trace_status": (main_factor.get("sample_trace") or {}).get("status"),
            }
            if main_factor
            else None
        ),
        "alert_count": len(alerts),
        "alert_threshold_pp": float(cfg["checks"]["alert_pp"]),
        "scope": {
            "dimensions": sorted(scope_names),
            "requested": params["dimensions"],
            "fallback": scope_fallback,
        },
        "warnings": warnings,
    }

    alerts_payload: Dict[str, Any] = {
        "generated_at": generated_at,
        "record_id": record_id,
        "eval_id": target.get("eval_id"),
        "external_task_id": target.get("external_task_id"),
        "metric": metric,
        "threshold_pp": params["alert_pp"],
        "count": len(alerts),
        "alerts": alerts,
    }

    # 版本对比：只有 baseline_type=version 时才是「版本间对比」，model 类型只留标记不冒充
    version_compare: Dict[str, Any] = {
        "generated_at": generated_at,
        "applicable": params["baseline_type"] == "version",
        "baseline_type": params["baseline_type"],
        "baseline_id": picked["baseline_label"],
        "target": {
            "eval_id": target.get("eval_id"),
            "version_id": target.get("version_id"),
            "model": target.get("model"),
            "metrics": target_metrics,
            "sample_count": target_samples,
        },
        "baseline": {
            "eval_id": baseline.get("eval_id"),
            "version_id": baseline.get("version_id"),
            "model": baseline.get("model"),
            "metrics": baseline_metrics,
            "sample_count": baseline_samples,
        },
        "overall_delta_pp": {
            name: _round_half_up(target_metrics[name] - baseline_metrics[name], params["diff_digits"])
            for name in METRIC_NAMES
            if name in target_metrics and name in baseline_metrics
        },
        "dimension_deltas": [
            {
                "dimension": item["dimension"],
                "value": item["value"],
                "diff_pp": item["diff_pp"],
                "samples": item["samples"],
                "affected": item["affected"],
                "in_scope": item["in_scope"],
            }
            for item in factors
            if item["comparable"]
        ],
        "note": "仅 baseline_type=version 时可用于跨版本对比；model 类型为跨模型对比，不写入版本比较结论",
    }

    outputs = {
        "attribution": iofs.write_json(ctx.out("008_attribution.json"), attribution),
        "alerts": iofs.write_json(ctx.out("008_alerts.json"), alerts_payload),
        "version_compare": iofs.write_json(ctx.out("008_version_compare.json"), version_compare),
    }
    for path in outputs.values():
        ctx.log.write_output(path.name)

    statistics = {
        "input_records": len(records),
        "attributable_records": len(eligible),
        "skipped_not_attributable": skipped,
        "target_eval_id": target.get("eval_id"),
        "baseline_eval_id": baseline.get("eval_id"),
        "baseline_type": params["baseline_type"],
        "baseline_id": picked["baseline_label"],
        "target_samples": target_samples,
        "baseline_samples": baseline_samples,
        "metric": metric,
        "target_metric": target_value,
        "baseline_metric": baseline_value,
        "total_net_error": total_net_error,
        "factors_total": len(factors),
        "factors_comparable": len([item for item in factors if item["comparable"]]),
        "factors_missing_baseline": len([item for item in factors if not item["baseline_present"]]),
        "factors_out_of_scope": len([item for item in factors if not item["in_scope"]]),
        "alerts": len(alerts),
        "main_cause_found": main_cause is not None,
        "scope_fallback": scope_fallback,
        "sample_trace_exact": len(
            [item for item in factors if (item.get("sample_trace") or {}).get("status") == "exact"]
        ),
    }

    if main_cause is not None:
        ctx.log.add(
            "处理",
            "归因完成",
            "主因 %s（占比 %.2f%%，影响 %d 条，置信度 %s），告警 %d 条"
            % (main_cause, main_share * 100.0, main_affected, main_confidence, len(alerts)),
        )
    else:
        ctx.log.add("处理", "归因完成", "%s；告警 %d 条" % (main_reason, len(alerts)))
    if skipped:
        ctx.log.warning("已回滚版本记录不参与归因：跳过 %d 条" % skipped)
    for item in warnings:
        ctx.log.warning(item)

    status = "success" if statistics["factors_comparable"] > 0 else "partial"
    if status == "partial":
        ctx.log.partial("无可比维度，归因结论仅含整体指标对比，未给出主因")

    result = {
        "task_id": target.get("external_task_id") or target.get("eval_id"),
        "eval_id": target.get("eval_id"),
        "baseline_eval_id": baseline.get("eval_id"),
        "metric": metric,
        "total_net_error": total_net_error,
        "main_cause": main_cause,
        "share": main_share,
        "affected": main_affected,
        "confidence": main_confidence,
        "alerts": len(alerts),
        "factors": len(factors),
        "status": status,
    }
    return {"outputs": outputs, "statistics": statistics, "result": result, "status": status}
