"""部署配置、业务覆盖与 ``/predict`` 请求模型（pydantic，严格模式）。

分层（TECHNICAL_DESIGN §5）：
- ``deployment``：``operator.name`` / ``operator.version`` / ``stage_id`` / 端口等身份项，
  **不允许**被 ``/predict`` 覆盖；
- ``business``：表1 #20 声明的可调口径（``enabled``、``quality_subset_fallback``、
  ``topic_skew_threshold``、各测点的 ``upstream_first``、反向检索阈值），
  由 ``/predict`` 的 ``config`` 覆盖；
- 全模型禁止未声明字段，越界值一律 ``ConfigError``，
  保证错误配置不会静默生效。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Literal, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, ValidationError,
                      field_validator)

OPERATOR_NAME = "alignment_qc"
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S4"
FORMAL_OPERATOR_NUMBER = 20
# [v4.2] §4.20：回写前报告状态只能 pending_writeback
REPORT_STATUS = "pending_writeback"
VERSION_STATE_PENDING = "pending_release"
DEFAULT_PORT = 8120

#: 部署身份字段，禁止由 /predict 覆盖
DEPLOYMENT_KEYS = ("operator", "stage_id")

MetricMode = Literal["upstream_first", "local_required", "upstream_only", "disable"]


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
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class MetricsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ppl: MetricMode = "upstream_first"
    brisque: MetricMode = "upstream_first"
    snr: MetricMode = "upstream_first"
    ffmpeg: MetricMode = "upstream_first"


class ClipConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library: str = "auto"
    model_name: str = "ViT-B-32"
    weights: Optional[str] = None
    device: str = "cpu"
    batch_size: int = Field(default=8, ge=1, le=256)


class ReverseVerifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = True
    top_k: int = Field(default=5, ge=1, le=100)
    min_consistency: float = Field(default=0.25, ge=-1.0, le=1.0)
    clip: ClipConfig = Field(default_factory=ClipConfig)


class OperatorConfig(BaseModel):
    """算子完整配置：``configs/config.yaml`` 的结构。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorIdentity = Field(
        default_factory=lambda: OperatorIdentity(name=OPERATOR_NAME,
                                                version=OPERATOR_VERSION))
    stage_id: str = STAGE_ID
    #: 表1 #20「默认开启、必须」
    enabled: StrictBool = True
    #: 无上游指标时补测的子集比例
    quality_subset_fallback: float = Field(default=0.10, ge=0.0, le=1.0)
    #: 偏科阈值（> 该比例出待补清单，但不整批隔离）
    topic_skew_threshold: float = Field(default=0.70, gt=0.0, le=1.0)
    #: 质量报告状态：固定 pending_writeback，禁止提前封版
    report_status: str = REPORT_STATUS
    #: 模型/测点后端不可用时是否允许显式降级（degraded）继续
    allow_degraded: StrictBool = True
    min_text_chars: int = Field(default=1, ge=0, le=100000)
    silence_rms_threshold: float = Field(default=1e-4, ge=0.0, le=1.0)
    #: 视频取证帧数上限，沿用表1 #14 的「最多 8 帧」
    max_video_frames_probe: int = Field(default=8, ge=1, le=64)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    reverse_verify: ReverseVerifyConfig = Field(default_factory=ReverseVerifyConfig)

    @field_validator("report_status")
    @classmethod
    def _freeze_report_status(cls, value: str) -> str:
        if value != REPORT_STATUS:
            raise ValueError("report_status 必须为 %s（抽检尚未回写，不允许提前封版）"
                             % REPORT_STATUS)
        return value

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
            "quality_subset_fallback": self.quality_subset_fallback,
            "topic_skew_threshold": self.topic_skew_threshold,
            "report_status": self.report_status,
            "allow_degraded": self.allow_degraded,
            "min_text_chars": self.min_text_chars,
            "silence_rms_threshold": self.silence_rms_threshold,
            "max_video_frames_probe": self.max_video_frames_probe,
            "metrics": self.metrics.model_dump(),
            "reverse_verify": {
                "enabled": self.reverse_verify.enabled,
                "top_k": self.reverse_verify.top_k,
                "min_consistency": self.reverse_verify.min_consistency,
                "clip": self.reverse_verify.clip.model_dump(),
            },
        }

    def deployment_dict(self) -> Dict[str, Any]:
        return {
            "operator": self.operator.model_dump(),
            "stage_id": self.stage_id,
            "formal_operator_number": FORMAL_OPERATOR_NUMBER,
            "port": DEFAULT_PORT,
            "report_status": REPORT_STATUS,
            "version_state": VERSION_STATE_PENDING,
        }


class BusinessOverrides(BaseModel):
    """``/predict`` 的 ``config`` 允许覆盖的业务字段（未列出的键一律拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[StrictBool] = None
    quality_subset_fallback: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    topic_skew_threshold: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    allow_degraded: Optional[StrictBool] = None
    min_text_chars: Optional[int] = Field(default=None, ge=0, le=100000)
    silence_rms_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    max_video_frames_probe: Optional[int] = Field(default=None, ge=1, le=64)
    metrics: Optional[MetricsConfig] = None
    reverse_verify: Optional[ReverseVerifyConfig] = None


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
    # [v4.2] §4.20 接口二别名：subset_ratio → 现有子集比例字段
    data = dict(raw)
    if "subset_ratio" in data:
        data["quality_subset_fallback"] = data.pop("subset_ratio")
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
