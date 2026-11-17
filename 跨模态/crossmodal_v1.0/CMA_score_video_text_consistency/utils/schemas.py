"""配置与请求模型（Pydantic v2，严格类型 + 定义域校验）。

表1 冻结项（不可被 ``/predict.config`` 改写为其它口径）：
- ``enabled`` 默认 false；
- ``low_score_threshold`` 复用图文低分阈值 0.25（可配 0.20–0.40）；
- ``max_frames_per_segment`` 范围 1–8；
- 模型族固定 CLIP-ViT/B-32。

布尔字段一律 ``StrictBool``：``"false"`` 这类字符串必须被拒绝而不是被当成 True。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator, model_validator)

OPERATOR_NAME = "video_text_scoring"
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S3"
FORMAL_OPERATOR_NUMBER = 14

#: 表1 低分阈值定义域（复用图文评分低分阈值 0.25）
LOW_SCORE_THRESHOLD_MIN = 0.20
LOW_SCORE_THRESHOLD_MAX = 0.40
#: 表1 **整段视频合计**帧数上限定义域（配置键名沿用 max_frames_per_segment）
MAX_FRAMES_MIN = 1
MAX_FRAMES_MAX = 8

#: CLIP-ViT/B-32 等价别名（统一归一到 ``ViT-B-32``）
ARCH_ALIASES = {
    "clip-vit/b-32": "ViT-B-32",
    "clip-vit-b-32": "ViT-B-32",
    "vit-b-32": "ViT-B-32",
    "vit-b/32": "ViT-B-32",
    "clip_vit_b_32": "ViT-B-32",
    "openai/clip-vit-base-patch32": "ViT-B-32",
    "openai:clip-vit-base-patch32": "ViT-B-32",
}
DEFAULT_ARCH = "ViT-B-32"

ALLOWED_LIBRARIES = ("auto", "open_clip", "clip", "transformers", "none")
ALLOWED_DEVICES = ("cpu", "cuda", "auto")

#: ``/predict.config`` 允许覆盖的键（读写边界）
OVERRIDABLE_KEYS = (
    "enabled", "low_score_threshold", "max_frames_per_segment",
    "allow_degraded", "max_text_chars", "temporal_route_exclusion", "clip",
)
OVERRIDABLE_CLIP_KEYS = ("library", "model_name", "weights", "device", "batch_size")


class ConfigError(ValueError):
    """配置缺失、不可解析或超出表1 定义域。"""


def normalize_arch(value: Any) -> str:
    """把 CLIP 架构别名归一到 ``ViT-B-32``；``None`` 取默认。"""
    if value is None:
        return DEFAULT_ARCH
    if not isinstance(value, str):
        raise ConfigError("clip.model_name 必须是字符串")
    key = value.strip().lower()
    if not key:
        return DEFAULT_ARCH
    if key in ARCH_ALIASES:
        return ARCH_ALIASES[key]
    raise ConfigError("clip.model_name 只支持 CLIP-ViT/B-32（表1 固定模型），收到：%s" % value)


class OperatorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name_matches_contract(cls, value: str) -> str:
        if value != OPERATOR_NAME:
            raise ValueError("operator.name 必须是 %s，收到：%s" % (OPERATOR_NAME, value))
        return value


class ClipConfig(BaseModel):
    """复用图文评分的 CLIP-ViT/B-32 后端配置。"""

    model_config = ConfigDict(extra="forbid")

    library: str = "auto"
    model_name: str = DEFAULT_ARCH
    weights: Optional[str] = None
    device: str = "cpu"
    batch_size: StrictInt = Field(default=8, ge=1, le=256)

    @field_validator("library")
    @classmethod
    def _library_allowed(cls, value: str) -> str:
        if value not in ALLOWED_LIBRARIES:
            raise ValueError("clip.library 必须是 %s 之一" % (list(ALLOWED_LIBRARIES),))
        return value

    @field_validator("model_name")
    @classmethod
    def _arch_normalized(cls, value: str) -> str:
        return normalize_arch(value)

    @field_validator("device")
    @classmethod
    def _device_allowed(cls, value: str) -> str:
        if value not in ALLOWED_DEVICES:
            raise ValueError("clip.device 必须是 %s 之一" % (list(ALLOWED_DEVICES),))
        return value

    @field_validator("weights")
    @classmethod
    def _weights_stripped(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None


class OperatorConfig(BaseModel):
    """算子运行配置（表1 冻结项 + 可覆盖业务项）。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorMeta
    stage_id: str
    enabled: StrictBool = False
    # [round10 标定] 默认 0.25→0.23：round9 同源 62 条最低分 0.2313，0.23 时正例零
    # 误隔离；错配负例 88.9% 隔离、0 条达到自动对齐（见 crossmodal_test5/阈值标定.md）。
    low_score_threshold: float = 0.23
    max_frames_per_segment: StrictInt = 8
    max_text_chars: StrictInt = Field(default=500, ge=1, le=5000)
    allow_degraded: StrictBool = False
    #: 时序路线边界：true（默认）时，记录自带 timeline/temporal_bindings/temporal_anchors
    #: 就不参与 #14 评分（判 not_applicable，不隔离），交给 #15。false 仅用于回归对比。
    temporal_route_exclusion: StrictBool = True
    clip: ClipConfig = Field(default_factory=ClipConfig)

    @field_validator("stage_id")
    @classmethod
    def _stage_matches_contract(cls, value: str) -> str:
        if value != STAGE_ID:
            raise ValueError("stage_id 必须是 %s（表1 第 3 环节）" % STAGE_ID)
        return value

    @field_validator("low_score_threshold")
    @classmethod
    def _threshold_in_range(cls, value: float) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("low_score_threshold 必须是数字")
        if not LOW_SCORE_THRESHOLD_MIN <= float(value) <= LOW_SCORE_THRESHOLD_MAX:
            raise ValueError("low_score_threshold 必须落在 %.2f–%.2f"
                             % (LOW_SCORE_THRESHOLD_MIN, LOW_SCORE_THRESHOLD_MAX))
        return float(value)

    @field_validator("max_frames_per_segment")
    @classmethod
    def _max_frames_in_range(cls, value: int) -> int:
        if not MAX_FRAMES_MIN <= int(value) <= MAX_FRAMES_MAX:
            raise ValueError("max_frames_per_segment 必须落在 %d–%d"
                             % (MAX_FRAMES_MIN, MAX_FRAMES_MAX))
        return int(value)

    @model_validator(mode="after")
    def _stage_matches_operator(self) -> "OperatorConfig":
        return self


