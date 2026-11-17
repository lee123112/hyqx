"""Evaluate rules ported 1:1 from the platform frontend.

The frontend module `dataset-evaluation.js` is the contract.  Every user-visible
message and every rounding rule here intentionally mirrors that implementation.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from .templates import (
    DEFAULT_WEIGHTS,
    GRADE_USE,
    MODEL_PROFILES,
    TEMPLATES,
    default_config,
    get_template,
)


class RuleError(ValueError):
    def __init__(self, message: str, error_code: str = "SAMPLE_INVALID") -> None:
        super().__init__(message)
        self.error_code = error_code


def is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def round2(value: float) -> float:
    # JS: Math.round(n * 100) / 100
    result = math.floor(value * 100 + 0.5) / 100
    return 0.0 if result == 0 else result


def _js_or(*values: Any) -> Any:
    for value in values:
        if value:
            return value
    return values[-1] if values else None


def validate_config(type_: str, config: dict[str, Any]) -> None:
    template = TEMPLATES.get(type_)
    if not template:
        raise RuleError("请选择有效的评测类型", "CONFIG_INVALID")
    if not isinstance(config, dict):
        raise RuleError("评测配置不正确", "CONFIG_INVALID")
    thresholds = config.get("thresholds")
    if not isinstance(thresholds, dict):
        raise RuleError("评测配置缺少 thresholds", "CONFIG_INVALID")
    for metric in template["metrics"]:
        value = thresholds.get(metric["key"])
        maximum = 1 if metric["key"] in ("iou", "lossCv") else 100
        if not is_num(value) or value < 0 or value > maximum:
            raise RuleError(f"{metric['name']}阈值应为 0–{maximum}", "CONFIG_INVALID")
    if type_ == "video":
        weights = config.get("weights")
        if (not isinstance(weights, list) or len(weights) != 5
                or any(not is_num(v) or v < 0 for v in weights)
                or abs(sum(weights) - 100) > 0.001):
            raise RuleError("五项权重之和必须为 100%", "CONFIG_INVALID")
        grades = config.get("grades")
        if not isinstance(grades, dict):
            raise RuleError("视频等级配置缺失", "CONFIG_INVALID")
        aa, a, b = grades.get("AA"), grades.get("A"), grades.get("B")
        if (not all(is_num(v) and 0 <= v <= 100 for v in (aa, a, b))
                or not (aa > a > b)):
            raise RuleError("等级阈值须满足 100 ≥ AA > A > B ≥ 0", "CONFIG_INVALID")


def normalize_samples(value: Any, type_: str, mode: str) -> list[dict[str, Any]]:
    get_template(type_)
    rows = value if isinstance(value, list) else (value.get("samples") if isinstance(value, dict) else None)
    if not isinstance(rows, list) or not rows:
        raise RuleError("数据集至少包含一条 samples 数据", "SAMPLE_INVALID")
    if len(rows) > 5000:
        raise RuleError("单次演示最多导入 5,000 条数据", "SAMPLE_INVALID")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RuleError(f"第 {index + 1} 条数据格式不正确", "SAMPLE_INVALID")
        sample_id = str(_js_or(row.get("id"), row.get("sample_id"), f"S{index + 1:03d}"))
        if sample_id in seen:
            raise RuleError(f"样本 ID 重复：{sample_id}", "SAMPLE_INVALID")
        seen.add(sample_id)
        parameters = row.get("parameters") or {}
        sample = {
            "id": sample_id,
            "fileName": row.get("fileName") or None,
            "sourceFile": row.get("sourceFile") or None,
            "blobKey": row.get("blobKey") or None,
            "uri": row.get("uri") or None,
            "mimeType": row.get("mimeType") or None,
            "fileSize": row.get("fileSize") if row.get("fileSize") is not None else None,
            "title": str(_js_or(row.get("title"), row.get("scene"), f"样本 {index + 1}")),
            "input": str(_js_or(row.get("input"), row.get("source"), row.get("question"),
                                row.get("instruction"), row.get("uri"),
                                row.get("audio") if type_ == "audio" else None, "")),
            "output": str(row.get("output") or ""),
            "scene": str(_js_or(row.get("scene"), row.get("title"), "通用场景")),
            "sourceStep": str(_js_or(row.get("sourceStep"), row.get("source_step"), "数据处理")),
            "parameters": parameters if isinstance(parameters, dict) else {},
            "highValue": bool(row.get("highValue")),
            "measurements": row.get("measurements") or None,
            "_demoRemediated": mode == "demo" and bool(row.get("_demoRemediated")),
        }
        for key in ("datasetId", "dataset_id", "datasetID", "path", "assetPath", "audio", "file"):
            if key in row:
                sample[key] = row[key]
        if type_ == "video":
            for key in ("caption", "description", "source", "sourceUrl", "origin",
                        "expectedDuration", "expectedFrames", "frameCount", "frames",
                        "duration", "durationSeconds", "source_url", "url"):
                if key in row:
                    sample[key] = row[key]
        if type_ == "audio":
            for key in ("transcript", "reference", "text", "speaker", "speakerId",
                        "speaker_id", "spk", "domain", "domainLabel", "domain_label", "language"):
                if key in row:
                    sample[key] = row[key]
        if not sample["input"].strip():
            raise RuleError(f"{sample_id} 缺少 input、source 或 uri 字段", "SAMPLE_INVALID")
        if mode == "measured":
            for metric in TEMPLATES[type_]["metrics"]:
                maximum = (math.inf if type_ == "audio" and metric["key"] == "cer"
                           else 1 if metric["key"] in ("iou", "lossCv") else 100)
                for model_key in ("baseline", "candidate"):
                    model_values = (sample.get("measurements") or {}).get(model_key) or {}
                    metric_value = model_values.get(metric["key"])
                    if not is_num(metric_value) or metric_value < 0 or metric_value > maximum:
                        raise RuleError(
                            f"{sample_id} 的 {model_key}.{metric['key']} 须为 0–{maximum} 的数值",
                            "MEASUREMENTS_INCOMPLETE",
                        )
            if type_ == "audio":
                for side in ("baseline", "candidate"):
                    values = sample["measurements"][side]
                    if "cerEdits" in values or "cerReferenceChars" in values:
                        edits, chars = values.get("cerEdits"), values.get("cerReferenceChars")
                        if (not is_num(edits) or edits < 0 or int(edits) != edits
                                or not is_num(chars) or chars <= 0 or int(chars) != chars
                                or abs(values["cer"] - 100 * edits / chars) > .011):
                            raise RuleError(f"{sample_id} 的 CER 与编辑数/参考字数不一致",
                                            "MEASUREMENTS_INCOMPLETE")
        normalized.append(sample)
    return normalized


def evaluate(type_: str, samples: Any, config: dict[str, Any]) -> dict[str, Any]:
    validate_config(type_, config)
    normalized = normalize_samples(samples, type_, "measured")
    template = get_template(type_)
    results: list[dict[str, Any]] = []
    for sample in normalized:
        baseline = sample["measurements"]["baseline"]
        candidate = sample["measurements"]["candidate"]
        checks: list[dict[str, Any]] = []
        for metric in template["metrics"]:
            key = metric["key"]
            if metric["direction"] == "lower":
                passed = candidate[key] <= config["thresholds"][key]
            else:
                passed = candidate[key] >= config["thresholds"][key]
            if type_ == "text" and key != "lossCv":
                passed = passed and candidate[key] > baseline[key]
            if type_ == "benchmark":
                passed = candidate["score"] - baseline["score"] >= config["thresholds"]["score"]
            checks.append({
                "key": key,
                "name": metric["name"],
                "unit": metric["unit"],
                "baseline": baseline[key],
                "candidate": candidate[key],
                "delta": round2(candidate[key] - baseline[key]),
                "threshold": config["thresholds"][key],
                "passed": bool(passed),
            })

        score = None
        grade = None
        if type_ == "video":
            score = round2(sum(
                candidate[metric["key"]] * config["weights"][index] / 100
                for index, metric in enumerate(template["metrics"])
            ))
            high_value = bool(config.get("highValue")) or bool(sample.get("highValue"))
            if score >= config["grades"]["AA"] and high_value:
                grade = "AA"
            elif score >= config["grades"]["A"]:
                grade = "A"
            elif score >= config["grades"]["B"]:
                grade = "B"
            else:
                grade = "C"

        issues = [f"{check['name']}未达标" for check in checks if not check["passed"]]
        if type_ == "video":
            passed = grade in ("AA", "A")
        else:
            passed = all(check["passed"] for check in checks)
        usage = (GRADE_USE.get(grade) if grade else
                 ("可进入目标训练或评测环节" if passed else "需回溯优化"))
        output = copy.deepcopy(sample)
        output.update({
            "checks": checks,
            "score": score,
            "grade": grade,
            "usage": usage,
            "passed": passed,
            "issues": issues,
            "review": {"status": "pending", "note": ""},
        })
        results.append(output)

    metrics: list[dict[str, Any]] = []
    for metric in template["metrics"]:
        key = metric["key"]
        baseline_mean = mean([s["measurements"]["baseline"][key] for s in results])
        candidate_mean = mean([s["measurements"]["candidate"][key] for s in results])
        aggregation = None
        if type_ == "audio" and key == "cer":
            # Legacy imported/demo rows have no counts; retain their schema.
            counts = [s["measurements"][model] for s in results
                      for model in ("baseline", "candidate")]
            if all(is_num(v.get("cerEdits")) and v["cerEdits"] >= 0
                   and is_num(v.get("cerReferenceChars")) and v["cerReferenceChars"] > 0
                   for v in counts):
                def corpus(model):
                    values = [s["measurements"][model] for s in results]
                    return 100 * sum(v["cerEdits"] for v in values) / sum(v["cerReferenceChars"] for v in values)
                baseline_mean, candidate_mean = corpus("baseline"), corpus("candidate")
                aggregation = "corpus_edit_distance"
        if metric["direction"] == "lower":
            passed = candidate_mean <= config["thresholds"][key]
        else:
            passed = candidate_mean >= config["thresholds"][key]
        if type_ == "text" and key != "lossCv":
            passed = passed and candidate_mean > baseline_mean
        if type_ == "benchmark":
            passed = candidate_mean - baseline_mean >= config["thresholds"]["score"]
        metrics.append({
            **metric,
            **({"aggregation": aggregation} if aggregation else {}),
            "baseline": round2(baseline_mean),
            "candidate": round2(candidate_mean),
            "delta": round2(candidate_mean - baseline_mean),
            "threshold": config["thresholds"][key],
            "passed": bool(passed),
        })

    grades = {"AA": 0, "A": 0, "B": 0, "C": 0}
    for result in results:
        if result["grade"]:
            grades[result["grade"]] += 1

    summary = {
        "total": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "issues": sum(1 for item in results if not item["passed"]),
        "grades": grades,
        "metrics": metrics,
        "accepted": (grades["C"] == 0 if type_ == "video"
                     else all(metric["passed"] for metric in metrics)),
    }
    return {"samples": results, "summary": summary}


def suggestion(type_: str, sample: dict[str, Any]) -> str:
    if type_ == "image" and any(
            check["key"] == "iou" and not check["passed"] for check in sample.get("checks", [])):
        return "回退至图像分割前一步，复核边界与标注；专业场景由人工确认后调整增强参数。"
    if type_ == "text":
        return "回溯文本分块和清洗参数，检查语义断裂；修订后以相同验证集重新比较准确率、F1 与损失。"
    if type_ == "audio":
        return "分析当前场景与发音人错误，补采高质量语音；检查低质片段并重新清洗后复评。"
    if type_ == "benchmark":
        return "检查难度过低或题目设计问题，调整复杂样本比例，用相同能力模型重新验证区分度。"
    if type_ == "video":
        return "回溯视频清晰度、图文对齐和来源安全性，重新处理低分样本；C 级仅保留用于错误分析。"
    return "追溯场景增强参数与模型性能关联，修订参数后以同一验证集重新验证。"


def default_source(role: str) -> str:
    return {
        "text": "DEMO-S",
        "image": "IMG-S",
        "video": "VID-S",
        "audio": "AUD-S",
        "benchmark": "DEMO-S",
    }[role]


def demoSamples(type_: str, source: Any = None) -> list[dict[str, Any]]:
    """Port of the frontend `demoSamples()` fixed-value simulation."""
    get_template(type_)
    if source and isinstance(source, list) and len(source) > 0:
        rows = source
    elif type_ in ("image", "video", "audio"):
        labels = {
            "image": ["晴天车辆", "雨天车辆", "工业设备", "边界标注", "高价值专业图像"],
            "video": ["室外道路视频", "工业作业视频", "低光照视频", "缺帧视频", "高价值场景视频"],
            "audio": ["普通话指令", "室内说话人", "领域操作指令", "噪声语音", "专业术语语音"],
        }[type_]
        prefix = {"image": "IMG", "video": "VID", "audio": "AUD"}[type_]
        suffix = {"image": ".jpg", "video": ".mp4", "audio": ".wav"}[type_]
        rows = [
            {"id": f"{prefix}-S{index + 1:03d}", "title": label,
             "input": f"demo://{type_}/sample-{index + 1}{suffix}"}
            for index, label in enumerate(labels)
        ]
    else:
        labels = ["通用场景", "专业场景", "复杂场景", "边界场景", "高价值场景"]
        inputs = ["场景一的数据记录", "场景二的数据记录", "场景三的数据记录",
                  "场景四的数据记录", "场景五的数据记录"]
        rows = [
            {"id": f"DEMO-S{index + 1:03d}", "title": label, "input": inputs[index]}
            for index, label in enumerate(labels)
        ]

    defaults = {
        "text": {"chunk_size": 512, "overlap": 64},
        "image": {"mask_scale": 1, "rain_drop_size": 2, "rain_density": 0.3},
        "audio": {"segment_seconds": 10, "min_snr": 15},
        "video": {"frame_interval": 2, "alignment_version": "v1"},
        "benchmark": {"complex_sample_ratio": 0.2},
    }[type_]
    source_step = {
        "image": "图像分割与数据增强",
        "text": "文本分块",
        "audio": "音频切分与转写",
        "video": "帧采样与图文对齐",
        "benchmark": "评测题构造",
    }[type_]

    normalized = normalize_samples(rows, type_, "demo")
    output: list[dict[str, Any]] = []
    for index, sample in enumerate(normalized):
        j = 1 if sample.get("_demoRemediated") else index % 5
        baseline: dict[str, float] = {}
        candidate: dict[str, float] = {}
        for metric_index, metric in enumerate(TEMPLATES[type_]["metrics"]):
            key = metric["key"]
            if key == "iou":
                baseline[key] = 0.81
                candidate[key] = 0.72 if j == 3 else 0.88 + j * 0.01
            elif key == "lossCv":
                baseline[key] = 0.18
                candidate[key] = 0.28 if j == 3 else 0.1 + j * 0.01
            elif key == "cer":
                baseline[key] = 16.0
                candidate[key] = 19.0 if j == 3 else float(7 + j)
            elif type_ == "benchmark":
                baseline[key] = 52.0 + j * 3
                candidate[key] = baseline[key] + (2 if j == 3 else 15 + j)
            elif type_ == "video":
                baseline[key] = 70.0
                candidate[key] = [94, 88, 76, 63, 97][j] - metric_index * 0.4
            else:
                baseline[key] = 77.0 + metric_index
                candidate[key] = 74.0 + metric_index if j == 3 else 86.0 + j + metric_index
        item = dict(sample)
        item.update({
            "parameters": sample["parameters"] if sample["parameters"] else dict(defaults),
            "highValue": bool(sample.get("highValue")) or index == 4,
            "sourceStep": source_step,
            "measurements": {"baseline": baseline, "candidate": candidate},
        })
        output.append(item)
    return output


def demo_baseline_plan(type_: str, model_id: str) -> dict[str, Any] | None:
    model = next((m for m in MODEL_PROFILES.get(type_, []) if m["id"] == model_id), None)
    if not model:
        return None
    descriptions = {
        "text": ["通用文本理解对比", "与公开语料参考表现对比，观察文本分类、实体识别及训练稳定性。"],
        "image": ["图像处理效果对比", "对比处理前后图像质量，检查边界、清晰度和场景识别效果。"],
        "video": ["视频质量分级对比", "对比处理前后五维质量评分，自动给出样本等级。"],
        "audio": ["通用语音能力对比", "检查识别错误率、说话人识别和指令理解表现。"],
        "benchmark": ["模型能力区分度对比", "对比基础模型与优化模型得分，判断题目是否有区分度。"],
    }[type_]
    return {
        "id": f"demo-baseline-{type_}-{model_id}",
        "type": type_,
        "modelId": model_id,
        "name": descriptions[0],
        "description": descriptions[1],
        "version": "1.0",
        "mode": "demo",
        "status": "published",
        "validationVersion": "validation-v1",
        "seed": 42,
        "reference": f"demo://baseline/{type_}/{model_id}",
    }


def evaluation_preset(type_: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    template = get_template(type_)
    same = bool(previous) and previous.get("type") == type_
    snapshot = (previous.get("planSnapshot") if same and previous else None) or {}
    model_name = (previous.get("candidateModel") if same and previous else None) or template["model"]
    model = next((m for m in MODEL_PROFILES.get(type_, []) if m["name"] == model_name), {})
    baseline_config = None
    if same and previous:
        baseline_config = previous.get("baselineConfig") or snapshot.get("baselineConfig")
    return {
        "id": snapshot.get("id") or f"builtin-{type_}-v1",
        "version": snapshot.get("version") or "1.0",
        "type": type_,
        "modelId": (previous.get("modelId") if same and previous else None)
                   or snapshot.get("modelId") or model.get("id"),
        "modelVersion": snapshot.get("modelVersion") or "1.0",
        "baselineConfig": copy.deepcopy(baseline_config),
        "name": f"{template['name']}评测方案",
        "description": template["description"],
        "baselineModel": (previous.get("baselineModel") if same and previous else None)
                         or template["baseline"],
        "candidateModel": (previous.get("candidateModel") if same and previous else None)
                          or template["model"],
        "downstreamTask": (previous.get("downstreamTask") if same and previous else None)
                          or template["task"],
        "validationVersion": (previous.get("validationVersion") if same and previous else None)
                             or "validation-v1",
        "seed": (previous.get("seed") if same and previous and isinstance(previous.get("seed"), int)
                 else 42),
        "config": copy.deepcopy(
            (previous.get("config") if same and previous else None) or default_config(type_)),
    }


def evaluation_demo_task(type_: str, source: Any = None,
                         created_at: str | None = None) -> dict[str, Any]:
    from ..core.ids import now

    preset = evaluation_preset(type_)
    samples = demoSamples(type_, source)
    dataset_id = f"DEMO-EVALUATION-{type_.upper()}-001"
    template = get_template(type_)
    return {
        "id": f"EVAL-DEMO-{type_.upper()}-001",
        "name": f"{template['name']} · Demo",
        "type": type_,
        "mode": "demo",
        "status": "draft",
        "step": 0,
        "createdAt": created_at or now(),
        "datasetId": dataset_id,
        "datasetName": f"{template['name']} Demo 数据集",
        "datasetOrigin": {
            "kind": "raw", "id": dataset_id, "name": f"{template['name']} Demo 数据集",
            "type": type_, "mode": "demo", "version": "v1",
            "batchId": dataset_id, "sampleIds": [sample["id"] for sample in samples],
        },
        "baselineModel": preset["baselineModel"],
        "candidateModel": preset["candidateModel"],
        "downstreamTask": preset["downstreamTask"],
        "validationVersion": preset["validationVersion"],
        "seed": preset["seed"],
        "planSnapshot": copy.deepcopy(preset),
        "config": copy.deepcopy(preset["config"]),
        "samples": samples,
        "logs": [],
        "feedback": [],
        "parentId": None,
        "_presetDemo": True,
    }
