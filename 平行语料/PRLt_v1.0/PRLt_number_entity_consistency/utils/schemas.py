"""算子17 数据契约：配置模型、请求模型与校验规则；未知配置字段一律拒绝。"""
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .extract import CATEGORY_ORDER
from .units import CANONICAL_KEYS, DIMENSIONS


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
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    # 上游默认产物名；阶段内实际路径由 /predict 的 inputFile 指定
    input_file_name: str = "16_domain_checked.jsonl"
    # 参与一致性检测的字段：第一个为源侧、第二个为目标侧（只读，不改写原文）
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])

    # 任务声明的语言方向；记录 language_pair 必须与之一致
    source_lang: str = "zh"
    target_lang: str = "en"
    language_check: bool = True

    # 启用的抽取类别（按 CATEGORY_ORDER 归一）
    enabled_categories: List[str] = Field(default_factory=lambda: list(CATEGORY_ORDER))

    # 数值近似匹配容差：|a-b| <= abs_tol + rel_tol * max(|a|,|b|)
    numeric_absolute_tolerance: float = Field(default=0.0, ge=0.0)
    numeric_relative_tolerance: float = Field(default=0.0, ge=0.0)

    # 单位换算白名单
    unit_conversion: bool = True
    disabled_dimensions: List[str] = Field(default_factory=list)
    unit_aliases: Dict[str, str] = Field(default_factory=dict)

    # 货币别名（符号 / 词 -> ISO 代码）
    currency_aliases: Dict[str, str] = Field(default_factory=dict)

    # 实体模式：实体名 -> 正则列表（编译时统一加 IGNORECASE）
    entity_patterns: Dict[str, List[str]] = Field(default_factory=dict)
    entity_case_sensitive: bool = False

    # ---- 后端门禁 ----
    # rule=仅规则（默认，表格指定「规则归一化」）；auto=有模型用模型、无模型按 fallback_policy；
    # model=必须用模型（不可用即失败，绝不静默降级）
    backend: Literal["rule", "auto", "model"] = "rule"
    model_service_url: Optional[str] = None
    model_path: Optional[str] = None
    model_version: Optional[str] = None
    model_timeout_seconds: float = Field(default=30.0, gt=0)
    model_entrypoint: str = "check_consistency"
    fallback_policy: Literal["rule", "fail"] = "rule"
    model_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    max_model_errors: int = Field(default=20, ge=0)

    # ---- 记录去向 ----
    inconsistent_policy: Literal["mark_and_keep", "move_to_rejects"] = "mark_and_keep"
    waiting_manual_policy: Literal["mark_and_keep", "move_to_rejects"] = "mark_and_keep"

    # ---- 证据规模与扫描上限 ----
    max_conflicts_per_record: int = Field(default=20, ge=1)
    max_unparsed_per_record: int = Field(default=20, ge=1)
    max_text_scan_chars: int = Field(default=20000, ge=1)

    @model_validator(mode="after")
    def _validate_contract(self):
        problems: List[str] = []

        if not self.input_encodings:
            problems.append("input_encodings 不能为空")

        if self.source_lang.strip().lower() == self.target_lang.strip().lower():
            problems.append("source_lang 与 target_lang 不能相同")

        if len(self.text_fields) != 2:
            problems.append("text_fields 必须是两个字段名（源在前、目标在后）")
        elif not all(isinstance(field, str) and field.strip() for field in self.text_fields):
            problems.append("text_fields 的字段名必须是非空字符串")
        elif self.text_fields[0].strip() == self.text_fields[1].strip():
            problems.append("text_fields 的源字段与目标字段不能相同")

        if not self.enabled_categories:
            problems.append("enabled_categories 不能为空")
        unknown = [item for item in self.enabled_categories if item not in CATEGORY_ORDER]
        if unknown:
            problems.append(f"enabled_categories 含未知类别：{','.join(unknown)}")
        if len(set(self.enabled_categories)) != len(self.enabled_categories):
            problems.append("enabled_categories 不能重复")

        bad_dimensions = [item for item in self.disabled_dimensions if item not in DIMENSIONS]
        if bad_dimensions:
            problems.append(f"disabled_dimensions 含未知量纲：{','.join(bad_dimensions)}")

        for alias, code in self.unit_aliases.items():
            if code not in CANONICAL_KEYS:
                problems.append(f"unit_aliases 的值必须是白名单单位代码：{alias} -> {code}")

        for name, patterns in self.entity_patterns.items():
            if not patterns:
                problems.append(f"entity_patterns.{name} 的匹配列表不能为空")
                continue
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    problems.append(f"entity_patterns.{name} 正则非法：{exc}")

        if self.backend == "model" and not (self.model_service_url or self.model_path):
            problems.append("backend=model 必须配置 model_service_url 或 model_path")

        if problems:
            raise ValueError("；".join(problems))
        return self

    def normalized_categories(self) -> List[str]:
        enabled = set(self.enabled_categories)
        return [category for category in CATEGORY_ORDER if category in enabled]

    def expected_language_pair(self) -> str:
        return f"{self.source_lang.strip().lower()}-{self.target_lang.strip().lower()}"
