"""算子 7 数据契约：部署配置、请求覆盖与请求/响应模型。

设计约束（TECHNICAL_DESIGN §2）：
- ``operator`` 与 ``stage_id`` 是部署配置，不接受请求改写血缘身份；
- 未知字段一律报错（``extra="forbid"``），不静默采用另一个算法；
- 数值必须有限并落在定义域内，枚举未知值报错。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
)


class ConfigError(ValueError):
    """配置结构、定义域或枚举非法。"""


def _check_ratio(value):
    """数值类型校验：只接受 int/float（拒绝字符串与布尔），并要求有限。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("max_av_duration_diff_ratio 必须是数值（不接受字符串/布尔）")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("max_av_duration_diff_ratio 必须是有限数值")
    return number


class StrictBaseModel(BaseModel):
    """禁止未知字段：部署配置和请求体都不接受额外键。"""

    model_config = ConfigDict(extra="forbid")


class OperatorIdentity(StrictBaseModel):
    """算子身份，来自部署 YAML，不由请求覆盖。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class TranscriptionConfig(StrictBaseModel):
    """表1 #8 语音转写设置（**默认关闭**）。

    两个开关任一关闭都不加载模型：总开关 ``OperatorConfig.enabled``，
    以及本节的 ``enabled``（「是否做语音转写」）。
    ``model``/``language`` 是表1 固定项，写别的值直接校验失败。
    """

    #: 「是否做语音转写」，默认关闭
    enabled: StrictBool = False
    #: 表1 固定模型：只允许 whisper-base（边界：不换成更大的 Whisper）
    model: Literal["whisper-base"] = "whisper-base"
    #: 表1 固定按中文解码
    language: Literal["zh"] = "zh"
    #: 本地权重根目录与子目录，只从本地加载（local_files_only）。
    #: 默认 ``weight``（与显式 ``.`` 等价）：按 ``$MODEL_ROOT``／算子本地
    #: ``<算子目录>/weight`` 解析，因此 ``weight`` + ``whisper-base`` 即
    #: 算子本地 ``weight/whisper-base``（见 ``weights_dir()`` 兼容分支）。
    model_root: str = Field(default="weight", min_length=1)
    model_dir: str = Field(default="whisper-base", min_length=1)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    #: 单段落最大时长（秒）：超过即按段落切开
    max_segment_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    #: 相邻转写片段间隔超过该秒数即切为新段落
    paragraph_silence_seconds: float = Field(default=0.6, ge=0.0, le=30.0)
    beam_size: int = Field(default=1, ge=1, le=10)
    # 可选中文书写提示，仅用于提升 Whisper-base 解码稳定性；空值表示不使用。
    initial_prompt: str = Field(default="", max_length=200)
    #: 送 ASR 前的时长/信噪比预检（默认开启）：明显不可转写的音频不白跑 Whisper，
    #: 也避免音乐/噪声片段触发解码循环产出垃圾文本。结果落 ``no_speech``，
    #: 原因码区分 ``audio_too_short`` / ``low_snr`` / ``audio_silent``。
    precheck: StrictBool = True
    #: 音频短于该秒数不送 ASR（Whisper-base 在极短音频上极易输出幻觉或空文本）
    min_duration_seconds: float = Field(default=0.4, gt=0.0, le=10.0)
    #: 估计信噪比低于该 dB 数不送 ASR（噪声/稳态音乐片段会触发解码循环）
    min_snr_db: float = Field(default=6.0, ge=-20.0, le=60.0)
    #: 转写失败处置：表1 要求留在产线、进候选描述、不能直接评分
    fail_action: Literal["keep_in_pipeline"] = "keep_in_pipeline"

    @field_validator("max_segment_seconds", "paragraph_silence_seconds",
                     "min_duration_seconds", "min_snr_db", mode="before")
    @classmethod
    def _finite_seconds(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("时长参数必须是数值（不接受字符串/布尔）")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("时长参数必须是有限数值")
        return number


class OperatorConfig(StrictBaseModel):
    """算子 7 部署配置（configs/config.yaml）。"""

    operator: OperatorIdentity
    stage_id: str = Field(default="S2", min_length=1)
    #: 算子总开关；关闭时不做任何模型加载。[v4.2 §4.8] 默认关闭（需显式打开）
    enabled: StrictBool = False
    #: 表1 #8 语音转写设置
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    #: 语义单元策略；demo 只实现每个 sample_id 一个单元。
    unit_policy: Literal["per_sample"] = "per_sample"
    #: 音视频时长差异比例阈值，超过按 av_mismatch_action 处理。
    max_av_duration_diff_ratio: float = Field(default=0.20, ge=0.0, le=1.0)
    #: 超阈值动作：review 保留并标记；reject 进入 07_rejects.jsonl。
    av_mismatch_action: Literal["review", "reject"] = "review"
    #: 是否要求媒体文件实际存在。HTTP 不允许关闭（服务端强制 true）。
    require_existing_media: StrictBool = True
    #: 是否校验视频帧与音频片段时间戳定义域与单调性。
    validate_frame_timestamps: StrictBool = True

    @field_validator("max_av_duration_diff_ratio", mode="before")
    @classmethod
    def _finite_ratio(cls, value):
        return _check_ratio(value)

    def business_dict(self) -> Dict[str, Any]:
        """血缘里记录的业务参数快照（不含部署身份）。"""
        return {
            "enabled": self.enabled,
            "transcription_enabled": self.transcription.enabled,
            "transcription": self.transcription.model_dump(),
            "unit_policy": self.unit_policy,
            "max_av_duration_diff_ratio": self.max_av_duration_diff_ratio,
            "av_mismatch_action": self.av_mismatch_action,
            "require_existing_media": self.require_existing_media,
            "validate_frame_timestamps": self.validate_frame_timestamps,
        }

    def deployment_dict(self) -> Dict[str, Any]:
        return {
            "operator": self.operator.model_dump(),
            "stage_id": self.stage_id,
        }


class BusinessOverrides(StrictBaseModel):
    """请求 ``config`` 允许覆盖的业务参数子集。

    携带 ``operator``/``stage_id``（或任何未知键）会直接校验失败，请求无法改写
    血缘身份。表1 #8 的模型与解码语言是固定项，**不在**可覆盖范围内。
    """

    enabled: Optional[StrictBool] = None
    #: 「是否做语音转写」开关（写入 transcription.enabled）
    transcription_enabled: Optional[StrictBool] = None
    unit_policy: Optional[Literal["per_sample"]] = None
    max_av_duration_diff_ratio: Optional[float] = None
    av_mismatch_action: Optional[Literal["review", "reject"]] = None
    require_existing_media: Optional[StrictBool] = None
    validate_frame_timestamps: Optional[StrictBool] = None

    @field_validator("max_av_duration_diff_ratio", mode="before")
    @classmethod
    def _finite_ratio(cls, value):
        return None if value is None else _check_ratio(value)

    def provided(self) -> Dict[str, Any]:
        """只返回请求显式给出的参数。"""
        data = self.model_dump(exclude_none=True)
        return {key: value for key, value in data.items() if value is not None}


class PredictRequest(BaseModel):
    """``POST /predict`` 请求体；未知字段直接校验失败。"""

    model_config = ConfigDict(extra="forbid")

    uniqueId: str
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)


class RemoveFileRequest(BaseModel):
    """保留类型定义以便文档说明；HTTP 层按契约从 query 读取 uniqueId。"""

    uniqueId: str
