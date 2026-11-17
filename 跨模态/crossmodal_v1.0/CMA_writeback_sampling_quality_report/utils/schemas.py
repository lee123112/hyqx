"""部署配置、业务覆盖与 ``/predict`` 请求模型（pydantic，严格模式）。

分层（TECHNICAL_DESIGN §5）：

- ``deployment``：``operator.name`` / ``operator.version`` / ``stage_id`` / 端口等
  身份项，**不允许**被 ``/predict`` 覆盖；
- ``business``：表1 #24 声明的可调口径（``sample_rate``、``sampling_salt``、
  ``report_source_names``、``allow_degraded``、``require_reviewer_identity``）。
  其中 ``enabled``、``strata``、``include_auto_aligned`` 是表1 冻结值，
  只做校验不做放开：``enabled`` 必须为真（#24 不可跳过）、
  ``strata`` 必须恰为「场景 / 模态 / 对齐类型」、``include_auto_aligned`` 必须为真；
- 全模型禁止未声明字段，越界值一律 ``ConfigError``，保证错误配置不会静默生效。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, ValidationError,
                      field_validator)

OPERATOR_NAME = "spot_check_writeback"
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S7"  # [v4.2] §0.3 阶段表：M24 由 S5 改归 S7 人工抽检复核
FORMAL_OPERATOR_NUMBER = 24
DEFAULT_PORT = 8124

#: 表1 #24 冻结的分层维度与顺序
STRATA_FIELDS = ("scene", "modality", "alignment_type")
#: 表1 #24 抽样比例定义域
SAMPLE_RATE_MIN = 0.10
SAMPLE_RATE_MAX = 0.20

# [v4.2] §4.24：报告状态 pending_writeback → completed（回写前不得标完成）
REPORT_STATUS_WAITING = "pending_writeback"
REPORT_STATUS_WRITTEN = "completed"
VERSION_STATE_PENDING = "pending_release"
VERSION_STATE_DELIVERABLE = "deliverable"

#: 默认查找的上游「待回写质量报告」文件名（按顺序）
DEFAULT_REPORT_SOURCE_NAMES = ("20_quality_report.json", "quality_report.json",
                               "24_quality_report.json")

#: 部署身份字段，禁止由 /predict 覆盖
DEPLOYMENT_KEYS = ("operator", "stage_id")


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


def _validate_report_source_names(names: List[str]) -> List[str]:
    if not names:
        raise ValueError("report_source_names 不能为空")
    cleaned: List[str] = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("report_source_names 每一项都必须是非空文件名")
        value = name.strip()
        if value != Path(value).name or value in (".", ".."):
            raise ValueError("report_source_names 只能是纯文件名（不允许路径）：%s" % name)
        if "/" in value or "\\" in value:
            raise ValueError("report_source_names 不允许分隔符：%s" % name)
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


class OperatorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class OperatorConfig(BaseModel):
    """算子完整配置：``configs/config.yaml`` 的结构。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorIdentity = Field(
        default_factory=lambda: OperatorIdentity(name=OPERATOR_NAME,
                                                version=OPERATOR_VERSION))
    stage_id: str = STAGE_ID
    #: 表1 #24「默认开启、不能关」
    enabled: StrictBool = True
    #: 分层抽样比例（表1 允许 10%–20%，默认 10%）
    sample_rate: float = Field(default=SAMPLE_RATE_MIN,
                               ge=SAMPLE_RATE_MIN, le=SAMPLE_RATE_MAX)
    #: 表1 冻结的分层维度
    strata: List[str] = Field(default_factory=lambda: list(STRATA_FIELDS))
    #: 表1 明确「自动对齐也进入抽样框」
    include_auto_aligned: StrictBool = True
    #: 抽样哈希盐：保证同一批样本抽检结果可复现
    sampling_salt: str = "operator24"
    #: 查找上游「待回写质量报告」的文件名（按顺序，纯文件名）
    report_source_names: List[str] = Field(
        default_factory=lambda: list(DEFAULT_REPORT_SOURCE_NAMES))
    #: 允许显式降级；置为 false 时缺上游报告直接失败（绝不伪造回写成功）
    allow_degraded: StrictBool = False
    #: 人工结论必须带复核人身份，否则只能判待人工
    require_reviewer_identity: StrictBool = True

    @field_validator("enabled")
    @classmethod
    def _force_enabled(cls, value: bool) -> bool:
        if not value:
            raise ValueError("表1 #24 为必须算子，enabled 不能为 false")
        return value

    @field_validator("include_auto_aligned")
    @classmethod
    def _force_auto_aligned(cls, value: bool) -> bool:
        if not value:
            raise ValueError("表1 #24 要求「自动对齐也进入抽样框」，不能关闭")
        return value

    @field_validator("strata")
    @classmethod
    def _force_strata(cls, value: List[str]) -> List[str]:
        if sorted(value) != sorted(STRATA_FIELDS):
            raise ValueError("strata 必须恰为 %s（表1 冻结）" % list(STRATA_FIELDS))
        return list(STRATA_FIELDS)

    @field_validator("report_source_names")
    @classmethod
    def _check_report_names(cls, value: List[str]) -> List[str]:
        return _validate_report_source_names(value)

    @field_validator("operator")
    @classmethod
    def _check_operator(cls, value: OperatorIdentity) -> OperatorIdentity:
        if value.name != OPERATOR_NAME:
            raise ValueError("operator.name 必须为 %s" % OPERATOR_NAME)
        return value

    # -- 导出 ------------------------------------------------------------- #
    def business_dict(self) -> Dict[str, Any]:
        """业务参数字典（供核心业务与测试共用，避免两套默认值）。"""
        return {
            "operator": self.operator.name,
            "operator_version": self.operator.version,
            "stage_id": self.stage_id,
            "enabled": self.enabled,
            "sample_rate": self.sample_rate,
            "strata": list(self.strata),
            "include_auto_aligned": self.include_auto_aligned,
            "sampling_salt": self.sampling_salt,
            "report_source_names": list(self.report_source_names),
            "allow_degraded": self.allow_degraded,
            "require_reviewer_identity": self.require_reviewer_identity,
        }

    def deployment_dict(self) -> Dict[str, Any]:
        return {
            "operator": self.operator.model_dump(),
            "stage_id": self.stage_id,
            "formal_operator_number": FORMAL_OPERATOR_NUMBER,
            "port": DEFAULT_PORT,
            "report_status_waiting": REPORT_STATUS_WAITING,
            "report_status_written": REPORT_STATUS_WRITTEN,
            "version_state_deliverable": VERSION_STATE_DELIVERABLE,
        }


