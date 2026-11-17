"""Load the evaluation contract extracted from the platform frontend."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ..core.envelope import ApiError
from ..core.config import settings

_RAW: dict[str, Any] = json.loads(settings.templates_path.read_text(encoding="utf-8"))

TEMPLATES: dict[str, dict[str, Any]] = _RAW["TEMPLATES"]
MODEL_PROFILES: dict[str, list[dict[str, Any]]] = _RAW["MODEL_PROFILES"]
DEFAULT_WEIGHTS: list[int] = _RAW["DEFAULT_WEIGHTS"]
GRADE_USE: dict[str, str] = _RAW["GRADE_USE"]
DEFAULT_CONFIG: dict[str, dict[str, Any]] = _RAW["DEFAULT_CONFIG"]
BASELINE_PLANS: list[dict[str, Any]] = _RAW["BASELINE_PLANS"]

EVAL_TYPES: tuple[str, ...] = ("text", "image", "video", "audio", "benchmark")


def get_template(type_: str) -> dict[str, Any]:
    template = TEMPLATES.get(type_)
    if not template:
        raise ApiError(422, "CONFIG_INVALID", "请选择有效的评测类型")
    return template


def default_config(type_: str) -> dict[str, Any]:
    get_template(type_)
    value = DEFAULT_CONFIG.get(type_)
    if not value:
        value = {
            "thresholds": {m["key"]: m["threshold"] for m in TEMPLATES[type_]["metrics"]},
            "weights": list(DEFAULT_WEIGHTS),
            "grades": {"AA": 95, "A": 85, "B": 70},
            "highValue": False,
        }
    return deepcopy(value)


def default_baseline(type_: str, model_id: str | None = None) -> dict[str, Any] | None:
    for plan in BASELINE_PLANS:
        if plan["type"] == type_ and (model_id is None or plan["modelId"] == model_id):
            return deepcopy(plan)
    return None
