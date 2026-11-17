"""配置与请求模型（pydantic v2）：**表1 #26 只读口径在此固化为校验规则**。

只读项（请求不可放宽，配置写错即失败）：
- ``format`` 只能是 ``xlsx``（表1：导出 Excel）；
- ``source_status`` 只能是 ``可交付``（表1：只从可交付包抽样）；
- ``max_samples_per_route`` ∈ [1, 10]（表1：每条已开启路线最多抽 10 条）；
- ``transport.format`` 只能是 ``crossmodal-files-v1``（共享契约）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from . import sampling

__all__ = [
    "ConfigError",
    "OperatorConfig",
    "OperatorMeta",
    "PredictRequest",
    "RouteSwitches",
    "TransportConfig",
    "TRANSPORT_FORMAT",
]

TRANSPORT_FORMAT = "crossmodal-files-v1"
SOURCE_STATUS = "可交付"


class ConfigError(ValueError):
    """配置不合法（未知键、越界、与表1 只读口径冲突）。"""


def _strip_token(value: Any) -> Any:
    """去掉计划文档 settings 里常见的结尾句号/空白（如 ``格式：xlsx。``）。"""
    if isinstance(value, str):
        return value.strip().strip("。．. ").strip()
    return value


class OperatorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = sampling.OPERATOR_NUMBER
    id: str = sampling.OPERATOR
    name: str = sampling.OPERATOR_LABEL
    label: str = sampling.OPERATOR_LABEL
    version: str = sampling.OPERATOR_VERSION
    enabled: bool = True
    settings: List[str] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _non_empty_version(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("operator.version 不能为空")
        return value.strip()


class RouteSwitches(BaseModel):
    """路线开关；包内版本说明声明了启用路线时以包为准（见 inference）。"""

    model_config = ConfigDict(extra="forbid")

    image_text: bool = True
    audio_text: bool = False
    video_text: bool = False

    def enabled_routes(self) -> List[str]:
        return [route for route in sampling.ROUTE_ORDER
                if bool(getattr(self, route))]


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = TRANSPORT_FORMAT
    max_response_bytes: int = Field(default=64 * 1024 * 1024, ge=1024,
                                    le=1024 * 1024 * 1024)

    @field_validator("format")
    @classmethod
    def _check_format(cls, value: str) -> str:
        if _strip_token(value) != TRANSPORT_FORMAT:
            raise ValueError("transport.format 只能是 %s" % TRANSPORT_FORMAT)
        return TRANSPORT_FORMAT


class OperatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: OperatorMeta = Field(default_factory=OperatorMeta)
    # [v4.2] 阶段号按 §4.26 修正为 S8（交付样例），原 S7 与表1 不符
    stage_id: str = Field(default="S8", min_length=1)
    enabled: bool = True
    format: Literal["xlsx"] = "xlsx"
    source_status: str = SOURCE_STATUS
    max_samples_per_route: int = 10
    sampling_mode: Literal["random", "first"] = "random"
    sampling_seed: int = Field(default=26, ge=0, le=2 ** 31 - 1)
    description_summary_chars: int = Field(default=120, ge=1, le=4000)
    max_embedded_record_bytes: int = Field(default=32768, ge=0)
    require_deliverable_source: bool = False
    require_deliverable_record: bool = True
    route_source: Literal["package", "config"] = "package"
    routes: RouteSwitches = Field(default_factory=RouteSwitches)
    transport: TransportConfig = Field(default_factory=TransportConfig)

    @field_validator("stage_id")
    @classmethod
    def _check_stage(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("stage_id 不能为空")
        return value.strip()

    @field_validator("source_status")
    @classmethod
    def _check_source_status(cls, value: str) -> str:
        if _strip_token(value) != SOURCE_STATUS:
            raise ValueError("source_status 只能是 %s（表1 #26：只从可交付包抽样）"
                             % SOURCE_STATUS)
        return SOURCE_STATUS

    @field_validator("max_samples_per_route")
    @classmethod
    def _check_max_samples(cls, value: int) -> int:
        if not (1 <= int(value) <= sampling.TABLE1_MAX_SAMPLES_PER_ROUTE):
            raise ValueError("max_samples_per_route 必须在 1..%d 之间（表1 #26："
                             "每条已开启路线最多抽 %d 条）"
                             % (sampling.TABLE1_MAX_SAMPLES_PER_ROUTE,
                                sampling.TABLE1_MAX_SAMPLES_PER_ROUTE))
        return int(value)

    @model_validator(mode="before")
    @classmethod
    def _lift_plan_settings(cls, data: Any) -> Any:
        """兼容计划文档写法：``operator.settings`` 为 ``key=value`` 字符串数组。"""
        if not isinstance(data, dict):
            return data
        merged = dict(data)
        operator = merged.get("operator")
        if isinstance(operator, dict):
            settings = operator.get("settings")
            if isinstance(settings, (list, tuple)):
                for item in settings:
                    if not isinstance(item, str) or "=" not in item:
                        continue
                    key, _, raw = item.partition("=")
                    key = key.strip()
                    if not key or key in {"settings"}:
                        continue
                    text = raw.strip()
                    lowered = text.lower()
                    if lowered in {"true", "false"}:
                        merged.setdefault(key, lowered == "true")
                    elif text.lstrip("-").isdigit():
                        merged.setdefault(key, int(text))
                    else:
                        merged.setdefault(key, _strip_token(text))
        return merged

    def effective_enabled(self) -> bool:
        return bool(self.enabled and self.operator.enabled)

    def enabled_routes(self) -> List[str]:
        return self.routes.enabled_routes()

    def business_snapshot(self) -> Dict[str, Any]:
        """配置快照：只含业务开关与阈值，可安全写入血缘与示例。"""
        return {
            "enabled": self.effective_enabled(),
            "format": self.format,
            "source_status": self.source_status,
            "max_samples_per_route": self.max_samples_per_route,
            "table1_max_samples_per_route": sampling.TABLE1_MAX_SAMPLES_PER_ROUTE,
            "sampling_mode": self.sampling_mode,
            "sampling_seed": self.sampling_seed,
            "description_summary_chars": self.description_summary_chars,
            "max_embedded_record_bytes": self.max_embedded_record_bytes,
            "require_deliverable_source": self.require_deliverable_source,
            "require_deliverable_record": self.require_deliverable_record,
            "route_source": self.route_source,
            "routes": {route: bool(getattr(self.routes, route))
                       for route in sampling.ROUTE_ORDER},
        }


class PredictRequest(BaseModel):
    """``POST /predict`` 请求体（严格模式：未知字段直接 4xx）。"""

    model_config = ConfigDict(extra="forbid")

    uniqueId: Optional[str] = None
    unique_id: Optional[str] = None
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    overrides: Optional[Dict[str, Any]] = None

    @field_validator("uniqueId", "unique_id")
    @classmethod
    def _check_unique_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        from . import io as io_utils

        return io_utils.validate_unique_id(value)

    @model_validator(mode="after")
    def _require_unique_id(self) -> "PredictRequest":
        if not self.uniqueId and not self.unique_id:
            raise ValueError("uniqueId 不能为空")
        if self.uniqueId and self.unique_id and self.uniqueId != self.unique_id:
            raise ValueError("uniqueId 与 unique_id 不一致")
        return self

    def task_id(self) -> str:
        return str(self.uniqueId or self.unique_id)

    def override_payload(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        if self.config:
            merged.update(self.config)
        if self.overrides:
            merged.update(self.overrides)
        return merged
