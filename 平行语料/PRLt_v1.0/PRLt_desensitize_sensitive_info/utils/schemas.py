"""算子8 数据契约：三接口请求与敏感信息脱敏配置。

配置口径（`TECHNICAL_DESIGN.md` §1/§2）：
- 规则必须带边界，且正则可编译；编译失败或未知字段一律拒绝（`failed`）；
- 规则按 `priority` 升序优先，同优先级按「更长匹配优先，再按起点靠前」处理重叠；
- 占位符默认 `[REDACTED_<TYPE>_<N>]`，同一记录内同类同值共享序号；
- 源/目标命中数量或规范化值不一致时写 `redaction_alignment_warning`；
  `strict_sync=true` 时该记录转入 `waiting_manual`，禁止猜测对应关系；
- 本算子不调用任何外部模型，不产生置信度或概率。
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, field_validator,
                      model_validator)

# 直辖市：与「省 + 市 + 区」模式互斥，单独用「市 + 区」模式识别
_MUNICIPALITIES = ("北京", "上海", "天津", "重庆")

_TYPE_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{1,31}$"


def default_address_regions() -> List[str]:
    """地址识别的省级锚点词表；正式业务可按需替换。"""
    return [
        "北京", "天津", "上海", "重庆",
        "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
        "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "四川",
        "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆", "内蒙古",
        "香港", "澳门", "台湾",
    ]


def municipality_regions() -> List[str]:
    return list(_MUNICIPALITIES)


def default_rules() -> List[dict]:
    """默认规则集；全部带显式边界，避免命中长数字串或英文单词内部。"""
    return [
        {
            "name": "email_basic",
            "type": "email",
            "priority": 10,
            "pattern": (
                r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@"
                r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
                r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+"
            ),
        },
        {
            "name": "id_card_18",
            "type": "id_card",
            "priority": 15,
            "pattern": (
                r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}"
                r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![0-9A-Za-z])"
            ),
        },
        {
            "name": "id_card_15",
            "type": "id_card",
            "priority": 16,
            "pattern": (
                r"(?<![0-9A-Za-z])[1-9]\d{5}\d{2}(?:0[1-9]|1[0-2])"
                r"(?:0[1-9]|[12]\d|3[01])\d{3}(?![0-9A-Za-z])"
            ),
        },
        {
            "name": "phone_cn_mobile",
            "type": "phone",
            "priority": 20,
            "pattern": r"(?<!\d)(?:\+?86[\s\-]?)?1[3-9]\d{9}(?!\d)",
        },
        {
            "name": "phone_cn_landline",
            "type": "phone",
            "priority": 25,
            "pattern": r"(?<!\d)0\d{2,3}[\s\-]?\d{7,8}(?!\d)",
        },
        {
            "name": "bank_card_plain",
            "type": "account",
            "priority": 30,
            "pattern": r"(?<!\d)\d{16,19}(?!\d)",
        },
        {
            "name": "bank_card_spaced",
            "type": "account",
            "priority": 31,
            "pattern": r"(?<!\d)\d{4}(?:[ \-]\d{4}){3,4}(?!\d)",
        },
        {
            "name": "account_kv_cn",
            "type": "account",
            "priority": 35,
            "group": 1,
            # 中文分支允许零分隔符，英文分支必须有 : ： # = 之一（否则 accountable /
            # account work 之类的普通词会被当成账号值）。两分支共用一个捕获组。
            "pattern": (
                r"(?:(?:账号|帐号|账户|卡号)[\s:：#]*"
                r"|\b(?:account|Account)[\s]*[:：#=][\s]*)"
                r"([A-Za-z0-9_\-]{4,32})"
            ),
        },
        {
            "name": "address_cn_province",
            "type": "address",
            "priority": 40,
            "pattern": (
                r"(?:{provinces})"
                r"(?:特别行政区|壮族自治区|回族自治区|维吾尔自治区|自治区|省)"
                r"[\u4e00-\u9fff]{2,10}(?:市|自治州|地区)"
                r"[\u4e00-\u9fff]{2,10}(?:区|县|旗|镇|街道)"
                r"[\u4e00-\u9fffA-Za-z0-9\-]{0,30}"
            ),
        },
        {
            "name": "address_cn_municipality",
            "type": "address",
            "priority": 41,
            "pattern": (
                r"(?:{municipalities})市"
                r"[\u4e00-\u9fff]{2,10}(?:区|县)"
                r"[\u4e00-\u9fffA-Za-z0-9\-]{0,30}"
            ),
        },
    ]


class PredictRequest(BaseModel):
    uniqueId: str
    operatorId: Optional[str] = None
    isLastOperator: Optional[StrictBool] = None
    inputFile: Optional[str] = None
    params: Dict[str, object] = Field(default_factory=dict)
    config: Dict[str, object] = Field(default_factory=dict)


class PredictResponse(BaseModel):
    code: int
    msg: str
    data: Optional[Dict[str, object]] = None


class RedactionRule(BaseModel):
    """一条脱敏规则；`group=0` 表示整段匹配，`group>0` 表示只替换该捕获组。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    pattern: str
    group: int = Field(default=0, ge=0, le=9)
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("rule.name 不能为空")
        return value.strip()

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value):
        import re
        if not isinstance(value, str) or not re.match(_TYPE_PATTERN, value):
            raise ValueError("rule.type 必须是 2-32 位字母开头的标识符")
        return value

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, value):
        import re
        if not isinstance(value, str) or not value.strip():
            raise ValueError("rule.pattern 不能为空")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"rule.pattern 不是合法正则：{exc}") from exc
        return value


