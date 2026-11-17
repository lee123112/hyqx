"""算子13 分级核心：0-100 质量分解析、量纲换算与 S/A/C 严格开闭区间判定。

设计要点：
- 严格开区间与闭区间，禁止 round 改变边界语义；
- 置信度 0-1 量纲先乘 100，并记录转换版本；
- 已存在 quality_score 时必须与置信度换算值一致，否则判冲突；
- 缺分、NaN/Inf、越界一律拒绝，不静默 clamp、不补分。
"""
import math
from typing import Any, Dict, List, Optional, Tuple

_SCALE_RATIO = "ratio_0_1"
_SCALE_PERCENT = "percent_0_100"


def _fmt(value: float) -> str:
    """阈值展示：整数不带小数位，其余保留原值，避免 round 改变语义。"""
    number = float(value)
    if number == int(number):
        return str(int(number))
    return repr(number)


def boundary_rule(labels: Dict[str, str], s_threshold: float, a_threshold: float) -> str:
    """生成可解释的边界规则串，随记录与血缘固定。"""
    return "{}:({},100];{}:[{},{}];{}:[0,{})".format(
        labels["S"], _fmt(s_threshold),
        labels["A"], _fmt(a_threshold), _fmt(s_threshold),
        labels["C"], _fmt(a_threshold),
    )


def grade_of(score: Optional[float], s_threshold: float, a_threshold: float) -> Optional[str]:
    """按严格开闭区间返回内部等级键 S/A/C；非有限值返回 None。

    采用 DEVELOPMENT_PLAN.md「验收」给定的边界（79.999=C、80=A、90=A、90.001=S）：
        S: s_threshold <  N <= 100
        A: a_threshold <= N <= s_threshold
        C: 0 <= N < a_threshold
    该划分无缝无重叠；与计划 §2 正文「C 为 N<=80」的表述冲突，已在 README 与
    交付报告中显式标记，并以「验收」一行为准。
    """
    if score is None:
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    if value > s_threshold:
        return "S"
    if value >= a_threshold:
        return "A"
    return "C"


def _as_number(value: Any, field: str) -> Tuple[Optional[float], Optional[str]]:
    """把输入值转成有限浮点数；bool 视为非法类型。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"invalid_type:{field}"
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None, "score_not_finite"
    return number, None


def _apply_scale(value: float, cfg) -> Tuple[Optional[float], Optional[str], List[str]]:
    """按配置量纲把分数换算到 0-100。"""
    mode = cfg.score_scale
    if mode == _SCALE_RATIO:
        if not 0.0 <= value <= 1.0:
            return None, None, ["score_out_of_range"]
        return value * 100.0, _SCALE_RATIO, []
    if mode == _SCALE_PERCENT:
        if not 0.0 <= value <= 100.0:
            return None, None, ["score_out_of_range"]
        return value, _SCALE_PERCENT, []
    # auto：<=1 视为 0-1 比例，1<N<=100 视为百分制
    if 0.0 <= value <= 1.0:
        return value * 100.0, _SCALE_RATIO, []
    if 1.0 < value <= 100.0:
        return value, _SCALE_PERCENT, []
    return None, None, ["score_out_of_range"]


def _lookup(record: dict, fields: List[str], cfg, metadata: Optional[dict]):
    """按优先级取字段：顶层优先，metadata 兜底（可配置关闭）。"""
    for field in fields:
        if field in record and record[field] is not None:
            return field, record[field]
    if cfg.read_metadata_fallback and isinstance(metadata, dict):
        for field in fields:
            if field in metadata and metadata[field] is not None:
                return field, metadata[field]
    return None, None


def resolve_quality_score(record: dict, cfg) -> Tuple[Optional[float], List[str], Dict[str, Any]]:
    """解析单条记录的 0-100 质量分。

    返回 (quality_score, reasons, info)；reasons 非空表示必须进入异常清单。
    info 记录原始输入值、实际使用的字段、量纲与是否发生换算，供血缘追溯。
    """
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else None
    info: Dict[str, Any] = {
        "raw_confidence": None,
        "raw_quality_score": None,
        "score_source": None,
        "score_scale": None,
        "score_conversion_applied": False,
        "score_conversion_version": cfg.score_conversion_version,
    }

    conf_field, conf_raw = _lookup(record, cfg.confidence_fields, cfg, metadata)
    declared_field, declared_raw = _lookup(record, cfg.declared_score_fields, cfg, metadata)

    conf_score: Optional[float] = None
    declared_score: Optional[float] = None

    if conf_field is not None:
        info["raw_confidence"] = conf_raw
        value, error = _as_number(conf_raw, conf_field)
        if error:
            return None, [error], info
        conf_score, scale, errors = _apply_scale(value, cfg)
        if errors:
            return None, errors, info
        info["score_scale"] = scale
        info["score_conversion_applied"] = scale == _SCALE_RATIO

    if declared_field is not None:
        info["raw_quality_score"] = declared_raw
        value, error = _as_number(declared_raw, declared_field)
        if error:
            return None, [error], info
        if not 0.0 <= value <= 100.0:
            return None, ["score_out_of_range"], info
        declared_score = value

    if conf_score is None and declared_score is None:
        return None, ["score_missing"], info

    if conf_score is not None and declared_score is not None:
        if abs(conf_score - declared_score) > cfg.score_consistency_tolerance:
            return None, ["score_conflict"], info
        info["score_source"] = "both"
        return declared_score, [], info

    if declared_score is not None:
        info["score_source"] = "quality_score"
        info["score_scale"] = _SCALE_PERCENT
        info["score_conversion_applied"] = False
        return declared_score, [], info

    info["score_source"] = conf_field
    return conf_score, [], info
