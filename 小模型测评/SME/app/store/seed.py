"""Seed demo datasets and built-in baseline plans (frontend-compatible)."""
from __future__ import annotations

from typing import Any

from ..core.ids import now
from ..eval import rules, templates
from ..store import db


def seed_demo() -> dict[str, int]:
    created_datasets = 0
    created_baselines = 0
    for type_ in templates.EVAL_TYPES:
        task = rules.evaluation_demo_task(type_)
        dataset = {
            "id": task["datasetId"],
            "name": task["datasetName"],
            "kind": "raw",
            "type": type_,
            "evaluationTypes": (["text", "benchmark"] if type_ == "text" else [type_]),
            "modality": templates.TEMPLATES[type_]["name"],
            "batchId": task["datasetId"],
            "version": "v1",
            "source": "预置 Demo",
            "status": "已移交",
            "mode": "demo",
            "samples": task["samples"],
            "count": len(task["samples"]),
            "createdAt": now(),
            "standard": "通用质量标准",
            "note": "固定五条演示样本，模型指标为模拟结果；媒体 URI 为演示标识。",
        }
        if not db.get_dataset("raw", dataset["id"]):
            db.save_dataset(dataset)
            created_datasets += 1

    for plan in templates.BASELINE_PLANS:
        if db.get_baseline(plan["id"]):
            continue
        type_ = plan["type"]
        models = templates.MODEL_PROFILES[type_]
        model = next((m for m in models if m["id"] == plan["modelId"]), {})
        item = {
            **plan,
            "name": f"{templates.TEMPLATES[type_]['name']}标准基线"
                    + (f" · {model.get('name')}" if len(models) > 1 else ""),
            "modelName": model.get("name") or plan.get("name"),
            "config": templates.default_config(type_),
            "source": "builtin",
            "updatedAt": now(),
        }
        db.save_baseline(item)
        created_baselines += 1
    return {"datasets": created_datasets, "baselines": created_baselines}


def ensure_seed() -> dict[str, int]:
    result = seed_demo()
    result['measuredBaselines'] = seed_measured_baselines()
    return result


def seed_measured_baselines() -> int:
    from ..eval.measured_baselines import load_measured_baselines
    items = load_measured_baselines()
    if not items:
        return 0
    active_ids = {item['id'] for item in items}
    created = 0
    # Superseded built-in calibrations remain queryable for old snapshots but
    # stop being offered as the current default. User plans are never changed.
    for previous in db.list_baselines():
        if (previous.get('source') == 'builtin' and previous.get('evidence')
                and previous.get('isDefault') and previous['id'] not in active_ids):
            previous.update(isDefault=False, status='archived')
            db.save_baseline(previous)
    for item in items:
        if not db.get_baseline(item['id']):
            db.save_baseline(item)
            created += 1
    return created
