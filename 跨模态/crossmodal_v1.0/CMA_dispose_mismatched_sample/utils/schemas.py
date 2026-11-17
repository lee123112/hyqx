"""算子 21 数据契约：部署配置、请求覆盖、请求模型与定义域校验。

设计约束（DEVELOPMENT_PLAN §3、TECHNICAL_DESIGN §2–§4、表1 #21 冻结口径）：
- ``operator`` / ``stage_id`` / ``enabled`` 是部署身份与表1 开关，请求不可改写；
- 表1 固定口径（原始文件问题进原始隔离区、治理不达标进样本隔离库、评分分层不进隔离区）
  属于只读项，请求携带同名键直接报错；
- 未知字段一律报错（``extra="forbid"``），不静默采用另一套阈值；
- 本算子**无模型、无权重**（纯规则），配置中不存在模型路径/后端字段，
  也不产生任何模型分数。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

#: 部署身份键：请求 config 携带同名键会被直接拒绝
DEPLOYMENT_KEYS = ("operator", "stage_id", "enabled", "transport")

#: 表1 只读配置项：请求不可覆盖
READONLY_KEYS = ("operator", "stage_id", "enabled", "raw_issue_destination",
                 "quality_issue_destination", "scoring_tiers_not_isolated", "transport")

#: 表1 固定去向字面量
RAW_ISOLATION = "raw_isolation"
SAMPLE_ISOLATION = "sample_isolation"

#: 文件对象格式
FILE_OBJECT_FORMAT = "crossmodal-files-v1"

#: 默认嵌入台账的原记录字节上限
DEFAULT_MAX_EMBEDDED_RECORD_BYTES = 32768


class ConfigError(ValueError):
    """配置结构、定义域或枚举非法。"""


def _check_int(value: Any, name: str, *, minimum: int, allow_zero: bool = True,
               maximum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数（不接受字符串/布尔）" % name)
    if value < minimum:
        raise ValueError("%s 不能小于 %d" % (name, minimum))
    if not allow_zero and value == 0:
        raise ValueError("%s 必须大于 0" % name)
    if maximum is not None and value > maximum:  # [v4.2] 支持上限校验（config 对齐）
        raise ValueError("%s 不能大于 %d" % (name, maximum))
    return value


def _check_fixed(value: Any, name: str, expected: str) -> str:
    if value != expected:
        raise ValueError("%s 由表1 固定为 %s，不可改" % (name, expected))
    return expected


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
        return _check_fixed(value, "transport.format", FILE_OBJECT_FORMAT)

    @field_validator("max_response_bytes", mode="before")
    @classmethod
    def _valid_limit(cls, value):
        return _check_int(value, "transport.max_response_bytes", minimum=1024)


class OperatorConfig(StrictBaseModel):
    """算子 21 部署配置（configs/config.yaml）。"""

    operator: OperatorIdentity
    stage_id: str = Field(default="S4", min_length=1)  # [v4.2] §0.3：M21→S4

    #: 表1 #21 是“必须开启”的工具，置 false 直接配置失败
    enabled: StrictBool = True

    #: 单条允许退回重跑次数上限（表1：单条最多退回重跑 1 次）
    return_limit: int = 1
    #: 全产线单条最多重跑次数；再退回即第 3 次 → 锁定样本隔离库
    max_retry_per_item: int = 2

    #: 表1 只读：原始文件问题去向
    raw_issue_destination: str = RAW_ISOLATION
    #: 表1 只读：治理不达标（业务问题）去向
    quality_issue_destination: str = SAMPLE_ISOLATION
    #: 表1 只读：评分分层/流程状态不进隔离区
    scoring_tiers_not_isolated: StrictBool = True

    #: 人工复核平台是否已接入；未接入时需人工确认的记录进入 waiting_manual
    manual_platform_available: StrictBool = False
    #: 人工平台不可用时是否允许显式降级（false → 结构化失败 DependencyError）
    allow_degraded: StrictBool = False

    #: 台账条目内嵌原记录的字节上限（超出截断并标记，不丢台账条目本身）
    max_embedded_record_bytes: int = DEFAULT_MAX_EMBEDDED_RECORD_BYTES

    transport: TransportConfig = Field(default_factory=TransportConfig)

    @field_validator("stage_id")
    @classmethod
    def _stage_id_not_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("stage_id 不能为空")
        return value.strip()

    @field_validator("enabled", mode="before")
    @classmethod
    def _must_be_enabled(cls, value):
        if value is not True:
            raise ValueError("表1 #21 为必须开启的工具，enabled 不能为 false")
        return True

    @field_validator("return_limit", mode="before")
    @classmethod
    def _valid_return_limit(cls, value):
        return _check_int(value, "return_limit", minimum=0)

    @field_validator("max_retry_per_item", mode="before")
    @classmethod
    def _valid_max_retry(cls, value):
        # [v4.2] config 对齐 §4.21：max_retry 默认 2，范围 1–2（第 3 次锁定样本隔离库）
        return _check_int(value, "max_retry_per_item", minimum=1, allow_zero=False,
                          maximum=2)

    @field_validator("raw_issue_destination", mode="before")
    @classmethod
    def _valid_raw_destination(cls, value):
        return _check_fixed(value, "raw_issue_destination", RAW_ISOLATION)

    @field_validator("quality_issue_destination", mode="before")
    @classmethod
    def _valid_sample_destination(cls, value):
        return _check_fixed(value, "quality_issue_destination", SAMPLE_ISOLATION)

    @field_validator("max_embedded_record_bytes", mode="before")
    @classmethod
    def _valid_embedded_limit(cls, value):
        return _check_int(value, "max_embedded_record_bytes", minimum=64, allow_zero=False)

    @model_validator(mode="after")
    def _check_budget_relation(self) -> "OperatorConfig":
        if self.return_limit > self.max_retry_per_item:
            raise ValueError("return_limit 不能大于 max_retry_per_item"
                             "（单条退回次数不能超过全产线重跑上限）")
        return self

    def business_dict(self) -> Dict[str, Any]:
        """血缘里记录的业务参数快照（不含部署身份）。"""
        return {
            "enabled": True,
            "return_limit": self.return_limit,
            "max_retry_per_item": self.max_retry_per_item,
            "raw_issue_destination": self.raw_issue_destination,
            "quality_issue_destination": self.quality_issue_destination,
            "scoring_tiers_not_isolated": self.scoring_tiers_not_isolated,
            "manual_platform_available": self.manual_platform_available,
            "allow_degraded": self.allow_degraded,
            "max_embedded_record_bytes": self.max_embedded_record_bytes,
            "transport": {"format": self.transport.format,
                          "max_response_bytes": self.transport.max_response_bytes},
        }

    def deployment_dict(self) -> Dict[str, Any]:
        return {
            "operator": self.operator.model_dump(),
            "stage_id": self.stage_id,
        }


class BusinessOverrides(StrictBaseModel):
    """请求 ``config`` 允许覆盖的业务参数子集。

    携带 ``operator``/``stage_id``/``enabled``/表1 只读去向键或任何未知键
    会直接校验失败，请求无法改写血缘身份与表1 固定口径。
    """

    return_limit: Optional[int] = None
    max_retry_per_item: Optional[int] = None
    manual_platform_available: Optional[StrictBool] = None
    allow_degraded: Optional[StrictBool] = None
    max_embedded_record_bytes: Optional[int] = None

    @field_validator("return_limit", mode="before")
    @classmethod
    def _valid_return_limit(cls, value):
        return None if value is None else _check_int(value, "return_limit", minimum=0)

    @field_validator("max_retry_per_item", mode="before")
    @classmethod
    def _valid_max_retry(cls, value):
        # [v4.2] config 对齐 §4.21：max_retry 默认 2，范围 1–2
        return None if value is None else _check_int(value, "max_retry_per_item",
                                                     minimum=1, allow_zero=False,
                                                     maximum=2)

    @field_validator("max_embedded_record_bytes", mode="before")
    @classmethod
    def _valid_embedded_limit(cls, value):
        return None if value is None else _check_int(value, "max_embedded_record_bytes",
                                                     minimum=64, allow_zero=False)

    @field_validator("manual_platform_available", "allow_degraded", mode="before")
    @classmethod
    def _valid_bool(cls, value):
        if value is None:
            return None
        if isinstance(value, bool) is False:
            raise ValueError("该参数必须是严格布尔值")
        return value

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


class RemoveFileRequest(BaseModel):
    """保留类型定义以便文档说明；HTTP 层按契约从 query 读取 uniqueId。"""

    uniqueId: str
