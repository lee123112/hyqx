"""算子4 数据契约。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import StrictBool, BaseModel, ConfigDict, Field


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

    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])
    strip_html: bool = True
    remove_invisible: bool = True
    normalize_whitespace: bool = True
    whitespace_mode: Literal["linewise", "global", "trim", "preserve"] = "linewise"
