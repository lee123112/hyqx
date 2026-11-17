"""算子 2 的轻量请求字段校验辅助。"""
from __future__ import annotations

from typing import Any


def validate_predict_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    allowed = {"uniqueId", "isLastOperator", "inputFile", "config"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("未知字段：%s" % sorted(unknown)[0])
    if not isinstance(payload.get("uniqueId"), str) or not payload["uniqueId"].strip():
        raise ValueError("uniqueId 不能为空")
    if not isinstance(payload.get("isLastOperator"), bool):
        raise ValueError("isLastOperator 必须是布尔值")
    return dict(payload)
