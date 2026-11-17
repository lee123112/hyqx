"""算子11 数据契约：严格校验语言方向、后端策略、窗口、权重与阈值。未知字段一律拒绝。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

# 内置中英术语表：source 为源语言（zh）词条，target 为可接受的目标语言（en）译文。
# 只用于可解释的词典证据，不参与任何模型分数计算。
DEFAULT_TERMINOLOGY = [
    {"source": "数据清洗", "target": ["data cleaning"]},
    {"source": "血缘", "target": ["lineage"]},
    {"source": "对齐结果", "target": ["alignment results"]},
    {"source": "人工复核", "target": ["review"]},
    {"source": "证据", "target": ["evidence"]},
    {"source": "系统", "target": ["system"]},
    {"source": "多种语言", "target": ["multiple languages"]},
    {"source": "平行语料", "target": ["parallel corpus"]},
]


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

    # 任务声明的语言方向；记录自带的 source_lang/target_lang 必须与之一致
    source_lang: str = "zh"
    target_lang: str = "en"
    input_file_name: str = "10_sentence_pairs.jsonl"
    pair_id_fields: List[str] = Field(default_factory=lambda: ["pair_id", "id"])
    # 缺失父句引用（metadata.source_ref/target_ref）时是否判为 schema_error
    require_parent_ref: bool = True
    # 是否校验记录声明语言与配置方向一致（不做真实语言检测，那是算子6 的职责）
    language_check: bool = True

    # 正式版模型：本地 mDeBERTa-v3-base（microsoft/mdeberta-v3-base）；权重与网络下载不是测试前提
    model_name: str = "microsoft/mdeberta-v3-base"
    model_path: Optional[str] = None
    model_service_url: Optional[str] = None
    model_timeout_seconds: float = Field(default=30.0, gt=0)
    # auto=优先模型；model=要求模型；rule_degraded=显式规则后端
    # 默认 rule_degraded：离线与无权重环境确定性运行；需要模型时显式改 auto/model
    model_backend: Literal["auto", "model", "rule_degraded"] = "rule_degraded"
    # 模型不可用时的策略：rule_degraded 降级并标记 degraded；fail 结构化失败
    fallback_policy: Literal["rule_degraded", "fail"] = "rule_degraded"
    device: Literal["cpu", "cuda", "mps", "auto"] = "cpu"
    batch_size: int = Field(default=32, ge=1)
    # 单条记录模型错误写入异常清单后继续；累计超过上限即整次执行结构化失败
    max_model_errors: int = Field(default=10, ge=1)

    # 子句内单调 DP 的候选窗口：|源序号 - 目标序号| > window 的配对不参与评分
    window: int = Field(default=3, ge=0)
    min_confidence: float = Field(default=0.50, ge=0.0, le=1.0)
    # 达到该阈值标记 auto_pass，否则标记 needs_review（两者都进通过集）
    review_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    ambiguity_margin: float = Field(default=0.02, ge=0.0, le=1.0)
    skip_penalty: float = Field(default=0.35, ge=0.0, le=1.0)

    semantic_weight: float = Field(default=0.45, ge=0.0)
    dictionary_weight: float = Field(default=0.20, ge=0.0)
    position_weight: float = Field(default=0.10, ge=0.0)
    length_weight: float = Field(default=0.15, ge=0.0)
    parent_weight: float = Field(default=0.10, ge=0.0)

    allow_1_to_2: bool = True
    allow_2_to_1: bool = True
    # 开启后，词典无证据的子句对进入异常清单（reason=no_evidence）
    require_lexicon_evidence: bool = False

    # 碎片门禁（2026-11-04）：源或目标任一侧去空白后 <= fragment_min_chars 的子句对
    # 不进入下游（如「教育、」→「science or charitable purposes,」）。
    fragment_gate: bool = True
    fragment_min_chars: int = Field(default=4, ge=0)
    # 跨位门禁（2026-11-04）：1-2 / 2-1 多单位对，若源/目标起始序号差超过该值，
    # 视为跨位错配，不进入下游。1-1 对不受此限（由置信度与碎片门禁治理）。
    cross_position_gate: bool = True
    cross_position_max_gap: int = Field(default=2, ge=0)

    # 重复单位门禁（2026-11-05）：同一父句内源或目标子句出现包含度 >= 阈值的重复，
    # 整个父句的子句对标记 review_status=needs_review（不删除，交下游 12/13 判定）。
    repetition_guard: bool = True
    repetition_containment: float = Field(default=0.80, ge=0.0, le=1.0)

    # 碎片/跨位门禁命中的子句对去向（2026-11-05，与算子08/09 同名同义）：
    #   drop          = 只写异常清单（旧行为）——碎片从下游彻底消失；
    #   mark_and_keep = 仍写通过输出并标记 review_status=needs_review + 原因，
    #                   让「这是一个碎片」这个判断传到 11/12/13/17 去处置。
    fragment_policy: Literal["drop", "mark_and_keep"] = "mark_and_keep"

    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])

    # 切分：标点 + 连词 + 配置词表
    zh_clause_punctuation: List[str] = Field(
        default_factory=lambda: ["，", "。", "；", "：", "！", "？", "、", "…"])
    en_clause_punctuation: List[str] = Field(
        default_factory=lambda: [",", ";", ":", ".", "!", "?"])
    zh_conjunctions: List[str] = Field(
        default_factory=lambda: ["并且", "而且", "但是", "然而", "因为", "所以", "如果",
                                 "虽然", "因此", "此外"])
    en_conjunctions: List[str] = Field(
        default_factory=lambda: ["and", "but", "or", "because", "so", "however", "if",
                                 "when", "while", "although"])
    split_terms: List[str] = Field(default_factory=list)
    terminology: List[Dict[str, Any]] = Field(
        default_factory=lambda: [dict(entry) for entry in DEFAULT_TERMINOLOGY])
    # 小于该计量单元数的片段并入相邻片段，避免碎片爆炸
    min_clause_units: int = Field(default=2, ge=1)
    # 单侧最多切出的子句数；超出时合并计量单元数最小的相邻片段
    max_clauses: int = Field(default=12, ge=1)

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.source_lang == self.target_lang:
            raise ValueError("source_lang 与 target_lang 不能相同")
        total = (self.semantic_weight + self.dictionary_weight + self.position_weight
                 + self.length_weight + self.parent_weight)
        if total <= 0:
            raise ValueError("五个分项权重不能同时为 0")
        if self.review_threshold < self.min_confidence:
            raise ValueError("review_threshold 不能低于 min_confidence")
        return self

