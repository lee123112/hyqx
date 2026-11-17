"""算子13 数据契约：分级阈值、量纲、状态映射与请求模型；未知配置字段一律拒绝。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

GRADE_KEYS = ("S", "A", "C")
DEFAULT_GRADE_LABELS = {"S": "S", "A": "A", "C": "C"}
DEFAULT_GRADE_STATUS = {
    "S": "accepted_for_quality_check",
    "A": "accepted_for_quality_check",
    "C": "waiting_manual",
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

    # 任务声明的语言方向；记录自带的 source_lang/target_lang 必须与之一致。
    # 只做声明一致性校验，真实语言检测属算子 6 职责。
    source_lang: str = "zh"
    target_lang: str = "en"
    language_check: bool = True

    # 上游默认产物文件名（阶段内实际路径由 /predict 的 inputFile 指定）
    input_file_name: str = "12_scored.jsonl"

    # 置信度字段读取优先级（顶层优先，其次 metadata，见 read_metadata_fallback）
    confidence_fields: List[str] = Field(
        default_factory=lambda: ["alignment_confidence", "confidence"])
    # 已存在的 0-100 质量分字段（若存在，必须与置信度换算值一致）
    declared_score_fields: List[str] = Field(
        default_factory=lambda: ["quality_score"])
    read_metadata_fallback: bool = True

    # 量纲：auto=<=1 视为 0-1 比例并乘 100，1<N<=100 视为百分制；
    # ratio_0_1=必须落在 [0,1]；percent_0_100=必须落在 [0,100]。
    score_scale: Literal["auto", "ratio_0_1", "percent_0_100"] = "auto"
    # 置信度换算值与已存在 quality_score 的最大允许偏差，超过判 score_conflict
    score_consistency_tolerance: float = Field(default=0.01, ge=0.0)

    # 严格开闭区间：S 为 (s,100]，A 为 (a,s]，C 为 [0,a]
    grade_s_threshold: float = Field(default=90.0, ge=0.0, le=100.0)
    grade_a_threshold: float = Field(default=80.0, ge=0.0, le=100.0)

    # 等级名做成配置项：甲方四级口径（优质/合格/待复核/不入库）可在此改名
    grade_labels: Dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_GRADE_LABELS))
    # 等级 -> 复核状态映射；C 级默认 waiting_manual，不得直接进入交付节点
    grade_status_map: Dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_GRADE_STATUS))
    # 允许进入交付节点的等级；其余由交付节点拦截
    delivery_eligible_grades: List[str] = Field(default_factory=lambda: ["S", "A"])

    grade_rule_version: str = "v1.0.0"
    score_conversion_version: str = "v1.0.0"

    # 本节点要求严格 UTF-8（上游已完成编码归一）
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.source_lang == self.target_lang:
            raise ValueError("source_lang 与 target_lang 不能相同")
        if not (0.0 <= self.grade_a_threshold < self.grade_s_threshold <= 100.0):
            raise ValueError("必须满足 0<=grade_a_threshold<grade_s_threshold<=100")
        if set(self.grade_labels) != set(GRADE_KEYS):
            raise ValueError("grade_labels 必须且只能包含 S/A/C 三个键")
        if any(not isinstance(v, str) or not v.strip() for v in self.grade_labels.values()):
            raise ValueError("grade_labels 的取值必须是非空字符串")
        if set(self.grade_status_map) != set(GRADE_KEYS):
            raise ValueError("grade_status_map 必须且只能包含 S/A/C 三个键")
        if any(not isinstance(v, str) or not v.strip() for v in self.grade_status_map.values()):
            raise ValueError("grade_status_map 的取值必须是非空字符串")
        unknown = [g for g in self.delivery_eligible_grades if g not in GRADE_KEYS]
        if unknown:
            raise ValueError(f"delivery_eligible_grades 含未知等级：{unknown}")
        if not self.confidence_fields:
            raise ValueError("confidence_fields 不能为空")
        if len(set(self.confidence_fields)) != len(self.confidence_fields):
            raise ValueError("confidence_fields 不能重复")
        if not self.declared_score_fields:
            raise ValueError("declared_score_fields 不能为空")
        if not self.input_encodings:
            raise ValueError("input_encodings 不能为空")
        return self
