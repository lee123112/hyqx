"""算子 5 数据契约：配置模型与 HTTP 请求模型。

设计约定（TECHNICAL_DESIGN §2）：
- ``operator`` 与 ``stage_id`` 属部署配置，请求 ``config`` 不能改写血缘身份；
- 业务参数（``sample_fps`` 等）可由请求 ``config`` 覆盖，但取值域受限且必须为有限值；
- ``io``、``server`` 属服务端部署配置，请求不得放宽限额；
- 未知枚举/字段一律报错，不静默采用另一种算法。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

#: 业务参数白名单：只有这些键允许被 HTTP/CLI 覆盖
BUSINESS_CONFIG_KEYS = ("enabled", "sample_fps", "max_frames")
#: 部署配置键：由服务端配置决定，请求里出现即报错（不静默忽略）
DEPLOYMENT_CONFIG_KEYS = ("operator", "stage_id", "io", "server")

#: 合法枚举：不支持的可变帧率策略
VFR_POLICIES = ("reject", "use_pts")

#: 采样率/限额定义域上限，防止把配置变成无界计算入口
MAX_SAMPLE_FPS = 2.0
MAX_FRAME_LIMIT = 8
MAX_DURATION_LIMIT = 36000.0
MAX_PROBE_TIMEOUT = 300.0


class OperatorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "video_keyframes"
    version: str = "v1.0.0"

    @field_validator("name", "version")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("operator.name/version 不能为空")
        return value.strip()


class IOConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_root: str = "output"

    @field_validator("output_root")
    @classmethod
    def _output_root_not_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("io.output_root 不能为空")
        return value.strip()


class ServerConfig(BaseModel):
    """服务端资源限额，请求不得放宽。"""

    model_config = ConfigDict(extra="forbid")

    max_files: int = Field(default=1000, gt=0, le=100000)
    max_unpacked_bytes: int = Field(default=67108864, gt=0)
    # v1.1.1：响应上限由 64MiB 提到 256MiB（与 #18 传输层一致），见 utils/transport.py
    max_response_bytes: int = Field(default=268435456, gt=0)

    @field_validator("max_files", "max_unpacked_bytes", "max_response_bytes")
    @classmethod
    def _finite_positive(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("限额必须是整数")
        if value <= 0:
            raise ValueError("限额必须为正数")
        return value


class OperatorConfig(BaseModel):
    """算子 5 完整配置（默认值见 TECHNICAL_DESIGN §2）。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorIdentity = Field(default_factory=OperatorIdentity)
    stage_id: str = "S2"

    # --- 业务参数（可被请求 config 覆盖） ---
    enabled: StrictBool = False
    sample_fps: float = 1.0
    max_frames: int = 8
    max_duration_seconds: float = 60.0
    crop_black_borders: Literal[False] = False
    probe_timeout_seconds: float = 15.0
    unsupported_vfr_policy: Literal["reject", "use_pts"] = "reject"

    # --- 部署参数（请求不可覆盖） ---
    io: IOConfig = Field(default_factory=IOConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @field_validator("stage_id")
    @classmethod
    def _stage_id_not_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("stage_id 不能为空")
        return value.strip()

    @field_validator("sample_fps")
    @classmethod
    def _sample_fps_domain(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("sample_fps 必须是数值")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("sample_fps 必须是有限值")
        if number < 1:
            raise ValueError("sample_fps 必须为1到2")
        if number > MAX_SAMPLE_FPS:
            raise ValueError("sample_fps 不能超过 %g" % MAX_SAMPLE_FPS)
        return number

    @field_validator("max_duration_seconds")
    @classmethod
    def _max_duration_domain(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("max_duration_seconds 必须是数值")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("max_duration_seconds 必须是有限值")
        if number <= 0:
            raise ValueError("max_duration_seconds 必须大于 0")
        if number > MAX_DURATION_LIMIT:
            raise ValueError("max_duration_seconds 不能超过 %g" % MAX_DURATION_LIMIT)
        return number

    @field_validator("probe_timeout_seconds")
    @classmethod
    def _probe_timeout_domain(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("probe_timeout_seconds 必须是数值")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("probe_timeout_seconds 必须是有限值")
        if number <= 0:
            raise ValueError("probe_timeout_seconds 必须大于 0")
        if number > MAX_PROBE_TIMEOUT:
            raise ValueError("probe_timeout_seconds 不能超过 %g" % MAX_PROBE_TIMEOUT)
        return number

    @field_validator("max_frames")
    @classmethod
    def _max_frames_domain(cls, value: int) -> int:
        # pydantic v2 严格模式下不接受 bool/字符串；此处再显式检查定义域
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_frames 必须是整数")
        if value < 1:
            raise ValueError("max_frames 必须大于等于 1")
        if value > MAX_FRAME_LIMIT:
            raise ValueError("max_frames 不能超过 %d" % MAX_FRAME_LIMIT)
        return value

    @field_validator("crop_black_borders")
    @classmethod
    def _bool_only(cls, value: bool) -> bool:
        if not isinstance(value, bool):
            raise ValueError("crop_black_borders 必须是布尔值")
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
