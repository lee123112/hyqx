"""部署配置、业务覆盖与 ``/predict`` 请求模型（pydantic，严格模式）。

分层：

- ``operator.*`` / ``stage_id`` / ``transport`` 属于**部署身份**，不被 ``/predict`` 覆盖；
- ``allowed_license`` / ``sources`` / ``file_formats`` 是表1 #01 **冻结值**，
  配置层只做校验（必须恰为「可训练」、五类来源、四组格式白名单），不提供放开开关；
- ``enabled`` / ``file_check_mode`` / ``dedupe`` / ``check_source_modality``
  可由 ``/predict`` 的 ``config`` 或 CLI ``--override`` 覆盖。

所有模型禁止未声明字段（``extra="forbid"``）且校验默认值（``validate_default=True``），
保证错误配置不会静默生效。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, ValidationError,
                      field_validator)

from .intake import (ALLOWED_LICENSE_LABEL, DEFAULT_MAX_RESPONSE_BYTES, DEFAULT_PORT,
                     FILE_CHECK_MODES, FILE_CHECK_MODE_HEADER, FORMAT_GROUPS,
                     FORMAL_OPERATOR_NUMBER, OPERATOR_LABEL, OPERATOR_NAME,
                     OPERATOR_VERSION, SOURCES, STAGE_ID, STAGE_LABEL,
                     TRANSPORT_FORMAT, normalize_source)

#: 部署身份字段，禁止由 /predict 覆盖
DEPLOYMENT_KEYS = ("operator", "stage_id", "transport", "allowed_license", "sources",
                   "file_formats")


class ConfigError(ValueError):
    """配置或请求参数非法。"""


def to_config_error(exc: Exception) -> ConfigError:
    if isinstance(exc, ValidationError):
        parts = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            parts.append("%s: %s" % (location or "<root>", item.get("msg")))
        return ConfigError("配置不合法 -> " + "; ".join(parts))
    return ConfigError(str(exc))


def deep_merge(base: Dict[str, Any], patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """递归合并字典：``patch`` 的同名 dict 合并，其余类型直接覆盖。"""
    result = dict(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class OperatorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default=OPERATOR_NAME, min_length=1)
    version: str = Field(default=OPERATOR_VERSION, min_length=1)
    label: str = OPERATOR_LABEL
    number: int = FORMAL_OPERATOR_NUMBER

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if value != OPERATOR_NAME:
            raise ValueError("operator.name 必须为 %s" % OPERATOR_NAME)
        return value

    @field_validator("label")
    @classmethod
    def _check_label(cls, value: str) -> str:
        if value != OPERATOR_LABEL:
            raise ValueError("operator.label 必须为 %s" % OPERATOR_LABEL)
        return value

    @field_validator("number")
    @classmethod
    def _check_number(cls, value: int) -> int:
        if int(value) != FORMAL_OPERATOR_NUMBER:
            raise ValueError("operator.number 必须为 %d" % FORMAL_OPERATOR_NUMBER)
        return int(value)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if value != OPERATOR_VERSION:
            raise ValueError("operator.version 必须为 %s" % OPERATOR_VERSION)
        return value


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = TRANSPORT_FORMAT
    max_response_bytes: int = Field(default=DEFAULT_MAX_RESPONSE_BYTES, ge=1024)

    @field_validator("format")
    @classmethod
    def _check_format(cls, value: str) -> str:
        if value != TRANSPORT_FORMAT:
            raise ValueError("transport.format 必须为 %s" % TRANSPORT_FORMAT)
        return value


class OperatorConfig(BaseModel):
    """算子完整配置：``configs/config.yaml`` 的结构。"""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    operator: OperatorIdentity = Field(default_factory=OperatorIdentity)
    stage_id: str = STAGE_ID

    #: 表1 #01「总开关」默认开启；关闭则本环节不跑
    enabled: StrictBool = True
    #: 文件可读性检查模式：whitelist / header（默认）/ strict
    file_check_mode: str = FILE_CHECK_MODE_HEADER
    #: 同一 sample_id + 链接重复登记时只保留首次，其余退回补登记
    dedupe: StrictBool = True
    #: 「来源产线模态与文件模态一致性」校验；表1 未明确要求，默认关闭
    check_source_modality: StrictBool = False

    #: 表1 冻结：只有「可训练」可以进入加工
    allowed_license: str = ALLOWED_LICENSE_LABEL
    #: 表1 冻结：来源只能选采集平台批次 / 四条预训练产线交付
    sources: List[str] = Field(default_factory=lambda: list(SOURCES))
    #: 表1 冻结：文本 txt/json/csv；图像 jpg/png/webp；音频 wav/mp3/flac；视频 mp4/mov/avi
    file_formats: Dict[str, List[str]] = Field(
        default_factory=lambda: {key: list(value) for key, value in FORMAT_GROUPS.items()})

    transport: TransportConfig = Field(default_factory=TransportConfig)

    @field_validator("stage_id")
    @classmethod
    def _check_stage(cls, value: str) -> str:
        if str(value) != STAGE_ID:
            raise ValueError("stage_id 必须为 %s" % STAGE_ID)
        return STAGE_ID

    @field_validator("file_check_mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        text = str(value).strip().lower()
        if text not in FILE_CHECK_MODES:
            raise ValueError("file_check_mode 必须为 %s" % list(FILE_CHECK_MODES))
        return text

    @field_validator("allowed_license")
    @classmethod
    def _force_license(cls, value: str) -> str:
        if str(value).strip() not in (ALLOWED_LICENSE_LABEL, "trainable"):
            raise ValueError("表1 #01 只允许「%s」进入加工" % ALLOWED_LICENSE_LABEL)
        return ALLOWED_LICENSE_LABEL

    @field_validator("sources")
    @classmethod
    def _force_sources(cls, value: List[str]) -> List[str]:
        normalized = [normalize_source(item) for item in value]
        if sorted(item for item in normalized if item) != sorted(SOURCES) \
                or len(normalized) != len(SOURCES) or None in normalized:
            raise ValueError("表1 #01 来源只能为 %s" % list(SOURCES))
        return list(SOURCES)

    @field_validator("file_formats")
    @classmethod
    def _force_formats(cls, value: Dict[str, List[str]]) -> Dict[str, List[str]]:
        expected = {key: list(item) for key, item in FORMAT_GROUPS.items()}
        given = {str(key): [str(item).lower() for item in items]
                 for key, items in (value or {}).items()}
        if given != expected:
            raise ValueError("表1 #01 允许的文件格式固定为 %s" % expected)
        return expected

    # -- 导出 ------------------------------------------------------------- #
    def business_dict(self) -> Dict[str, Any]:
        """业务参数字典（供核心业务与测试共用，避免两套默认值）。"""
        return {
            "operator": self.operator.name,
            "operator_label": self.operator.label,
            "operator_number": self.operator.number,
            "operator_version": self.operator.version,
            "stage_id": self.stage_id,
            "enabled": bool(self.enabled),
            "file_check_mode": self.file_check_mode,
            "dedupe": bool(self.dedupe),
            "check_source_modality": bool(self.check_source_modality),
            "allowed_license": self.allowed_license,
            "sources": list(self.sources),
            "file_formats": {key: list(value)
                             for key, value in self.file_formats.items()},
            "transport": self.transport.model_dump(),
        }

    def deployment_dict(self) -> Dict[str, Any]:
        return {
            "operator": self.operator.model_dump(),
            "stage_id": self.stage_id,
            "stage_label": STAGE_LABEL,
            "formal_operator_number": FORMAL_OPERATOR_NUMBER,
            "port": DEFAULT_PORT,
            "allowed_license": ALLOWED_LICENSE_LABEL,
            "sources": list(SOURCES),
            "file_formats": {key: list(value) for key, value in FORMAT_GROUPS.items()},
        }


class BusinessOverrides(BaseModel):
    """``/predict`` 的 ``config`` 允许覆盖的业务字段（未列出的键一律拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[StrictBool] = None
    file_check_mode: Optional[str] = None
    dedupe: Optional[StrictBool] = None
    check_source_modality: Optional[StrictBool] = None

    @field_validator("file_check_mode")
    @classmethod
    def _check_mode(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text not in FILE_CHECK_MODES:
            raise ValueError("file_check_mode 必须为 %s" % list(FILE_CHECK_MODES))
        return text


class PredictRequest(BaseModel):
    """/predict 请求体：``isLastOperator`` 必须显式、必须是布尔。"""

    model_config = ConfigDict(extra="forbid")

    uniqueId: str = Field(min_length=1)
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)


