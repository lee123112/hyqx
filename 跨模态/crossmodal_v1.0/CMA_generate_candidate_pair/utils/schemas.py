"""请求字段校验：/predict 的严格布尔与白名单字段。"""
from __future__ import annotations

from typing import Any, Dict

ALLOWED_PREDICT_FIELDS = {"uniqueId", "isLastOperator", "inputFile", "config"}


def validate_predict_payload(payload: Any) -> Dict[str, Any]:
    """校验 /predict 请求体；不合法直接抛 ValueError，由接口层转成统一失败信封。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - ALLOWED_PREDICT_FIELDS)
    if unknown:
        raise ValueError("未知字段：%s" % unknown[0])
    unique_id = payload.get("uniqueId")
    if not isinstance(unique_id, str) or not unique_id.strip():
        raise ValueError("uniqueId 不能为空")
    if "isLastOperator" not in payload:
        raise ValueError("isLastOperator 必须传布尔值")
    if not isinstance(payload["isLastOperator"], bool):
        raise ValueError("isLastOperator 必须是严格布尔值")
    if "inputFile" in payload and payload["inputFile"] is not None \
            and not isinstance(payload["inputFile"], str):
        raise ValueError("inputFile 必须是字符串")
    if "config" in payload and payload["config"] is not None \
            and not isinstance(payload["config"], dict):
        raise ValueError("config 必须是对象")
    return {
        "uniqueId": unique_id.strip(),
        "isLastOperator": payload["isLastOperator"],
        "inputFile": payload.get("inputFile"),
        "config": payload.get("config"),
    }
