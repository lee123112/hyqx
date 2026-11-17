"""Descriptive paired-score diagnostics; never changes the acceptance rule."""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
import unicodedata
from collections import defaultdict
from typing import Any

BOOTSTRAP_RESAMPLES = 1000


def _canonical(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _summary(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    baseline = [row["measurements"]["baseline"]["score"] for row in rows]
    candidate = [row["measurements"]["candidate"]["score"] for row in rows]
    deltas = [right - left for left, right in zip(baseline, candidate)]
    return {
        "count": len(rows),
        "baselineMean": round(statistics.mean(baseline), 6),
        "candidateMean": round(statistics.mean(candidate), 6),
        "deltaMean": round(statistics.mean(deltas), 6),
        "deltaMedian": round(statistics.median(deltas), 6),
        "deltaStd": round(statistics.stdev(deltas), 6) if len(rows) > 1 else None,
        "deltaMin": round(min(deltas), 6), "deltaMax": round(max(deltas), 6),
        "wins": sum(delta > 0 for delta in deltas),
        "ties": sum(delta == 0 for delta in deltas),
        "losses": sum(delta < 0 for delta in deltas),
        "thresholdPassed": sum(delta >= threshold for delta in deltas),
        "baselineFloor": baseline.count(0), "baselineCeiling": baseline.count(100),
        "candidateFloor": candidate.count(0), "candidateCeiling": candidate.count(100),
    }


def _paired_bootstrap(rows: list[dict[str, Any]], seed: int) -> list[float] | None:
    if len(rows) < 2:
        return None
    deltas = [row["measurements"]["candidate"]["score"] -
              row["measurements"]["baseline"]["score"] for row in rows]
    rng = random.Random(seed)
    # Resample question pairs, never the two model columns independently.
    means = sorted(math.fsum(rng.choices(deltas, k=len(rows))) / len(rows)
                   for _ in range(BOOTSTRAP_RESAMPLES))

    def percentile(p: float) -> float:
        index = (len(means) - 1) * p
        low, high = math.floor(index), math.ceil(index)
        return round(means[low] + (means[high] - means[low]) * (index - low), 6)

    return [percentile(.025), percentile(.975)]


def analyze(rows: list[dict[str, Any]], threshold: float, seed: int) -> dict[str, Any]:
    # Sorting makes hashes and Monte Carlo results invariant to file row order.
    rows = sorted(rows, key=lambda row: row["id"])
    overall = _summary(rows, threshold)
    overall["deltaMeanCI95"] = _paired_bootstrap(rows, seed)
    scenes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    questions: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        scenes[row["scene"]].append(row)
        if row["input"].strip():
            questions[_digest([_canonical(row["input"]), _canonical(row["output"])])].append(row["id"])
    duplicates = sorted(ids for ids in questions.values() if len(ids) > 1)
    warnings = ["PROTOCOL_UNVERIFIED", "TRAINING_LEAKAGE_NOT_CHECKED"]
    if len(rows) < 30:
        warnings.append("SMALL_SAMPLE")
    if duplicates:
        warnings.append("DUPLICATE_QUESTIONS")
    if overall["losses"]:
        warnings.append("MODEL_REGRESSIONS")
    if overall["baselineFloor"] or overall["candidateFloor"]:
        warnings.append("FLOOR_SCORES")
    if overall["baselineCeiling"] or overall["candidateCeiling"]:
        warnings.append("CEILING_SCORES")
    return {
        "sampleDigest": _digest(rows),
        "analysis": {
            "threshold": threshold, "scoreRange": [0, 100], "overall": overall,
            "byScene": {scene: _summary(group, threshold) for scene, group in sorted(scenes.items())},
            "duplicateQuestions": duplicates, "warnings": warnings,
            "uncertainty": {"method": "paired_percentile_bootstrap", "confidence": .95,
                            "resamples": BOOTSTRAP_RESAMPLES if len(rows) > 1 else 0,
                            "seed": seed, "unit": "question_pair",
                            "assumption": "independent representative questions; not training-seed uncertainty"},
        },
    }


def report_lines(meta: dict[str, Any]) -> list[str]:
    analysis = meta.get("analysis")
    if not isinstance(analysis, dict):
        return []
    overall = analysis["overall"]
    lines = ["", "## 配对区分度分析", "",
             f"- 题目数：{overall['count']}；平均分差：{overall['deltaMean']} 分；"
             f"样本标准差：{overall['deltaStd']}；中位分差：{overall['deltaMedian']} 分。",
             f"- 优化模型胜/平/负：{overall['wins']}/{overall['ties']}/{overall['losses']}；"
             f"达到分差阈值 {analysis['threshold']} 分：{overall['thresholdPassed']} 题。",
             f"- 平均分差 95% 配对 bootstrap 区间：{overall['deltaMeanCI95']}。",
             "- 区间仅反映题目重采样；依赖题目独立且有代表性的假设，不替代多种子训练结果。",
             "- 分差表示这两个模型的题目区分度，不代表训练数据质量差；导入分数的评分协议与训练集泄漏尚未核验。",
             f"- 审计提示：{', '.join(analysis['warnings'])}。",
             f"- 题目及分数 SHA256：`{meta.get('sampleDigest')}`。",
             "", "| 场景 | 题数 | 基线均分 | 优化均分 | 平均分差 | 退步题数 |",
             "|---|---|---|---|---|---|"]
    for scene, group in analysis["byScene"].items():
        label = scene.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        lines.append(f"| {label} | {group['count']} | {group['baselineMean']} | "
                     f"{group['candidateMean']} | {group['deltaMean']} | {group['losses']} |")
    if analysis["duplicateQuestions"]:
        lines += ["", "- 重复题组（NFKC + 空白规范化后的题目/标准答案一致，仅提示，未自动剔除）：" +
                  json.dumps(analysis["duplicateQuestions"], ensure_ascii=False)]
    return lines
