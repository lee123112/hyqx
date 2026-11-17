"""Offline video VQA protocol and MOS-correlation helper.

The online video engine still exposes the front-end five metrics.  This module
adds an auditable offline workflow around it:

* discover a directory or a manifest of videos;
* measure them with the same ``VideoEngine`` and ``rules.evaluate`` used by the
  API;
* optionally match a per-video MOS file and report Spearman/Pearson/Kendall
  correlation.

No MOS labels means **no SRCC/PLCC**.  The tool never invents a label and never
turns a heuristic VQA score into a paper-reported ``S_ref``.
"""
from __future__ import annotations

import csv
import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..core.envelope import ApiError
from ..core.ids import now
from ..eval import rules, templates
from .runner import run_experiment

VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v"}
_SCORE_KEYS = ("score", "mos", "quality", "label", "target")
_NAME_KEYS = ("video", "filename", "file", "name", "id")


def _label_key(value: Any) -> str:
    return Path(str(value or "")).name.casefold()


def load_mos_labels(path: str | Path) -> dict[str, float]:
    """Read ``{video: MOS}`` from CSV or JSON, preserving basename keys."""
    source = Path(path)
    if not source.is_file():
        raise ApiError(422, "LABELS_MISSING", f"MOS 标签文件不存在：{source.name}")
    rows: list[dict[str, Any]]
    if source.suffix.lower() == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows = [{"video": key, "score": value} for key, value in payload.items()]
        elif isinstance(payload, list):
            rows = [row for row in payload if isinstance(row, dict)]
        else:
            raise ApiError(422, "LABELS_INVALID", "MOS JSON 必须是对象或对象数组")
    else:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]

    labels: dict[str, float] = {}
    for row in rows:
        named = {str(key).strip().casefold(): value for key, value in row.items()}
        video = next((named[key] for key in _NAME_KEYS if named.get(key) not in (None, "")), None)
        score = next((named[key] for key in _SCORE_KEYS if named.get(key) not in (None, "")), None)
        if video is None or score is None:
            continue
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric):
            continue
        key = _label_key(video)
        if key in labels and labels[key] != numeric:
            raise ApiError(422, "LABELS_INVALID", f"同一视频出现冲突 MOS：{key}")
        labels[key] = numeric
    if not labels:
        raise ApiError(422, "LABELS_INVALID", "MOS 文件没有可用的 video/score 行")
    return labels


def discover_videos(root: str | Path, *, recursive: bool = False) -> list[Path]:
    """Find supported video files under a directory (or accept one file)."""
    source = Path(root)
    if source.is_file():
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            raise ApiError(422, "SAMPLE_INVALID", f"不支持的视频后缀：{source.suffix}")
        return [source]
    if not source.is_dir():
        raise ApiError(422, "SAMPLE_INVALID", f"视频目录不存在：{source.name}")
    iterator = source.rglob("*") if recursive else source.glob("*")
    files = sorted((path for path in iterator
                    if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES),
                   key=lambda path: path.as_posix().casefold())
    if not files:
        raise ApiError(422, "SAMPLE_INVALID", f"目录中没有可测视频：{source.name}")
    return files


def build_samples(paths: list[Path], *, prefix: str = "VID") -> list[dict[str, Any]]:
    """Build measured samples with stable IDs and a local source annotation."""
    samples: list[dict[str, Any]] = []
    for index, path in enumerate(paths, start=1):
        samples.append({
            "id": f"{prefix}-{index:04d}",
            "input": str(path.resolve()),
            # Do not use the filename as a caption: a filename containing
            # "red"/"walk" must not create semantic-alignment evidence.
            "title": "",
            "scene": "video-vqa",
            "sourceStep": "视频VQA",
            "parameters": {"source": "local"},
        })
    return samples


def _finite_round(value: Any, digits: int) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return round(numeric, digits) if math.isfinite(numeric) else None


