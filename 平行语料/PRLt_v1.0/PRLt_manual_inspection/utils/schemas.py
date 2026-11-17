"""算子20 数据契约：请求模型与可校验配置项。

配置项一律有默认值；非法枚举、越界数值和未知键都会被 pydantic 直接拒绝，
从而转成结构化的 `failed` 结果，不会静默取默认值。
【无血缘改造 2026-10-19】本算子不再生成/返回血缘文件，配置中无血缘专用项。
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

# 人工复核结果允许的状态；与 DEVELOPMENT_PLAN / TECHNICAL_DESIGN 一致。
KNOWN_REVIEW_STATUSES = ("accepted", "rejected", "needs_revision", "waiting_manual")

# 输入/输出文件名前缀由根 DATA_SCHEMA.md 固定，配置里仅做覆盖入口。
DEFAULT_EVIDENCE_FIELDS = [
    "quality_grade",
    "grade",
    "alignment_confidence",
    "confidence",
    "sentence_pair_confidence",
    "semantic_score",
    "fluency_score",
    "domain_confidence",
    "low_confidence",
    "conflict_flag",
    "review_required",
    "language_pair",
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

    # ---- 输入输出 ----
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    input_format: Literal["auto", "jsonl", "json"] = "auto"
    queue_name: str = "20_review_queue.jsonl"
    reviewed_name: str = "20_reviewed.jsonl"
    reject_name: str = "20_review_rejects.jsonl"
    state_file_name: str = "review_state.json"
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])
    record_id_fields: List[str] = Field(default_factory=lambda: ["record_id", "id"])

    # ---- 抽样 ----
    sample_ratio: float = 0.1
    min_sample_count: int = 1
    max_sample_count: int = 0  # 0 表示不设上限
    random_seed: int = 20261005
    governance_policy_version: str = "risk_review_v2"
    ordinary_sample_blocking: bool = False
    trigger_reasons: List[str] = Field(
        default_factory=lambda: ["grade_c", "low_confidence", "conflict", "review_required",
                                 "entity_conflict", "model_unavailable", "unknown_domain",
                                 "upstream_waiting_manual"])
    trigger_fields: List[str] = Field(
        default_factory=lambda: ["trigger_reason", "review_reason", "risk_reason",
                                 "risk_flags", "review_flags"])
    grade_fields: List[str] = Field(
        default_factory=lambda: ["quality_grade", "grade"])
    c_grade_values: List[str] = Field(default_factory=lambda: ["C"])
    low_confidence_fields: List[str] = Field(
        default_factory=lambda: ["low_confidence", "low_confidence_flag"])
    confidence_fields: List[str] = Field(
        default_factory=lambda: ["alignment_confidence", "confidence",
                                 "sentence_pair_confidence"])
    low_confidence_threshold: float = 0.7
    conflict_fields: List[str] = Field(
        default_factory=lambda: ["review_conflict", "conflict_flag", "conflict"])
    review_required_field: str = "review_required"

    # ---- 队列记录 ----
    queue_status: Literal["waiting_manual"] = "waiting_manual"
    evidence_fields: List[str] = Field(default_factory=lambda: list(DEFAULT_EVIDENCE_FIELDS))
    max_evidence_items: int = 8
    max_evidence_chars: int = 200
    max_reason_chars: int = 500

    # ---- 人工结果回写 ----
    review_payload_file: Optional[str] = None
    review_payload: List[Dict[str, Any]] = Field(default_factory=list)
    review_statuses: List[str] = Field(default_factory=lambda: list(KNOWN_REVIEW_STATUSES))
    require_reviewer: bool = True
    require_reviewed_at: bool = True
    require_review_reason: bool = True
    validate_reviewed_at: bool = True
    require_known_target: bool = True
    mask_reviewer_id: bool = True

    # ---- 校验 ----
    @field_validator("record_id_fields", "review_statuses")
    @classmethod
    def _non_empty_list(cls, value):
        if not value:
            raise ValueError("列表配置不能为空")
        return value

    @field_validator("review_statuses")
    @classmethod
    def _known_statuses(cls, value):
        unknown = [item for item in value if item not in KNOWN_REVIEW_STATUSES]
        if unknown:
            raise ValueError("存在未支持的人工复核状态：%s" % ",".join(unknown))
        return value

    @field_validator("sample_ratio")
    @classmethod
    def _ratio_range(cls, value):
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("sample_ratio 必须在 [0, 1] 之间")
        return float(value)

    @field_validator("low_confidence_threshold")
    @classmethod
    def _threshold_range(cls, value):
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("low_confidence_threshold 必须在 [0, 1] 之间")
        return float(value)

    @field_validator("min_sample_count", "max_sample_count", "random_seed")
    @classmethod
    def _non_negative_int(cls, value):
        if int(value) < 0:
            raise ValueError("抽样计数与随机种子必须为非负整数")
        return int(value)

    @field_validator("max_evidence_items", "max_evidence_chars", "max_reason_chars")
    @classmethod
    def _positive_int(cls, value):
        if int(value) <= 0:
            raise ValueError("证据与备注长度上限必须为正整数")
        return int(value)

    @field_validator("governance_policy_version")
    @classmethod
    def _non_empty_policy_version(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("governance_policy_version 不能为空")
        return value.strip()

    @model_validator(mode="after")
    def _check_sample_bounds(self):
        if self.max_sample_count and self.max_sample_count < self.min_sample_count:
            raise ValueError("max_sample_count 不能小于 min_sample_count")
        return self