def validate_overrides(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """校验并规范化业务覆盖；只返回显式提供的字段。"""
    if raw in (None, {}, []):
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("config 必须是对象")
    try:
        model = BusinessOverrides.model_validate(raw)
    except ValidationError as exc:
        raise to_config_error(exc) from exc
    return model.model_dump(exclude_unset=True, exclude_none=True)


def default_config() -> OperatorConfig:
    return OperatorConfig()


def load_config(config_path=None, overrides: Optional[Dict[str, Any]] = None
                ) -> OperatorConfig:
    """读取 YAML 配置并叠加已校验的覆盖项。"""
    payload: Dict[str, Any] = {}
    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError("配置文件不存在：%s" % path)
        try:
            import yaml  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - yaml 是硬依赖
            raise ConfigError("缺少 PyYAML，无法读取配置") from exc
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ConfigError("配置文件无法解析：%s" % path.name) from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError("配置文件顶层必须是对象")
        payload = loaded
    payload = deep_merge(payload, overrides or {})
    try:
        return OperatorConfig.model_validate(payload)
    except ValidationError as exc:
        raise to_config_error(exc) from exc


def config_snapshot(config: OperatorConfig, overridden_keys=None) -> Dict[str, Any]:
    return {
        "business": config.business_dict(),
        "deployment": config.deployment_dict(),
        "overridden_keys": sorted(overridden_keys or []),
    }
