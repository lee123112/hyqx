"""算子7 数据契约：长度阈值严格校验，未知字段拒绝。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import StrictBool, BaseModel, ConfigDict, Field, model_validator


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


class LanguagePairRatioPolicy(BaseModel):
    """A traceable soft/hard ratio policy for one declared language pair."""

    model_config = ConfigDict(extra="forbid")

    hard_min_length_ratio: float = Field(gt=0)
    hard_max_length_ratio: float = Field(gt=0)
    soft_violation_action: Literal["warn"] = "warn"

    @model_validator(mode="after")
    def _check_bounds(self):
        if self.hard_min_length_ratio > self.hard_max_length_ratio:
            raise ValueError("hard_min_length_ratio 不能大于 hard_max_length_ratio")
        return self


class OperatorConfig(BaseModel):
    """长度过滤配置。

    阈值语义：闭区间（含边界）。比例定义为 ``target_chars / max(source_chars, 1)``。
    未知字段一律拒绝；负数、最小值大于最大值、非正比例都会被拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    min_source_chars: int = Field(default=1, ge=0)
    max_source_chars: int = Field(default=5000, ge=0)
    min_target_chars: int = Field(default=1, ge=0)
    max_target_chars: int = Field(default=5000, ge=0)
    min_length_ratio: float = Field(default=0.2, gt=0)
    max_length_ratio: float = Field(default=5.0, gt=0)
    on_violation: Literal["reject", "warn"] = "reject"
    language_pair_ratio_policies: Dict[str, LanguagePairRatioPolicy] = Field(default_factory=dict)

    # 通用 IO 适配项（沿用算子4/6 模板），本节点仍只按 source/target 计算长度。
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])

    @model_validator(mode="after")
    def _check_bounds(self):
        if self.min_source_chars > self.max_source_chars:
            raise ValueError("min_source_chars 不能大于 max_source_chars")
        if self.min_target_chars > self.max_target_chars:
            raise ValueError("min_target_chars 不能大于 max_target_chars")
        if self.min_length_ratio > self.max_length_ratio:
            raise ValueError("min_length_ratio 不能大于 max_length_ratio")
        for pair, policy in self.language_pair_ratio_policies.items():
            if not pair.strip() or "-" not in pair:
                raise ValueError("language_pair_ratio_policies 键必须为 source-target")
            if policy.hard_min_length_ratio > self.min_length_ratio:
                raise ValueError("语言对 hard_min_length_ratio 不能高于基础最小比例")
            if policy.hard_max_length_ratio < self.max_length_ratio:
                raise ValueError("语言对 hard_max_length_ratio 不能低于基础最大比例")
        if set(self.text_fields) != {"source", "target"}:
            raise ValueError("本节点只按 source/target 计算长度，text_fields 只能是 [source, target]")
        if not self.input_encodings:
            raise ValueError("input_encodings 不能为空")
        return self
