"""数据契约（Pydantic 模型）。字段冻结口径见 demo/DATA_SCHEMA.md。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import StrictBool, BaseModel, ConfigDict, Field


class SentencePair(BaseModel):
    id: str
    source: str
    target: str
    source_lang: str = "zh"
    target_lang: str = "en"
    metadata: Dict[str, Any] = Field(default_factory=dict)


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

    required_fields: List[str] = Field(default_factory=lambda: ["id", "source", "target"])
    empty_check_fields: List[str] = Field(default_factory=lambda: ["source", "target"])
    check_declared_lang: bool = True
    source_lang: str = "zh"
    target_lang: str = "en"
    on_violation: Literal["reject", "warn"] = "reject"
