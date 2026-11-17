"""配置与请求模型（严格校验，越界即失败）。

表1 #19 冻结口径（逐字）：
- 默认开启、**必须**（``enabled`` 置 false 直接配置失败）；
- 类型 = 规则型，模型列 = ``--``（本算子无任何模型，不产生任何分数）；
- 处理边界：``评分开启且通过``、``整体自动/人工对齐``、``授权可训练``、
  ``有时间戳样本时序通过``；
- 研发测边界：四项同时满足才入池；不得用加权分把已隔离样本加回；
  **局部标注不作为入池硬条件**。
- 请求侧边界（不可削弱四项）：``required_alignment`` 与
  ``require_temporal_if_present`` 是**固定口径**，不在请求覆盖白名单内；
  ``/predict`` 的 ``overrides`` 或形态一表单携带这两个键一律 422
  （``FrozenOverrideError``，中文错误见 ``frozen_override_message``）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator, model_validator)

from . import io as io_utils

__all__ = [
    "ConfigError",
    "Envelope",
    "FROZEN_OVERRIDE_KEYS",
    "FrozenOverrideError",
    "MISSING_UPSTREAM_POLICY_CHOICES",
    "MISSING_UPSTREAM_POLICY_WAITING",
    "OPERATOR_LABEL",
    "OPERATOR_NAME",
    "OPERATOR_NUMBER",
    "OPERATOR_VERSION",
    "OVERRIDABLE_KEYS",
    "OperatorConfig",
    "OperatorMeta",
    "PredictRequest",
    "REQUIRED_ALIGNMENT_CHOICES",
    "REQUIRED_ALIGNMENT_DEFAULT",
    "REQUIRE_TEMPORAL_IF_PRESENT_DEFAULT",
    "STAGE_ID",
    "TRAINING_AUTHORIZATION_REQUIRED",
    "TRANSPORT_FORMAT_REQUIRED",
    "TRANSPORT_MAX_RESPONSE_BYTES",
    "TransportConfig",
    "allowed_alignments",
    "frozen_override_message",
    "to_config_error",
    "validate_overrides",
]

OPERATOR_NAME = "high_confidence_pool"
OPERATOR_LABEL = "筛选高置信样本入池"
OPERATOR_NUMBER = 19
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S3"

TRANSPORT_FORMAT_REQUIRED = "crossmodal-files-v1"
TRANSPORT_MAX_RESPONSE_BYTES = 67108864
MIN_TRANSPORT_MAX_RESPONSE_BYTES = 1024

REQUIRED_ALIGNMENT_DEFAULT = "auto|human"
REQUIRED_ALIGNMENT_CHOICES = ("auto", "human", "auto|human")
REQUIRE_TEMPORAL_IF_PRESENT_DEFAULT = True

TRAINING_AUTHORIZATION_REQUIRED = "required"

MISSING_UPSTREAM_POLICY_WAITING = "waiting_manual"
MISSING_UPSTREAM_POLICY_FAILED = "failed"
MISSING_UPSTREAM_POLICY_CHOICES = (MISSING_UPSTREAM_POLICY_WAITING,
                                  MISSING_UPSTREAM_POLICY_FAILED)

MAX_ENABLED_ROUTES = 3

#: 表1 #19 固定口径键：只能来自部署配置，**不可**由 ``/predict`` overrides 或形态一表单
#: 削弱（收窄整体对齐结论集合 / 关闭时序必过项），请求携带即 422。
FROZEN_OVERRIDE_KEYS = ("required_alignment", "require_temporal_if_present")

#: 请求级可覆盖的业务键（固定口径键已移出，携带即 ``FrozenOverrideError``）
OVERRIDABLE_KEYS = ("enabled", "training_authorization", "min_enabled_routes",
                    "missing_upstream_policy")


class ConfigError(ValueError):
    """配置非法（越界、未知键、类型错误）。"""


class FrozenOverrideError(ConfigError):
    """请求试图覆盖表1 #19 固定口径（``required_alignment`` / ``require_temporal_if_present``）。"""


def frozen_override_message(keys: Sequence[str]) -> str:
    """固定口径被请求覆盖时的统一中文错误（形态一/形态二共用同一措辞）。"""
    labels = {
        "required_alignment": '固定为 "%s"' % REQUIRED_ALIGNMENT_DEFAULT,
        "require_temporal_if_present": "固定为 true",
    }
    items = [str(key) for key in keys]
    detail = "、".join("%s %s" % (key, labels.get(key, "不可覆盖")) for key in items)
    target = "该键" if len(items) == 1 else "这些键"
    return ("固定口径不可通过请求覆盖：%s（表1 #19 四项必过项不得去掉任何一项）；"
            "请从 overrides/表单中移除%s，该口径只能由部署配置决定。"
            % (detail, target))


def _normalize_required_alignment(value: Any) -> str:
    """把 ``auto`` / ``human`` / ``auto|human``（顺序、大小写、空白可变）归一。"""
    if not isinstance(value, str):
        raise ValueError("required_alignment 必须是字符串")
    tokens = [token.strip().lower() for token in value.replace(",", "|").split("|")]
    tokens = [token for token in tokens if token]
    if not tokens:
        raise ValueError("required_alignment 不能为空")
    ordered: list = []
    for token in tokens:
        if token not in ("auto", "human"):
            raise ValueError("required_alignment 只允许 auto 与 human，收到 %r" % value)
        if token not in ordered:
            ordered.append(token)
    ordered.sort(key=lambda item: 0 if item == "auto" else 1)
    return "|".join(ordered)


