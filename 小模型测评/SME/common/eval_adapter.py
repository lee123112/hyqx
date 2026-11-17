"""Shared adapter used by each platform operator package.

The platform imports the class named in `map.json`; it delegates to the same
FastAPI `/predict` implementation used by the root service, so a package and the
root service can never drift apart.
"""
from __future__ import annotations

from typing import Any


class EvaluationOperator:
    operator_id: str = ""
    package: str = ""
    eval_type: str = ""

    def predict(self, request: dict[str, Any] | None) -> dict[str, Any]:
        from app.api.operator import predict as run_predict

        body = dict(request or {})
        body["operatorId"] = self.operator_id
        params = dict(body.get("params") or {})
        params.setdefault("type", self.eval_type)
        body["params"] = params
        return run_predict(body)

    def describe(self) -> dict[str, Any]:
        return {
            "operatorId": self.operator_id,
            "package": self.package,
            "type": self.eval_type,
        }

    def run_protocol(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run an explicit dataset-level training protocol with shared validation."""
        from app.experiments.training import run_training_protocol
        return run_training_protocol(self.eval_type, payload)
