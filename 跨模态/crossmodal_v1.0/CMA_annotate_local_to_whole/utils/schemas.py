"""配置与请求模式：身份、冻结口径、范围校验与「请求侧不可改写」约束。

口径来源：`DEVELOPMENT_PLAN.md` 第 1/3 节与表1 #17 行；
`DEVELOPMENT_PLAN` 只声明了 `enabled` / `detector_backend` / `model` / `classes` /
`video_timestamp_source` 五项冻结设置，其余为可实现细节，并在报告中标注来源。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictInt,
                      ValidationError, field_validator, model_validator)

# --------------------------------------------------------------------------- #
# 身份与冻结常量
# --------------------------------------------------------------------------- #

FORMAL_OPERATOR_NUMBER = 17
OPERATOR_NAME = "local_to_whole_annotation"
OPERATOR_NAME_ZH = "标注局部对整体"
DEFAULT_OPERATOR_VERSION = "v1.0.0"

#: 表1「流程环节：3 数据加工」→ 阶段 S3（与 demo 既有 S3 算子保持一致）
STAGE_ID = "S3"

#: 表1 冻结：模型/后端
DETECTOR_MODEL = "YOLOv8n"
FIXED_GENERAL_CLASSES_ONLY = True
GENERAL_CLASS_SOURCE = "coco80"
FIXED_VIDEO_TIMESTAMP_SOURCE = "transcript"

#: 表1 冻结：默认关闭
FIXED_DEFAULT_ENABLED = False

DETECTOR_BACKENDS = ("auto", "yolov8n", "none")
MODEL_NAME_ALIASES = {
    "yolov8n": DETECTOR_MODEL,
    "yolo-v8n": DETECTOR_MODEL,
    "yolo v8n": DETECTOR_MODEL,
    "yolov8-n": DETECTOR_MODEL,
    "yolov8_n": DETECTOR_MODEL,
}
OPERATOR_NAME_ALIASES = {
    OPERATOR_NAME: OPERATOR_NAME,
    OPERATOR_NAME_ZH: OPERATOR_NAME,
    "标注局部对整体": OPERATOR_NAME,
    "17_标注局部对整体": OPERATOR_NAME,
}

#: 由 DEVELOPMENT_PLAN 第 3 节 transport.max_response_bytes
MAX_RESPONSE_BYTES = 67_108_864

CONFIG_ENV_VAR = "OPERATOR_CONFIG"
DEFAULT_CONFIG_RELATIVE = os.path.join("configs", "config.yaml")

#: 请求侧允许改写的业务参数（模型/设备/路径/身份一律拒绝）
BUSINESS_OVERRIDE_KEYS = (
    "enabled", "allow_degraded", "require_whole_aligned",
    "max_text_chars", "max_time_ranges", "max_images_per_record",
)
DETECTOR_OVERRIDE_KEYS = ("conf_threshold", "iou_threshold", "max_boxes", "image_size")


class ConfigError(ValueError):
    """配置不可用：解析失败、越界或试图改写冻结口径。"""


class OperatorIdentity(BaseModel):
    """`operator` 分组：身份与冻结设置。"""

    model_config = ConfigDict(extra="forbid")

    number: StrictInt = FORMAL_OPERATOR_NUMBER
    name: str = OPERATOR_NAME_ZH
    version: str = DEFAULT_OPERATOR_VERSION
    #: 计划中 `operator.enabled` 与顶层 `enabled` 并存，这里统一收敛到顶层
    enabled: Optional[StrictBool] = None
    #: 计划中的冻结设置清单，仅作口径留档，不参与运行期决策
    settings: Optional[List[str]] = None

    @field_validator("number")
    @classmethod
    def _check_number(cls, value: int) -> int:
        if int(value) != FORMAL_OPERATOR_NUMBER:
            raise ValueError("operator.number must be %d, got %r"
                             % (FORMAL_OPERATOR_NUMBER, value))
        return int(value)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        key = str(value).strip().lower()
        if key not in OPERATOR_NAME_ALIASES:
            raise ValueError("operator.name must be %r or %r, got %r"
                             % (OPERATOR_NAME_ZH, OPERATOR_NAME, value))
        return OPERATOR_NAME_ALIASES[key]

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("operator.version must not be empty")
        return text


class DetectorConfig(BaseModel):
    """检测后端参数；`weights`/`device` 属部署口径，只能写在配置文件里。"""

    model_config = ConfigDict(extra="forbid")

    weights: Optional[str] = None
    device: str = "cpu"
    conf_threshold: float = Field(default=0.25, ge=0.05, le=0.95)
    iou_threshold: float = Field(default=0.45, ge=0.10, le=0.95)
    max_boxes: StrictInt = Field(default=50, ge=1, le=500)
    image_size: StrictInt = Field(default=640, ge=320, le=1280)
    general_classes_only: StrictBool = FIXED_GENERAL_CLASSES_ONLY

    @field_validator("device")
    @classmethod
    def _check_device(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("detector.device must not be empty")
        return text

    @field_validator("image_size")
    @classmethod
    def _check_image_size(cls, value: int) -> int:
        if int(value) % 32 != 0:
            raise ValueError("detector.image_size must be a multiple of 32")
        return int(value)

    @field_validator("general_classes_only")
    @classmethod
    def _check_general_classes_only(cls, value: bool) -> bool:
        if value is not FIXED_GENERAL_CLASSES_ONLY:
            raise ValueError(
                "detector.general_classes_only is frozen to true by 表1 #17 "
                "（专用设备不预填，由标注员手画）")
        return FIXED_GENERAL_CLASSES_ONLY


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = "crossmodal-files-v1"
    max_response_bytes: StrictInt = Field(default=MAX_RESPONSE_BYTES,
                                          ge=1024, le=2 * 1024 ** 3)

    @field_validator("format")
    @classmethod
    def _check_format(cls, value: str) -> str:
        if str(value) != "crossmodal-files-v1":
            raise ValueError("transport.format is frozen to crossmodal-files-v1")
        return "crossmodal-files-v1"


class OperatorConfig(BaseModel):
    """算子 #17 的完整生效配置。"""

    model_config = ConfigDict(extra="forbid")

    operator: OperatorIdentity = Field(default_factory=OperatorIdentity)
    stage_id: str = STAGE_ID
    enabled: StrictBool = FIXED_DEFAULT_ENABLED
    detector_backend: str = "auto"
    model_name: str = DETECTOR_MODEL
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    allow_degraded: StrictBool = True
    require_whole_aligned: StrictBool = True
    video_timestamp_source: str = FIXED_VIDEO_TIMESTAMP_SOURCE
    max_text_chars: StrictInt = Field(default=500, ge=1, le=2000)
    max_time_ranges: StrictInt = Field(default=50, ge=1, le=500)
    max_images_per_record: StrictInt = Field(default=4, ge=1, le=16)
    #: 无图视频每条记录最多检测的关键帧数（帧来自 #6 抽帧；只做通用类别预填）
    max_frames_per_record: StrictInt = Field(default=5, ge=1, le=32)
    transport: TransportConfig = Field(default_factory=TransportConfig)

    # ----------------------------------------------------------------- #
    # 校验
    # ----------------------------------------------------------------- #

    @model_validator(mode="before")
    @classmethod
    def _lift_operator_enabled(cls, payload: Any) -> Any:
        """兼容计划写法 `operator.enabled`：收敛到顶层 `enabled`。"""
        if not isinstance(payload, dict):
            return payload
        operator = payload.get("operator")
        if isinstance(operator, dict) and "enabled" in operator:
            lifted = operator["enabled"]
            if "enabled" in payload and payload["enabled"] != lifted:
                raise ValueError(
                    "operator.enabled conflicts with top-level enabled")
            payload = dict(payload)
            payload["enabled"] = lifted
        return payload

    @field_validator("stage_id")
    @classmethod
    def _check_stage(cls, value: str) -> str:
        text = str(value).strip().upper()
        if len(text) < 2 or text[0] != "S" or not text[1:].isdigit():
            raise ValueError("stage_id must look like S3, got %r" % value)
        return text

    @field_validator("detector_backend")
    @classmethod
    def _check_backend(cls, value: str) -> str:
        text = str(value).strip().lower()
        if text not in DETECTOR_BACKENDS:
            raise ValueError("detector_backend must be one of %s, got %r"
                             % (", ".join(DETECTOR_BACKENDS), value))
        if text == "none" and FIXED_DEFAULT_ENABLED:
            return text
        return text

    @field_validator("model_name")
    @classmethod
    def _check_model(cls, value: str) -> str:
        key = str(value).strip().lower()
        if key not in MODEL_NAME_ALIASES:
            raise ValueError(
                "model_name must be %r（表1 #17 明令不得部署 Grounding DINO 或自训检测模型），got %r"
                % (DETECTOR_MODEL, value))
        return MODEL_NAME_ALIASES[key]

    @field_validator("video_timestamp_source")
    @classmethod
    def _check_timestamp_source(cls, value: str) -> str:
        text = str(value).strip().rstrip("。.")
        if text != FIXED_VIDEO_TIMESTAMP_SOURCE:
            raise ValueError(
                "video_timestamp_source is frozen to %r by 表1 #17, got %r"
                % (FIXED_VIDEO_TIMESTAMP_SOURCE, value))
        return FIXED_VIDEO_TIMESTAMP_SOURCE

    # ----------------------------------------------------------------- #
    # 校验入口：把 pydantic 异常统一成 ConfigError
    # ----------------------------------------------------------------- #

    @classmethod
    def model_validate(cls, obj, **kwargs):  # type: ignore[override]
        try:
            return super().model_validate(obj, **kwargs)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc


