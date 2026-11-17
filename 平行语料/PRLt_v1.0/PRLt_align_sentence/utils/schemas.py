"""算子10 数据契约：严格校验模型后端、窗口、权重与阈值。未知字段一律拒绝。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import StrictBool, BaseModel, ConfigDict, Field, model_validator

# 新模型后端名；`labse` 是旧配置兼容别名，解析后统一归一化为该值。
BACKEND_EMBEDDING = "qwen3_embedding"
_BACKEND_ALIASES = {"labse": BACKEND_EMBEDDING}


class PredictRequest(BaseModel):
    uniqueId: str
    operatorId: Optional[str] = None
    isLastOperator: Optional[StrictBool] = None
    inputFile: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)


class PredictResponse(BaseModel):
    code: int
    msg: str
    data: Optional[Dict[str, Any]] = None


class OperatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_lang: str = "zh"
    target_lang: str = "en"

    model_name: str = "../models/Qwen3-Embedding-0.6B"
    model_service_url: Optional[str] = None
    model_timeout_seconds: float = Field(default=30.0, gt=0)
    # labse=旧配置兼容别名（映射到 qwen3_embedding）；auto=优先模型，不可用按 allow_degraded 处理
    model_backend: Literal["auto", "qwen3_embedding", "labse", "rule_degraded"] = "qwen3_embedding"
    device: Literal["cpu", "cuda", "mps", "auto"] = "cpu"
    batch_size: int = Field(default=32, ge=1)


    # 段落内单调 DP 的候选窗口：|src_idx - tgt_idx| > window 的句对不参与评分
    window: int = Field(default=5, ge=0)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    ambiguity_margin: float = Field(default=0.02, ge=0.0, le=1.0)
    skip_penalty: float = Field(default=0.35, ge=0.0, le=1.0)

    semantic_weight: float = Field(default=0.65, ge=0.0)
    length_weight: float = Field(default=0.25, ge=0.0)
    evidence_weight: float = Field(default=0.10, ge=0.0)

    # 单条段落模型错误写入异常清单并继续；累计超过该上限则整次执行结构化失败
    max_model_errors: int = Field(default=20, ge=1)

    allow_1_to_2: bool = True
    allow_2_to_1: bool = True
    allow_degraded: bool = True

    # 低置信/多候选句对的去向（2026-11-05，与算子08 同名同义）：
    #   drop          = 只写异常清单（旧行为）——整段句对低置信时该段落从链路消失；
    #   mark_and_keep = 仍写通过输出，标记 review_status=needs_review + 原因，交给下游判定。
    low_confidence_policy: Literal["drop", "mark_and_keep"] = "mark_and_keep"

    # [2026-11-05 译文残留编辑注记] 抓取/拼接语料里常见的「后半句缺失」「未翻译」
    # 「[sic]」这类编辑注记：它出现在句对文本里，说明这一对不是干净的句子对
    # （原文漏译、译文被拼接、截断处留了说明），而长度比/语义分都发现不了。
    # 命中即把置信度压到 residual_note_confidence_cap（低于 min_confidence），
    # 并按 residual_note_policy 处理；默认 mark_and_keep，不静默丢句对。
    residual_note_patterns: List[str] = Field(default_factory=lambda: [
        # 必须有「缺失/遗漏」的谓语才算注记：单说 the first part of the text 是正常英文
        r"(?:first|second|last|other|remaining)\s+(?:half|part|portion)\s+of\s+the\s+(?:sentence|paragraph|text|line)"
        r"[^.]{0,40}?\b(?:is|was|has\s+been|had\s+been)\s+(?:missing|omitted|cut|lost|absent|dropped)\b",
        r"\bnot\s+(?:been\s+)?(?:translated|transcribed)\b",
        r"\buntranslated\b",
        r"\bmissing\s+translation\b",
        r"\btranslation\s+(?:is\s+|was\s+)?missing\b",
        r"\[\s*(?:\.{2,}|…|sic)\s*\]",
        r"(?:前半句|后半句|前半部分|后半部分|其余部分)(?:缺失|遗漏|未译|漏译)",
        r"未翻译|漏译|未译出",
    ])
    residual_note_policy: Literal["ignore", "mark_and_keep", "drop"] = "mark_and_keep"
    residual_note_confidence_cap: float = Field(default=0.45, ge=0.0, le=1.0)

    # [2026-11-05 句界完整性] 一侧以逗号/冒号/破折号/左引号结尾、另一侧以句末终止符
    # 结尾，说明两条句子的句界没对上（译文的引语或列举跨了句界）；引号/括号不配对
    # 又没有终止符，说明句子被截断。语义分对这类情况不敏感——两条都是「差不多对」。
    boundary_gate: bool = True
    boundary_continuation_marks: List[str] = Field(
        default_factory=lambda: ["，", ",", "、", "：", ":", "—", "–", "-", "（", "(", "「", "“"])
    boundary_closing_marks: List[str] = Field(
        default_factory=lambda: ["\"", "'", "”", "’", "）", ")", "》", "】", "」", "』"])
    boundary_terminal_marks: List[str] = Field(
        default_factory=lambda: ["。", "！", "？", "；", "…", ".", "!", "?"])
    # 每项为 [开符号, 闭符号]，用于引号/括号配对检查
    boundary_quote_pairs: List[List[str]] = Field(default_factory=lambda: [
        ["\"", "\""], ["“", "”"], ["（", "）"], ["(", ")"],
        ["「", "」"], ["《", "》"], ["[", "]"], ["『", "』"],
    ])
    boundary_confidence_cap: float = Field(default=0.45, ge=0.0, le=1.0)

    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])
    zh_terminators: List[str] = Field(default_factory=lambda: ["。", "！", "？", "；", "…"])
    en_terminators: List[str] = Field(default_factory=lambda: [".", "!", "?", "…"])

    @model_validator(mode="after")
    def _check_weights(self):
        total = self.semantic_weight + self.length_weight + self.evidence_weight
        if total <= 0:
            raise ValueError("semantic_weight/length_weight/evidence_weight 不能同时为 0")
        self.model_backend = _BACKEND_ALIASES.get(self.model_backend, self.model_backend)
        if self.model_backend == "rule_degraded" and not self.allow_degraded:
            raise ValueError("model_backend=rule_degraded 与 allow_degraded=false 冲突")
        return self


