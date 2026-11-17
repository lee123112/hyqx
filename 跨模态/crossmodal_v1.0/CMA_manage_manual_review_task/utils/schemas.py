"""部署配置、业务覆盖与 ``/predict`` 请求模型（pydantic，严格模式）。

分层（TECHNICAL_DESIGN §5）：

- ``deployment``：``operator.*`` / ``stage_id`` / 端口等身份项，
  **不允许**被 ``/predict`` 覆盖；
- ``business``：表1 #25 声明的可调口径（``enabled``、``manual_platform_available``、
  ``auto_dispatch``、``require_reviewer_identity``、``allow_degraded``）。
  其中 ``trigger``、``max_review_count``、``eligible``、``repeat_spot_check``
  是表1 冻结值，只做校验不做放开：
  ``trigger`` 必须为 ``manual``；``max_review_count`` 必须为 2；
  ``eligible`` 必须恰为「存疑 / 抽检异议 / 加工退回」；``repeat_spot_check``
  必须为假（不重复 #24 抽样）。
- 全模型禁止未声明字段，越界值一律 ``ConfigError``，保证错误配置不会静默生效。

注：``DEVELOPMENT_PLAN.md`` §3 的 YAML 片段把 ``enabled/trigger/…`` 写在 ``operator``
块内，且末行以 ``trigger=manual；…`` 形式简述。本实现把身份项留在 ``operator`` 块、
把业务项放在顶层（见 ``configs/config.yaml``），语义完全一致且为合法 YAML。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, ValidationError,
                      field_validator)

OPERATOR_NAME = "manual_review_task_management"
OPERATOR_LABEL = "管理人工复核任务"
OPERATOR_VERSION = "v1.0.0"
# [v4.2] 阶段号按 V4.2 §4.25 修正：#25 属「人工抽检复核」阶段末，stageId=S7（原 S5 与表1 不符）。
STAGE_ID = "S7"
STAGE_LABEL = "人工抽检复核"
RUN_STAGE = "manual_review_task_management"
FORMAL_OPERATOR_NUMBER = 25
DEFAULT_PORT = 8125

#: 表1 #25 冻结的触发方式与复核次数上限
TRIGGER_MANUAL = "manual"
MAX_REVIEW_COUNT_FROZEN = 2
#: 表1 #25 冻结的「三类样本」范围
ELIGIBLE_CATEGORIES = ("uncertain", "sampling_dispute", "returned")

#: 阶段末传输格式（表1 / DEVELOPMENT_PLAN §3）
TRANSPORT_FORMAT = "crossmodal-files-v1"
DEFAULT_MAX_RESPONSE_BYTES = 67108864

#: 部署身份字段，禁止由 /predict 覆盖
DEPLOYMENT_KEYS = ("operator", "stage_id", "trigger", "max_review_count", "eligible",
                   "repeat_spot_check", "transport")


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

    @field_validator("number")
    @classmethod
    def _check_number(cls, value: int) -> int:
        if int(value) != FORMAL_OPERATOR_NUMBER:
            raise ValueError("operator.number 必须为 %d" % FORMAL_OPERATOR_NUMBER)
        return int(value)


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

    model_config = ConfigDict(extra="forbid")

    operator: OperatorIdentity = Field(default_factory=OperatorIdentity)
    stage_id: str = STAGE_ID
    #: 表1 #25「手动触发；可选」：默认关闭，需要显式开启（CLI/``/predict`` 覆盖）
    enabled: StrictBool = False
    #: 表1 冻结：只支持手动触发
    trigger: str = TRIGGER_MANUAL
    #: 表1 冻结：同一条最多复核 2 次，超过不再派单
    max_review_count: int = MAX_REVIEW_COUNT_FROZEN
    #: 表1 冻结：仅三类样本生成本算子任务
    eligible: List[str] = Field(default_factory=lambda: list(ELIGIBLE_CATEGORIES))
    #: 表1 冻结：不重复 #24 抽样
    repeat_spot_check: StrictBool = False
    #: 触发后是否真的生成派单任务（关闭时只做队列统计，不派单）
    auto_dispatch: StrictBool = True
    #: 人工复核平台是否已接入；未接入时任务只能停在 waiting_manual
    manual_platform_available: StrictBool = False
    #: 人工结论必须带复核人身份，否则只能判待人工
    require_reviewer_identity: StrictBool = True
    #: 允许显式降级（人工平台未接入时继续并标记 status=waiting_manual）
    allow_degraded: StrictBool = False
    transport: TransportConfig = Field(default_factory=TransportConfig)

    @field_validator("trigger")
    @classmethod
    def _force_manual(cls, value: str) -> str:
        if str(value).strip().lower() != TRIGGER_MANUAL:
            raise ValueError("表1 #25 为手动触发，trigger 必须为 %s" % TRIGGER_MANUAL)
        return TRIGGER_MANUAL

    @field_validator("max_review_count")
    @classmethod
    def _force_max_review_count(cls, value: int) -> int:
        if int(value) != MAX_REVIEW_COUNT_FROZEN:
            raise ValueError("表1 #25 要求同一条最多复核 %d 次"
                             % MAX_REVIEW_COUNT_FROZEN)
        return MAX_REVIEW_COUNT_FROZEN

    @field_validator("eligible")
    @classmethod
    def _force_eligible(cls, value: List[str]) -> List[str]:
        if sorted(value) != sorted(ELIGIBLE_CATEGORIES):
            raise ValueError("eligible 必须恰为 %s（表1 冻结）"
                             % list(ELIGIBLE_CATEGORIES))
        return list(ELIGIBLE_CATEGORIES)

    @field_validator("repeat_spot_check")
    @classmethod
    def _force_no_repeat(cls, value: bool) -> bool:
        if value:
            raise ValueError("表1 #25 不重复 #24 抽样，repeat_spot_check 必须为 false")
        return False

    # -- 导出 ------------------------------------------------------------- #
    def business_dict(self) -> Dict[str, Any]:
        """业务参数字典（供核心业务与测试共用，避免两套默认值）。"""
        return {
            "operator": self.operator.name,
            "operator_label": self.operator.label,
            "operator_number": self.operator.number,
            "operator_version": self.operator.version,
            "stage_id": self.stage_id,
            "enabled": self.enabled,
            "trigger": self.trigger,
            "max_review_count": self.max_review_count,
            "eligible": list(self.eligible),
            "repeat_spot_check": self.repeat_spot_check,
            "auto_dispatch": self.auto_dispatch,
            "manual_platform_available": self.manual_platform_available,
            "require_reviewer_identity": self.require_reviewer_identity,
            "allow_degraded": self.allow_degraded,
            "transport": self.transport.model_dump(),
        }

    def deployment_dict(self) -> Dict[str, Any]:
        return {
            "operator": self.operator.model_dump(),
            "stage_id": self.stage_id,
            "formal_operator_number": FORMAL_OPERATOR_NUMBER,
            "port": DEFAULT_PORT,
            "trigger": TRIGGER_MANUAL,
            "max_review_count": MAX_REVIEW_COUNT_FROZEN,
        }


class BusinessOverrides(BaseModel):
    """``/predict`` 的 ``config`` 允许覆盖的业务字段（未列出的键一律拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[StrictBool] = None
    auto_dispatch: Optional[StrictBool] = None
    manual_platform_available: Optional[StrictBool] = None
    require_reviewer_identity: Optional[StrictBool] = None
    allow_degraded: Optional[StrictBool] = None


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


def default_business(**overrides) -> Dict[str, Any]:
    """与部署配置同源的业务参数字典（测试与核心业务共用同一套默认值）。"""
    return deep_merge(default_config().business_dict(), overrides)