class OperatorConfig(BaseModel):
    """算子8 配置契约；未知字段、无效正则和非法占位符全部拒绝。"""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])
    # 声明允许脱敏的 metadata 字段；未声明的 metadata 一律不改写
    metadata_fields: List[str] = Field(default_factory=list)

    placeholder_template: str = "[REDACTED_{type}_{index}]"
    rule_version: str = "rules-v1.0.1"

    rules: List[RedactionRule] = Field(default_factory=default_rules)
    extra_rules: List[RedactionRule] = Field(default_factory=list)
    address_regions: List[str] = Field(default_factory=default_address_regions)

    sync_check_mode: Literal["value_multiset", "type_counts", "off"] = "value_multiset"
    strict_sync: bool = True
    max_hits_per_record: int = Field(default=200, ge=1, le=100000)

    @field_validator("placeholder_template")
    @classmethod
    def _validate_template(cls, value):
        if not isinstance(value, str) or "{type}" not in value or "{index}" not in value:
            raise ValueError("placeholder_template 必须同时包含 {type} 与 {index}")
        leftover = value.replace("{type}", "").replace("{index}", "")
        if "{" in leftover or "}" in leftover:
            raise ValueError("placeholder_template 只能包含 {type} 与 {index} 两个占位符")
        return value

    @field_validator("rule_version")
    @classmethod
    def _validate_rule_version(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("rule_version 不能为空")
        return value.strip()

    @field_validator("metadata_fields")
    @classmethod
    def _validate_metadata_fields(cls, value):
        for field in value:
            if not isinstance(field, str) or not field.strip():
                raise ValueError("metadata_fields 只能包含非空字符串")
        return value

    @field_validator("address_regions")
    @classmethod
    def _validate_regions(cls, value):
        cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not cleaned:
            raise ValueError("address_regions 不能为空，否则地址规则会失效")
        return cleaned

    @model_validator(mode="after")
    def _cross_check(self):
        if not self.input_encodings:
            raise ValueError("input_encodings 不能为空")
        missing = {"source", "target"} - set(self.text_fields)
        if missing:
            raise ValueError("text_fields 必须包含 source 与 target")
        for field in self.text_fields:
            if not isinstance(field, str) or not field.strip():
                raise ValueError("text_fields 只能包含非空字符串")
        if not self.rules and not self.extra_rules:
            raise ValueError("rules 与 extra_rules 不能同时为空")
        return self