def _correlation_coefficients(predictions: list[float],
                              labels: list[float]) -> dict[str, Any]:
    try:
        from scipy import stats  # type: ignore
    except ImportError:
        return {"status": "scipy_missing", "n": len(predictions),
                "reason": "缺少 scipy，不能计算 SRCC/PLCC"}
    if len(predictions) < 3:
        return {"status": "insufficient_labels", "n": len(predictions),
                "reason": "至少需要 3 对视频/MOS 才能计算相关"}
    if len(set(predictions)) < 2 or len(set(labels)) < 2:
        return {"status": "zero_variance", "n": len(predictions),
                "reason": "预测分数或 MOS 没有方差，相关系数无定义"}
    spearman = stats.spearmanr(predictions, labels)
    pearson = stats.pearsonr(predictions, labels)
    kendall = stats.kendalltau(predictions, labels)
    return {
        "status": "ok",
        "n": len(predictions),
        "spearman": _finite_round(spearman.statistic, 4),
        "spearmanP": _finite_round(spearman.pvalue, 6),
        "pearson": _finite_round(pearson.statistic, 4),
        "pearsonP": _finite_round(pearson.pvalue, 6),
        "kendall": _finite_round(kendall.statistic, 4),
        "kendallP": _finite_round(kendall.pvalue, 6),
    }


def _preview(values: set[str], limit: int = 20) -> list[str]:
    return sorted(values)[:limit]


def correlate(predictions: dict[str, float],
              labels: dict[str, float] | None) -> dict[str, Any]:
    """Compare VQA predictions with MOS labels on shared basenames only."""
    normalized = {_label_key(key): float(value) for key, value in predictions.items()
                  if isinstance(value, (int, float))
                  and math.isfinite(float(value))}
    if labels is None:
        return {
            "status": "labels_missing",
            "n": 0,
            "reason": "没有逐视频 MOS 标签，不能计算 SRCC/PLCC",
            "missingPredictions": [],
            "unlabeledVideos": _preview(set(normalized)),
            "unlabeledVideoCount": len(normalized),
        }
    label_map = {_label_key(key): float(value) for key, value in labels.items()}
    common = sorted(set(normalized) & set(label_map))
    if not common:
        missing = set(label_map) - set(normalized)
        return {
            "status": "no_matching_labels",
            "n": 0,
            "reason": "MOS 标签与视频文件名没有交集",
            "missingPredictions": _preview(missing),
            "missingPredictionCount": len(missing),
            "unlabeledVideos": _preview(set(normalized) - set(label_map)),
            "unlabeledVideoCount": len(set(normalized) - set(label_map)),
        }
    result = _correlation_coefficients(
        [normalized[key] for key in common], [label_map[key] for key in common])
    missing = set(label_map) - set(normalized)
    unlabeled = set(normalized) - set(label_map)
    result.update({
        "matched": common[:20],
        "matchedCount": len(common),
        "missingPredictions": _preview(missing),
        "missingPredictionCount": len(missing),
        "unlabeledVideos": _preview(unlabeled),
        "unlabeledVideoCount": len(unlabeled),
    })
    return result


