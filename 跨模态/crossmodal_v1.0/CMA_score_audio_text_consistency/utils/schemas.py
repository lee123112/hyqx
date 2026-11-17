"""配置与请求模型（Pydantic v2，严格布尔 + 定义域校验）。

表1 #13 冻结口径：
- 默认关闭；编辑距离比例默认 0.30（可配 0.10–0.50）；
- 开启后复用上游 Whisper-base 转写，生产门禁只使用字符级 CER；
- 文本比较前先做繁体→简体字符级归一化（默认开启，可配关闭），不改阈值语义；
- 关闭时不加载模型、不写音文评分文件、不隔离；
- Wav2Vec2/BERT 字段只为旧版本配置和实验诊断保留，不得参与生产判定。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, field_validator,
                      model_validator)

DEFAULT_AUDIO_MODEL = "facebook/wav2vec2-base"
DEFAULT_TEXT_MODEL = "bert-base-chinese"
SUPPORTED_SAMPLE_RATE = 16000
STAGE_ID = "S3"

EDIT_RATIO_MIN = 0.10
EDIT_RATIO_MAX = 0.50
# [v4.2] 裸余弦不得在 config 暴露为可调阈值（V4.2 §4.13）：cosine_threshold 已从
# OperatorConfig / BusinessOverrides 白名单移除，仅保留为内部诊断常量，不参与任何判定；
# 旧配置中再出现 cosine_threshold 将被严格校验拒绝（extra=forbid）。
DIAG_COSINE_THRESHOLD = 0.25

# [v4.2] judge_mode 枚举（V4.2 §2.8/§4.13）：operator=算子按快照阈值给分档；system=只回指标
JUDGE_MODES = ("operator", "system")
DEFAULT_JUDGE_MODE = "operator"
# 中文口语语速物理上限（字符/秒）的可配范围：正常口播 3–7 字/秒，15 字/秒已是
# 极快语速；60 字/秒以上则失去对 Whisper 重复循环（实测可达 90+ 字/秒）的识别能力。
CHARS_PER_SECOND_MIN = 15.0
CHARS_PER_SECOND_MAX = 60.0
MAX_AUDIO_SECONDS_MIN = 1.0
MAX_AUDIO_SECONDS_MAX = 120.0
MAX_TEXT_CHARS_MIN = 1
MAX_TEXT_CHARS_MAX = 1000
DEVICES = ("cpu", "cuda", "auto")

#: 请求不得覆盖的部署/口径键（表1 固定值或部署身份）
READONLY_KEYS = ("operator", "stage_id", "audio_model", "text_model", "sample_rate")


class ConfigError(ValueError):
    """配置非法。"""


def _range(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s 必须是数字" % name)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ConfigError("%s 必须是有限数字" % name)
    if number < low or number > high:
        raise ConfigError("%s 必须在 %.2f–%.2f 之间，实际 %s" % (name, low, high, value))
    return number


def _positive_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("%s 必须是整数" % name)
    if value < low or value > high:
        raise ConfigError("%s 必须在 %d–%d 之间，实际 %s" % (name, low, high, value))
    return value


class StrictBaseModel(BaseModel):
    """禁止未知字段：未知键一律失败，避免静默忽略配置错误。"""

    model_config = ConfigDict(extra="forbid")


class OperatorIdentity(StrictBaseModel):
    name: str
    version: str


class OperatorConfig(StrictBaseModel):
    """算子完整配置（YAML 快照的结构化模型）。"""

    operator: OperatorIdentity = Field(
        default_factory=lambda: OperatorIdentity(name="audio_text_scoring", version="v1.0.0"))
    stage_id: str = STAGE_ID

    # 表1 总开关：默认关
    enabled: StrictBool = False

    # 表1 参数 2：转写与描述的编辑距离比例上限
    edit_distance_ratio_max: float = 0.30
    # 文本归一化：比较前先把繁体字逐字符转简体（ASR 偶发繁体输出时避免误判）。
    # 默认开启；关闭后恢复纯 NFKC 口径，不改变 edit_distance_ratio_max 的阈值语义。
    traditional_to_simplified: StrictBool = True
    # 中文 ITN：比较前把中文数字/百分比/数量单位表达归一为阿拉伯数字（utils/itn.py）。
    # 默认开启；关闭后恢复 t2s+NFKC 口径，不改变 edit_distance_ratio_max 的阈值语义。
    inverse_text_normalization: StrictBool = True
    # 语言适用性门：中文 CER 阈值只对中文描述有语义。描述侧没有任何中文字符时
    # （英文素材 / 时序窗口英文字幕），判 not_applicable（不隔离、留产线），
    # 避免 Whisper 固定中文解码造成的 CER 虚高被误判为「语义不通」。默认开启。
    language_gate: StrictBool = True
    # 转写可靠性门：Whisper 解码重复循环（同一单元连续重复 6 次以上）或语速超出
    # 物理上限的转写，不能作为「语义不通」的证据，判 not_applicable（不隔离、留产线）。
    # 实测时序窗口出现 4.88s 音频解码出 453 字（93 字/秒）的重复循环，CER 必然虚高。
    transcript_reliability_gate: StrictBool = True
    # 中文口语语速物理上限（字符/秒），超过即判转写不可靠；正常中文口播约 3–7 字/秒，
    # 这里取 30 字/秒，只用于识别 ASR 故障，不用于区分快慢语速。
    max_chars_per_second: float = 30.0
    # 同音容错（默认开启）：编辑距离里「替换成同音字」的代价记为 0，阈值
    # edit_distance_ratio_max 与分母口径都不变。Whisper 的中文同音误写
    # （张工程师→張宮城師、午休时→胡修實）字符 CER 高达 0.56，但口语内容一致；
    # 归零同音替换后它们落到阈值内，真实语义差异（不同的人/地/事）不同音，仍超限。
    # 拼音表随包离线交付（utils/data/PinyinTable.txt），不可用时按关闭处理。
    homophone_tolerance: StrictBool = True
    # [v4.2] 判定模式（V4.2 §4.13）：operator（默认）/ system
    judge_mode: str = DEFAULT_JUDGE_MODE
    # [v4.2] cosine_threshold 已移除：裸余弦只作内部诊断常量（见 DIAG_COSINE_THRESHOLD），
    # 不再是可调配置项。

    # 表1 固定模型（不可覆盖）
    audio_model: str = DEFAULT_AUDIO_MODEL
    text_model: str = DEFAULT_TEXT_MODEL

    # 本地权重位置（仅本地加载，不联网；相对算子本地目录解析，
    # 默认 model_root="weight" 即 <算子目录>/weight，见 weight/README.md）
    model_root: str = "weight"
    audio_model_dir: str = "wav2vec2-base"
    text_model_dir: str = "bert-base-chinese"
    device: str = "cpu"

    # 资源与输入保护
    sample_rate: int = SUPPORTED_SAMPLE_RATE
    max_audio_seconds: float = 30.0
    max_text_chars: int = 500

    # 依赖缺失时的显式行为：false=失败，true=显式降级（绝不伪造分数）
    allow_degraded: StrictBool = False

    # 可选辅助匹配层：复用本地 Whisper-base 的中文条件概率；不替换表1两塔。
    auxiliary_enabled: StrictBool = False
    auxiliary_model_dir: str = "whisper-base"
    auxiliary_language: str = "zh"
    auxiliary_task: str = "transcribe"
    mapping_enabled: StrictBool = False
    mapping_path: str = ""

    @field_validator("enabled", "allow_degraded", "auxiliary_enabled", "mapping_enabled",
                     "traditional_to_simplified", "inverse_text_normalization", "language_gate",
                     "transcript_reliability_gate", "homophone_tolerance",
                     mode="before")
    @classmethod
    def _strict_bool(cls, value: Any, info) -> Any:
        if isinstance(value, bool):
            return value
        raise ConfigError("%s 必须是布尔值 true/false，实际 %r" % (info.field_name, value))

    @field_validator("edit_distance_ratio_max", mode="before")
    @classmethod
    def _valid_edit_ratio(cls, value: Any) -> float:
        return _range(value, "edit_distance_ratio_max", EDIT_RATIO_MIN, EDIT_RATIO_MAX)

    @field_validator("judge_mode", mode="before")
    @classmethod
    def _valid_judge_mode(cls, value: Any) -> str:
        # [v4.2] judge_mode 枚举校验（V4.2 §4.13：operator/system）
        text = str(value).strip().lower()
        if text not in JUDGE_MODES:
            raise ConfigError("judge_mode 非法：%s（可选 operator/system）" % value)
        return text

    @field_validator("max_chars_per_second", mode="before")
    @classmethod
    def _valid_chars_per_second(cls, value: Any) -> float:
        return _range(value, "max_chars_per_second", CHARS_PER_SECOND_MIN, CHARS_PER_SECOND_MAX)

    @field_validator("max_audio_seconds", mode="before")
    @classmethod
    def _valid_max_audio(cls, value: Any) -> float:
        return _range(value, "max_audio_seconds", MAX_AUDIO_SECONDS_MIN, MAX_AUDIO_SECONDS_MAX)

    @field_validator("max_text_chars", mode="before")
    @classmethod
    def _valid_max_text(cls, value: Any) -> int:
        return _positive_int(value, "max_text_chars", MAX_TEXT_CHARS_MIN, MAX_TEXT_CHARS_MAX)

    @field_validator("sample_rate", mode="before")
    @classmethod
    def _valid_sample_rate(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("sample_rate 必须是整数")
        if value != SUPPORTED_SAMPLE_RATE:
            raise ConfigError("sample_rate 固定为 %d（表1 前端只提供 16000Hz）"
                              % SUPPORTED_SAMPLE_RATE)
        return value

    @field_validator("audio_model", mode="before")
    @classmethod
    def _valid_audio_model(cls, value: Any) -> str:
        if value != DEFAULT_AUDIO_MODEL:
            raise ConfigError("audio_model 固定为 %s，不允许替换为 %r"
                              % (DEFAULT_AUDIO_MODEL, value))
        return DEFAULT_AUDIO_MODEL

    @field_validator("text_model", mode="before")
    @classmethod
    def _valid_text_model(cls, value: Any) -> str:
        if value != DEFAULT_TEXT_MODEL:
            raise ConfigError("text_model 固定为 %s，不允许替换为 %r"
                              % (DEFAULT_TEXT_MODEL, value))
        return DEFAULT_TEXT_MODEL

    @field_validator("device", mode="before")
    @classmethod
    def _valid_device(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text not in DEVICES:
            raise ConfigError("device 只能是 %s" % "、".join(DEVICES))
        return text

    @field_validator("stage_id", mode="before")
    @classmethod
    def _valid_stage(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ConfigError("stage_id 不能为空")
        return text

    @field_validator("model_root", "audio_model_dir", "text_model_dir", mode="before")
    @classmethod
    def _valid_path_text(cls, value: Any, info) -> str:
        text = str(value if value is not None else "").strip()
        if not text:
            raise ConfigError("%s 不能为空" % info.field_name)
        return text

    @field_validator("auxiliary_model_dir", mode="before")
    @classmethod
    def _valid_auxiliary_model_dir(cls, value: Any) -> str:
        text = str(value if value is not None else "").strip()
        if not text:
            raise ConfigError("auxiliary_model_dir 不能为空")
        return text

    @field_validator("mapping_path", mode="before")
    @classmethod
    def _valid_mapping_path(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("auxiliary_language", mode="before")
    @classmethod
    def _valid_auxiliary_language(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text != "zh":
            raise ConfigError("auxiliary_language 固定为 zh")
        return text

    @field_validator("auxiliary_task", mode="before")
    @classmethod
    def _valid_auxiliary_task(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text != "transcribe":
            raise ConfigError("auxiliary_task 固定为 transcribe")
        return text

    @model_validator(mode="after")
    def _check_consistency(self) -> "OperatorConfig":
        if self.operator.name != "audio_text_scoring":
            raise ConfigError("operator.name 固定为 audio_text_scoring")
        return self

    def business_dict(self) -> Dict[str, Any]:
        """业务口径快照（不含部署身份），用于血缘 config.business。"""
        return {
            "enabled": bool(self.enabled),
            "edit_distance_ratio_max": self.edit_distance_ratio_max,
            "traditional_to_simplified": bool(self.traditional_to_simplified),
            "inverse_text_normalization": bool(self.inverse_text_normalization),
            "language_gate": bool(self.language_gate),
            "transcript_reliability_gate": bool(self.transcript_reliability_gate),
            "max_chars_per_second": self.max_chars_per_second,
            "homophone_tolerance": bool(self.homophone_tolerance),
            "scoring_method": "character_edit_distance_ratio",
            "scoring_version": "v2",
            "transcription_model": "whisper-base",
        }

    def deployment_dict(self) -> Dict[str, Any]:
        """部署身份快照，用于血缘 config.deployment。"""
        return {
            "operator": {"name": self.operator.name, "version": self.operator.version},
            "stage_id": self.stage_id,
        }


class BusinessOverrides(StrictBaseModel):
    """``/predict`` 允许覆盖的业务子集；部署身份与固定模型不在其中。"""

    enabled: Optional[StrictBool] = None
    edit_distance_ratio_max: Optional[float] = None
    # 文本归一化开关（默认沿用 YAML 的 true）：请求可显式关闭做对照排查。
    traditional_to_simplified: Optional[StrictBool] = None
    # 中文 ITN 开关（默认沿用 YAML 的 true）：请求可显式关闭做对照排查。
    inverse_text_normalization: Optional[StrictBool] = None
    # 语言适用性门开关（默认沿用 YAML 的 true）：请求可显式关闭做对照排查。
    language_gate: Optional[StrictBool] = None
    # 转写可靠性门开关（默认沿用 YAML 的 true）：请求可显式关闭做对照排查。
    transcript_reliability_gate: Optional[StrictBool] = None
    # 语速上限（字符/秒）：默认沿用 YAML 的 12.0。
    max_chars_per_second: Optional[float] = None
    # 同音容错开关（默认沿用 YAML 的 true）：请求可显式关闭做对照排查。
    homophone_tolerance: Optional[StrictBool] = None
    # [v4.2] 判定模式（V4.2 §4.13 接口二参数表）：operator/system，默认 operator
    judge_mode: Optional[str] = None

    @field_validator("enabled", "traditional_to_simplified", "inverse_text_normalization",
                     "language_gate", "transcript_reliability_gate",
                     "homophone_tolerance", mode="before")
    @classmethod
    def _strict_bool(cls, value: Any, info) -> Any:
        if value is None or isinstance(value, bool):
            return value
        raise ConfigError("%s 必须是布尔值 true/false" % info.field_name)

    @field_validator("edit_distance_ratio_max", mode="before")
    @classmethod
    def _valid_edit_ratio(cls, value: Any) -> Any:
        if value is None:
            return value
        return _range(value, "edit_distance_ratio_max", EDIT_RATIO_MIN, EDIT_RATIO_MAX)

    @field_validator("max_chars_per_second", mode="before")
    @classmethod
    def _valid_chars_per_second(cls, value: Any) -> Any:
        if value is None:
            return value
        return _range(value, "max_chars_per_second", CHARS_PER_SECOND_MIN, CHARS_PER_SECOND_MAX)

    @field_validator("judge_mode", mode="before")
    @classmethod
    def _valid_judge_mode(cls, value: Any) -> Any:
        if value is None:
            return value
        text = str(value).strip().lower()
        if text not in JUDGE_MODES:
            raise ConfigError("judge_mode 非法：%s（可选 operator/system）" % value)
        return text

    def provided(self) -> Dict[str, Any]:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class PredictRequest(BaseModel):
    """/predict 请求体；``extra=forbid`` 与 ``StrictBool`` 保证形状不可放宽。"""

    model_config = ConfigDict(extra="forbid")

    uniqueId: str
    isLastOperator: StrictBool
    inputFile: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)


class RemoveFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uniqueId: str
