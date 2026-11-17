"""算子16 数据契约：领域集合、阈值、后端门禁与请求模型；未知配置字段一律拒绝。"""
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from utils.relevance import KeywordConfigError, parse_keywords


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
    input_file_name: str = "15_fluency_checked.jsonl"
    # 参与领域判定的字段（只读，不改写原文）
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])

    # 任务声明的语言方向；记录 language_pair 必须与之一致
    source_lang: str = "zh"
    target_lang: str = "en"
    language_check: bool = True

    # ---- 后端门禁 ----
    # auto=有模型用模型、无模型按 fallback_policy；model=必须用模型；rule_degraded=只做规则降级
    backend: Literal["auto", "model", "rule_degraded"] = "auto"
    model_service_url: Optional[str] = None
    model_name: str = "Qwen/Qwen2.5-7B-Instruct"
    model_path: Optional[str] = None
    model_version: Optional[str] = None
    model_timeout_seconds: float = Field(default=30.0, gt=0)
    model_entrypoint: str = "classify_domain"
    # 模型不可用时：rule_degraded=降级并标记 degraded；fail=结构化失败
    fallback_policy: Literal["rule_degraded", "fail"] = "rule_degraded"
    max_model_errors: int = Field(default=20, ge=0)

    # ---- 领域集合与筛选 ----
    # 目标业务范围；allow_all_domains=true 时不做范围外判定
    allowed_domains: List[str] = Field(
        default_factory=lambda: ["medical", "legal", "finance", "technology", "education"])
    allow_all_domains: bool = False
    # 默认阈值 0.80；仅对模型给出的 0–1 置信度生效（规则后端置信度为 null）
    domain_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)

    # ---- 碎片守卫（2026-11-05）----
    # 源或目标去空白后 <= fragment_min_chars 的记录不做相关性判定，直接
    # relevance_status=low_relevance、reason=fragment_too_short（进抽检台）。
    # 依据：Fullflow-Effect-Bench-v1 的 20 条 fragment 标签是「银行 / bank」这类
    # 无法判领域的两字碎片，关键词分对它们没有意义；这类记录此前在算子10 的
    # 碎片门禁就被删掉，本算子根本看不到，缺陷在链路上不可见。
    fragment_guard: bool = True
    fragment_min_chars: int = Field(default=4, ge=0)
    allow_multi_label: bool = False
    max_candidates: int = Field(default=5, ge=1)

    # ---- 规则后端词表与别名 ----
    domain_keywords: Dict[str, List[str]] = Field(default_factory=dict)
    subdomain_keywords: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)
    domain_aliases: Dict[str, str] = Field(default_factory=dict)

    # ---- 记录去向 ----
    # 低置信 / 未知领域 / 规则降级记录的处置方式
    low_confidence_policy: Literal["mark_and_keep", "move_to_rejects"] = "mark_and_keep"
    degraded_policy: Literal["mark_and_keep", "move_to_rejects"] = "mark_and_keep"

    # ---- 关键词相关性（2026-11-03，需求 30 号）----
    # keyword_relevance=按任务关键词算相关性分；legacy_domain=回滚到固定领域分类。
    relevance_mode: Literal["keyword_relevance", "legacy_domain"] = "keyword_relevance"
    # 输入框原始字符串，按「；」分隔（工程侧同时容忍 ; 与换行）；也接受 JSON 数组。
    relevance_keywords: Union[str, List[str]] = ""
    # 领域相关性阈值：score >= threshold 判为相关；默认 60%（业务阈值，非准确率）。
    relevance_threshold: float = Field(default=0.60, gt=0.0, le=1.0)
    relevance_temperature: float = Field(default=0.05, gt=0.0, le=1.0)
    relevance_aggregate: Literal["max", "top3_mean"] = "max"
    relevance_entrypoint: str = "score_relevance"
    # 低于阈值：manual_review=保留在主输出并进抽检台（默认）；reject=物理拒收；keep=仅标记放行。
    low_relevance_action: Literal["manual_review", "reject", "keep"] = "manual_review"
    # 关键词为空：manual_review=全部进抽检台（默认）；pass_all=不做相关性过滤。
    empty_keyword_policy: Literal["manual_review", "pass_all"] = "manual_review"
    max_relevance_keywords: int = Field(default=200, ge=1)
    max_keyword_length: int = Field(default=64, ge=1)

    # ---- 父句/段落聚合（2026-11-04）----
    # 子句单独打分天然偏低，会把整体相关的父句误杀。开启后同一父级分组只按父级文本打一次分，
    # 再把父级分下发给组内记录；分组键按 relevance_parent_key_fields 顺序查找。
    relevance_parent_aggregation: bool = True
    relevance_parent_key_fields: List[str] = Field(
        default_factory=lambda: ["parent_pair_id", "paragraph_id"])
    # 组内记录数达到该值才聚合；1 表示不聚合（等同于逐条打分）。
    relevance_parent_min_units: int = Field(default=2, ge=1)
    # parent=子记录直接继承父级分（默认，长文本上下文更完整）；max=父级分与自身分取大。
    relevance_parent_policy: Literal["parent", "max"] = "parent"
    # 父级聚合文本的字符上限（0=不截断）。嵌入模型有长度上限，默认 1200 足够容纳一个段落。
    relevance_parent_max_chars: int = Field(default=1200, ge=0)

    # ---- 文档主题一致性（2026-11-05）----
    # 一篇文档里如果有相当比例的单位已经判为低相关，说明这份文档本身主题混杂；
    # 剩下那些「恰好命中关键词」的单位处在跑题上下文里，模型分只看见关键词、
    # 看不见文档环境，不能单独放行。命中即标 manual_review（待复核），不物理删除。
    # 文档键取记录 id 的 "::" 前缀（<document>::<para>-...），这是全链路既有约定。
    document_consistency_guard: bool = True
    document_consistency_min_units: int = Field(default=5, ge=2)
    document_consistency_min_low_ratio: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.source_lang == self.target_lang:
            raise ValueError("source_lang 与 target_lang 不能相同")
        if not self.input_encodings:
            raise ValueError("input_encodings 不能为空")
        if not self.text_fields:
            raise ValueError("text_fields 不能为空")
        for field in self.text_fields:
            if field not in {"source", "target"}:
                raise ValueError("text_fields 只能是 source/target")
        if len(set(self.text_fields)) != len(self.text_fields):
            raise ValueError("text_fields 不能重复")
        if self.relevance_mode == "legacy_domain" \
                and not self.allow_all_domains and not self.allowed_domains:
            raise ValueError("未配置 allowed_domains 时必须显式设置 allow_all_domains=true")
        if self.relevance_mode == "keyword_relevance":
            # 关键词超限/超长必须报配置错误，绝不静默截断；空关键词按策略处理。
            try:
                self.parsed_relevance_keywords()
            except KeywordConfigError as exc:
                raise ValueError(str(exc)) from exc
        if self.backend == "model" and not (self.model_service_url or self.model_path):
            raise ValueError("backend=model 时必须提供 model_service_url 或 model_path")
        for domain, keywords in self.domain_keywords.items():
            if not isinstance(domain, str) or not domain.strip():
                raise ValueError("domain_keywords 的领域名不能为空")
            if not isinstance(keywords, list) or not keywords:
                raise ValueError("domain_keywords 的每个领域必须给出非空关键词列表")
        return self

    def expected_language_pair(self) -> str:
        return f"{self.source_lang}-{self.target_lang}"

    def parsed_relevance_keywords(self) -> List[str]:
        """按输入框契约解析关键词表（「；」分隔、去重、超限报错）。"""
        return parse_keywords(self.relevance_keywords,
                              self.max_relevance_keywords, self.max_keyword_length)

    def normalized_allowed_domains(self) -> List[str]:
        seen: List[str] = []
        for item in self.allowed_domains:
            if not isinstance(item, str):
                raise ValueError("allowed_domains 只能包含字符串")
            value = item.strip().lower()
            if value and value not in seen:
                seen.append(value)
        return seen
