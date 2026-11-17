"""配置与请求模型（严格校验，越界即失败）。

表1 口径：
- ``enabled`` 默认 **false**（工具默认关闭）；
- ``min_clip_score`` 默认 0.25，可配范围 **0.20–0.40**；
- ``tokenizer`` 取 ``pretrained``（复用预训练文本产线分词）或 ``jieba``；
- CLIP 复用 #12/#14 的同一实例与配方：``CLIP-ViT/B-32``（open_clip ``ViT-B-32``）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator, model_validator)

from . import io as io_utils

__all__ = [
    "CLASS_CALIBRATION_CHOICES",
    "CLIP_MODEL_NAME",
    "CROP_PADDING_MAX",
    "ConfigError",
    "ClipConfig",
    "CropConfig",
    "Envelope",
    "FORMAL_OPERATOR_NUMBER",
    "MIN_CLIP_SCORE_DEFAULT",
    "MIN_CLIP_SCORE_MAX",
    "MIN_CLIP_SCORE_MIN",
    "OPERATOR_NAME",
    "OPERATOR_VERSION",
    "OVERRIDABLE_KEYS",
    "OperatorConfig",
    "PredictRequest",
    "STAGE_ID",
    "SUPPORTED_ARCHS",
    "SUPPRESSION_CHOICES",
    "TOKENIZER_CHOICES",
    "TOKENIZER_JIEBA",
    "TOKENIZER_PRETRAINED",
    "normalize_arch",
    "to_config_error",
]

OPERATOR_NAME = "phrase_region_annotation"
OPERATOR_VERSION = "v1.0.0"
STAGE_ID = "S3"
FORMAL_OPERATOR_NUMBER = 18
CLIP_MODEL_NAME = "CLIP-ViT/B-32"

MIN_CLIP_SCORE_DEFAULT = 0.25
MIN_CLIP_SCORE_MIN = 0.20
MIN_CLIP_SCORE_MAX = 0.40

TOKENIZER_PRETRAINED = "pretrained"
TOKENIZER_JIEBA = "jieba"
TOKENIZER_CHOICES = (TOKENIZER_PRETRAINED, TOKENIZER_JIEBA)

SUPPORTED_ARCHS = ("ViT-B-32", "ViT-B-16", "ViT-L-14")
_ARCH_ALIASES = {
    "vit-b-32": "ViT-B-32",
    "vit-b32": "ViT-B-32",
    "vit-base-patch32": "ViT-B-32",
    "vit-b-16": "ViT-B-16",
    "vit-b16": "ViT-B-16",
    "vit-base-patch16": "ViT-B-16",
    "vit-l-14": "ViT-L-14",
    "vit-large-patch14": "ViT-L-14",
}
LIBRARY_CHOICES = ("auto", "open_clip", "transformers", "torchvision")

#: 裁图扩边上限（框外扩边比例）；背景抑制只在扩边后才有「框外」可抑制。
CROP_PADDING_MAX = 0.25
SUPPRESSION_CHOICES = ("none", "mean", "gray")

#: 类别校准方法（只影响一词一框的指派，不改原始分数与 min_clip_score 阈值）：
#: none = 直接按原始余弦指派；znorm = 按框类别做列方向标准化后指派；
#: prior = 原始余弦 + 类别名语义先验（cos(匹配文本, 框类别名) × weight）后指派。
CLASS_CALIBRATION_CHOICES = ("none", "znorm", "prior")
#: 类别名先验的权重上限（先验只是二次排序项，不能盖过原始 CLIP 分）
CLASS_PRIOR_WEIGHT_MAX = 0.5

OVERRIDABLE_KEYS = ("enabled", "min_clip_score", "tokenizer", "allow_degraded",
                    "max_tokens", "max_text_chars", "merge_phrases", "clip", "crop",
                    "class_calibration", "class_prior_weight")


class ConfigError(ValueError):
    """配置非法（越界、未知键、类型错误）。"""


def normalize_arch(value: Optional[str]) -> Optional[str]:
    """把各种写法归一到 open_clip 架构名：``CLIP-ViT/B-32`` → ``ViT-B-32``。"""
    if value is None:
        return None
    token = str(value).strip()
    if not token:
        return None
    lowered = token.lower().replace("\\", "/").replace("/", "-").replace("_", "-")
    if "clip-" in lowered:
        lowered = lowered.rsplit("clip-", 1)[-1]
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return _ARCH_ALIASES.get(slug, token)


class ClipConfig(BaseModel):
    """CLIP 后端配置（复用 #12/#14 的实例与配方）。"""

    model_config = ConfigDict(extra="forbid")

    library: str = "auto"
    model_name: str = CLIP_MODEL_NAME
    weights: Optional[str] = None
    device: str = "cpu"
    batch_size: StrictInt = 8

    @field_validator("library")
    @classmethod
    def _check_library(cls, value: str) -> str:
        candidate = str(value).strip().lower()
        if candidate not in LIBRARY_CHOICES:
            raise ValueError("clip.library 必须是 %s 之一" % (LIBRARY_CHOICES,))
        return candidate

    @field_validator("model_name")
    @classmethod
    def _check_model_name(cls, value: str) -> str:
        arch = normalize_arch(value)
        if arch not in SUPPORTED_ARCHS:
            raise ValueError("clip.model_name 仅支持 %s，收到 %r"
                             % ("、".join(SUPPORTED_ARCHS), value))
        return arch

    @field_validator("device")
    @classmethod
    def _check_device(cls, value: str) -> str:
        candidate = str(value).strip()
        if not candidate:
            raise ValueError("clip.device 不能为空")
        return candidate

    @field_validator("batch_size")
    @classmethod
    def _check_batch_size(cls, value: int) -> int:
        if not 1 <= int(value) <= 64:
            raise ValueError("clip.batch_size 必须在 1–64 之间")
        return int(value)


