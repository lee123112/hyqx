"""Engine registry and measurement merge helpers."""
from __future__ import annotations

from typing import Any

from ..core.envelope import ApiError
from .base import BaseEngine, EngineResult


def get_engine(type_: str) -> BaseEngine | None:
    try:
        if type_ == "text":
            from .text import TextEngine
            return TextEngine()
        if type_ == "image":
            from .image import ImageEngine
            return ImageEngine()
        if type_ == "audio":
            from .audio import AudioEngine
            return AudioEngine()
        if type_ == "video":
            from .video import VideoEngine
            return VideoEngine()
        if type_ == "benchmark":
            from .benchmark import BenchmarkEngine
            return BenchmarkEngine()
    except ImportError:
        return None
    return None


def measure_samples(type_: str, samples: list[dict[str, Any]],
                    config: dict[str, Any], *, seed: int = 42,
                    strict: bool = True) -> EngineResult:
    engine = get_engine(type_)
    if engine is None:
        raise ApiError(422, "ENGINE_UNAVAILABLE", "当前类型没有可用的测量引擎")
    return engine.measure(samples, config, seed=seed, strict=strict)


def audit_block(result: EngineResult) -> dict[str, Any]:
    """Serialize the protocol/audit fields without losing engine metadata.

    ``EngineResult.meta`` contains the fields needed to reproduce a training or
    measurement run (engine version, seed, corpus sizes, ASR backend, etc.).  A
    task result must retain that block; otherwise reports can only show the
    final score and cannot prove which protocol produced it.
    """
    return {
        "provenance": dict(result.provenance or {}),
        "meta": dict(result.meta or {}),
        "missing": list(result.missing or []),
        "notes": list(result.notes or []),
    }


def apply_measurements(samples: list[dict[str, Any]], result: EngineResult
                       ) -> list[dict[str, Any]]:
    merged = []
    for sample in samples:
        item = dict(sample)
        values = result.measurements.get(str(sample.get("id")))
        if values:
            item["measurements"] = values
        merged.append(item)
    return merged


def missing_metrics(type_: str, result: EngineResult) -> list[dict[str, Any]]:
    if result.missing:
        return result.missing
    from ..eval import templates
    needed = [metric["key"] for metric in templates.TEMPLATES[type_]["metrics"]]
    missing: list[dict[str, Any]] = []
    for sample in result.measurements.values():
        for model in ("baseline", "candidate"):
            for key in needed:
                if sample.get(model, {}).get(key) is None:
                    missing.append({"sampleId": None, "model": model, "metric": key})
                    break
            if missing:
                break
        if missing:
            break
    return missing


def validate_seed(value: Any = 42) -> int:
    """Keep zero and reject coercions that change the requested protocol."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(422, "CONFIG_INVALID", "seed 必须是整数")
    return value


def imported_audit(type_: str, samples: list[dict[str, Any]], *, seed: int = 42) -> dict[str, Any]:
    from ..eval import templates
    return {
        "provenance": {m["key"]: "imported" for m in templates.TEMPLATES[type_]["metrics"]},
        "meta": {"engine": "import", "engineVersion": "import-audit-v1",
                 "metricNature": "imported", "sampleCount": len(samples),
                 "seed": validate_seed(seed), "upstreamProtocolVerified": False},
        "missing": [], "notes": ["导入指标；上游训练及评估协议未核验。"],
    }
