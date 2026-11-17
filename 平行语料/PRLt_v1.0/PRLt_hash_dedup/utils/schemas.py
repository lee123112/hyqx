"""算子8 数据契约：去重配置严格校验，未知字段拒绝。"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

# 源文与目标文之间的固定分隔符，写入配置与血缘，避免边界碰撞。
PAIR_SEPARATOR = "\x1f"

SUPPORTED_HASH_ALGORITHMS = ("sha256", "md5")
SUPPORTED_NORMALIZATIONS = ("NFC", "NFD", "NFKC", "NFKD", "none")


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
    """哈希去重配置。

    - ``hash_algorithm``：``sha256``（默认）或 ``md5``（仅为兼容）。
    - ``unicode_normalization``：NFC（默认）/NFD/NFKC/NFKD/none。
    - ``casefold``：是否对规范化文本做 Unicode casefold。
    - ``collapse_whitespace``：是否把连续空白折叠为单空格并裁剪首尾。
    - ``keep_first``：保留首次出现。本版本仅支持 ``True``，``False`` 明确失败。
    - ``max_tracked_pairs``：去重状态上限，超过即明确失败，不静默截断去重范围。
    """

    model_config = ConfigDict(extra="forbid")

    hash_algorithm: Literal["sha256", "md5"] = "sha256"
    unicode_normalization: Literal["NFC", "NFD", "NFKC", "NFKD", "none"] = "NFC"
    casefold: StrictBool = True
    collapse_whitespace: StrictBool = True
    keep_first: StrictBool = True
    max_tracked_pairs: int = Field(default=5000000, gt=0)

    # 通用 IO 适配项（沿用算子4/7 模板），本节点仍只对 source/target 建键。
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])

    @model_validator(mode="after")
    def _check_bounds(self):
        if not self.keep_first:
            raise ValueError("当前版本仅支持 keep_first=true（保留首次出现）")
        if set(self.text_fields) != {"source", "target"}:
            raise ValueError("本节点只对 source/target 建键，text_fields 只能是 [source, target]")
        if not self.input_encodings:
            raise ValueError("input_encodings 不能为空")
        return self
