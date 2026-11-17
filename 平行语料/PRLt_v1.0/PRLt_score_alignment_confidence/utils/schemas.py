"""算子12 数据契约：组件权重、阈值、策略与请求模型；未知配置字段一律拒绝。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

GRANULARITIES = ("clause_phrase", "sentence", "paragraph")

DEFAULT_WEIGHTS = {"clause_phrase": 0.6, "sentence": 0.3, "paragraph": 0.1}
DEFAULT_SOURCE_OPERATORS = {
    "clause_phrase": "clause_phrase_alignment",
    "sentence": "sentence_alignment",
    "paragraph": "paragraph_alignment",
}


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
    # 上游默认产物名（阶段内实际路径由 /predict 的 inputFile 指定）
    input_file_name: str = "11_clause_pairs.jsonl"
    # pair_id 取值优先级：pair_id 优先，回退 id（兼容上游子句级输出）
    pair_id_fields: List[str] = Field(default_factory=lambda: ["pair_id", "id"])
    # 缺少 source_ref/target_ref 时判为 schema_error
    require_refs: bool = True
    # 只校验记录声明语言与配置方向是否一致（真实语言检测属算子 6 职责）
    language_check: bool = True

    # 参与融合的粒度与权重；未启用的粒度不进分母
    enabled_granularities: List[str] = Field(
        default_factory=lambda: list(GRANULARITIES))
    component_weights: Dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # 分项来源算子，用于血缘内的可追溯标注
    component_source_operators: Dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_SOURCE_OPERATORS))

    # 低置信只做标记/审计；低于该阈值视为低置信
    low_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # >= review_threshold 标记 auto_pass，否则 needs_review
    review_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    # 最优与次优候选融合分差 <= ambiguity_margin 视为并列冲突
    ambiguity_margin: float = Field(default=0.005, ge=0.0, le=1.0)
    # mark_and_keep=保留在通过集并同时写审计；move_to_rejects=只写异常清单
    low_confidence_policy: Literal["mark_and_keep", "move_to_rejects"] = "mark_and_keep"

    # ---- 父句级守卫（2026-11-05）----
    # 融合分只看得见单条子句对，看不见父句层面的结构问题与跨语言长度失衡。
    # 命中任一守卫时，该父句下全部记录的 alignment_confidence 封顶到
    # guard_confidence_cap（本算子不再为这份对齐背书），原因写入 metadata.parent_guard。
    parent_guard: bool = True
    # 同一父句内源/目标子句的包含度 >= 该值即判重复（口径同算子10 utils/duplication.py）
    repetition_containment: float = Field(default=0.80, ge=0.0, le=1.0)
    # 父句整段的「源计量单元 / 目标计量单元」比值合理区间；中文按字、英文按词
    parent_ratio_min: float = Field(default=0.88, ge=0.0)
    parent_ratio_max: float = Field(default=4.10, ge=0.0)
    guard_confidence_cap: float = Field(default=0.45, ge=0.0, le=1.0)
    # 段内兄弟句对重复：同一段落的两条 pair 给出同一段译文（整段译文被复制后
    # 被 09/10 拆成多条 1-1 句对）。只用 target 侧、阈值同 repetition_containment。
    sibling_duplicate_guard: bool = True

    confidence_version: str = "v1.0.0"

    # 可选：通过 metadata 引用上游 10/09 产物的旁路证据文件，按 id→置信度建索引
    sentence_evidence_file: Optional[str] = None
    paragraph_evidence_file: Optional[str] = None
    evidence_id_fields: List[str] = Field(default_factory=lambda: ["pair_id", "id"])
    evidence_confidence_fields: List[str] = Field(
        default_factory=lambda: ["confidence", "alignment_confidence"])

    # 本节点要求严格 UTF-8（上游已完成编码归一）
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.source_lang == self.target_lang:
            raise ValueError("source_lang 与 target_lang 不能相同")
        if not self.enabled_granularities:
            raise ValueError("enabled_granularities 不能为空")
        unknown = [g for g in self.enabled_granularities if g not in self.component_weights]
        if unknown:
            raise ValueError(f"enabled_granularities 含未知粒度：{unknown}")
        if len(set(self.enabled_granularities)) != len(self.enabled_granularities):
            raise ValueError("enabled_granularities 不能重复")
        for name, weight in self.component_weights.items():
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                raise ValueError(f"component_weights.{name} 必须是数值")
            if weight < 0:
                raise ValueError(f"component_weights.{name} 不能为负")
        enabled_total = sum(self.component_weights[g] for g in self.enabled_granularities)
        if enabled_total <= 0:
            raise ValueError("启用粒度的权重之和必须大于 0")
        if self.review_threshold < self.low_confidence_threshold:
            raise ValueError("review_threshold 不能低于 low_confidence_threshold")
        if self.parent_ratio_min >= self.parent_ratio_max:
            raise ValueError("parent_ratio_min 必须小于 parent_ratio_max")
        return self
