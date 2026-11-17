"""算子 4 数据契约：配置模型与 HTTP 请求/响应模型。

设计约定（TECHNICAL_DESIGN §2、DEVELOPMENT_CONTRACT §1）：
- ``operator`` 与 ``stage_id`` 属于部署配置，不接受请求覆盖血缘身份；
- 业务参数可由 ``config`` 覆盖，但取值域必须受限且为有限值；
- ``io``、``server`` 属于服务端部署配置，请求不得放宽限额；
- 未知字段、未知枚举一律报错，不静默采用另一种算法或格式。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

#: 业务参数白名单：只有这些键允许被 HTTP config / CLI --override 覆盖
BUSINESS_CONFIG_KEYS = ("enabled", "max_side", "output_format")
#: 部署配置键：请求覆盖会被忽略（保持服务端快照）
DEPLOYMENT_CONFIG_KEYS = ("operator", "stage_id", "io", "server")

#: 输出格式 -> 允许的扩展名（扩展名必须与格式匹配）
FORMAT_SUFFIXES = {"PNG": (".png",), "JPEG": (".jpg", ".jpeg")}


class OperatorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "image_standardization"
    version: str = "v1.0.0"


class IOConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_root: str = "output"


class ServerConfig(BaseModel):
    """服务端资源限额，请求不得放宽。"""

    model_config = ConfigDict(extra="forbid")

    max_files: int = Field(default=1000, gt=0, le=100000)
    max_unpacked_bytes: int = Field(default=67108864, gt=0)
    max_response_bytes: int = Field(default=67108864, gt=0)

    @field_validator("max_files", "max_unpacked_bytes", "max_response_bytes")
    @classmethod
    def _finite_positive(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("限额必须是整数")
        if value <= 0:
            raise ValueError("限额必须为正数")
        return value


class OperatorConfig(BaseModel):
    """算子 4 完整配置（TECHNICAL_DESIGN §2 默认值）。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorIdentity = Field(default_factory=OperatorIdentity)
    stage_id: str = "S2"

    # --- 业务参数（可被请求 config 覆盖） ---
    enabled: StrictBool = False
    max_side: int = Field(default=1024, ge=512, le=2048)
    allow_upscale: Literal[False] = False
    output_format: Literal["PNG", "JPEG"] = "JPEG"
    alpha_background: List[int] = Field(default_factory=lambda: [255, 255, 255])
    max_pixels: int = Field(default=40000000, gt=0)
    jpeg_quality: int = Field(default=95, ge=1, le=100)

    # --- 部署参数（请求不可覆盖） ---
    io: IOConfig = Field(default_factory=IOConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @field_validator("stage_id")
    @classmethod
    def _stage_id_not_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("stage_id 不能为空")
        return value.strip()

    @field_validator("output_format", mode="before")
    @classmethod
    def _format_alias(cls, value):
        return {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG"}.get(str(value).lower(), value)

    @field_validator("max_side", "max_pixels", "jpeg_quality")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("该参数必须是整数")
        if value <= 0:
            raise ValueError("该参数必须为正数")
        return value

    @field_validator("allow_upscale")
    @classmethod
    def _strict_bool(cls, value: bool) -> bool:
        if not isinstance(value, bool):
            raise ValueError("allow_upscale 必须是布尔值")
        return value

    @field_validator("alpha_background")
    @classmethod
    def _alpha_background_shape(cls, value: List[int]) -> List[int]:
        """背景色必须是 3（RGB）或 4（RGBA）个 0—255 整数通道。"""
        if not isinstance(value, (list, tuple)):
            raise ValueError("alpha_background 必须是数组")
        channels = list(value)
        if len(channels) not in (3, 4):
            raise ValueError("alpha_background 必须是 3 或 4 个通道")
        for index, item in enumerate(channels):
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError("alpha_background[%d] 必须是整数" % index)
            if not 0 <= item <= 255:
                raise ValueError("alpha_background[%d] 必须在 0—255 之间" % index)
        return channels


class PredictRequest(BaseModel):
    """``POST /predict`` 请求体；``isLastOperator`` 必须是严格布尔。"""

    model_config = ConfigDict(extra="forbid")

    uniqueId: str
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)


class RemoveFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uniqueId: str


class PredictResponse(BaseModel):
    code: int
    msg: str
    data: Dict[str, Any] = Field(default_factory=dict)


def ensure_finite_numbers(payload: Dict[str, Any], path: str = "config") -> None:
    """递归校验配置中的数值全部为有限值（拒绝 NaN/Infinity）。"""
    for key, value in payload.items():
        child = "%s.%s" % (path, key)
        if isinstance(value, bool):
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("%s 必须是有限数值" % child)
        elif isinstance(value, dict):
            ensure_finite_numbers(value, child)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, float) and not math.isfinite(item):
                    raise ValueError("%s[%d] 必须是有限数值" % (child, index))


def validate_config(config: Dict[str, Any]) -> OperatorConfig:
    """从已合并字典构造并校验配置对象（供业务层单测直接调用）。"""
    ensure_finite_numbers(config)
    return OperatorConfig(**config)