class DetectorOverrides(BaseModel):
    """请求侧允许改写的检测阈值（不含权重、设备与后端选择）。"""

    model_config = ConfigDict(extra="forbid")

    conf_threshold: Optional[float] = Field(default=None, ge=0.05, le=0.95)
    iou_threshold: Optional[float] = Field(default=None, ge=0.10, le=0.95)
    max_boxes: Optional[StrictInt] = Field(default=None, ge=1, le=500)
    image_size: Optional[StrictInt] = Field(default=None, ge=320, le=1280)


class BusinessOverrides(BaseModel):
    """请求侧业务覆盖参数白名单；其余键一律拒绝。"""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[StrictBool] = None
    allow_degraded: Optional[StrictBool] = None
    require_whole_aligned: Optional[StrictBool] = None
    max_text_chars: Optional[StrictInt] = Field(default=None, ge=1, le=2000)
    max_time_ranges: Optional[StrictInt] = Field(default=None, ge=1, le=500)
    max_images_per_record: Optional[StrictInt] = Field(default=None, ge=1, le=16)
    detector: Optional[DetectorOverrides] = None


# --------------------------------------------------------------------------- #
# 加载与合并
# --------------------------------------------------------------------------- #

def default_config_path() -> Path:
    """配置文件解析顺序：显式参数 → 环境变量 → 本目录 `configs/config.yaml`。"""
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / DEFAULT_CONFIG_RELATIVE


