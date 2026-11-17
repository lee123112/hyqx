from __future__ import annotations

import importlib.util

from fastapi import APIRouter

from ..core.config import settings
from ..core.envelope import ok
from ..core.jobs import jobs
from ..core.registry import operators as registered_operators
from ..eval import templates
from ..store import db
from ..store.seed import ensure_seed

router = APIRouter(tags=["health"])

ENGINE_MODULES = {
    "text": "app.engines.text", "image": "app.engines.image", "audio": "app.engines.audio",
    "video": "app.engines.video", "benchmark": "app.engines.benchmark",
}


@router.get("/health")
def health() -> dict:
    engines = {
        type_: importlib.util.find_spec(module) is not None
        for type_, module in ENGINE_MODULES.items()
    }
    operator_list = [
        {"operatorId": key, "name": meta.get("name"), "type": meta.get("type"),
         "operatorCode": meta.get("operatorCode"), "package": meta.get("package"),
         "class": meta.get("class")}
        for key, meta in registered_operators().items()
    ]
    return ok({
        "status": "ok",
        "service": "small-model-evaluation",
        "version": "1.0.0",
        "operatorCount": len(operator_list),
        "operators": operator_list,
        "engines": engines,
        "port": settings.port,
        "modes": ["demo", "measured"],
    })


@router.get("/api/v1/eval/templates")
def get_templates() -> dict:
    return ok({
        "types": list(templates.EVAL_TYPES),
        "templates": templates.TEMPLATES,
        "modelProfiles": templates.MODEL_PROFILES,
        "defaultWeights": templates.DEFAULT_WEIGHTS,
        "gradeUse": templates.GRADE_USE,
        "defaultConfig": {type_: templates.default_config(type_) for type_ in templates.EVAL_TYPES},
    })


@router.get("/api/v1/eval/overview")
def overview() -> dict:
    ensure_seed()
    return ok(db.stats())


@router.get("/api/v1/operator/jobs/{unique_id}/{operator_id}")
def job_status(unique_id: str, operator_id: str) -> dict:
    item = jobs.get(unique_id, operator_id)
    if item is None:
        from ..core.envelope import ApiError
        raise ApiError(404, "NOT_FOUND", "运行记录不存在")
    return ok(item)
