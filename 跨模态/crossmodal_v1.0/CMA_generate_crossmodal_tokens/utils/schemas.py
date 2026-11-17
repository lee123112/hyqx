"""配置与请求模型（严格校验，越界即失败）。

表1 v4.1 #22 冻结口径（逐字落地）：
- 流程环节：5 样本构建；开关：默认开启；是否必须：必须；优先级：P0；模型列：``--``；
- 模板配置项 1（只读）：图像写成 ``<image>``；音频写成 ``<audio>``；视频写成 ``<video>``；
  页面说明「没打开的路线不会生成对应文件」→ 音文/视文/局部标注路线可开关；
- 模板配置项 2（只读）：样本文件名固定 ``samples.jsonl``，页面说明「隔离区里的样本不会写进这个文件」
  → ``exclude_isolated`` 必须为 true；
- 总开关「默认开。不能关。」→ ``enabled`` 置 false 直接配置失败；
- 边界：本工具不压缩、不改版本状态。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator, model_validator)

from . import io as io_utils
from . import names

__all__ = [
    "ConfigError",
    "Envelope",
    "Markers",
    "MISSING_QUALITY_POLICY_CHOICES",
    "MISSING_QUALITY_POLICY_FAILED",
    "MISSING_QUALITY_POLICY_WAITING",
    "OPERATOR_LABEL",
    "OPERATOR_NAME",
    "OPERATOR_NUMBER",
    "OPERATOR_VERSION",
    "OVERRIDABLE_KEYS",
    "OperatorConfig",
    "OperatorMeta",
    "PredictRequest",
    "Routes",
    "SAMPLES_FILE_NAME",
    "STAGE_ID",
    "TRANSPORT_FORMAT_REQUIRED",
    "TRANSPORT_MAX_RESPONSE_BYTES",
    "TransportConfig",
    "to_config_error",
    "validate_overrides",
]

OPERATOR_NAME = names.OPERATOR
OPERATOR_LABEL = names.OPERATOR_LABEL
OPERATOR_NUMBER = names.OPERATOR_NUMBER
OPERATOR_VERSION = names.OPERATOR_VERSION
STAGE_ID = names.STAGE_ID
SAMPLES_FILE_NAME = names.SAMPLES_FILE_NAME

TRANSPORT_FORMAT_REQUIRED = "crossmodal-files-v1"
TRANSPORT_MAX_RESPONSE_BYTES = 67108864
MIN_TRANSPORT_MAX_RESPONSE_BYTES = 1024

MISSING_QUALITY_POLICY_WAITING = "waiting_manual"
MISSING_QUALITY_POLICY_FAILED = "failed"
MISSING_QUALITY_POLICY_CHOICES = (MISSING_QUALITY_POLICY_WAITING,
                                 MISSING_QUALITY_POLICY_FAILED)

#: 请求级 ``config`` 只允许业务参数；模型/路径/身份不可被请求改写
OVERRIDABLE_KEYS = ("enabled", "routes", "missing_quality_policy")


class ConfigError(ValueError):
    """配置非法（越界、未知键、类型错误）。"""


class OperatorMeta(BaseModel):
    """算子身份。``name`` 为工程 slug（目录/血缘 operator），``label`` 为中文名。"""

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
            raise ValueError("operator.enabled 必须为 true：表1 #22 为必须工具（默认开，不能关）")
        return True


class TransportConfig(BaseModel):
    """接口文件内容对象与响应体积约束。"""

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


class Markers(BaseModel):
    """表1 #22 配置项 1：文字里用什么标记模态（页面控件**只读**，值固定）。"""

    model_config = ConfigDict(extra="forbid")

    image: str = names.MARKER_IMAGE
    audio: str = names.MARKER_AUDIO
    video: str = names.MARKER_VIDEO

    @field_validator("image", "audio", "video")
    @classmethod
    def _check_marker(cls, value: str) -> str:
        candidate = str(value).strip()
        if candidate not in names.MARKERS.values():
            raise ValueError("模态标记只读，必须是 %s 之一"
                             % (sorted(set(names.MARKERS.values())),))
        return candidate

    @model_validator(mode="after")
    def _check_fixed_assignment(self) -> "Markers":
        pairs = ((self.image, "image"), (self.audio, "audio"), (self.video, "video"))
        for value, modality in pairs:
            if value != names.MARKERS[modality]:
                raise ValueError("模态标记只读：%s 必须是 %r"
                                 % (modality, names.MARKERS[modality]))
        return self

    def as_dict(self) -> Dict[str, str]:
        return {"image": self.image, "audio": self.audio, "video": self.video}


