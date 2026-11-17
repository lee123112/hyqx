"""算子 10 数据契约：部署配置、请求覆盖、请求模型与格式白名单。

设计约束（TECHNICAL_DESIGN §2、表1 逐字段冻结摘录）：
- ``operator`` 与 ``stage_id`` 是部署配置，不接受请求改写血缘身份；
- 表1 只读项（授权要求“必须是可训练”、格式白名单）不接受请求覆盖，请求携带即报错；
- 未知字段一律报错（``extra="forbid"``），不静默采用另一套阈值；
- 阈值/数量分别校验定义域：模态数必须是 2–4 的整数，描述字数必须是正整数且上限大于下限；
- 本算子**没有模型与权重**（纯规则准入核对），配置中不存在权重路径/后端选择等模型字段，
  也不产生任何模型分数。
"""
from __future__ import annotations

import math
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
DEPLOYMENT_KEYS = ("operator", "stage_id")

#: 表1 只读配置项：请求不可覆盖
READONLY_KEYS = ("required_license", "trainable_license_values", "allowed_extensions")

#: 逐模态允许格式（表1 白名单 ∪ demo 共享 DATA_SCHEMA 白名单）
TEXT_EXTENSIONS = ("txt", "json", "csv")
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "bmp", "gif")
VIDEO_EXTENSIONS = ("mp4", "mov", "avi", "mkv")
AUDIO_EXTENSIONS = ("wav", "mp3", "flac", "aiff", "aif", "m4a")

MODALITY_EXTENSIONS: Dict[str, tuple] = {
    "text": TEXT_EXTENSIONS,
    "image": IMAGE_EXTENSIONS,
    "video": VIDEO_EXTENSIONS,
    "audio": AUDIO_EXTENSIONS,
}

#: 默认允许扩展名（不含点号）
DEFAULT_ALLOWED_EXTENSIONS = [
    extension for extensions in MODALITY_EXTENSIONS.values() for extension in extensions
]

#: 文件检查模式：header=纯 Python 头签名；decode=追加真实解码校验
FILE_CHECK_MODES = ("header", "decode")

#: 表1 只读授权要求
REQUIRED_LICENSE = "可训练"


class ConfigError(ValueError):
    """配置结构、定义域或枚举非法。"""


def _check_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数（不接受字符串/布尔）" % name)
    if value <= 0:
        raise ValueError("%s 必须为正整数" % name)
    return value


def _check_non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数（不接受字符串/布尔）" % name)
    if value < 0:
        raise ValueError("%s 不能为负数" % name)
    return value


def _check_min_modalities(value: Any) -> int:
    number = _check_positive_int(value, "min_modalities")
    if not 2 <= number <= 4:
        raise ValueError("min_modalities 必须取 2 到 4（表1：至少 2 种模态，可选 2–4）")
    return number


