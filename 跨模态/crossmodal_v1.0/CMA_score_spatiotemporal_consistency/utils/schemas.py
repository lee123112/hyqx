"""配置与请求模型（Pydantic v2，严格布尔 + 定义域校验）。

表1 #15 冻结口径：
- 默认关闭；这批数据没有音视频时间戳时不应打开；
- 打开后用**线性插值**核验，画面/声音/字幕时间差默认不超过 0.5 秒（可配 0.2–2.0）；
- 超过阈值 → 样本隔离库；没有时间戳的图文 → 不适用（不隔离，也不能写成时序已通过）；
- 纯图文不执行时序判定。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, field_validator,
                      model_validator)

DEFAULT_OPERATOR_NAME = "temporal_consistency"
STAGE_ID = "S3"
#: 表1 正式编号（单一来源：配置校验、业务常量与 map.json 均以此为准）
FORMAL_OPERATOR_NUMBER = 15

#: 表1 参数 2 的定义域（单位：秒）
MAX_DELTA_MIN = 0.2
MAX_DELTA_MAX = 2.0

#: 插值方法：表1 固定为线性；piecewise 为默认实现，global 为全局最小二乘
INTERPOLATION_METHODS = ("piecewise_linear", "global_linear")
#: 表1 固定插值类型，不允许被请求替换
FIXED_INTERPOLATION = "linear"

MIN_ANCHORS_MIN = 2
MIN_ANCHORS_MAX = 100
MAX_BINDINGS_MIN = 1
MAX_BINDINGS_MAX = 100000
MAX_EVIDENCE_MIN = 0
MAX_EVIDENCE_MAX = 1000

#: 请求不得覆盖的部署/口径键（表1 固定值或部署身份）
READONLY_KEYS = ("operator", "stage_id", "interpolation")


class ConfigError(ValueError):
    """配置非法。"""


def _range(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s 必须是数字" % name)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigError("%s 必须是有限数字" % name)
    if number < low or number > high:
        raise ConfigError("%s 必须在 %.2f–%.2f 之间，实际 %s" % (name, low, high, value))
    return number


def _int_in(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("%s 必须是整数" % name)
    if value < low or value > high:
        raise ConfigError("%s 必须在 %d–%d 之间，实际 %s" % (name, low, high, value))
    return value


class StrictBaseModel(BaseModel):
    """禁止未知字段：未知键一律失败，避免静默忽略配置错误。"""

    model_config = ConfigDict(extra="forbid")


class OperatorIdentity(StrictBaseModel):
    """配置里的算子身份：编号固定为表1 #15，name 为英文 id（中文名见 map.json）。"""

    number: int = FORMAL_OPERATOR_NUMBER
    name: str
    version: str

    @field_validator("number", mode="before")
    @classmethod
    def _valid_number(cls, value: Any) -> int:
        number = _int_in(value, "operator.number", 1, 999)
        if number != FORMAL_OPERATOR_NUMBER:
            raise ConfigError("operator.number 固定为 %d（表1 #15），实际 %s"
                              % (FORMAL_OPERATOR_NUMBER, value))
        return number

    @field_validator("version", mode="before")
    @classmethod
    def _valid_version(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ConfigError("operator.version 不能为空")
        return text


class OperatorConfig(StrictBaseModel):
    """算子完整配置（YAML 快照的结构化模型）。"""

    operator: OperatorIdentity = Field(
        default_factory=lambda: OperatorIdentity(name=DEFAULT_OPERATOR_NAME, version="v1.0.0"))
    stage_id: str = STAGE_ID

    # 表1 总开关：默认关
    enabled: StrictBool = False

    # 表1 参数 1：画面/声音/字幕最多差几秒（0.2–2.0，默认 0.5）
    max_time_delta_sec: float = 0.5

    # 表1 固定插值类型（仅允许 linear）
    interpolation: str = FIXED_INTERPOLATION
    # 线性插值的具体实现：分段线性（默认）或全局最小二乘线性
    interpolation_method: str = "piecewise_linear"
    # 少于该锚点数视为“无可用校准”（显式记录，不伪造校准结果）
    min_anchors_for_calibration: int = 2
    # true 时，缺少足够锚点的记录进入待人工校准台账（不隔离、不伪造通过）
    require_calibration: StrictBool = False

    # 资源与输入保护
    max_bindings: int = 5000
    max_evidence_bindings: int = 20

    @field_validator("enabled", "require_calibration", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any, info) -> Any:
        if isinstance(value, bool):
            return value
        raise ConfigError("%s 必须是布尔值 true/false，实际 %r" % (info.field_name, value))

    @field_validator("max_time_delta_sec", mode="before")
    @classmethod
    def _valid_max_delta(cls, value: Any) -> float:
        return _range(value, "max_time_delta_sec", MAX_DELTA_MIN, MAX_DELTA_MAX)

    @field_validator("min_anchors_for_calibration", mode="before")
    @classmethod
    def _valid_min_anchors(cls, value: Any) -> int:
        return _int_in(value, "min_anchors_for_calibration", MIN_ANCHORS_MIN, MIN_ANCHORS_MAX)

    @field_validator("max_bindings", mode="before")
    @classmethod
    def _valid_max_bindings(cls, value: Any) -> int:
        return _int_in(value, "max_bindings", MAX_BINDINGS_MIN, MAX_BINDINGS_MAX)

    @field_validator("max_evidence_bindings", mode="before")
    @classmethod
    def _valid_max_evidence(cls, value: Any) -> int:
        return _int_in(value, "max_evidence_bindings", MAX_EVIDENCE_MIN, MAX_EVIDENCE_MAX)

    @field_validator("interpolation", mode="before")
    @classmethod
    def _valid_interpolation(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text != FIXED_INTERPOLATION:
            raise ConfigError("interpolation 固定为 %s（表1），不允许替换为 %r"
                              % (FIXED_INTERPOLATION, value))
        return FIXED_INTERPOLATION

    @field_validator("interpolation_method", mode="before")
    @classmethod
    def _valid_method(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        # [v4.2] §4.15 接口二规范枚举为 linear/nearest/overlap；本实现的分段线性
        # 即规范中的 linear，故接受 linear 别名并归一为 piecewise_linear。
        # nearest/overlap 尚未实现，仍按配置错误拒绝（算法层待后续版本）。
        text = {"linear": "piecewise_linear"}.get(text, text)
        if text not in INTERPOLATION_METHODS:
            raise ConfigError("interpolation_method 只能是 %s" % "、".join(INTERPOLATION_METHODS))
        return text

    @field_validator("stage_id", mode="before")
    @classmethod
    def _valid_stage(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ConfigError("stage_id 不能为空")
        return text

    @model_validator(mode="after")
    def _check_consistency(self) -> "OperatorConfig":
        if self.operator.name != DEFAULT_OPERATOR_NAME:
            raise ConfigError("operator.name 固定为 %s" % DEFAULT_OPERATOR_NAME)
        if self.require_calibration and self.min_anchors_for_calibration < 2:
            raise ConfigError("require_calibration=true 时 min_anchors_for_calibration 必须 >= 2")
        return self

    def business_dict(self) -> Dict[str, Any]:
        """业务口径快照（不含部署身份），用于血缘 config.business。"""
        return {
            "enabled": bool(self.enabled),
            "max_time_delta_sec": self.max_time_delta_sec,
            "interpolation": self.interpolation,
            "interpolation_method": self.interpolation_method,
            "min_anchors_for_calibration": self.min_anchors_for_calibration,
            "require_calibration": bool(self.require_calibration),
            "max_bindings": self.max_bindings,
            "max_evidence_bindings": self.max_evidence_bindings,
        }

    def deployment_dict(self) -> Dict[str, Any]:
        """部署身份快照，用于血缘 config.deployment。"""
        return {
            "operator": {"number": self.operator.number, "name": self.operator.name,
                         "version": self.operator.version},
            "stage_id": self.stage_id,
        }


class BusinessOverrides(StrictBaseModel):
    """``/predict`` 允许覆盖的业务子集；部署身份与固定插值类型不在其中。"""

    enabled: Optional[StrictBool] = None
    max_time_delta_sec: Optional[float] = None
    interpolation_method: Optional[str] = None
    min_anchors_for_calibration: Optional[int] = None
    require_calibration: Optional[StrictBool] = None
    max_bindings: Optional[int] = None
    max_evidence_bindings: Optional[int] = None

    @field_validator("enabled", "require_calibration", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any, info) -> Any:
        if value is None or isinstance(value, bool):
            return value
        raise ConfigError("%s 必须是布尔值 true/false" % info.field_name)

    def provided(self) -> Dict[str, Any]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class PredictRequest(BaseModel):
    """/predict 请求体；``extra=forbid`` 与 ``StrictBool`` 保证形状不可放宽。"""

    model_config = ConfigDict(extra="forbid")

    uniqueId: str
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)


class RemoveFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uniqueId: str
