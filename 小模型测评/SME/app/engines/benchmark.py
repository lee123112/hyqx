"""Benchmark measurement engine.

A post-training benchmark corpus never trains a model here: the baseline and
the optimized model must both be evaluated on every question upstream and the
per-sample scores imported.  The engine only validates and returns those
scores; without them it refuses to invent numbers.
"""
from __future__ import annotations

import math
from typing import Any

from ..core.envelope import ApiError
from .base import BaseEngine, EngineResult
from .benchmark_analysis import analyze

ENGINE_VERSION = "benchmark-import-v2"
BENCHMARK_METRICS: tuple[str, ...] = ("score",)
MODEL_KEYS: tuple[str, ...] = ("baseline", "candidate")
MAX_SCORE = 100.0


def _is_score(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    # Check range before conversion: untrusted large integers may overflow.
    return 0 <= value <= MAX_SCORE and math.isfinite(value)


def _clean_measurements(value: Any) -> dict[str, dict[str, float]] | None:
    if not isinstance(value, dict):
        return None
    cleaned: dict[str, dict[str, float]] = {}
    for model_key in MODEL_KEYS:
        values = value.get(model_key)
        if not isinstance(values, dict):
            return None
        score = values.get("score")
        if not _is_score(score):
            return None
        cleaned[model_key] = {"score": float(score)}
    return cleaned


class BenchmarkEngine(BaseEngine):
    type = "benchmark"

    def measure(self, samples: list[dict[str, Any]], config: dict[str, Any], *,
                seed: int = 42, strict: bool = True) -> EngineResult:
        config = config if isinstance(config, dict) else {}
        thresholds = config.get("thresholds", {})
        if not isinstance(thresholds, dict):
            raise ApiError(422, "CONFIG_INVALID", "thresholds 必须是对象")
        threshold = thresholds.get("score", 5)
        if not _is_score(threshold):
            raise ApiError(422, "CONFIG_INVALID", "分差阈值须为 0–100 的数值")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ApiError(422, "CONFIG_INVALID", "seed 必须是整数")
        rows, imported, missing = self.validate_import(samples, strict=strict)
        if missing:
            message = "后训练评测语料需要导入基础/优化模型逐题得分"
            return EngineResult(
                measurements=imported,
                provenance={},
                meta={
                    "engineVersion": ENGINE_VERSION,
                    "mode": "missing",
                    "seed": seed,
                    "sampleCount": len(rows),
                    "measuredCount": len(imported),
                },
                missing=missing,
                notes=[message],
            )

        return EngineResult(
            measurements=imported,
            provenance={metric: "imported" for metric in BENCHMARK_METRICS},
            meta={
                "engineVersion": ENGINE_VERSION,
                "mode": "imported",
                "seed": seed,
                "sampleCount": len(rows),
                "metricNature": "imported_paired_model_scores; not training-data quality",
                "measuredCount": len(imported),
                **analyze([{**row, "measurements": imported[row["id"]]} for row in rows],
                          float(threshold), seed),
            },
            notes=["benchmark 不训练模型，逐题得分按导入数据处理。",
                   "分差表示两个模型的题目区分度，不代表训练数据质量差。",
                   "未核验上游模型、评分协议和训练集泄漏；bootstrap 不替代多种子训练。"],
        )

    @classmethod
    def validate_import(cls, samples: Any, *, strict: bool = True) -> tuple[
            list[dict[str, Any]], dict[str, dict[str, dict[str, float]]], list[dict[str, Any]]]:
        """Validate without running analysis (also used before task creation)."""
        rows = cls._validate_samples(samples)
        imported: dict[str, dict[str, dict[str, float]]] = {}
        missing: list[dict[str, Any]] = []
        for row in rows:
            cleaned = _clean_measurements(row.get("measurements"))
            if cleaned is not None:
                imported[row["id"]] = cleaned
                continue
            values = row.get("measurements")
            for model_key in MODEL_KEYS:
                side = values.get(model_key) if isinstance(values, dict) else None
                if not isinstance(side, dict) or not _is_score(side.get("score")):
                    missing.append({"sampleId": row["id"], "model": model_key, "metric": "score"})
        if missing and strict:
            raise ApiError(422, "MEASUREMENTS_REQUIRED",
                           "后训练评测语料需要导入基础/优化模型逐题得分", details=missing)
        return rows, imported, missing

    @staticmethod
    def _validate_samples(samples: Any) -> list[dict[str, Any]]:
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "SAMPLE_INVALID", "后训练评测至少需要一条样本")
        if len(samples) > 5000:
            raise ApiError(422, "SAMPLE_INVALID", "单次评分最多支持 5000 条样本")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                raise ApiError(422, "SAMPLE_INVALID", f"第 {index + 1} 条样本格式不正确")
            sample_id = sample.get("id")
            if sample_id is None or str(sample_id).strip() == "":
                raise ApiError(422, "SAMPLE_INVALID", f"第 {index + 1} 条样本缺少 id")
            sample_id = str(sample_id)
            if sample_id in seen:
                raise ApiError(422, "SAMPLE_INVALID", f"样本 ID 重复：{sample_id}")
            seen.add(sample_id)
            text = (sample.get("input") or sample.get("source") or sample.get("question")
                    or sample.get("instruction") or sample.get("uri") or "")
            rows.append({
                "id": sample_id,
                "input": str(text),
                "output": str(sample.get("output") or ""),
                "scene": str(sample.get("scene") or sample.get("title") or "通用场景"),
                "measurements": sample.get("measurements"),
            })
        return rows
