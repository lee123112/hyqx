"""算子 3 数据契约：配置模型与 HTTP 请求/响应模型。

设计约定（TECHNICAL_DESIGN §2）：
- ``operator`` 与 ``stage_id`` 属于部署配置，不接受请求覆盖血缘身份；
- 业务参数可由 ``config`` 覆盖，但取值域必须受限；
- ``io``、``server`` 属于服务端部署配置，请求不得放宽限额；
- 未知字段一律报错，不静默采用另一种算法。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

#: ad_patterns 单条正则的最大长度，避免把配置变成无界计算入口
MAX_AD_PATTERN_LENGTH = 200
#: 业务参数白名单：只有这些键允许被 HTTP/CLI 覆盖
BUSINESS_CONFIG_KEYS = ("enabled",)
#: 部署配置键：请求覆盖会被忽略（保持服务端快照）
DEPLOYMENT_CONFIG_KEYS = ("operator", "stage_id", "io", "server")


class OperatorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "text_normalization"
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
    """算子 3 完整配置。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorIdentity = Field(default_factory=OperatorIdentity)
    stage_id: str = "S2"

    enabled: StrictBool = False
    lang: str = "zh"  # 上游语言声明；非页面处理开关

    # --- 部署参数（请求不可覆盖） ---
    io: IOConfig = Field(default_factory=IOConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @field_validator("stage_id")
    @classmethod
    def _stage_id_not_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("stage_id 不能为空")
        return value.strip()

    @field_validator("lang")
    @classmethod
    def _lang_shape(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("lang 不能为空")
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})*", value):
            raise ValueError("lang 必须是形如 zh / en / zh-hans 的语言码")
        return value



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
