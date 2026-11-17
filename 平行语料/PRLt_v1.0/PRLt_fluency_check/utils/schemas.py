"""算子15 数据契约：流畅度阈值、后端门禁与请求模型；未知配置字段一律拒绝。"""
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


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

    # 严格 UTF-8（上游算子 3/4 已完成编码归一）
    input_encodings: list = Field(default_factory=lambda: ["utf-8"])
    # 上游默认产物名；阶段内实际路径由 /predict 的 inputFile 指定
    input_file_name: str = "14_semantic_checked.jsonl"
    # 只读取 target，不改写 source/target 原文
    text_field: Literal["target"] = "target"
    # 任务声明的语言方向；记录声明 target_lang 必须与之一致
    source_lang: str = "zh"
    target_lang: str = "en"
    language_check: bool = True

    # ---- 后端门禁 ----
    # auto=有模型用模型、无模型按 fallback_policy；model=必须用模型；rule_degraded=只做规则降级
    backend: Literal["auto", "model", "rule_degraded"] = "auto"
    model_service_url: Optional[str] = None
    model_name: str = "Qwen3.5-2B"
    model_path: Optional[str] = None
    model_version: Optional[str] = None
    model_timeout_seconds: float = Field(default=900.0, gt=0)
    model_entrypoint: str = "score_fluency"
    # 模型不可用时：rule_degraded=降级并标记 degraded；fail=结构化失败
    fallback_policy: Literal["rule_degraded", "fail"] = "rule_degraded"
    # 模型错误容忍度：实际上限 = max(max_model_errors, ceil(rate × 输入条数))。
    # 只留绝对值时，5000 条的容忍度只剩 0.4%；只留比例时，小样本会一有错就整次失败。
    max_model_errors: int = Field(default=20, ge=0)
    max_model_error_rate: float = Field(default=0.02, ge=0.0, le=1.0)

    # ---- 评分与过滤 ----
    fluency_threshold: float = Field(default=60.0, ge=0.0, le=100.0)
    score_min: float = Field(default=0.0, ge=0.0, le=100.0)
    score_max: float = Field(default=100.0, ge=0.0, le=100.0)
    # 只有经 Gold 校准的分数才可参与阈值门禁；默认只认 calibrated_probability_percent，
    # 未校准的 rubric 分（model_score_0_100）默认不放行——配置缺省时门禁朝"不出结论"
    # 失败，而不是悄悄按未校准分数判放行/拦截（规范 4.15）。
    require_calibrated_score: bool = True
    require_approved_calibration: bool = True
    accepted_score_semantics: list[str] = Field(
        default_factory=lambda: ["calibrated_probability_percent"])
    # low_fluency / degraded 记录的去向
    low_fluency_policy: Literal["mark_and_keep", "move_to_rejects"] = "mark_and_keep"
    degraded_policy: Literal["mark_and_keep", "move_to_rejects"] = "mark_and_keep"
    # 为 true 时降级记录标记 waiting_manual（明确允许进入人工复核）；否则 model_unavailable
    allow_degraded_delivery: bool = False
    # 规则门禁：结构性硬伤（连续标点串等）由规则直接判 low_fluency，不依赖未校准模型分。
    # 只放结构性证据，不把规则结论伪装成模型分数——fluency_score 仍原样保留。
    rule_gate: bool = True
    rule_gate_kinds: list[str] = Field(default_factory=lambda: ["punctuation_run"])

    # ---- 评分口径（defect_count_v1）----
    # rubric_v2：旧口径，模型直接给 0-100 整体印象分（保留兼容，仅用于历史对比）。
    # defect_count_v1：模型不再给分，只提「类型 + 原文定位 + 最小修正」的缺陷主张；
    #   算子核验后按条数计分。自然语料实测证明整体印象分只有 3-4 档分辨率，且低分
    #   多为事后编造（详见 _eval/reports/op15_fluency_redesign.md），故设为默认。
    scoring_mode: Literal["defect_count_v1", "rubric_v2"] = "defect_count_v1"
    defect_taxonomy_version: str = "en-defect-taxonomy-v1"
    # 只有列在这里的类型可能进入分数；其余类型即使被模型提出也只作 suspect 证据。
    # 默认只含可用机械规则独立确证的类型——无法确证的主张不得影响分数。
    scored_defect_types: list[str] = Field(
        default_factory=lambda: ["agreement", "verb_form", "dup_word", "punctuation"])
    severity_weights: Dict[str, float] = Field(
        default_factory=lambda: {"hard": 1.0, "minor": 0.4})
    # score = 100 × max(0, 1 − rate / score_scale)，rate = 加权错误数 / max(词数, token_floor) × 100。
    # score_scale=12.5 的含义：每 100 词 12.5 个加权错误 → 0 分；60 分线 = 每 100 词 ≤ 5 个。
    # 这是先验定义，不随数据回调；验收不达标只报告现象，不改这两个常数。
    score_scale: float = Field(default=12.5, gt=0.0)
    token_floor: int = Field(default=20, ge=1)
    max_defects: int = Field(default=5, ge=1)
    # fix 与 span 的 token 级编辑距离上限；0 表示 fix 必须改（完全相同的 fix 不算主张）。
    max_fix_edit_distance: int = Field(default=2, ge=0)
    # 词数低于该值的句对打 low_confidence_length 标记（统计时分组报告）。
    low_confidence_length_tokens: int = Field(default=8, ge=1)

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.source_lang == self.target_lang:
            raise ValueError("source_lang 与 target_lang 不能相同")
        if not self.input_encodings:
            raise ValueError("input_encodings 不能为空")
        if self.score_max <= self.score_min:
            raise ValueError("score_max 必须大于 score_min")
        if not (self.score_min <= self.fluency_threshold <= self.score_max):
            raise ValueError("fluency_threshold 必须落在 [score_min, score_max] 内")
        if self.require_calibrated_score and not self.accepted_score_semantics:
            raise ValueError("require_calibrated_score=true 时 accepted_score_semantics 不能为空")
        if self.backend == "model" and not (self.model_service_url or self.model_path):
            raise ValueError("backend=model 时必须提供 model_service_url 或 model_path")
        # 兜底护栏：defect_count_v1 是「规则计数 → 线性映射」的展示分，不是校准概率。
        # 除非显式关闭 require_calibrated_score，否则不允许把它写进准入白名单——
        # 否则一次配置手滑就会让未校准的规则计数获得放行/拦截权（规范 4.15）。
        if (self.require_calibrated_score
                and self.scoring_mode == "defect_count_v1"
                and "defect_count_v1" in self.accepted_score_semantics):
            raise ValueError(
                "defect_count_v1 是规则计数映射值，不是校准概率，不能进入 accepted_score_semantics；"
                "如确需开闸，请先显式设置 require_calibrated_score=false 并留书面依据")
        if self.scoring_mode == "defect_count_v1" and not self.scored_defect_types:
            raise ValueError("scoring_mode=defect_count_v1 时 scored_defect_types 不能为空")
        if self.scoring_mode == "defect_count_v1" and not self.severity_weights:
            raise ValueError("scoring_mode=defect_count_v1 时 severity_weights 不能为空")
        return self
