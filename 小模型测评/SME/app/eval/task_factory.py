"""Build a frontend-compatible evaluation task from an API request."""
from __future__ import annotations

import copy
from typing import Any

from ..core.envelope import ApiError
from ..core.ids import is_valid_id, new_dataset_id, new_task_id, now
from ..datasets import ingest
from ..store import db
from . import rules, templates


def _source_of(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("source")
    if isinstance(source, str):
        return {"kind": source}
    if isinstance(source, dict):
        return source
    if payload.get("parentId"):
        return {"kind": "rerun"}
    if payload.get("samples"):
        return {"kind": "upload"}
    return {"kind": "preset"}


def _measurements_complete(type_: str, samples: list[dict[str, Any]]) -> bool:
    keys = [metric["key"] for metric in templates.TEMPLATES[type_]["metrics"]]
    for sample in samples:
        measurements = sample.get("measurements") if isinstance(sample, dict) else None
        if not isinstance(measurements, dict):
            return False
        for model in ("baseline", "candidate"):
            values = measurements.get(model)
            if not isinstance(values, dict):
                return False
            for key in keys:
                if not rules.is_num(values.get(key)):
                    return False
    return True


def build_task(payload: dict[str, Any], *, allow_existing: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError(422, "REQUEST_INVALID", "请求体格式不正确")
    if 'protocol' in payload:
        from .protocol_tasks import build_protocol_task
        return build_protocol_task(payload, allow_existing=allow_existing)
    type_ = str(payload.get("type") or "").strip()
    if type_ not in templates.TEMPLATES:
        raise ApiError(422, "CONFIG_INVALID", "请选择有效的评测类型")
    mode = str(payload.get("mode") or "demo")
    if mode not in ("demo", "measured"):
        raise ApiError(422, "CONFIG_INVALID", "评测模式必须是 demo 或 measured")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(422, "REQUEST_INVALID", "请输入评测任务名称")

    source = _source_of(payload)
    kind = source.get("kind") or "preset"
    parent = None
    dataset = None
    raw_samples: list[dict[str, Any]] | None = None
    dataset_id = payload.get("datasetId")
    dataset_name = payload.get("datasetName") or ""
    origin: dict[str, Any] | None = payload.get("datasetOrigin")

    if kind == "rerun":
        parent_id = payload.get("parentId")
        if not parent_id:
            raise ApiError(422, "REQUEST_INVALID", "复评任务缺少 parentId")
        parent = db.get_task(parent_id)
        if not parent:
            raise ApiError(404, "NOT_FOUND", "复评来源任务不存在")
        if parent.get("type") != type_:
            raise ApiError(422, "CONFIG_INVALID", "复评任务类型必须与原任务一致")
        if parent.get("mode") == "demo" and mode == "measured":
            raise ApiError(422, "DEMO_ONLY_DATASET", "该批次仅有演示指标，请上传或选择包含小模型实测指标的数据")
        raw_samples = copy.deepcopy(parent.get("samples") or [])
        dataset_id = parent.get("datasetId")
        dataset_name = parent.get("datasetName") or ""
        origin = copy.deepcopy(parent.get("datasetOrigin")) or origin
        if payload.get("samples"):
            raw_samples = payload["samples"]
    elif kind in ("raw", "production"):
        dataset_id = source.get("id") or dataset_id
        if not dataset_id:
            raise ApiError(422, "REQUEST_INVALID", "请选择数据集")
        dataset = db.get_dataset(kind, str(dataset_id))
        if not dataset:
            raise ApiError(404, "NOT_FOUND", "数据集不存在")
        if type_ not in (dataset.get("evaluationTypes") or [dataset.get("type")]):
            raise ApiError(422, "DATASET_TYPE_MISMATCH", "请选择与评测类型匹配的数据集")
        if source.get("version") and str(source["version"]) != str(dataset.get("version")):
            raise ApiError(409, "DATASET_CHANGED", "来源数据已更新，请重新选择数据集")
        if source.get("sampleHash") and source["sampleHash"] != dataset.get("sampleHash"):
            raise ApiError(409, "DATASET_CHANGED", "来源数据已更新，请重新选择数据集")
        if mode == "measured" and dataset.get("mode") == "demo":
            raise ApiError(422, "DEMO_ONLY_DATASET", "该批次仅有演示指标，请上传或选择包含小模型实测指标的数据")
        raw_samples = copy.deepcopy(dataset.get("samples") or [])
        dataset_name = dataset.get("name") or dataset_name
        origin = origin or {
            "kind": kind, "id": dataset["id"], "name": dataset["name"],
            "type": dataset.get("type"), "mode": dataset.get("mode"),
            "version": dataset.get("version"), "batchId": dataset.get("batchId"),
            "sampleIds": [sample.get("id") for sample in raw_samples],
        }
    elif kind == "preset":
        if mode == "measured":
            raise ApiError(422, "DEMO_ONLY_DATASET",
                           "该批次仅有演示指标，请上传或选择包含小模型实测指标的数据")
        demo = rules.evaluation_demo_task(type_)
        dataset_id = demo["datasetId"]
        dataset_name = demo["datasetName"]
        raw_samples = copy.deepcopy(demo["samples"])
        origin = demo["datasetOrigin"]
    elif kind == "upload":
        raw_samples = payload.get("samples")
        if not raw_samples:
            raise ApiError(422, "REQUEST_INVALID", "请先上传文件或选择数据集")
        dataset_id = dataset_id or new_dataset_id()
        dataset_name = dataset_name or "本地数据集"
        origin = origin or {
            "kind": "upload", "id": dataset_id, "name": dataset_name,
            "type": type_, "mode": mode, "version": "v1",
            "sampleIds": [sample.get("id") for sample in raw_samples if isinstance(sample, dict)],
        }
    else:
        raise ApiError(422, "REQUEST_INVALID", "不支持的数据集来源")

    if not raw_samples:
        raise ApiError(422, "REQUEST_INVALID", "请先上传文件或选择数据集")
    if not dataset_id:
        dataset_id = new_dataset_id()

    measurement_source = str(payload.get("measurementSource") or "import")
    if parent and mode == "measured":
        old_evaluation = ((parent.get("result") or {}).get("engine") or {}).get("meta") or {}
        if parent.get("measurementSource") == "engine" or old_evaluation.get("evaluation"):
            measurement_source = "engine"
    if mode == "measured" and measurement_source in ("engine", "auto"):
        raw_samples = copy.deepcopy(raw_samples)
        for row in raw_samples:
            row.pop("measurements", None)
    needs_engine = False
    if mode == "demo":
        samples = rules.demoSamples(type_, raw_samples)
    elif type_ == "benchmark":
        from ..engines.benchmark import BenchmarkEngine
        BenchmarkEngine.validate_import(raw_samples)
        samples = rules.normalize_samples(raw_samples, type_, "measured")
    elif _measurements_complete(type_, raw_samples):
        samples = rules.normalize_samples(raw_samples, type_, "measured")
    else:
        if measurement_source in ("engine", "auto"):
            samples = rules.normalize_samples(raw_samples, type_, "demo")
            for row in samples:
                row.pop("measurements", None)
            needs_engine = True
        else:
            raise ApiError(422, "MEASUREMENTS_REQUIRED",
                           "实测模式需要逐条 baseline/candidate 指标；请导入指标或选择引擎测量")

    previous = parent if parent else None
    preset = rules.evaluation_preset(type_, previous)
    config = copy.deepcopy(payload.get("config") or preset["config"])
    rules.validate_config(type_, config)

    if mode == "measured" and type_ == "benchmark":
        benchmark_seed = payload.get("seed", 42)
        if isinstance(benchmark_seed, bool) or not isinstance(benchmark_seed, int):
            raise ApiError(422, "CONFIG_INVALID", "seed 必须是整数")

    baseline_config = payload.get("baselineConfig")
    if baseline_config is None:
        baseline_config = preset.get("baselineConfig")
    baseline_config = copy.deepcopy(baseline_config) if baseline_config else None
    if baseline_config and baseline_config.get('id'):
        stored_baseline = db.get_baseline(str(baseline_config['id']))
        if stored_baseline and stored_baseline.get('evidence') and stored_baseline.get('source') == 'builtin':
            selected_model = payload.get('modelId') or preset.get('modelId')
            if (stored_baseline.get('type') != type_ or stored_baseline.get('mode') != mode
                    or stored_baseline.get('modelId') != selected_model):
                raise ApiError(422, 'BASELINE_MISMATCH', '实测基线须与模态、模型和模式一致')
            baseline_config = copy.deepcopy(stored_baseline)
            if not payload.get('config'):
                config = copy.deepcopy(stored_baseline['config'])
                rules.validate_config(type_, config)
    if (mode == "measured" and baseline_config
            and baseline_config.get("status") not in ("published", "verified")
            and not payload.get("allowUnverifiedBaseline")):
        raise ApiError(422, "BASELINE_UNVERIFIED", "实测任务必须使用已发布/已验证的基线")

    model_id = payload.get("modelId") or preset.get("modelId")
    model_version = payload.get("modelVersion") or preset.get("modelVersion") or "1.0"
    from ..engines.service import validate_seed
    seed = validate_seed(payload.get("seed", preset.get("seed", 42)))
    validation_version = payload.get("validationVersion") or preset.get("validationVersion") or "validation-v1"
    parameter_source = payload.get("parameterSource") or {
        "mode": "custom",
        "baselineId": (baseline_config or {}).get("id"),
        "baselineVersion": (baseline_config or {}).get("version"),
    }
    if baseline_config and baseline_config.get('evidence'):
        parameter_source = {
            'mode': 'baseline' if config == baseline_config['config'] else 'custom',
            'baselineId': baseline_config['id'], 'baselineVersion': baseline_config['version'],
            'runSha256': baseline_config['evidence']['runSha256'],
        }
    plan_snapshot = copy.deepcopy(preset)
    plan_snapshot["config"] = copy.deepcopy(config)
    plan_snapshot["parameterSource"] = copy.deepcopy(parameter_source)
    plan_snapshot["baselineConfig"] = copy.deepcopy(baseline_config)

    task_id = payload.get("id") or new_task_id()
    if not is_valid_id(task_id):
        raise ApiError(422, "REQUEST_INVALID", "任务 ID 不合法")
    if not allow_existing and db.get_task(task_id):
        raise ApiError(409, "TASK_EXISTS", "任务 ID 已存在")

    task = {
        "id": task_id,
        "name": name,
        "type": type_,
        "mode": mode,
        "status": "draft",
        "step": 0,
        "createdAt": payload.get("createdAt") or now(),
        "completedAt": None,
        "error": "",
        "datasetName": dataset_name,
        "datasetId": dataset_id,
        "datasetOrigin": origin,
        "modelId": model_id,
        "modelVersion": model_version,
        "baselineConfig": baseline_config,
        "baselineModel": payload.get("baselineModel") or preset["baselineModel"],
        "candidateModel": payload.get("candidateModel") or preset["candidateModel"],
        "downstreamTask": payload.get("downstreamTask") or preset["downstreamTask"],
        "validationVersion": validation_version,
        "seed": seed,
        "planSnapshot": plan_snapshot,
        "parameterSource": parameter_source,
        "config": config,
        "samples": samples,
        "logs": [],
        "feedback": [],
        "parentId": payload.get("parentId"),
        "_needsEngine": needs_engine,
        "measurementSource": ("engine" if needs_engine else
                              ("import" if mode == "measured" else "demo")),
    }
    if baseline_config and baseline_config.get('evidence'):
        task['baselineModel'] = baseline_config.get('modelName') or task['baselineModel']
        for key in ('modelId', 'modelVersion', 'seed', 'validationVersion', 'baselineModel', 'candidateModel'):
            plan_snapshot[key] = task[key]
    return task


def save_new_task(task: dict[str, Any]) -> dict[str, Any]:
    db.save_task(task)
    return db.get_task(task["id"])  # type: ignore[return-value]

