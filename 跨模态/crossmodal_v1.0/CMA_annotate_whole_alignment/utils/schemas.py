"""HTTP 请求字段校验：/predict 的严格布尔与白名单字段（表1 契约 §三）。

身份/部署项（operator、stageId、输出根目录、模型权重路径）不接受请求改写：
``config`` 只允许业务参数，未知键一律报错，避免请求侧偷偷换模型或换输出目录。
"""
from __future__ import annotations

from typing import Any, Dict

ALLOWED_PREDICT_FIELDS = {"uniqueId", "isLastOperator", "inputFile", "config"}

#: 允许通过请求 config 覆盖的业务参数（模型/设备/路径/身份不可覆盖）。
#: #16 的阈值随表1 冻结口径，允许按数据集覆盖但仍在定义域内校验。
ALLOWED_CONFIG_KEYS = {"isolation_threshold", "auto_align_threshold"}


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
    config = payload.get("config")
    if isinstance(config, dict):
        unknown_config = sorted(set(config) - ALLOWED_CONFIG_KEYS)
        if unknown_config:
            raise ValueError("不支持的配置项：%s" % unknown_config[0])
    return {
        "uniqueId": unique_id.strip(),
        "isLastOperator": payload["isLastOperator"],
        "inputFile": payload.get("inputFile"),
        "config": config,
    }
