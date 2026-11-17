from __future__ import annotations

import importlib

from fastapi import FastAPI

OPTIONAL_ROUTERS = ("eval_tasks", "datasets", "feedback", "baselines", "reports", "operator",
                    "image_protocol", "audio_protocol")


def include_routers(app: FastAPI) -> list[str]:
    included: list[str] = []
    from .health import router as health_router

    app.include_router(health_router)
    included.append("health")
    for name in OPTIONAL_ROUTERS:
        try:
            module = importlib.import_module(f"app.api.{name}")
        except ModuleNotFoundError:
            continue
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)
            included.append(name)
    return included
