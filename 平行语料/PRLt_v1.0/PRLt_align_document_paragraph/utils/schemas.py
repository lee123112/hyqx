"""算子9 数据契约。"""
from typing import Any, Dict, List, Optional

from pydantic import StrictBool, BaseModel, ConfigDict, Field, field_validator

FALLBACK_POLICIES = ("rule_degraded", "fail")
BACKENDS = ("rule_degraded", "model")
LOW_CONFIDENCE_POLICIES = ("drop", "mark_and_keep")


class PredictRequest(BaseModel):
    uniqueId: str
    operatorId: Optional[str] = None
    isLastOperator: Optional[StrictBool] = None
    # 阶段内上游算子返回的 data.outputFile；算子9 用它读取 10_align_plan.json。
    # 也可通过 params.planDirectory 指定包含对齐计划与文档的计划目录。
    inputFile: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)


class PredictResponse(BaseModel):
    code: int
    msg: str
    data: Optional[Dict[str, Any]] = None


class OperatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])

    # 计划未给出 source.lang / target.lang 时的兜底语言（只影响可比单元口径）。
    source_lang: str = "zh"
    target_lang: str = "en"

    # 候选搜索窗口：源/目标段落起始索引差超过该值不再作为候选。
    candidate_window: int = 3
    # 置信度阈值，低于阈值的候选关系进入异常清单，不写入通过输出。
    min_confidence: float = 0.45
    # 打分权重，三项在 overlap 不可计算时对前两项重新归一化。
    length_weight: float = 0.55
    position_weight: float = 0.30
    overlap_weight: float = 0.15
    # 是否允许 1:2 / 2:1 关系。
    allow_1_to_2: bool = True
    allow_2_to_1: bool = True
    # 低置信段落关系的去向（2026-11-05）：
    #   drop          = 只写异常清单，不进入通过输出（旧行为，下游再也看不到这一段落）；
    #   mark_and_keep = 仍然写通过输出，但标记 review_status=needs_review 并带上原因，
    #                   由下游算子继续判定（与算子12 的 low_confidence_policy 同名同义）。
    # 默认 mark_and_keep：段落对齐不是内容终裁者，低置信是「需要复核」而不是「不存在」。
    low_confidence_policy: str = "mark_and_keep"
    # 多段落合并关系的结构惩罚，避免 DP 过度合并。
    group_penalty: float = 0.05
    # 次优候选与最优候选的置信度差小于该值时记为“多候选”。
    ambiguity_margin: float = 0.10

    # 模型不可用时：rule_degraded 走规则降级；fail 直接结构化失败。
    fallback_policy: str = "rule_degraded"
    # 本次 demo 只实现规则后端；backend=model 时不会伪造分数，直接失败。
    backend: str = "rule_degraded"

    # 文档路径是否允许落在任务目录之外。默认 false：源/目标文档只能解析到
    # 同一任务目录（或由计划文件所在目录显式扩展的允许根）。
    allow_external_document_paths: bool = False

    @field_validator("candidate_window")
    @classmethod
    def _check_window(cls, value: int) -> int:
        if value < 1:
            raise ValueError("candidate_window 必须 >= 1")
        return value

    @field_validator("min_confidence", "ambiguity_margin")
    @classmethod
    def _check_unit_interval(cls, value: float) -> float:
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("阈值必须位于 [0, 1]")
        return float(value)

    @field_validator("length_weight", "position_weight", "overlap_weight")
    @classmethod
    def _check_weight(cls, value: float) -> float:
        if float(value) < 0.0:
            raise ValueError("权重不能为负")
        return float(value)

    @field_validator("group_penalty")
    @classmethod
    def _check_penalty(cls, value: float) -> float:
        if float(value) < 0.0:
            raise ValueError("group_penalty 不能为负")
        return float(value)

    @field_validator("fallback_policy")
    @classmethod
    def _check_policy(cls, value: str) -> str:
        if value not in FALLBACK_POLICIES:
            raise ValueError(
                "fallback_policy 只能是 " + "|".join(FALLBACK_POLICIES))
        return value

    @field_validator("backend")
    @classmethod
    def _check_backend(cls, value: str) -> str:
        if value not in BACKENDS:
            raise ValueError("backend 只能是 " + "|".join(BACKENDS))
        return value

    @field_validator("low_confidence_policy")
    @classmethod
    def _check_low_confidence_policy(cls, value: str) -> str:
        if value not in LOW_CONFIDENCE_POLICIES:
            raise ValueError(
                "low_confidence_policy 只能是 " + "|".join(LOW_CONFIDENCE_POLICIES))
        return value

    def weights_sum(self) -> float:
        return self.length_weight + self.position_weight + self.overlap_weight


