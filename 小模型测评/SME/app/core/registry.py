"""Load the platform operator registry (`map.json`) at the service root."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .config import settings


@lru_cache(maxsize=1)
def load_map() -> dict[str, Any]:
    path = settings.project_root / "map.json"
    if not path.exists():
        return {"service": "small-model-evaluation", "operators": {}}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def operators() -> dict[str, dict[str, Any]]:
    return load_map().get("operators") or {}


def resolve(operator_id: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve canonical key, package name, operatorCode or alias."""
    items = operators()
    if operator_id in items:
        return operator_id, items[operator_id]
    for key, item in items.items():
        names = {key, item.get("operatorCode"), item.get("package"), *(item.get("aliases") or [])}
        if operator_id in names:
            return key, item
    return None


def type_of(operator_id: str) -> str | None:
    found = resolve(operator_id)
    return found[1].get("type") if found else None