class Routes(BaseModel):
    """路线开关。默认图文必须开启（默认图文任务）；其余路线关闭时不产出对应文件。

    ``video_text`` 出厂默认开启：视频样本在上游是合法模态，未配置时按「无路线」
    处理会让全部视频样本以 ``route_disabled`` 落进待处理；显式配 ``false`` 仍可关闭。
    """

    model_config = ConfigDict(extra="forbid")

    image_text: StrictBool = True
    audio_text: StrictBool = False
    video_text: StrictBool = True
    local_annotation: StrictBool = False

    @field_validator("image_text")
    @classmethod
    def _check_default_route(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("routes.image_text 必须为 true：表1「默认图文」为默认路线，不能关闭")
        return True

    def as_dict(self) -> Dict[str, bool]:
        return {"image_text": bool(self.image_text), "audio_text": bool(self.audio_text),
                "video_text": bool(self.video_text),
                "local_annotation": bool(self.local_annotation)}


class OperatorConfig(BaseModel):
    """算子 22 配置。未知键直接失败，避免静默忽略配置错误。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorMeta = Field(default_factory=OperatorMeta)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    stage_id: str = STAGE_ID
    enabled: StrictBool = True
    exclude_isolated: StrictBool = True
    samples_file_name: str = SAMPLES_FILE_NAME
    markers: Markers = Field(default_factory=Markers)
    routes: Routes = Field(default_factory=Routes)
    missing_quality_policy: str = MISSING_QUALITY_POLICY_WAITING

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
            raise ValueError("enabled 必须为 true：表1 #22 总开关「默认开。不能关。」")
        return True

    @field_validator("exclude_isolated")
    @classmethod
    def _check_exclude_isolated(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("exclude_isolated 必须为 true：表1「隔离区里的样本不会写进这个文件」")
        return True

    @field_validator("samples_file_name")
    @classmethod
    def _check_samples_name(cls, value: str) -> str:
        candidate = str(value).strip()
        if candidate != SAMPLES_FILE_NAME:
            raise ValueError("samples_file_name 固定为 %r（页面控件只读）" % SAMPLES_FILE_NAME)
        return candidate

    @field_validator("missing_quality_policy")
    @classmethod
    def _check_policy(cls, value: str) -> str:
        candidate = str(value).strip().lower()
        if candidate not in MISSING_QUALITY_POLICY_CHOICES:
            raise ValueError("missing_quality_policy 必须是 %s 之一"
                             % (MISSING_QUALITY_POLICY_CHOICES,))
        return candidate

    @model_validator(mode="after")
    def _check_operator_consistency(self) -> "OperatorConfig":
        if bool(self.operator.enabled) is not bool(self.enabled):
            raise ValueError("operator.enabled 与顶层 enabled 必须一致")
        return self

    # -- 派生属性 ---------------------------------------------------------- #

    @property
    def enabled_routes(self) -> Tuple[str, ...]:
        current = self.routes.as_dict()
        return tuple(name for name in names.ROUTES if current.get(name))

    @property
    def strict_quality(self) -> bool:
        return self.missing_quality_policy == MISSING_QUALITY_POLICY_FAILED

    def route_enabled(self, route: str) -> bool:
        return bool(self.routes.as_dict().get(route))

    def route_files(self) -> Dict[str, str]:
        return {route: names.ROUTE_FILES[route] for route in self.enabled_routes}

    def business_snapshot(self) -> Dict[str, Any]:
        """业务配置快照（不含任何凭据、路径、权重）。"""
        return {
            "enabled": bool(self.enabled),
            "exclude_isolated": bool(self.exclude_isolated),
            "samples_file_name": self.samples_file_name,
            "markers": self.markers.as_dict(),
            "routes": self.routes.as_dict(),
            "routes_enabled": list(self.enabled_routes),
            "missing_quality_policy": self.missing_quality_policy,
            "media_storage": "original_files_link_only",
            "compresses_output": False,
            "changes_version_state": False,
            "rule_based": True,
            "model": None,
            "score_fields_produced": False,
            "subword_tokenizer": None,
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
    """请求级覆盖白名单校验；未在白名单内的键直接失败。"""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ConfigError("overrides 必须是对象")
    unknown = sorted(set(overrides) - set(OVERRIDABLE_KEYS))
    if unknown:
        raise ConfigError("overrides 含未允许的键：%s" % ", ".join(unknown))
    return dict(overrides)
