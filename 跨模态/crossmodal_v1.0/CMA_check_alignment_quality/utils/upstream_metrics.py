"""上游质量指标与反向验证结论的复用，以及无上游时的 10% 子集补测。

表1 #20 研发边界：
> 困惑度 / BRISQUE / SNR 优先复用四条预训练产线，无上游才在 10% 子集补测。

本模块负责三件事：
1. **复用**：按固定优先级在样本中定位上游已经算好的指标
   （``qc.text_fluency.ppl``、``image.brisque``、``qc.media_quality.snr_db`` …）；
2. **抽样**：``quality_subset_fallback`` 子集用 ``blake2b(sample_id)`` 稳定抽样，
   同一份输入永远得到同一子集（可复现），与输入顺序无关；
3. **补测**：只对「缺上游 + 落在子集内」的样本尝试本地测量。
   本地后端缺失时明确返回 ``insufficient_evidence`` 与原因，
   **不产出固定值、随机值或把缺失当成 0**。
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from . import media_probe

METRIC_KINDS = ("ppl", "brisque", "snr")
METRIC_MODES = ("upstream_first", "local_required", "upstream_only", "disable")

#: 上游指标在样本中的候选路径（按优先级）
UPSTREAM_PATHS: Dict[str, Sequence[Sequence[str]]] = {
    "ppl": (
        ("qc", "text_fluency", "ppl"),
        ("qc", "text", "ppl"),
        ("qc", "ppl"),
        ("processing", "03", "metrics", "ppl"),
        ("processing", "3", "metrics", "ppl"),
        ("metrics", "ppl"),
    ),
    "brisque": (
        ("qc", "media_quality", "brisque"),
        ("qc", "media", "brisque"),
        ("image", "brisque"),
        ("image", "quality", "brisque"),
        ("processing", "04", "metrics", "brisque"),
        ("processing", "4", "metrics", "brisque"),
        ("metrics", "brisque"),
    ),
    "snr": (
        ("qc", "media_quality", "snr_db"),
        ("qc", "media_quality", "snr"),
        ("qc", "audio", "snr_db"),
        ("audio", "snr_db"),
        ("audio", "snr"),
        ("processing", "06", "metrics", "snr_db"),
        ("processing", "6", "metrics", "snr_db"),
        ("processing", "06", "metrics", "snr"),
        ("metrics", "snr_db"),
    ),
}

#: 上游反向验证结论的候选路径（表1 #12/#14 的产出）
UPSTREAM_REVERSE_PATHS: Sequence[Sequence[str]] = (
    ("qc", "reverse_verify"),
    ("qc", "alignment_qc", "reverse_verify"),
    ("qc", "cross_modal_reverse"),
    ("processing", "19", "reverse_verify"),
    ("reverse_verify",),
)

#: 子集抽样基数（万分之一分辨率）
_SAMPLE_SCALE = 10000


def _lookup(record: Any, path: Sequence[str]) -> Any:
    current = record
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _is_real_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and float(value) >= 0.0)


def resolve_upstream_metric(record: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    """在样本中查找上游已算好的指标值；找不到或值非法时返回 ``None``。"""
    if kind not in UPSTREAM_PATHS:
        raise ValueError("未知指标类型：%s" % kind)
    for path in UPSTREAM_PATHS[kind]:
        value = _lookup(record, path)
        if _is_real_number(value):
            return {"kind": kind, "value": float(value), "source": "upstream",
                    "path": ".".join(path)}
    return None


def resolve_upstream_reverse(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """复用上游反向验证结论。

    返回 ``{passed, score, check_status, model, path}``；结论对象既没有
    ``passed`` 也没有 ``hit_at_1`` / ``rank`` 时视为「无上游结论」。
    """
    for path in UPSTREAM_REVERSE_PATHS:
        value = _lookup(record, path)
        if not isinstance(value, dict):
            continue
        passed = value.get("passed")
        if not isinstance(passed, bool):
            hit = value.get("hit_at_1")
            if isinstance(hit, bool):
                passed = hit
            else:
                rank = value.get("rank")
                if isinstance(rank, int) and not isinstance(rank, bool):
                    passed = rank == 1
        if passed is None:
            continue
        score = value.get("score")
        if not _is_real_number(score):
            score = value.get("similarity")
        result = {
            "passed": bool(passed),
            "score": float(score) if _is_real_number(score) else None,
            "check_status": str(value.get("check_status") or "checked"),
            "model": value.get("model"),
            "path": ".".join(path),
            "source": "upstream",
        }
        return result
    return None


def _sample_score(key: str) -> int:
    digest = hashlib.blake2b(str(key).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % _SAMPLE_SCALE


def is_fallback_selected(key: str, ratio: float) -> bool:
    """稳定抽样：``blake2b(key)`` 落在 ``ratio`` 比例内才进入补测子集。"""
    if ratio is None:
        return False
    value = float(ratio)
    if value <= 0.0:
        return False
    if value >= 1.0:
        return True
    return _sample_score(key) < int(round(value * _SAMPLE_SCALE))


def select_fallback_keys(keys: Iterable[str], ratio: float) -> Set[str]:
    """对给定标识集合做确定性抽样，返回补测子集。"""
    return {str(key) for key in keys if is_fallback_selected(str(key), ratio)}


def local_backend_status() -> Dict[str, Any]:
    """本地补测后端可用性（用于报告与降级判定）。"""
    backends = media_probe.available_backends()
    return {
        "ppl": {"available": False, "reason": "ppl_backend_missing",
                "note": "本算子不内置语言模型，困惑度只能复用上游产出"},
        "brisque": {"available": bool(backends["brisque_backend"]),
                    "reason": None if backends["brisque_backend"]
                    else "brisque_backend_missing",
                    "note": "需要 opencv-contrib 的 cv2.quality 与 BRISQUE 模型文件"},
        "snr": {"available": backends["audio_decoder"] is not None,
                "reason": None if backends["audio_decoder"] is not None
                else "audio_decoder_missing",
                "note": "真实解码后用分帧能量法计算"},
    }


def local_measure(kind: str, *, audio_path=None, pcm=None, framerate: Optional[int] = None,
                  text: Optional[str] = None, image_path=None) -> Dict[str, Any]:
    """本地补测单个指标。

    返回统一结构 ``{kind, status, value, backend, reason}``；
    ``status`` ∈ ``ok`` / ``insufficient_evidence``。**永不编造数值**。
    """
    if kind == "snr":
        if pcm is None and audio_path is not None:
            try:
                decoded = media_probe.decode_audio(audio_path)
            except media_probe.AudioDecodeError as exc:
                return {"kind": kind, "status": "insufficient_evidence", "value": None,
                        "backend": None, "reason": "audio_undecodable",
                        "detail": str(exc)[:200]}
            pcm = decoded["samples"]
            framerate = decoded["sample_rate"]
            backend = decoded["decoder"]
        elif pcm is not None:
            backend = "in_memory"
        else:
            return {"kind": kind, "status": "insufficient_evidence", "value": None,
                    "backend": None, "reason": "audio_source_missing"}
        value = media_probe.audio_snr_db(pcm, framerate or 0)
        if value is None:
            return {"kind": kind, "status": "insufficient_evidence", "value": None,
                    "backend": backend, "reason": "audio_silent_or_too_short"}
        return {"kind": kind, "status": "ok", "value": float(value),
                "backend": backend, "reason": None}

    if kind == "brisque":
        if not media_probe.brisque_backend_available():
            return {"kind": kind, "status": "insufficient_evidence", "value": None,
                    "backend": None, "reason": "brisque_backend_missing"}
        if image_path is None:
            return {"kind": kind, "status": "insufficient_evidence", "value": None,
                    "backend": None, "reason": "image_missing"}
        try:  # pragma: no cover - 本机无 opencv-contrib，无法执行
            import cv2  # noqa: PLC0415

            image = cv2.imread(str(image_path))
            if image is None:
                return {"kind": kind, "status": "insufficient_evidence", "value": None,
                        "backend": None, "reason": "image_unreadable"}
            score = cv2.quality.QualityBRISQUE_compute(
                image, str(image_path) + ".brisque_model", str(image_path) + ".brisque_range")
            return {"kind": kind, "status": "ok", "value": float(score[0]),
                    "backend": "opencv_quality", "reason": None}
        except Exception as exc:  # noqa: BLE001
            return {"kind": kind, "status": "insufficient_evidence", "value": None,
                    "backend": None, "reason": "brisque_backend_missing",
                    "detail": type(exc).__name__}

    if kind == "ppl":
        return {"kind": kind, "status": "insufficient_evidence", "value": None,
                "backend": None, "reason": "ppl_backend_missing",
                "detail": "本算子不内置语言模型，仅复用上游困惑度"}

    raise ValueError("未知指标类型：%s" % kind)


def evaluate_metric(kind: str, *, mode: str, upstream: Optional[Dict[str, Any]],
                    applicable: bool, fallback_selected: bool,
                    audio_path=None, text: Optional[str] = None,
                    image_path=None) -> Dict[str, Any]:
    """按配置的 ``mode`` 决定复用、补测或如实标记。

    返回结构可直接作为质量报告与检查明细里的 ``metrics.<kind>`` 条目。
    """
    base: Dict[str, Any] = {"kind": kind, "mode": mode, "value": None,
                            "source": None, "path": None, "backend": None,
                            "reason": None}
    if mode == "disable":
        base.update({"status": "not_applicable", "reason": "metric_disabled"})
        return base
    if not applicable:
        base.update({"status": "not_applicable", "reason": "modality_absent"})
        return base

    if upstream is not None:
        base.update({"status": "ok", "value": upstream["value"],
                     "source": "upstream", "path": upstream.get("path")})
        return base

    if mode == "upstream_only":
        base.update({"status": "insufficient_evidence", "reason": "upstream_missing"})
        return base

    if mode == "upstream_first" and not fallback_selected:
        base.update({"status": "insufficient_evidence",
                     "reason": "outside_fallback_subset"})
        return base

    measured = local_measure(kind, audio_path=audio_path, text=text,
                             image_path=image_path)
    if measured["status"] == "ok":
        base.update({"status": "ok", "value": measured["value"], "source": "local",
                     "backend": measured.get("backend")})
        return base
    base.update({"status": "insufficient_evidence",
                 "reason": measured.get("reason") or "%s_backend_missing" % kind,
                 "backend": measured.get("backend")})
    return base


#: 指标汇总的互斥分档
METRIC_BUCKETS = ("upstream", "local", "not_applicable", "insufficient_evidence")


def metric_bucket(item: Dict[str, Any]) -> str:
    """单条指标结果唯一归属：复用上游 / 本地补测 / 不适用 / 证据不足。"""
    status = item.get("status")
    if status == "ok":
        return "local" if item.get("source") == "local" else "upstream"
    if status == "not_applicable":
        return "not_applicable"
    return "insufficient_evidence"


def summarize_metrics(records: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """汇总各指标分档计数（互斥，合计等于被统计的记录数）。"""
    summary: Dict[str, Dict[str, int]] = {
        kind: {bucket: 0 for bucket in METRIC_BUCKETS} for kind in METRIC_KINDS
    }
    for record in records:
        for kind in METRIC_KINDS:
            item = record.get(kind)
            if not isinstance(item, dict):
                continue
            summary[kind][metric_bucket(item)] += 1
    return summary


def fallback_usage(ratio: float, keys: List[str]) -> Dict[str, Any]:
    """抽样规模统计（用于报告，便于审计抽样是否真的按比例执行）。"""
    selected = select_fallback_keys(keys, ratio)
    return {
        "ratio": float(ratio),
        "candidate_records": len(keys),
        "selected_records": len(selected),
        "selection": "blake2b(sample_id)%%%d" % _SAMPLE_SCALE,
    }


def metric_paths(kind: str) -> List[str]:
    return [".".join(path) for path in UPSTREAM_PATHS[kind]]