class BusinessOverrides(BaseModel):
    """``/predict.config`` 白名单覆盖项；未列出的键一律拒绝。"""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[StrictBool] = None
    low_score_threshold: Optional[float] = None
    max_frames_per_segment: Optional[StrictInt] = None
    allow_degraded: Optional[StrictBool] = None
    max_text_chars: Optional[StrictInt] = None
    temporal_route_exclusion: Optional[StrictBool] = None
    clip: Optional[ClipConfig] = None


class PredictRequest(BaseModel):
    """``POST /predict`` 请求体：``isLastOperator`` 必须是真正的 JSON 布尔。"""

    model_config = ConfigDict(extra="ignore")

    uniqueId: str = Field(min_length=1)
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class UploadData(BaseModel):
    uniqueId: str
    entrypoint: str
    files: List[str] = Field(default_factory=list)
    mediaFiles: List[str] = Field(default_factory=list)
    sizeBytes: int = 0


class UploadResponse(BaseModel):
    code: int
    msg: str
    data: Optional[UploadData] = None


class PredictResponse(BaseModel):
    code: int
    msg: str
    data: Optional[Dict[str, Any]] = None


class RemoveData(BaseModel):
    uniqueId: str
    removed: bool


class RemoveResponse(BaseModel):
    code: int
    msg: str
    data: Optional[RemoveData] = None


class HealthResponse(BaseModel):
    code: int
    msg: str
    data: Dict[str, Any]


def to_config_error(exc: Exception, where: str) -> ConfigError:
    """把 pydantic 校验错误转成可读的 ``ConfigError``（不含完整堆栈）。"""
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:8]:
            location = ".".join(str(item) for item in error.get("loc", ())) or "<root>"
            parts.append("%s: %s" % (location, error.get("msg")))
        return ConfigError("%s 校验失败：%s" % (where, "; ".join(parts)))
    return ConfigError("%s 非法：%s" % (where, type(exc).__name__))
