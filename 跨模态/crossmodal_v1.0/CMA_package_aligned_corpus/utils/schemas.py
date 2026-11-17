"""配置与请求模型（严格校验，越界即失败）。

表1 #23 冻结口径（逐字）：
- 默认开启、**必须**（``enabled`` 置 false 直接配置失败）；
- 类型 = 系统功能，模型列 = ``--``（本算子无任何模型，不产生任何分数）；
- 「压缩成什么格式」默认 ``zip`` 且只有 zip；
- 「什么时候可以从待发布改成可交付」三项**只读**：训练文件已生成、
  质量报告已写回、抽检已写回，缺一不可（请求不可改写）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator, model_validator)

from . import io as io_utils
from . import delivery

__all__ = [
    "ARCHIVE_FORMAT",
    "ConfigError",
    "Envelope",
    "MaterialPaths",
    "OPERATOR_LABEL",
    "OPERATOR_NAME",
    "OPERATOR_NUMBER",
    "OPERATOR_VERSION",
    "OVERRIDABLE_KEYS",
    "OperatorConfig",
    "OperatorMeta",
    "PredictRequest",
    "STAGE_ID",
    "STATUS_AFTER_REVIEW",
    "STATUS_BEFORE_REVIEW",
    "TRANSPORT_FORMAT_REQUIRED",
    "TRANSPORT_MAX_RESPONSE_BYTES",
    "TransportConfig",
    "to_config_error",
    "validate_overrides",
]

OPERATOR_NAME = delivery.OPERATOR
OPERATOR_LABEL = delivery.OPERATOR_LABEL
OPERATOR_NUMBER = delivery.OPERATOR_NUMBER
OPERATOR_VERSION = delivery.OPERATOR_VERSION
STAGE_ID = delivery.STAGE_ID

TRANSPORT_FORMAT_REQUIRED = "crossmodal-files-v1"
TRANSPORT_MAX_RESPONSE_BYTES = 67108864
MIN_TRANSPORT_MAX_RESPONSE_BYTES = 1024

ARCHIVE_FORMAT = "zip"
STATUS_BEFORE_REVIEW = delivery.STATUS_LABEL_PENDING
STATUS_AFTER_REVIEW = delivery.STATUS_LABEL_DELIVERABLE

DEFAULT_MAX_MEDIA_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_EMBEDDED_RECORD_BYTES = 32768

#: 请求级可覆盖键（只允许业务开关；身份/阶段/门禁/路径一律不可覆盖）
OVERRIDABLE_KEYS = ("include_media", "require_deliverable", "require_token")


class ConfigError(ValueError):
    """配置非法（越界、未知键、类型错误）。"""


def _coerce_setting(raw_value: str) -> Any:
    """把 ``settings`` 里的字符串值转成 bool/int/float/str（去掉引号与句末标点）。"""
    text = raw_value.strip().strip('"').strip("'").rstrip("。").strip()
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", ""}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


class OperatorMeta(BaseModel):
    """算子身份。``name``/``id`` 为工程 slug（目录/血缘 ``operator`` 字段），
    ``label`` 为表1/计划中的中文名称。

    兼容性：计划文档里的 ``operator.name: 压缩交付对齐语料包`` 与
    ``operator.settings: ["enabled=true", "archive_format=zip", ...]`` 两种写法
    均可直接使用；``settings`` 会被提升为顶层业务配置。
    """

    model_config = ConfigDict(extra="forbid")

    number: StrictInt = OPERATOR_NUMBER
    id: str = OPERATOR_NAME
    name: str = OPERATOR_NAME
    label: str = OPERATOR_LABEL
    version: str = OPERATOR_VERSION
    enabled: StrictBool = True
    settings: Optional[List[str]] = None

    @field_validator("number")
    @classmethod
    def _check_number(cls, value: int) -> int:
        if int(value) != OPERATOR_NUMBER:
            raise ValueError("operator.number 必须是表1 正式编号 %d" % OPERATOR_NUMBER)
        return int(value)

    @field_validator("id", "name")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        candidate = str(value).strip()
        if candidate not in {OPERATOR_NAME, OPERATOR_LABEL}:
            raise ValueError("operator.id/name 必须是 %r 或 %r"
                             % (OPERATOR_NAME, OPERATOR_LABEL))
        return OPERATOR_NAME if candidate == OPERATOR_LABEL else candidate

    @field_validator("settings")
    @classmethod
    def _check_settings(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("operator.settings 必须是字符串数组")
        return value

    @field_validator("label")
    @classmethod
    def _check_label(cls, value: str) -> str:
        candidate = str(value).strip()
        if not candidate:
            raise ValueError("operator.label 不能为空")
        return candidate

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        candidate = str(value).strip()
        if not candidate:
            raise ValueError("operator.version 不能为空")
        return candidate

    @field_validator("enabled")
    @classmethod
    def _check_enabled(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("operator.enabled 必须为 true：表1 #23 为必须工具")
        return True


class TransportConfig(BaseModel):
    """接口文件内容对象与响应体积约束（计划中 transport 段的落地）。"""

    model_config = ConfigDict(extra="forbid")

    format: str = TRANSPORT_FORMAT_REQUIRED
    max_response_bytes: StrictInt = TRANSPORT_MAX_RESPONSE_BYTES

    @field_validator("format")
    @classmethod
    def _check_format(cls, value: str) -> str:
        candidate = str(value).strip()
        if candidate != TRANSPORT_FORMAT_REQUIRED:
            raise ValueError("transport.format 必须是 %r（禁止改动字段语义）"
                             % TRANSPORT_FORMAT_REQUIRED)
        return candidate

    @field_validator("max_response_bytes")
    @classmethod
    def _check_limit(cls, value: int) -> int:
        if int(value) < MIN_TRANSPORT_MAX_RESPONSE_BYTES:
            raise ValueError("transport.max_response_bytes 不能小于 %d"
                             % MIN_TRANSPORT_MAX_RESPONSE_BYTES)
        return int(value)


class MaterialPaths(BaseModel):
    """材料路径的**部署级**显式覆盖（请求不可覆盖）。

    留空表示自动发现：``<root>/materials/`` → ``<root>/`` → 递归。
    """

    model_config = ConfigDict(extra="forbid")

    training_files: Optional[str] = None
    quality_report: Optional[str] = None
    sampling_conclusion: Optional[str] = None
    isolation_list: Optional[str] = None
    version_notes: Optional[str] = None

    def explicit_paths(self) -> Dict[str, str]:
        """只返回显式给出的角色路径（未配置的角色不出现）。"""
        return {role: value for role, value in
                (("training_files", self.training_files),
                 ("quality_report", self.quality_report),
                 ("sampling_conclusion", self.sampling_conclusion),
                 ("isolation_list", self.isolation_list),
                 ("version_notes", self.version_notes))
                if value}


class OperatorConfig(BaseModel):
    """算子 23 配置。未知键直接失败，避免静默忽略配置错误。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorMeta = Field(default_factory=OperatorMeta)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    stage_id: str = STAGE_ID
    enabled: StrictBool = True
    archive_format: str = ARCHIVE_FORMAT
    exclude_isolated: StrictBool = True
    status_before_review: str = STATUS_BEFORE_REVIEW
    status_after_review: str = STATUS_AFTER_REVIEW
    include_media: StrictBool = True
    require_deliverable: StrictBool = False
    require_token: StrictBool = True
    materials: MaterialPaths = Field(default_factory=MaterialPaths)
    max_media_bytes: StrictInt = DEFAULT_MAX_MEDIA_BYTES
    max_embedded_record_bytes: StrictInt = DEFAULT_MAX_EMBEDDED_RECORD_BYTES

    @field_validator("stage_id")
    @classmethod
    def _check_stage(cls, value: str) -> str:
        candidate = str(value).strip()
        if not candidate:
            raise ValueError("stage_id 不能为空")
        return candidate

    @field_validator("enabled")
    @classmethod
    def _check_enabled(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("enabled 必须为 true：表1 #23 为默认开启且不可跳过的必须工具")
        return True

    @field_validator("archive_format")
    @classmethod
    def _check_archive_format(cls, value: str) -> str:
        candidate = str(value).strip().lower()
        if candidate != ARCHIVE_FORMAT:
            raise ValueError("archive_format 只能是 %r：表1 #23「只有 zip」"
                             % ARCHIVE_FORMAT)
        return candidate

    @field_validator("exclude_isolated")
    @classmethod
    def _check_exclude_isolated(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("exclude_isolated 必须为 true：隔离样本不得进入交付包")
        return True

    @field_validator("status_before_review")
    @classmethod
    def _check_status_before(cls, value: str) -> str:
        candidate = str(value).strip()
        if candidate != STATUS_BEFORE_REVIEW:
            raise ValueError("status_before_review 必须是 %r（表1 只读口径）"
                             % STATUS_BEFORE_REVIEW)
        return candidate

    @field_validator("status_after_review")
    @classmethod
    def _check_status_after(cls, value: str) -> str:
        candidate = str(value).strip()
        if candidate != STATUS_AFTER_REVIEW:
            raise ValueError("status_after_review 必须是 %r（表1 只读口径）"
                             % STATUS_AFTER_REVIEW)
        return candidate

    @field_validator("max_media_bytes")
    @classmethod
    def _check_media_bytes(cls, value: int) -> int:
        if int(value) <= 0:
            raise ValueError("max_media_bytes 必须为正整数")
        return int(value)

    @field_validator("max_embedded_record_bytes")
    @classmethod
    def _check_embedded_bytes(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("max_embedded_record_bytes 不能为负")
        return int(value)

    @model_validator(mode="before")
    @classmethod
    def _lift_operator_settings(cls, data: Any) -> Any:
        """把计划文档里的 ``operator.settings``（``key=value`` 字符串数组）提升为顶层配置。

        若顶层已显式给出同名键，则以顶层为准（显式优先）。
        """
        if not isinstance(data, dict):
            return data
        operator_block = data.get("operator")
        if not isinstance(operator_block, dict):
            return data
        settings = operator_block.get("settings")
        if not isinstance(settings, list):
            return data
        lifted = dict(data)
        for item in settings:
            if not isinstance(item, str) or "=" not in item:
                raise ValueError("operator.settings 每项必须是 key=value：%r" % item)
            key, _, raw_value = item.partition("=")
            key = key.strip()
            if not key:
                raise ValueError("operator.settings 含空键：%r" % item)
            if key in lifted:
                continue
            lifted[key] = _coerce_setting(raw_value)
        return lifted

    @model_validator(mode="after")
    def _check_operator_consistency(self) -> "OperatorConfig":
        """计划里的 ``operator.enabled`` 与顶层 ``enabled`` 必须一致（都为 true）。"""
        if bool(self.operator.enabled) is not bool(self.enabled):
            raise ValueError("operator.enabled 与顶层 enabled 必须一致")
        return self

    # -- 派生属性 ---------------------------------------------------------- #

    @property
    def strict_delivery(self) -> bool:
        """严格模式：不可交付即显式失败（投产前口径校验用）。"""
        return bool(self.require_deliverable)

    def business_snapshot(self) -> Dict[str, Any]:
        """业务配置快照（不含任何凭据、路径、权重）。"""
        return {
            "enabled": bool(self.enabled),
            "archive_format": self.archive_format,
            "exclude_isolated": bool(self.exclude_isolated),
            "status_before_review": self.status_before_review,
            "status_after_review": self.status_after_review,
            "deliverable_requirements": list(delivery.GATE_REQUIREMENTS),
            "include_media": bool(self.include_media),
            "require_deliverable": bool(self.require_deliverable),
            "require_token": bool(self.require_token),
            "max_media_bytes": int(self.max_media_bytes),
            "materials": sorted(self.materials.model_dump().keys()),
            "rule_based": True,
            "model": None,
            "score_fields_produced": False,
        }


class PredictRequest(BaseModel):
    """``POST /predict`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    uniqueId: str
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    overrides: Optional[Dict[str, Any]] = None
    # 与 overrides 等价的受限配置入口（技术设计 §4 写作 `config`），二者可同时给出
    config: Optional[Dict[str, Any]] = None

    @field_validator("uniqueId")
    @classmethod
    def _check_unique_id(cls, value: str) -> str:
        return io_utils.validate_unique_id(value)

    @field_validator("inputFile")
    @classmethod
    def _check_input_file(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not str(value).strip():
            raise ValueError("inputFile 不能为空字符串")
        return str(value)


class Envelope(BaseModel):
    """统一外层响应信封。``data`` 保持宽松以承载文件内容对象等嵌套结构。"""

    code: int
    msg: str
    data: Dict[str, Any] = Field(default_factory=dict)


def to_config_error(exc: ValidationError) -> ConfigError:
    details = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = error.get("msg", "非法取值")
        details.append("%s: %s" % (location or "<root>", message))
    return ConfigError("; ".join(details))


def validate_overrides(overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """请求级覆盖白名单校验；未在白名单内的键直接失败。"""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ConfigError("overrides 必须是对象")
    unknown = sorted(set(overrides) - set(OVERRIDABLE_KEYS))
    if unknown:
        raise ConfigError("overrides 含未允许的键：%s" % ", ".join(unknown))
    return dict(overrides)
