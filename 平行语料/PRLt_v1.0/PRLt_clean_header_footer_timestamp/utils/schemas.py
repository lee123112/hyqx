"""算子5 数据契约：三接口请求与页眉页脚/时间戳清理配置。

配置口径（TECHNICAL_DESIGN.md §4）：
- 未知字段、负数/越界比例、无效正则一律拒绝（ValidationError）；
- `timestamp_patterns` / `page_number_patterns` / `header_footer_patterns` 为整行模式，
  使用 `fullmatch` 匹配，模式必须覆盖整行；
- `boundary_only=true` 表示该模式只在记录首/尾区域生效，命中正文中间时不删除；
- `uncertain_action` 控制「模式命中但可能影响正文」时的去向。
"""
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)


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


class PatternSpec(BaseModel):
    """一条整行匹配模式：name 进血缘与证据，pattern 必须是合法正则。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    pattern: str
    boundary_only: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("pattern.name 不能为空")
        return value.strip()

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("pattern.pattern 不能为空")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern 不是合法正则：{exc}") from exc
        return value


def _default_timestamp_patterns() -> List[dict]:
    return [
        {"name": "subtitle_range",
         "pattern": r"^\s*\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*$"},
        {"name": "subtitle_range_short", "pattern": r"^\s*\d{1,2}:\d{2}\s*-->\s*\d{1,2}:\d{2}\s*$"},
        {"name": "subtitle_single",
         "pattern": r"^\s*\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*$", "boundary_only": True},
        {"name": "bracket_time",
         "pattern": r"^\s*[\[\(]\s*\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?\s*[\]\)]\s*$",
         "boundary_only": True},
    ]


def _default_page_number_patterns() -> List[dict]:
    return [
        {"name": "page_number_decorated", "pattern": r"^\s*[-–—]\s*\d{1,4}\s*[-–—]\s*$"},
        {"name": "page_number_cn",
         "pattern": r"^\s*第\s*\d{1,4}\s*页(?:\s*[/／]\s*共?\s*\d{1,4}\s*页)?\s*$"},
        {"name": "page_number_en", "pattern": r"^\s*[Pp]age\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$"},
        {"name": "page_number_fraction", "pattern": r"^\s*\d{1,4}\s*[/／]\s*\d{1,4}\s*$"},
        {"name": "page_number_bare", "pattern": r"^\s*\d{1,4}\s*$", "boundary_only": True},
    ]


def _default_header_footer_patterns() -> List[dict]:
    return [
        {"name": "header_footer_url", "pattern": r"^\s*https?://\S+\s*$", "boundary_only": True},
        {"name": "header_footer_copyright",
         "pattern": r"^\s*(?:©|\(c\)|版权所有|版权|Copyright)\s*.*$", "boundary_only": True},
        {"name": "header_footer_confidential",
         "pattern": r"^\s*(?:机密|内部资料|Confidential)\s*$", "boundary_only": True},
    ]


def _default_inline_timestamp_patterns() -> List[dict]:
    return [{
        "name": "subtitle_range_inline",
        "pattern": r"\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}",
    }]


def _default_inline_page_patterns() -> List[dict]:
    return [
        {"name": "page_marker_cn", "pattern": r"第\s*\d{1,4}\s*页"},
        {"name": "page_marker_en", "pattern": r"[Pp]age\s*\d{1,4}(?![0-9])"},
    ]


def _normalize_patterns(value, prefix: str) -> List[dict]:
    """允许 `- '正则'` 简写，统一成 PatternSpec 需要的对象列表。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{prefix} 必须是列表")
    normalized = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            normalized.append({"name": f"{prefix}_{index}", "pattern": item})
        elif isinstance(item, dict):
            normalized.append(item)
        else:
            raise ValueError(f"{prefix} 第 {index} 项必须是字符串或对象")
    return normalized


class OperatorConfig(BaseModel):
    """算子5 配置契约；未知字段、非法比例与无效正则全部拒绝。"""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])

    # 总开关
    remove_timestamps: bool = True
    remove_page_numbers: bool = True
    remove_header_footer: bool = True
    remove_repeated_lines: bool = True
    remove_blank_lines: bool = True
    remove_inline_timestamps: bool = True
    remove_inline_page_markers: bool = False

    # 整行模式
    timestamp_patterns: List[PatternSpec] = Field(default_factory=_default_timestamp_patterns)
    page_number_patterns: List[PatternSpec] = Field(default_factory=_default_page_number_patterns)
    header_footer_patterns: List[PatternSpec] = Field(default_factory=_default_header_footer_patterns)

    # 行内模式
    inline_timestamp_patterns: List[PatternSpec] = Field(
        default_factory=_default_inline_timestamp_patterns)
    inline_page_patterns: List[PatternSpec] = Field(default_factory=_default_inline_page_patterns)

    # 重复行判定
    min_repeat_ratio: float = Field(default=0.5, gt=0.0, le=1.0)
    min_repeat_count: int = Field(default=2, ge=2)
    head_lines: int = Field(default=2, ge=0)
    tail_lines: int = Field(default=2, ge=0)
    max_tracked_lines: int = Field(default=200000, ge=1)
    min_repeat_line_chars: int = Field(default=2, ge=1)
    max_repeat_line_chars: int = Field(default=120, ge=1)

    # 不确定性处理
    max_whole_line_chars: int = Field(default=120, ge=1)
    protect_whole_field: bool = True
    uncertain_action: Literal["waiting_manual", "keep_original"] = "waiting_manual"

    @field_validator("timestamp_patterns", "page_number_patterns", "header_footer_patterns",
                     "inline_timestamp_patterns", "inline_page_patterns", mode="before")
    @classmethod
    def _normalize_pattern_fields(cls, value, info):
        return _normalize_patterns(value, info.field_name.replace("_patterns", ""))

    @model_validator(mode="after")
    def _cross_check(self):
        if not self.input_encodings:
            raise ValueError("input_encodings 不能为空")
        for field in self.text_fields:
            if not isinstance(field, str) or not field.strip():
                raise ValueError("text_fields 只能包含非空字符串")
        missing = {"source", "target"} - set(self.text_fields)
        if missing:
            raise ValueError("text_fields 必须包含 source 与 target")
        if self.max_repeat_line_chars < self.min_repeat_line_chars:
            raise ValueError("max_repeat_line_chars 不能小于 min_repeat_line_chars")
        return self
