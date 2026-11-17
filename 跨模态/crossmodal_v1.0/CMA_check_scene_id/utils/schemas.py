"""算子 #02 数据契约：部署配置、请求模型与定义域校验。

设计约束（DEVELOPMENT_PLAN §3、TECHNICAL_DESIGN §2–§4、表1 #02 冻结口径）：
- ``operator`` / ``stage_id`` / ``transport`` 是部署身份与传输约定，请求不可改写；
- 表1 固定口径（``bind_once`` 只绑定一次、设备并发与故障切换只读采集平台结果）
  属于只读项，请求携带同名键直接报错；
- 未知字段一律报错（``extra="forbid"``），不静默采用另一套阈值；
- 本算子**无模型、无权重**（表1 模型列 ``--``），配置中不存在模型路径/后端字段，
  也不产生任何模型分数或降级分。

与表1 页面控件一致、允许任务级覆盖的业务参数只有三个：
``enabled``（总开关，默认关；融合集任务打开）、``scene_id_source``（场景编号用谁的）、
``min_modalities``（同一场景至少几种模态，2–4）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
)

from utils.scene import (
    DEFAULT_MIN_MODALITIES,
    DEVICE_CONTROL_READ_ONLY,
    MIN_MODALITIES_RANGE,
    SCENE_ID_SOURCES,
)
from utils.transport import FILE_OBJECT_FORMAT

#: 部署身份键：请求 config 携带同名键会被直接拒绝
DEPLOYMENT_KEYS = ("operator", "stage_id", "transport")

#: [v4.2] §4.2 规范取值 ``synthetic`` = 本产线补一个场景编号；
#: 实现内部沿用历史命名 ``generate``（输出 scene_source=supplemented），此处做别名归一。
SCENE_ID_SOURCE_ALIASES = {"synthetic": "generate"}


def _normalize_scene_source(value: Any) -> Any:
    if isinstance(value, str):
        return SCENE_ID_SOURCE_ALIASES.get(value.strip().lower(), value)
    return value


#: 表1 只读配置项：请求不可覆盖（含「只绑定一次」和「设备只读」两条冻结口径）
READONLY_KEYS = ("operator", "stage_id", "transport", "bind_once", "device_control")


class ConfigError(ValueError):
    """配置结构、定义域或枚举非法。"""


def _check_int(value: Any, name: str, *, minimum: int, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数（不接受字符串/布尔/浮点）" % name)
    if value < minimum:
        raise ValueError("%s 不能小于 %d" % (name, minimum))
    if maximum is not None and value > maximum:
        raise ValueError("%s 不能大于 %d" % (name, maximum))
    return value


def _check_literal(value: Any, name: str, expected: str) -> str:
    if value != expected:
        raise ValueError("%s 由表1 固定为 %s，不可改" % (name, expected))
    return expected


def _check_choice(value: Any, name: str, allowed) -> str:
    if value not in allowed:
        raise ValueError("%s 非法：%r（可选 %s）" % (name, value, " / ".join(allowed)))
    return value


class StrictBaseModel(BaseModel):
    """禁止未知字段：部署配置和请求体都不接受额外键。"""

    model_config = ConfigDict(extra="forbid")


class OperatorIdentity(StrictBaseModel):
    """算子身份，来自部署 YAML，不由请求覆盖。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class TransportConfig(StrictBaseModel):
    """传输层约定（demo 格式，暂不接受请求改写）。"""

    format: str = FILE_OBJECT_FORMAT
    max_response_bytes: int = 67108864

    @field_validator("format", mode="before")
    @classmethod
    def _valid_format(cls, value):
        return _check_literal(value, "transport.format", FILE_OBJECT_FORMAT)

    @field_validator("max_response_bytes", mode="before")
    @classmethod
    def _valid_limit(cls, value):
        return _check_int(value, "transport.max_response_bytes", minimum=1024)


