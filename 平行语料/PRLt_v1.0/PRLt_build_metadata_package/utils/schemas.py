"""算子18 数据契约：接口请求模型与配置校验（未知配置字段一律拒绝）。

【无血缘改造 2026-10-19】本算子不再读取/生成/打包任何血缘文件：
`upstream_lineage_file`、`require_lineage_refs` 两个血缘专用配置项已停用；
`operator_versions` 仅来自显式配置，`lineage_refs` 恒为 `[]`。
"""
from typing import Any, Dict, List, Literal, Optional

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

    # ---- 输入输出 ----
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    input_file_name: str = "16_domain_checked.jsonl"
    dataset_jsonl_name: str = "18_dataset.jsonl"
    dataset_tsv_name: str = "18_dataset.tsv"
    manifest_name: str = "18_manifest.json"
    package_name: str = "18_delivery.zip"
    reject_name: str = "18_rejects.jsonl"
    # [无血缘改造 2026-10-19] 不再读取上游血缘文件，配置项停用：
    # upstream_lineage_file: str = "lineage.json"

    # ---- 入库门禁 ----
    grade_fields: List[str] = Field(default_factory=lambda: ["quality_grade", "grade"])
    allowed_grades: List[str] = Field(default_factory=lambda: ["S", "A"])
    allow_c: bool = False
    allow_waiting_manual: bool = False
    # 最小长度门禁（2026-11-04）：最终兜底，源/目标去空白后少于该字符数即拒收。
    # 上游 10/14 已有碎片治理，这里防的是"任何上游漏网的超短片段"。
    min_source_chars: int = Field(default=5, ge=0)
    min_target_chars: int = Field(default=5, ge=0)
    # 2026-11-04：纯长度会把「增编/Addendum」这类真术语对一起砍掉。fragment_only
    # 只在短记录同时呈碎片形态（源文以顿号/逗号收尾、译文小写开头或半截逗号收尾、
    # 译文是裸日期）时才拒收；always 退回旧的纯长度硬门。
    short_record_policy: Literal["fragment_only", "always"] = "fragment_only"
    waiting_manual_values: List[str] = Field(
        default_factory=lambda: ["waiting_manual", "waiting_quality"])
    review_status_fields: List[str] = Field(default_factory=lambda: ["review_status"])
    status_fields: Dict[str, List[str]] = Field(default_factory=lambda: {
        "semantic": ["quality_status", "semantic_status"],
        "fluency": ["fluency_status"],
        "domain": ["domain_status"],
    })
    accepted_status_map: Dict[str, List[str]] = Field(default_factory=lambda: {
        "semantic": ["accepted", "passed"],
        "fluency": ["accepted"],
        "domain": ["accepted"],
    })
    # 上游 15 在 Gold 校准完成前只输出 waiting_calibration，规范 4.15 要求该分数
    # 仅作辅助证据，不作为放行/拦截的最终依据。enforce=严格按 accepted_status_map；
    # evidence_only_until_gold=仅当业务批准字段明确为 false 时，把等待状态放行为证据。
    fluency_gate_mode: Literal["enforce", "evidence_only_until_gold"] = "enforce"
    fluency_approval_fields: List[str] = Field(default_factory=lambda: [
        "fluency_business_gate_approved"])
    fluency_evidence_statuses: List[str] = Field(default_factory=lambda: [
        "waiting_calibration"])
    require_status_fields: bool = True
    respect_delivery_eligible: bool = True
    delivery_eligible_fields: List[str] = Field(default_factory=lambda: ["delivery_eligible"])
    # 实体一致性是独立交付门禁；即使任务允许临时 waiting_manual 打包，
    # 明确不一致的记录也必须隔离，不能随 waiting_manual 旁路进入临时包。
    entity_status_fields: List[str] = Field(
        default_factory=lambda: ["entity_consistency_status"])
    accepted_entity_statuses: List[str] = Field(default_factory=lambda: ["consistent"])
    reject_inconsistent_entities: bool = True

    # ---- 元数据完整性 ----
    required_fields: List[str] = Field(default_factory=lambda: [
        "id", "source", "target", "source_lang", "target_lang", "quality_grade",
        "quality_score", "alignment_confidence", "domain", "source_ref",
    ])
    derive_language_pair: bool = True
    language_pair_fields: List[str] = Field(default_factory=lambda: ["language_pair"])
    language_pair_separator: str = "-"
    conflict_check_fields: List[str] = Field(default_factory=lambda: [
        "id", "source", "target", "source_lang", "target_lang", "quality_grade",
        "quality_score", "alignment_confidence", "domain", "language_pair",
    ])
    metadata_conflict_policy: Literal["reject", "prefer_top_level", "prefer_metadata"] = "reject"
    quality_score_fields: List[str] = Field(default_factory=lambda: ["quality_score"])
    alignment_confidence_fields: List[str] = Field(
        default_factory=lambda: ["alignment_confidence"])
    quality_score_range: List[float] = Field(default_factory=lambda: [0.0, 100.0])
    alignment_confidence_range: List[float] = Field(default_factory=lambda: [0.0, 1.0])
    duplicate_id_policy: Literal["reject", "keep"] = "reject"
    require_operator_versions: bool = False
    # [无血缘改造 2026-10-19] lineage_refs 恒为空，不再作为必填校验项：
    # require_lineage_refs: bool = False

    # ---- 版本与来源 ----
    # operator_versions 仅来自显式配置；lineage_refs 恒为 [] 且来源 unavailable。
    operator_versions: Dict[str, str] = Field(default_factory=dict)
    pipeline_version: str = "v1.0.0"
    package_version: str = "v1.0.0"
    manifest_version: str = "v1.0.0"
    default_unit_type: str = "sentence"
    unit_type_fields: List[str] = Field(default_factory=lambda: ["granularity"])
    model_version_fields: List[str] = Field(default_factory=lambda: [
        "model_version", "semantic_model_version", "fluency_model_version", "domain_model_version"])
    rule_version_fields: List[str] = Field(default_factory=lambda: [
        "grade_rule_version", "confidence_version", "score_conversion_version"])
    source_type_fields: List[str] = Field(default_factory=lambda: ["source_type", "source_batch"])

    # ---- 打包 ----
    tsv_fields: List[str] = Field(default_factory=lambda: [
        "id", "source", "target", "source_lang", "target_lang", "language_pair",
        "quality_grade", "quality_score", "alignment_confidence", "domain",
        "source_ref", "created_at",
    ])
    tsv_delimiter: str = "\t"
    zip_members: List[str] = Field(default_factory=lambda: [
        "18_dataset.jsonl", "18_dataset.tsv", "18_manifest.json"])
    zip_compression: Literal["deflated", "stored"] = "deflated"
    # 输入>0 但全部被门禁拦截属于正常裁定：默认 partial 产出空数据集；failed 为严格模式
    empty_delivery_policy: Literal["failed", "partial"] = "partial"
    indexes: Dict[str, Dict[str, Any]] = Field(default_factory=lambda: {
        "mongodb": {"enabled": False, "mode": "reserved"},
        "elasticsearch": {"enabled": False, "mode": "reserved"},
        "parquet": {"enabled": False, "mode": "reserved"},
    })

    @model_validator(mode="after")
    def _check_consistency(self):
        if not self.input_encodings:
            raise ValueError("input_encodings 不能为空")
        if not self.allowed_grades:
            raise ValueError("allowed_grades 不能为空")
        if len(self.tsv_delimiter) != 1:
            raise ValueError("tsv_delimiter 必须是单个字符")
        for name, values in self.accepted_status_map.items():
            if name not in self.status_fields:
                raise ValueError("accepted_status_map 中的状态名必须在 status_fields 中定义：%s" % name)
            if not values:
                raise ValueError("accepted_status_map[%s] 不能为空" % name)
        for name in self.status_fields:
            if name not in self.accepted_status_map:
                raise ValueError("status_fields[%s] 缺少对应的 accepted_status_map" % name)
        if self.fluency_gate_mode == "evidence_only_until_gold":
            if "fluency" not in self.status_fields:
                raise ValueError("evidence_only_until_gold 需要在 status_fields 中定义 fluency")
            if not self.fluency_approval_fields:
                raise ValueError("fluency_approval_fields 不能为空")
            if not self.fluency_evidence_statuses:
                raise ValueError("fluency_evidence_statuses 不能为空")
        for name, limits in (("quality_score_range", self.quality_score_range),
                             ("alignment_confidence_range", self.alignment_confidence_range)):
            if len(limits) != 2 or limits[0] > limits[1]:
                raise ValueError("%s 必须是 [最小值, 最大值] 且最小值不大于最大值" % name)
        valid_names = {self.dataset_jsonl_name, self.dataset_tsv_name,
                       self.manifest_name, self.reject_name}
        if not self.zip_members:
            raise ValueError("zip_members 不能为空")
        unknown = [name for name in self.zip_members if name not in valid_names]
        if unknown:
            raise ValueError("zip_members 含未知产物名：%s" % ",".join(unknown))
        if self.dataset_jsonl_name not in self.zip_members:
            raise ValueError("zip_members 必须包含数据集主文件 %s" % self.dataset_jsonl_name)
        return self
