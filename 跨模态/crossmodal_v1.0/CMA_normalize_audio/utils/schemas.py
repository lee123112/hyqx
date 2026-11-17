"""配置模型、请求模型与音频业务对象（Pydantic v2 严格校验）。

约束（TECHNICAL_DESIGN §2、DEVELOPMENT_CONTRACT §2）：
- ``operator`` 与 ``stage_id`` 是部署配置，请求 ``config`` 只能覆盖业务参数；
- 业务参数校验定义域与有限值，``clip_start``/``clip_end`` 要么都给要么都不给；
- 未知字段一律报错（``extra="forbid"``），不静默采用另一套算法；
- ``isLastOperator`` 必须是严格 bool，字符串 ``"false"`` 必须被拒绝。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

#: 支持的目标采样率定义域（Hz）
SAMPLE_RATE_MIN = 8000
SAMPLE_RATE_MAX = 192000
#: 支持的目标声道数
CHANNELS_ALLOWED = (1, 2)
#: 支持的 PCM 位宽
PCM_BITS_ALLOWED = (8, 16, 24, 32)
#: 单条音频最大时长（秒）
MAX_DURATION_LIMIT = 3600.0
#: 子进程超时上限（秒）
PROCESS_TIMEOUT_LIMIT = 300.0


class ConfigError(ValueError):
    """配置非法（结构、定义域、未知字段）。"""


class StrictBaseModel(BaseModel):
    """禁止未知字段：避免拼写错误的配置被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class OperatorIdentity(StrictBaseModel):
    """部署身份，不允许请求改写。"""

    name: str
    version: str

    @field_validator("name", "version")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("operator.name/version 不能为空")
        return value


class OperatorConfig(StrictBaseModel):
    """完整配置：部署身份 + 业务参数（configs/config.yaml 的结构）。"""

    operator: OperatorIdentity
    stage_id: str
    enabled: StrictBool = False
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    pcm_bits: Literal[16] = 16
    max_duration_seconds: float = 60.0
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    process_timeout_seconds: float = 30.0

    @field_validator("stage_id")
    @classmethod
    def _check_stage(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("stage_id 不能为空")
        return value

    @field_validator("sample_rate", mode="before")
    @classmethod
    def _check_sample_rate(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("sample_rate 必须是整数")
        if not SAMPLE_RATE_MIN <= value <= SAMPLE_RATE_MAX:
            raise ValueError("sample_rate 必须在 %d-%d 之间" % (SAMPLE_RATE_MIN, SAMPLE_RATE_MAX))
        return value

    @field_validator("channels", mode="before")
    @classmethod
    def _check_channels(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("channels 必须是整数")
        if value not in CHANNELS_ALLOWED:
            raise ValueError("channels 只支持 %s" % (CHANNELS_ALLOWED,))
        return value

    @field_validator("pcm_bits", mode="before")
    @classmethod
    def _check_pcm_bits(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("pcm_bits 必须是整数")
        if value not in PCM_BITS_ALLOWED:
            raise ValueError("pcm_bits 只支持 %s" % (PCM_BITS_ALLOWED,))
        return value

    @field_validator("max_duration_seconds", mode="before")
    @classmethod
    def _check_max_duration(cls, value: Any) -> Any:
        number = _as_finite_float(value, "max_duration_seconds")
        if not 0.0 < number <= MAX_DURATION_LIMIT:
            raise ValueError("max_duration_seconds 必须在 (0, %g] 之间" % MAX_DURATION_LIMIT)
        return number

    @field_validator("process_timeout_seconds", mode="before")
    @classmethod
    def _check_timeout(cls, value: Any) -> Any:
        number = _as_finite_float(value, "process_timeout_seconds")
        if not 0.0 < number <= PROCESS_TIMEOUT_LIMIT:
            raise ValueError("process_timeout_seconds 必须在 (0, %g] 之间" % PROCESS_TIMEOUT_LIMIT)
        return number

    @field_validator("ffmpeg_path", "ffprobe_path")
    @classmethod
    def _check_executable(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("ffmpeg_path/ffprobe_path 不能为空")
        if "\x00" in value:
            raise ValueError("ffmpeg_path/ffprobe_path 不能包含 NUL")
        return value

    def business_dict(self) -> Dict[str, Any]:
        """仅业务参数（用于血缘 config 与日志，不含部署身份）。"""
        return {
            "enabled": self.enabled,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "pcm_bits": self.pcm_bits,
            "max_duration_seconds": self.max_duration_seconds,
            "ffmpeg_path": self.ffmpeg_path,
            "ffprobe_path": self.ffprobe_path,
            "process_timeout_seconds": self.process_timeout_seconds,
        }


class BusinessOverrides(StrictBaseModel):
    """请求 ``config`` 允许覆盖的业务参数（全部可选、拒绝部署身份字段）。

    整数字段用 ``StrictInt``、浮点字段经 ``_strict_number`` 前置校验，
    拒绝 ``"16000"`` 这类数字字符串与 bool，避免宽松强制转换掩盖客户端错误。
    """

    enabled: Optional[StrictBool] = None
    sample_rate: Optional[Literal[16000]] = None
    channels: Optional[Literal[1]] = None
    pcm_bits: Optional[Literal[16]] = None

    def provided(self) -> Dict[str, Any]:
        """仅返回请求中显式给出的键。

        只允许总开关和表1固定的采样率、声道、位宽；部署项不能由请求覆盖。
        """
        return dict(self.model_dump(exclude_unset=True))


class PredictRequest(StrictBaseModel):
    """``POST /predict`` 请求体：``isLastOperator`` 必须是严格 bool。"""

    uniqueId: str
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Optional[Dict[str, Any]] = None

    @field_validator("uniqueId")
    @classmethod
    def _check_unique_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("uniqueId 不能为空")
        return value


class AudioObject(StrictBaseModel):
    """输出 audio 业务子对象（DATA_SCHEMA §7.1）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    sample_rate: int
    channels: int
    duration: float
    segments: List[Dict[str, Any]] = Field(default_factory=list)
    original_path: Optional[str] = None
    pcm_bits: int = 16
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    clipped: bool = False
    decoder_backend: str = "wave"

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("audio.path 不能为空")
        return value

    @field_validator("duration")
    @classmethod
    def _check_duration(cls, value: float) -> float:
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            raise ValueError("audio.duration 必须是非负有限值")
        return number


def _as_finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s 必须是数值" % name)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("%s 必须是有限值" % name)
    return number