class CropConfig(BaseModel):
    """裁图质量配置。

    - ``padding_ratio``：框外扩边比例（相对框宽/高），0 表示紧贴检测框；
    - ``background_suppression``：扩边后把框外环形区域填充为纯色
      （``mean``=框内均值，``gray``=中性灰），让 CLIP 只关注框内目标。
    """

    model_config = ConfigDict(extra="forbid")

    padding_ratio: float = 0.0
    background_suppression: str = "none"

    @field_validator("padding_ratio", mode="before")
    @classmethod
    def _check_padding(cls, value: Any) -> float:
        if isinstance(value, bool) or value is None:
            raise ValueError("crop.padding_ratio 必须是数值")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("crop.padding_ratio 必须是数值，收到 %r" % (value,))
        if not 0.0 <= number <= CROP_PADDING_MAX:
            raise ValueError("crop.padding_ratio 必须在 0.00–%.2f 之间" % CROP_PADDING_MAX)
        return number

    @field_validator("background_suppression")
    @classmethod
    def _check_suppression(cls, value: str) -> str:
        candidate = str(value).strip().lower()
        if candidate not in SUPPRESSION_CHOICES:
            raise ValueError("crop.background_suppression 必须是 %s 之一"
                             % (SUPPRESSION_CHOICES,))
        return candidate

    @model_validator(mode="after")
    def _check_combination(self) -> "CropConfig":
        if self.background_suppression != "none" and self.padding_ratio <= 0:
            raise ValueError("crop.background_suppression 需要 padding_ratio > 0："
                             "紧贴框没有可抑制的框外区域")
        return self


class OperatorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = OPERATOR_NAME
    version: str = OPERATOR_VERSION


