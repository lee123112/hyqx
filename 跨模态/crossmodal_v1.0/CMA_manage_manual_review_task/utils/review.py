"""人工复核结论解析（**只来自输入证据**，绝不自行生成结论）。

表1 #25 处理边界为「手动触发派单并回写；通过回候选池，不通过进样本隔离库」。
人工复核平台（或资深标注员工作台）把结论随记录回传后，本模块只做归一化与校验：

- 优先读取 ``processing["25"].review``（重跑幂等），其次 ``operator_25.review``、
  ``review``、``human_review``、``manual_review``、``metadata.review``，
  最后兼容扁平字段 ``human_verdict``/``reviewer``；
- 结论取值必须是 ``pass`` / ``fail`` / ``uncertain``（允许常见别名与中文）；
- 默认要求带复核人身份，缺失即判为「待人工」，**不得**当作人工确认；
- 任一维度判错则整体判错（``overridden_by_dimension=true`` 留痕）。

人工平台未接入时输入不含结论 → 队列中的记录全部保持「待人工」，
报告 ``status=waiting_manual``（表1「低置信但尚未判错：留人工队列」）。
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNCERTAIN = "uncertain"

#: 复核维度（与 #24 抽检口径保持一致，便于跨算子对齐证据）
DIMENSIONS = ("semantics", "temporal", "scene")
_DIMENSION_LABELS = {"semantics": "语义", "temporal": "时序", "scene": "场景"}

_ALIASES = {
    "pass": VERDICT_PASS, "passed": VERDICT_PASS, "ok": VERDICT_PASS,
    "aligned": VERDICT_PASS, "consistent": VERDICT_PASS, "correct": VERDICT_PASS,
    "通过": VERDICT_PASS, "对齐": VERDICT_PASS, "一致": VERDICT_PASS,
    "fail": VERDICT_FAIL, "failed": VERDICT_FAIL, "reject": VERDICT_FAIL,
    "rejected": VERDICT_FAIL, "mismatch": VERDICT_FAIL, "wrong": VERDICT_FAIL,
    "not_aligned": VERDICT_FAIL, "unaligned": VERDICT_FAIL,
    "不对齐": VERDICT_FAIL, "判错": VERDICT_FAIL, "不一致": VERDICT_FAIL,
    "uncertain": VERDICT_UNCERTAIN, "unsure": VERDICT_UNCERTAIN,
    "doubt": VERDICT_UNCERTAIN, "ambiguous": VERDICT_UNCERTAIN,
    "存疑": VERDICT_UNCERTAIN, "待定": VERDICT_UNCERTAIN,
}


def _empty(source: Optional[str] = None, reason: Optional[str] = None,
           present: bool = False) -> Dict[str, Any]:
    return {
        "present": present,
        "valid": False,
        "verdict": None,
        "dimensions": {},
        "reviewer": None,
        "reviewed_at": None,
        "notes": None,
        "round": None,
        "reason": reason,
        "source": source,
        "overridden_by_dimension": False,
    }


def normalise_verdict(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    return _ALIASES.get(raw.strip().lower())


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _candidate_sources(record: Mapping[str, Any]):
    """按优先级给出 ``(来源名, 原始对象)``。"""
    processing = record.get("processing")
    if isinstance(processing, dict):
        for namespace in ("25", 25):
            node = processing.get(namespace)
            if isinstance(node, dict) and isinstance(node.get("review"), dict):
                yield "processing_25", node["review"]
    operator_ns = record.get("operator_25")
    if isinstance(operator_ns, dict) and isinstance(operator_ns.get("review"), dict):
        yield "operator_25", operator_ns["review"]
    for key in ("manual_review", "human_review", "review"):
        value = record.get(key)
        if isinstance(value, dict):
            yield key, value
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in ("manual_review", "human_review", "review"):
            value = metadata.get(key)
            if isinstance(value, dict):
                yield "metadata", value
    if normalise_verdict(record.get("human_verdict")) is not None:
        yield "flat", {
            "verdict": record.get("human_verdict"),
            "reviewer": record.get("reviewer"),
            "reviewed_at": record.get("reviewed_at"),
            "dimensions": record.get("review_dimensions"),
            "notes": record.get("review_notes"),
        }


def resolve_review(record: Mapping[str, Any], *,
                   require_reviewer: bool = True) -> Dict[str, Any]:
    """归一化并校验人工复核结论。"""
    if not isinstance(record, Mapping):
        return _empty(reason="no_human_conclusion")

    raw: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    for name, candidate in _candidate_sources(record):
        raw, source = candidate, name
        break
    if raw is None:
        return _empty(reason="no_human_conclusion")

    dimensions: Dict[str, str] = {}
    raw_dimensions = raw.get("dimensions")
    if isinstance(raw_dimensions, dict):
        for key, value in raw_dimensions.items():
            if key not in DIMENSIONS:
                result = _empty(source=source, reason="invalid_dimension", present=True)
                result["reviewer"] = _text(raw.get("reviewer"))
                return result
            normalised = normalise_verdict(value)
            if normalised is None:
                result = _empty(source=source, reason="invalid_dimension", present=True)
                result["reviewer"] = _text(raw.get("reviewer"))
                return result
            dimensions[key] = normalised

    verdict = normalise_verdict(raw.get("verdict"))
    if verdict is None and not dimensions:
        result = _empty(source=source, reason="invalid_verdict", present=True)
        result["reviewer"] = _text(raw.get("reviewer"))
        return result

    overridden = False
    fails = [key for key, value in dimensions.items() if value == VERDICT_FAIL]
    if fails:
        overridden = verdict not in (None, VERDICT_FAIL)
        verdict = VERDICT_FAIL
    elif verdict is None:
        uncertain = [key for key, value in dimensions.items()
                     if value == VERDICT_UNCERTAIN]
        verdict = VERDICT_UNCERTAIN if uncertain else VERDICT_PASS

    reviewer = _text(raw.get("reviewer"))
    result = {
        "present": True,
        "valid": True,
        "verdict": verdict,
        "dimensions": dimensions,
        "reviewer": reviewer,
        "reviewed_at": _text(raw.get("reviewed_at")),
        "notes": _text(raw.get("notes")),
        "round": _as_round(raw.get("round")),
        "reason": None,
        "source": source,
        "overridden_by_dimension": overridden,
    }
    if require_reviewer and not reviewer:
        result["valid"] = False
        result["reason"] = "missing_reviewer"
    return result


def _as_round(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def review_summary(resolved: Mapping[str, Any]) -> Dict[str, Any]:
    """写入记录/台账的安全摘要（不含备注原文，避免把大段文本写进台账）。"""
    return {
        "verdict": resolved.get("verdict"),
        "reviewer": resolved.get("reviewer"),
        "reviewed_at": resolved.get("reviewed_at"),
        "round": resolved.get("round"),
        "dimensions": dict(resolved.get("dimensions") or {}),
        "source": resolved.get("source"),
        "valid": bool(resolved.get("valid")),
        "reason": resolved.get("reason"),
    }


def failed_dimensions(resolved: Mapping[str, Any]) -> Tuple[str, ...]:
    return tuple(key for key, value in (resolved.get("dimensions") or {}).items()
                 if value == VERDICT_FAIL)


def dimension_label(name: str) -> str:
    return _DIMENSION_LABELS.get(name, name)