def read_config_payload(path) -> Dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise ConfigError("config file does not exist: %s" % target)
    try:
        raw = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("config file is not valid UTF-8: %s" % exc) from exc
    except OSError as exc:
        raise ConfigError("config file is unreadable: %s" % exc) from exc
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError("config file is not valid YAML: %s" % exc) from exc
    if not isinstance(payload, dict):
        raise ConfigError("config root must be a mapping")
    return payload


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def validated_overrides(overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """校验并摊平请求侧覆盖参数；非法键直接 ConfigError。"""
    if not overrides:
        return {}
    try:
        model = BusinessOverrides.model_validate(overrides)
    except ValidationError as exc:
        raise ConfigError("invalid request config: %s" % exc) from exc
    dumped = model.model_dump(exclude_none=True)
    flattened: Dict[str, Any] = {}
    for key, value in dumped.items():
        flattened[key] = value
    return flattened


def load_config(path=None, overrides: Optional[Dict[str, Any]] = None) -> OperatorConfig:
    """加载并校验配置；应用请求侧业务覆盖（白名单）。"""
    target = Path(path) if path else default_config_path()
    payload = read_config_payload(target)
    patch = validated_overrides(overrides)
    if patch:
        payload = _deep_merge(payload, patch)
    return OperatorConfig.model_validate(payload)


def config_identity(config: OperatorConfig) -> Dict[str, Any]:
    return {
        "operator": config.operator.name,
        "operatorNameZh": OPERATOR_NAME_ZH,
        "formalOperatorNumber": config.operator.number,
        "operatorVersion": config.operator.version,
        "stageId": config.stage_id,
        "enabled": bool(config.enabled),
    }