class OperatorConfig(StrictBaseModel):
    """算子 #02 部署配置（configs/config.yaml）。"""

    operator: OperatorIdentity
    stage_id: str = Field(default="S1", min_length=1)

    #: 表1 页面「模板上的总开关」：默认关；配对集保持关闭，融合集任务打开
    enabled: StrictBool = False

    #: 表1 页面「场景编号用谁的」：沿用采集平台已有编号 / 本产线补一个编号
    scene_id_source: str = SCENE_ID_SOURCES[0]

    #: 表1 页面「同一场景至少几种模态」：默认 2，可选 2–4
    min_modalities: int = DEFAULT_MIN_MODALITIES

    #: 表1 只读：无编号时补的编号只绑定一次（不复用、不重复绑定）
    bind_once: StrictBool = True

    #: 表1 只读：设备并发与故障切换只读采集平台结果，本算子不调度设备
    device_control: str = DEVICE_CONTROL_READ_ONLY

    transport: TransportConfig = Field(default_factory=TransportConfig)

    @field_validator("stage_id")
    @classmethod
    def _stage_id_not_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("stage_id 不能为空")
        return value.strip()

    @field_validator("enabled", mode="before")
    @classmethod
    def _strict_boolean(cls, value):
        if not isinstance(value, bool):
            raise ValueError("enabled 必须是严格布尔值")
        return value

    @field_validator("scene_id_source", mode="before")
    @classmethod
    def _valid_source(cls, value):
        return _check_choice(_normalize_scene_source(value), "scene_id_source",
                             SCENE_ID_SOURCES)

    @field_validator("min_modalities", mode="before")
    @classmethod
    def _valid_min_modalities(cls, value):
        return _check_int(value, "min_modalities",
                          minimum=MIN_MODALITIES_RANGE[0], maximum=MIN_MODALITIES_RANGE[1])

    @field_validator("bind_once", mode="before")
    @classmethod
    def _bind_once_always_true(cls, value):
        if value is not True:
            raise ValueError("表1 固定「只绑定一次」，bind_once 不能为 false")
        return True

    @field_validator("device_control", mode="before")
    @classmethod
    def _device_control_read_only(cls, value):
        return _check_literal(value, "device_control", DEVICE_CONTROL_READ_ONLY)

    def business_dict(self) -> Dict[str, Any]:
        """血缘里记录的业务参数快照（不含部署身份）。"""
        return {
            "enabled": bool(self.enabled),
            "scene_id_source": self.scene_id_source,
            "min_modalities": self.min_modalities,
            "bind_once": True,
            "device_control": self.device_control,
            "transport": {"format": self.transport.format,
                          "max_response_bytes": self.transport.max_response_bytes},
        }

    def deployment_dict(self) -> Dict[str, Any]:
        return {
            "operator": self.operator.model_dump(),
            "stage_id": self.stage_id,
        }


class BusinessOverrides(StrictBaseModel):
    """请求 ``config`` 允许覆盖的业务参数子集（表1 三个页面控件）。

    携带 ``operator``/``stage_id``/``transport``/``bind_once``/``device_control``
    或任何未知键会直接校验失败，请求无法改写血缘身份与表1 固定口径。
    """

    enabled: Optional[StrictBool] = None
    scene_id_source: Optional[str] = None
    min_modalities: Optional[int] = None

    @field_validator("scene_id_source", mode="before")
    @classmethod
    def _valid_source(cls, value):
        if value is None:
            return None
        return _check_choice(_normalize_scene_source(value), "scene_id_source",
                             SCENE_ID_SOURCES)

    @field_validator("min_modalities", mode="before")
    @classmethod
    def _valid_min_modalities(cls, value):
        if value is None:
            return None
        return _check_int(value, "min_modalities",
                          minimum=MIN_MODALITIES_RANGE[0],
                          maximum=MIN_MODALITIES_RANGE[1])

    def provided(self) -> Dict[str, Any]:
        """只返回请求显式给出的参数。"""
        data = self.model_dump(exclude_none=True)
        return {key: value for key, value in data.items() if value is not None}


class PredictRequest(BaseModel):
    """``POST /predict`` 请求体；未知字段直接校验失败。"""

    model_config = ConfigDict(extra="forbid")

    uniqueId: str
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