def _check_extension_list(value: Any, name: str) -> List[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("%s 必须是非空字符串数组" % name)
    cleaned: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("%s 元素必须是非空字符串" % name)
        cleaned.append(item.strip().lstrip(".").lower())
    return cleaned


def _check_str_list(value: Any, name: str) -> List[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("%s 必须是非空字符串数组" % name)
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("%s 元素必须是非空字符串" % name)
    return [item.strip() for item in value]


def _check_file_check_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in FILE_CHECK_MODES:
        raise ValueError("file_check_mode 只能是 %s" % " 或 ".join(FILE_CHECK_MODES))
    return value


class StrictBaseModel(BaseModel):
    """禁止未知字段：部署配置和请求体都不接受额外键。"""

    model_config = ConfigDict(extra="forbid")


class OperatorIdentity(StrictBaseModel):
    """算子身份，来自部署 YAML，不由请求覆盖。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class OperatorConfig(StrictBaseModel):
    """算子 10 部署配置（configs/config.yaml）。"""

    operator: OperatorIdentity
    stage_id: str = Field(default="S3", min_length=1)

    #: 至少要有几种模态（表1：默认 2，可选 2–4）
    min_modalities: int = Field(default=2, ge=2, le=4)
    #: 描述最短字数：短于即视为没有有效描述，退回 #9（表1 #9 默认 5）
    min_description_chars: int = 5
    #: 描述最长字数：超出说明 #9 未按 500 字截断，退回 #9（表1 #9 默认 500）
    max_description_chars: int = 500
    #: 单条允许退回重跑次数上限；达到上限的下一次进入样本隔离库并锁定（规则 §2.3）
    max_return_count: int = 2
    #: 是否要求来源 ID 与批次号齐备（表1 #10：来源ID、批次号都要能核对）
    require_source_and_batch: StrictBool = True
    #: 文件可打开检查模式：header=头签名（无第三方依赖）；decode=叠加真实解码
    file_check_mode: str = "header"
    #: decode 模式下解码器缺失时是否允许降级为头签名（降级结果显式标记 header_only）
    allow_degraded: StrictBool = False

    #: 表1 只读：授权要求固定为“可训练”，这里不能改数据的授权结论
    required_license: str = REQUIRED_LICENSE
    #: 上游授权字段的等价取值（用于归一化，不改变“必须可训练”的口径）
    trainable_license_values: List[str] = Field(default_factory=lambda: [REQUIRED_LICENSE,
                                                                       "trainable"])
    #: 允许的媒体扩展名（表1∪demo 共享 schema）
    allowed_extensions: List[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_EXTENSIONS))

    @field_validator("stage_id")
    @classmethod
    def _stage_id_not_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("stage_id 不能为空")
        return value.strip()

    @field_validator("min_modalities", mode="before")
    @classmethod
    def _valid_min_modalities(cls, value):
        return _check_min_modalities(value)

    @field_validator("min_description_chars", "max_description_chars", mode="before")
    @classmethod
    def _valid_description_chars(cls, value, info):
        return _check_positive_int(value, info.field_name)

    @field_validator("max_return_count", mode="before")
    @classmethod
    def _valid_max_return_count(cls, value):
        return _check_non_negative_int(value, "max_return_count")

    @field_validator("file_check_mode", mode="before")
    @classmethod
    def _valid_file_check_mode(cls, value):
        return _check_file_check_mode(value)

    @field_validator("required_license")
    @classmethod
    def _valid_required_license(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("required_license 不能为空")
        return value.strip()

    @field_validator("trainable_license_values", mode="before")
    @classmethod
    def _valid_trainable_values(cls, value):
        return _check_str_list(value, "trainable_license_values")

    @field_validator("allowed_extensions", mode="before")
    @classmethod
    def _valid_allowed_extensions(cls, value):
        return _check_extension_list(value, "allowed_extensions")

    @model_validator(mode="after")
    def _check_description_range(self) -> "OperatorConfig":
        if self.max_description_chars <= self.min_description_chars:
            raise ValueError("max_description_chars 必须大于 min_description_chars")
        return self

    def business_dict(self) -> Dict[str, Any]:
        """血缘里记录的业务参数快照（不含部署身份）。"""
        return {
            "min_modalities": self.min_modalities,
            "min_description_chars": self.min_description_chars,
            "max_description_chars": self.max_description_chars,
            "max_return_count": self.max_return_count,
            "require_source_and_batch": self.require_source_and_batch,
            "file_check_mode": self.file_check_mode,
            "allow_degraded": self.allow_degraded,
            "required_license": self.required_license,
            "trainable_license_values": list(self.trainable_license_values),
            "allowed_extensions": list(self.allowed_extensions),
        }

    def deployment_dict(self) -> Dict[str, Any]:
        return {
            "operator": self.operator.model_dump(),
            "stage_id": self.stage_id,
        }


class BusinessOverrides(StrictBaseModel):
    """请求 ``config`` 允许覆盖的业务参数子集。

    携带 ``operator``/``stage_id``/``required_license`` 等只读键（或任何未知键）
    会直接校验失败，请求无法改写血缘身份与表1 只读口径。
    """

    min_modalities: Optional[int] = None
    min_description_chars: Optional[int] = None
    max_description_chars: Optional[int] = None
    max_return_count: Optional[int] = None
    require_source_and_batch: Optional[StrictBool] = None
    file_check_mode: Optional[str] = None
    allow_degraded: Optional[StrictBool] = None

    @field_validator("min_modalities", mode="before")
    @classmethod
    def _valid_min_modalities(cls, value):
        return None if value is None else _check_min_modalities(value)

    @field_validator("min_description_chars", "max_description_chars", mode="before")
    @classmethod
    def _valid_description_chars(cls, value, info):
        return None if value is None else _check_positive_int(value, info.field_name)

    @field_validator("max_return_count", mode="before")
    @classmethod
    def _valid_max_return_count(cls, value):
        return None if value is None else _check_non_negative_int(value, "max_return_count")

    @field_validator("file_check_mode", mode="before")
    @classmethod
    def _valid_file_check_mode(cls, value):
        return None if value is None else _check_file_check_mode(value)

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