def allowed_alignments(value: str) -> Tuple[str, ...]:
    """把 ``required_alignment`` 展开为允许的整体对齐结论集合。"""
    normalized = _normalize_required_alignment(value)
    mapping = {"auto": "auto_aligned", "human": "manual_aligned"}
    return tuple(mapping[token] for token in normalized.split("|"))


class OperatorMeta(BaseModel):
    """算子身份。``name`` 为工程 slug（用于目录/血缘 operator 字段），
    ``label`` 为表1/计划中的中文名称。"""

    model_config = ConfigDict(extra="forbid")

    number: StrictInt = OPERATOR_NUMBER
    id: str = OPERATOR_NAME
    name: str = OPERATOR_NAME
    label: str = OPERATOR_LABEL
    version: str = OPERATOR_VERSION
    enabled: StrictBool = True

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
        if candidate != OPERATOR_NAME:
            raise ValueError("operator.id/name 必须是 %r" % OPERATOR_NAME)
        return candidate

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
            raise ValueError("operator.enabled 必须为 true：表1 #19 为必须工具")
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


class OperatorConfig(BaseModel):
    """算子 19 配置。未知键直接失败，避免静默忽略配置错误。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorMeta = Field(default_factory=OperatorMeta)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    stage_id: str = STAGE_ID
    enabled: StrictBool = True
    required_alignment: str = REQUIRED_ALIGNMENT_DEFAULT
    require_temporal_if_present: StrictBool = REQUIRE_TEMPORAL_IF_PRESENT_DEFAULT
    #: 无任何评分路线生效时，把 #15「时序通过」作为路线兜底证据（默认开启）；
    #: 仅部署配置可改，不在请求覆盖白名单内。置 false 复现旧行为（一律待人工）。
    temporal_route_fallback: StrictBool = True
    training_authorization: str = TRAINING_AUTHORIZATION_REQUIRED
    min_enabled_routes: StrictInt = 1
    missing_upstream_policy: str = MISSING_UPSTREAM_POLICY_WAITING

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
            raise ValueError("enabled 必须为 true：表1 #19 为默认开启且不可跳过的必须工具")
        return True

    @field_validator("required_alignment", mode="before")
    @classmethod
    def _check_required_alignment(cls, value: Any) -> str:
        return _normalize_required_alignment(value)

    @field_validator("training_authorization")
    @classmethod
    def _check_training_authorization(cls, value: str) -> str:
        candidate = str(value).strip().lower()
        if candidate != TRAINING_AUTHORIZATION_REQUIRED:
            raise ValueError("training_authorization 必须是 %r：表1 #19 要求授权可训练"
                             % TRAINING_AUTHORIZATION_REQUIRED)
        return candidate

    @field_validator("min_enabled_routes")
    @classmethod
    def _check_min_routes(cls, value: int) -> int:
        if not 1 <= int(value) <= MAX_ENABLED_ROUTES:
            raise ValueError("min_enabled_routes 必须在 1–%d 之间" % MAX_ENABLED_ROUTES)
        return int(value)

    @field_validator("missing_upstream_policy")
    @classmethod
    def _check_policy(cls, value: str) -> str:
        candidate = str(value).strip().lower()
        if candidate not in MISSING_UPSTREAM_POLICY_CHOICES:
            raise ValueError("missing_upstream_policy 必须是 %s 之一"
                             % (MISSING_UPSTREAM_POLICY_CHOICES,))
        return candidate

    @model_validator(mode="after")
    def _check_operator_consistency(self) -> "OperatorConfig":
        """计划里的 ``operator.enabled`` 与顶层 ``enabled`` 必须一致（都为 true）。"""
        if bool(self.operator.enabled) is not bool(self.enabled):
            raise ValueError("operator.enabled 与顶层 enabled 必须一致")
        return self

    # -- 派生属性 ---------------------------------------------------------- #

    @property
    def alignment_choices(self) -> Tuple[str, ...]:
        return allowed_alignments(self.required_alignment)

    @property
    def strict_upstream(self) -> bool:
        return self.missing_upstream_policy == MISSING_UPSTREAM_POLICY_FAILED

    def business_snapshot(self) -> Dict[str, Any]:
        """业务配置快照（不含任何凭据、路径、权重）。"""
        return {
            "enabled": bool(self.enabled),
            "required_alignment": self.required_alignment,
            "allowed_alignments": list(self.alignment_choices),
            "require_temporal_if_present": bool(self.require_temporal_if_present),
            "temporal_route_fallback": bool(self.temporal_route_fallback),
            "training_authorization": self.training_authorization,
            "min_enabled_routes": int(self.min_enabled_routes),
            "missing_upstream_policy": self.missing_upstream_policy,
            "local_annotation_hard_requirement": False,
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
    """请求级覆盖白名单校验；固定口径键与未在白名单内的键直接失败。

    - ``required_alignment`` / ``require_temporal_if_present``：表1 #19 固定口径，
      请求携带即 ``FrozenOverrideError``（HTTP 422），避免收窄四项必过项；
    - 其余未在白名单内的键：``ConfigError``（HTTP 400）。
    """
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ConfigError("overrides 必须是对象")
    frozen = [key for key in FROZEN_OVERRIDE_KEYS if key in overrides]
    if frozen:
        raise FrozenOverrideError(frozen_override_message(frozen))
    unknown = sorted(set(overrides) - set(OVERRIDABLE_KEYS))
    if unknown:
        raise ConfigError("overrides 含未允许的键：%s" % ", ".join(unknown))
    return dict(overrides)