class OperatorConfig(BaseModel):
    """算子 18 配置。未知键直接失败，避免静默忽略配置错误。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorMeta = Field(default_factory=OperatorMeta)
    stage_id: str = STAGE_ID
    enabled: StrictBool = False
    min_clip_score: float = MIN_CLIP_SCORE_DEFAULT
    tokenizer: str = TOKENIZER_PRETRAINED
    max_tokens: StrictInt = 64
    max_text_chars: StrictInt = 500
    merge_phrases: StrictBool = True
    class_calibration: str = "prior"
    class_prior_weight: float = 0.05
    allow_degraded: StrictBool = False
    clip: ClipConfig = Field(default_factory=ClipConfig)
    crop: CropConfig = Field(default_factory=CropConfig)

    @field_validator("stage_id")
    @classmethod
    def _check_stage(cls, value: str) -> str:
        candidate = str(value).strip()
        if not candidate:
            raise ValueError("stage_id 不能为空")
        return candidate

    @field_validator("min_clip_score", mode="before")
    @classmethod
    def _check_min_score(cls, value: Any) -> float:
        if isinstance(value, bool) or value is None:
            raise ValueError("min_clip_score 必须是数值")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("min_clip_score 必须是数值，收到 %r" % (value,))
        if not MIN_CLIP_SCORE_MIN <= number <= MIN_CLIP_SCORE_MAX:
            raise ValueError("min_clip_score 必须在 %.2f–%.2f 之间（表1 口径）"
                             % (MIN_CLIP_SCORE_MIN, MIN_CLIP_SCORE_MAX))
        return number

    @field_validator("tokenizer")
    @classmethod
    def _check_tokenizer(cls, value: str) -> str:
        candidate = str(value).strip().lower()
        if candidate not in TOKENIZER_CHOICES:
            raise ValueError("tokenizer 必须是 %s 之一" % (TOKENIZER_CHOICES,))
        return candidate

    @field_validator("class_calibration")
    @classmethod
    def _check_class_calibration(cls, value: str) -> str:
        candidate = str(value).strip().lower()
        if candidate not in CLASS_CALIBRATION_CHOICES:
            raise ValueError("class_calibration 必须是 %s 之一"
                             % (CLASS_CALIBRATION_CHOICES,))
        return candidate

    @field_validator("class_prior_weight", mode="before")
    @classmethod
    def _check_class_prior_weight(cls, value: Any) -> float:
        if isinstance(value, bool) or value is None:
            raise ValueError("class_prior_weight 必须是数值")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("class_prior_weight 必须是数值，收到 %r" % (value,))
        if not 0.0 <= number <= CLASS_PRIOR_WEIGHT_MAX:
            raise ValueError("class_prior_weight 必须在 0–%.1f 之间"
                             % CLASS_PRIOR_WEIGHT_MAX)
        return number

    @field_validator("max_tokens")
    @classmethod
    def _check_max_tokens(cls, value: int) -> int:
        if not 1 <= int(value) <= 512:
            raise ValueError("max_tokens 必须在 1–512 之间")
        return int(value)

    @field_validator("max_text_chars")
    @classmethod
    def _check_max_text_chars(cls, value: int) -> int:
        if not 1 <= int(value) <= 10000:
            raise ValueError("max_text_chars 必须在 1–10000 之间")
        return int(value)

    def business_snapshot(self) -> Dict[str, Any]:
        return {"enabled": bool(self.enabled),
                "min_clip_score": float(self.min_clip_score),
                "min_clip_score_range": [MIN_CLIP_SCORE_MIN, MIN_CLIP_SCORE_MAX],
                "tokenizer": self.tokenizer,
                "max_tokens": int(self.max_tokens),
                "max_text_chars": int(self.max_text_chars),
                "merge_phrases": bool(self.merge_phrases),
                "class_calibration": self.class_calibration,
                "class_prior_weight": float(self.class_prior_weight),
                "allow_degraded": bool(self.allow_degraded),
                "clip_model": CLIP_MODEL_NAME,
                "pairing": "one_to_one",
                "requires_human_confirmation": True}

    def crop_snapshot(self) -> Dict[str, Any]:
        return {"padding_ratio": float(self.crop.padding_ratio),
                "background_suppression": self.crop.background_suppression}

    def clip_snapshot(self) -> Dict[str, Any]:
        return {"library": self.clip.library,
                "model_name": self.clip.model_name,
                "clip_model": CLIP_MODEL_NAME,
                "weights": self.clip.weights,
                "device": self.clip.device,
                "batch_size": int(self.clip.batch_size)}


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


def min_score_range() -> Tuple[float, float]:
    return MIN_CLIP_SCORE_MIN, MIN_CLIP_SCORE_MAX