def run_video_vqa(paths: list[Path], *, labels: dict[str, float] | None = None,
                  config: dict[str, Any] | None = None, seed: int = 42,
                  frames: int = 12, prefix: str = "VID",
                  input_label: str = "videos") -> dict[str, Any]:
    """Measure videos through the shared protocol and optionally correlate MOS."""
    if not paths:
        raise ApiError(422, "SAMPLE_INVALID", "至少需要一条视频")
    effective = deepcopy(config) if isinstance(config, dict) else templates.default_config("video")
    effective["frameSamples"] = max(4, min(24, int(frames)))
    samples = build_samples(paths, prefix=prefix)
    experiment = run_experiment(
        "video", samples, effective, seed=seed, strict=True,
        data_manifest={"kind": "video-vqa", "inputLabel": input_label,
                       "videoCount": len(paths)})
    engine_meta = experiment["engine"]["meta"]
    video_meta = engine_meta.get("videos", {})
    rows: list[dict[str, Any]] = []
    predictions: dict[str, float] = {}
    for sample in experiment["result"]["samples"]:
        sample_id = str(sample["id"])
        quality = (video_meta.get(sample_id) or {}).get("quality") or {}
        video_name = Path(str(sample.get("input") or "")).name
        if video_name and quality.get("vqaScore") is not None:
            predictions[video_name] = float(quality["vqaScore"])
        rows.append({
            "id": sample_id,
            "video": video_name,
            "score": sample.get("score"),
            "grade": sample.get("grade"),
            "checks": sample.get("checks", []),
            "measurements": sample.get("measurements", {}),
            "quality": quality,
            "assessmentTarget": "input_video",
            "evidence": (video_meta.get(sample_id) or {}).get("evidence", {}),
        })
    label_result = correlate(predictions, labels)
    label_result["assessmentTarget"] = "input_video"
    metrics = experiment["result"]["summary"]["metrics"]
    grades = experiment["result"]["summary"]["grades"]
    total_rows = max(1, len(rows))
    grade_shares = {
        grade: round(float(count) / total_rows, 4)
        for grade, count in grades.items()
    }
    vqa_values = [row["quality"]["vqaScore"] for row in rows if row.get("quality")]
    summary = {
        "sampleCount": len(rows),
        "accepted": experiment["result"]["summary"]["accepted"],
        "grades": grades,
        "gradeShares": grade_shares,
        "metrics": metrics,
        "vqa": {
            "method": "heuristic-vqa-v1",
            "mean": round(float(sum(vqa_values) / len(vqa_values)), 4) if vqa_values else None,
            "min": round(float(min(vqa_values)), 4) if vqa_values else None,
            "max": round(float(max(vqa_values)), 4) if vqa_values else None,
        },
        "labels": label_result,
    }
    return {
        "protocol": {
            "schemaVersion": "1.0",
            "assessmentTarget": "input_video",
            "comparisonMode": "same_input_diagnostic",
            "trainingGainEvidence": False,
            "createdAt": now(),
            "type": "video",
            "engineVersion": engine_meta.get("engineVersion"),
            "sampleFrames": effective["frameSamples"],
            "seed": seed,
            "sampleCount": len(rows),
            "inputLabel": input_label,
            "labelsProvided": labels is not None,
            "labelCount": len(labels or {}),
            "provenance": experiment["engine"].get("provenance", {}),
            "notes": experiment["engine"].get("notes", []),
        },
        "rows": rows,
        "summary": summary,
    }


def write_outputs(result: dict[str, Any], output: str | Path) -> Path:
    """Write protocol/measurements/summary/report without host absolute paths."""
    target = Path(output)
    target.mkdir(parents=True, exist_ok=True)
    protocol = result["protocol"]
    summary = result["summary"]
    rows = result["rows"]
    (target / "protocol.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (target / "measurements.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (target / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# 视频 VQA 测试结果",
        "",
        f"- 引擎：`{protocol.get('engineVersion')}`",
        f"- 视频数：{summary['sampleCount']}；每视频采样：{protocol.get('sampleFrames')} 帧",
        f"- 等级：{json.dumps(summary['grades'], ensure_ascii=False)}；"
        f"占比：{json.dumps(summary.get('gradeShares', {}), ensure_ascii=False)}",
        f"- 是否接受：{summary['accepted']}",
        "",
        "## 五维均值",
        "",
        "| 指标 | 基线 | 候选 | 阈值 | 通过 |",
        "|---|---:|---:|---:|:---:|",
    ]
    for metric in summary["metrics"]:
        lines.append(
            f"| {metric['name']} | {metric['baseline']} | {metric['candidate']} | "
            f"{metric['threshold']} | {'是' if metric['passed'] else '否'} |")
    labels = summary["labels"]
    lines += ["", "## MOS 相关性", ""]
    if labels.get("status") == "ok":
        lines.append(
            f"- n={labels['n']}；Spearman={labels['spearman']}（p={labels['spearmanP']}）；"
            f"Pearson={labels['pearson']}（p={labels['pearsonP']}）；"
            f"Kendall={labels['kendall']}（p={labels['kendallP']}）。")
    else:
        lines.append(f"- 未计算：`{labels.get('status')}`，{labels.get('reason', '')}")
    lines += [
        "",
        "## 边界",
        "",
        "- 五维、quality、VQA 与 MOS 匹配均评价输入原视频；baseline/candidate 同输入诊断不表示训练收益；",
        "- 安全仅为描述文本关键词规则，未使用画面安全模型；无 caption 不能声称安全证据充分；",
        "- `vqaScore` 是启发式技术质量分，不是训练好的 MOS 模型；",
        "- 没有逐视频 MOS 标签时不得写 SRCC/PLCC；",
        "- 五维 clear/alignment 等仍按各自 provenance 标注，不能冒充人工质量真值。",
        "",
    ]
    (target / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return target