class BusinessOverrides(BaseModel):
    """``/predict`` 的 ``config`` 允许覆盖的业务字段（未列出的键一律拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    sample_rate: Optional[float] = Field(default=None, ge=SAMPLE_RATE_MIN,
                                        le=SAMPLE_RATE_MAX)
    sampling_salt: Optional[str] = Field(default=None, min_length=1, max_length=128)
    report_source_names: Optional[List[str]] = None
    allow_degraded: Optional[StrictBool] = None
    require_reviewer_identity: Optional[StrictBool] = None

    @field_validator("report_source_names")
    @classmethod
    def _check_report_names(cls, value):
        if value is None:
            return None
        return _validate_report_source_names(value)


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
    # [v4.2] §4.24 接口二别名：sampling_ratio → sample_rate；sampling_seed → sampling_salt
    data = dict(raw)
    if "sampling_ratio" in data:
        data["sample_rate"] = data.pop("sampling_ratio")
    if "sampling_seed" in data:
        data["sampling_salt"] = str(data.pop("sampling_seed"))
    try:
        model = BusinessOverrides.model_validate(data)
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


def default_business(**overrides) -> Dict[str, Any]:
    """与部署配置同源的业务参数字典（测试与核心业务共用同一套默认值）。"""
    return deep_merge(default_config().business_dict(), overrides)
