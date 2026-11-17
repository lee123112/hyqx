"""In-process operator job registry (status, logs, cancellation)."""
from __future__ import annotations

import threading
from typing import Any

from .ids import now


class JobRegistry:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, unique_id: str, operator_id: str, params: dict[str, Any]) -> dict[str, Any]:
        item = {
            "uniqueId": unique_id, "operatorId": operator_id, "status": "running",
            "params": params, "logs": [], "createdAt": now(), "finishedAt": None,
            "result": None, "error": "",
        }
        with self._lock:
            self._items[f"{unique_id}:{operator_id}"] = item
        return item

    def get(self, unique_id: str, operator_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._items.get(f"{unique_id}:{operator_id}")

    def log(self, unique_id: str, operator_id: str, tag: str, message: str) -> None:
        with self._lock:
            item = self._items.get(f"{unique_id}:{operator_id}")
            if item is not None:
                item["logs"].append({"time": now(), "tag": tag, "message": message})

    def finish(self, unique_id: str, operator_id: str, *,
               result: dict[str, Any] | None = None, error: str = "") -> None:
        with self._lock:
            item = self._items.get(f"{unique_id}:{operator_id}")
            if item is not None:
                item["status"] = "failed" if error else "succeeded"
                item["result"] = result
                item["error"] = error
                item["finishedAt"] = now()


jobs = JobRegistry()
