"""Baseline plan endpoints (built-in plans are read-only)."""
from __future__ import annotations

import copy
from typing import Any

from fastapi import APIRouter, Body, Query

from ..core.envelope import ApiError, ok
from ..core.ids import now, random_code
from ..eval import rules, templates
from ..store import db

router = APIRouter(prefix="/api/v1/eval/baselines", tags=["baselines"])


def _check_type(type_: str) -> str:
    type_ = (type_ or "").strip()
    if type_ not in templates.TEMPLATES:
        raise ApiError(422, "CONFIG_INVALID", "请选择有效的评测类型")
    return type_


def _merge_config(type_: str, supplied: Any) -> dict[str, Any]:
    base = templates.default_config(type_)
    if supplied is None:
        config = base
    elif not isinstance(supplied, dict):
        raise ApiError(422, "CONFIG_INVALID", "评测配置不正确")
    else:
        if '_baselineMetadata' in supplied:
            raise ApiError(422, 'CONFIG_INVALID', '不允许通过规则配置注入实测基线证据')
        config = copy.deepcopy(base)
        for key, value in supplied.items():
            if key in ("thresholds", "grades") and isinstance(value, dict):
                merged = dict(config.get(key) or {})
                merged.update(value)
                config[key] = merged
            else:
                config[key] = copy.deepcopy(value)
    try:
        rules.validate_config(type_, config)
    except rules.RuleError as exc:
        raise ApiError(422, "CONFIG_INVALID", str(exc)) from exc
    return config


def _get_baseline(baseline_id: str) -> dict[str, Any]:
    item = db.get_baseline(baseline_id)
    if not item:
        raise ApiError(404, "NOT_FOUND", "基线方案不存在")
    return item


@router.get("")
def list_baselines(type_: str = Query("", alias="type")) -> dict:
    type_ = type_.strip()
    if type_:
        _check_type(type_)
    from ..store.seed import seed_measured_baselines
    seed_measured_baselines()
    items = db.list_baselines(type_ or None)
    return ok({"items": items, "total": len(items)})


@router.post("")
def create_baseline(payload: dict[str, Any] = Body(...)) -> dict:
    if not isinstance(payload, dict):
        raise ApiError(422, "REQUEST_INVALID", "请求体格式不正确")
    type_ = _check_type(str(payload.get("type") or ""))
    baseline_id = str(payload.get("id") or "").strip()
    if not baseline_id:
        baseline_id = f"custom-baseline-{type_}-{random_code(6).lower()}"
    existing = db.get_baseline(baseline_id)
    if existing and existing.get("source") == "builtin":
        raise ApiError(422, "BASELINE_BUILTIN", "内置基线不可覆盖")
    if payload.get('source') == 'builtin':
        raise ApiError(422, 'BASELINE_BUILTIN', '内置基线由服务端目录登记')
    item = {
        "id": baseline_id,
        "type": type_,
        "modelId": payload.get("modelId"),
        "mode": payload.get("mode") or "demo",
        "name": str(payload.get("name") or "").strip()
                or f"{templates.TEMPLATES[type_]['name']}自定义基线",
        "description": payload.get("description") or "",
        "version": str(payload.get("version") or "1.0"),
        "status": payload.get("status") or "published",
        "validationVersion": payload.get("validationVersion") or "validation-v1",
        "seed": payload.get("seed", 42),
        "config": _merge_config(type_, payload.get("config")),
        "modelName": payload.get("modelName"),
        "source": "custom",
        "updatedAt": now(),
    }
    return ok(db.save_baseline(item))


@router.patch("/{baseline_id}")
def update_baseline(baseline_id: str, payload: dict[str, Any] = Body(...)) -> dict:
    existing = _get_baseline(baseline_id)
    if existing.get('source') == 'builtin':
        raise ApiError(422, 'BASELINE_BUILTIN', '内置基线不可修改，请复制为自定义方案')
    if not isinstance(payload, dict):
        raise ApiError(422, "REQUEST_INVALID", "请求体格式不正确")
    if payload.get("type") and str(payload["type"]) != existing.get("type"):
        raise ApiError(422, "CONFIG_INVALID", "不允许修改基线方案的评测类型")
    updated = copy.deepcopy(existing)
    for key in ("modelId", "mode", "name", "description", "version", "status",
                "validationVersion", "seed", "modelName"):
        if key in payload:
            updated[key] = payload[key]
    if "config" in payload:
        updated["config"] = _merge_config(str(existing.get("type")), payload.get("config"))
    if not str(updated.get("name") or "").strip():
        raise ApiError(422, "REQUEST_INVALID", "请输入基线方案名称")
    updated["updatedAt"] = now()
    return ok(db.save_baseline(updated))


@router.delete("/{baseline_id}")
def delete_baseline(baseline_id: str) -> dict:
    existing = _get_baseline(baseline_id)
    if existing.get("source") == "builtin":
        raise ApiError(422, "BASELINE_BUILTIN", "内置基线方案不可删除")
    db.delete_baseline(baseline_id)
    return ok({"id": baseline_id, "deleted": True})
