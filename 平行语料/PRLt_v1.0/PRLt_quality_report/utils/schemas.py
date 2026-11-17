"""算子19 数据契约：请求模型与可校验配置项。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool

# 每个阶段统计记录必须提供的业务单元计数（禁止用文件大小替代）。
STAGE_COUNTER_FIELDS = [
    "input_units",
    "output_units",
    "filtered_units",
    "failed_units",
    "duplicate_units",
    "low_confidence_units",
    "manual_rejected_units",
]

DEFAULT_STAGE_ORDER = [
    "输入核验",
    "通用粗清洗",
    "多粒度双语对齐处理",
    "双语质量检测过滤",
    "数据集交付",
]

DEFAULT_UNIT_TYPE_ALIASES = {
    "sentence": "sentence_pair",
    "paragraph": "paragraph_pair",
    "clause": "clause_pair",
    "phrase": "phrase_pair",
    "document": "document_pair",
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
    """配置项全部有默认值；非法枚举与未知键都会被 pydantic 拒绝。"""

    model_config = ConfigDict(extra="forbid")

    # ---- 输入输出 ----
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    input_format: Literal["auto", "jsonl", "json"] = "auto"
    report_name: str = "19_quality_report.json"
    group_detail_name: str = "19_quality_report.jsonl"
    reject_name: str = "19_rejects.jsonl"
    write_group_detail: bool = True

    # ---- 版本与口径 ----
    report_version: str = "v1.0.0"
    config_version: str = "v1.0.0"
    default_unit_type: str = "sentence_pair"
    unit_type_aliases: Dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_UNIT_TYPE_ALIASES))
    default_pipeline_type: str = "parallel_corpus"
    default_pipeline_version: str = "v1.0.0"

    # ---- 阶段与口径 ----
    stage_order: List[str] = Field(default_factory=lambda: list(DEFAULT_STAGE_ORDER))
    require_stage_continuity: bool = True
    require_known_stage: bool = True
    stage_required_fields: List[str] = Field(
        default_factory=lambda: list(STAGE_COUNTER_FIELDS))
    missing_field_policy: Literal["failed", "partial", "skip"] = "failed"
    stage_balance_check: bool = False
    # [无血缘改造 2026-10-19] 血缘专用配置已停用；阶段链连续性（require_stage_continuity）保留。
    # require_lineage: bool = True
    # 转化率分母固定取产线首个阶段的有效输入；该阶段统计缺失即视为缺少分母来源。
    low_quality_input_stage: Optional[str] = "输入核验"

    # ---- 指标与阈值 ----
    low_quality_conversion_threshold: float = 0.85
    high_quality_retention_threshold: float = 0.90
    high_quality_grades: List[str] = Field(default_factory=lambda: ["S", "A"])
    rate_precision: int = 6
    below_threshold_status: Literal["partial", "failed"] = "partial"

    # ---- 一致性 ----
    require_source_consistency: bool = True
    require_group_consistency: bool = True
    # 一期链路 09→10→11 会切换统计单元（句对→子句对）：true=混合单元不阻断，
    # 降级为 warning 并按阶段各自口径报告；false=维持旧的 unit_type_inconsistent 硬阻断
    allow_mixed_unit_types: bool = True
    group_dimensions: List[str] = Field(
        default_factory=lambda: ["task_id", "language_pair", "source_type", "domain"])
    task_id_field: str = "task_id"
    task_id_prefix_separator: str = "_"

    # ---- 侧输入（工程侧可选提供） ----
    stage_statistics_file: Optional[str] = "stage_statistics.json"
    manifest_file: Optional[str] = "18_manifest.json"
    # [无血缘改造 2026-10-19] 血缘侧输入配置已停用。
    # upstream_lineage_file: Optional[str] = "lineage.json"
    # use_lineage_statistics: bool = True
