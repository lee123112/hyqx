"""算子14 数据契约：严格校验语言方向、模型后端、阈值与规则证据项；未知字段一律拒绝。"""
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

    # 任务声明的语言方向；记录自带的 source_lang/target_lang 必须与之一致
    source_lang: str = "zh"
    target_lang: str = "en"
    # 上游默认产物文件名（阶段内实际路径由 /predict 的 inputFile 指定）
    input_file_name: str = "13_graded.jsonl"

    # 输入准入：S/A/C 全收（或人工确认合格的记录）。C 级由算子17 的门禁处置，
    # 本算子只判语义等价，不越权删记录（规范 13 号行：C 级留在 13_graded.jsonl）。
    allowed_grades: List[str] = Field(default_factory=lambda: ["S", "A", "C"])
    grade_fields: List[str] = Field(default_factory=lambda: ["grade", "quality_grade"])
    allow_manual_confirmed: bool = True
    manual_confirmed_fields: List[str] = Field(
        default_factory=lambda: ["manual_confirmed", "human_confirmed"])
    # 缺少等级字段时是否放行（默认拦截，交付节点默认可拒收）
    allow_missing_grade: bool = False

    # 语义等价阈值：只对真实 semantic_score 生效；降级记录不参与该判定
    threshold: float = Field(default=0.65, ge=0.0, le=1.0)

    # 短单位豁免（2026-11-02）：判定单元源文去空白后不超过 short_unit_max_chars 时，
    # 嵌入相似度不按主阈值硬判，改用 short_unit_threshold（更宽松）。
    # 依据：标题式短单位在父句级判定下分数偏低（全链路 20 字以内父句拦截率 14.9%，
    # 长句 3.1%，分数与句长 r=0.26），人工标注的 8 条误拒里 6 条是这类单位。
    # 0 表示关闭豁免，回到只按 threshold 判定的旧行为。
    short_unit_max_chars: int = Field(default=20, ge=0)
    short_unit_threshold: float = Field(default=0.50, ge=0.0, le=1.0)

    # 判定单元：parent=同父句的子句拼成整句打一次分、整句判定（默认，与算子09 同模型同句对）；
    # clause=逐子句判定（旧行为）。取不到父句标识时自动退回 clause。
    scoring_unit: Literal["parent", "clause"] = "parent"
    # 父句标识来源（metadata 字段名），算子10 写作 parent_pair_id
    parent_fields: List[str] = Field(default_factory=lambda: ["parent_pair_id"])

    # 清单指定本地 Qwen3-Embedding-0.6B（1024 维）；权重与网络下载不是测试前提
    model_name: str = "Qwen3-Embedding-0.6B"
    model_path: Optional[str] = None
    model_service_url: Optional[str] = None
    model_timeout_seconds: float = Field(default=30.0, gt=0)
    # auto=优先模型；model=要求模型；rule_degraded=显式规则后端
    # 默认 rule_degraded：离线与无权重环境确定性运行且不伪造分数；需要模型时显式改 auto/model
    model_backend: Literal["auto", "model", "rule_degraded"] = "rule_degraded"
    # 模型不可用时：rule_degraded 降级并标记 degraded；fail 结构化失败
    fallback_policy: Literal["rule_degraded", "fail"] = "rule_degraded"
    device: Literal["cpu", "cuda", "mps", "auto"] = "cpu"
    batch_size: int = Field(default=32, ge=1)
    # 单条记录模型错误写入异常清单后继续；累计超过上限即整次执行结构化失败
    max_model_errors: int = Field(default=10, ge=1)

    # 降级记录去向：waiting_quality 写入通过文件并标记待复核；reject 直接进异常清单
    degraded_policy: Literal["waiting_quality", "reject"] = "waiting_quality"

    # 非等价记录去向（2026-11-05）：mark_and_keep=留在输出流里打标记（默认），
    # drop=只写异常清单、从输出流删除（2026-11-05 之前的旧行为）。
    # 规范 4.14 只授予本算子「判语义等价」的职责，放行/拒付的最终决定权在算子 17 的
    # quality_status / delivery_eligible 门禁；物理删除会让下游 15/16/17 看不到这条缺陷，
    # 属于闭环缺口（基准实测 70 条缺陷段落里 61 条被本算子删掉，16 号只能评到 9 条）。
    non_equivalent_policy: Literal["mark_and_keep", "drop"] = "mark_and_keep"

    # 规则证据：长度比合理区间与数字一致性（只做真实观测，不拼成分数）
    length_ratio_min: float = Field(default=0.4, gt=0.0)
    length_ratio_max: float = Field(default=2.5, gt=0.0)
    number_check: bool = True
    # 数字守卫（2026-11-03）：源文有、译文缺失且位数 >= number_guard_min_digits 的数字直接拦截。
    # 依据：嵌入分对"改数字"这类局部错误几乎不敏感（负例包 12 条改数字全部漏过），
    # 而规则证据 12/12 全对；1–2 位小数字只在标签里留痕，不拦（正确译文有一对多写法）。
    number_guard: bool = True
    number_guard_min_digits: int = Field(default=3, ge=1)
    # 单位/币种守卫（2026-11-04）：数字相同但单位或币种不同即拦截
    # （3 年 → 3 months、5000 美元 → 5000 euros）。数字不同仍由 number_guard 负责。
    unit_currency_guard: bool = True
    # 否定守卫（2026-11-04）：译文出现否定、原文没有否定即拦截（zh→en 单方向）。
    # 依据：嵌入分对否定翻转不敏感（defect-14-025 三段 0.7194–0.7854 全部漏过），
    # 而该方向的规则在 876 个父句单元上只误报 6 条（0.68%）。
    # 反方向（原文有否定、译文没有）自然语料误报 17.5%，不判。
    negation_guard: bool = True
    # 译文去空白后短于该字符数不判（「Yes. / No.」这类短答句）；0 表示不设下限
    negation_guard_min_chars: int = Field(default=12, ge=0)
    # 单位换算的相对容差：1 year == 12 months == 4 quarters 精确相等；
    # 2% 只用于吸收非严格换算（如 1 year 与 365 days 的写法差异）。
    unit_guard_relative_tolerance: float = Field(default=0.02, ge=0.0, le=0.5)
    # 不完整译文守卫（2026-11-05）：原文以句末标点收尾、译文却在中途断开即拦截。
    # 依据：clause_drop 15 条里 14 条嵌入分高于阈值（前半句译对，整句余弦被撑住），
    # 而自然语料 1554 条入口句对里「原文完整、译文无收尾」只有 23 条（1.7%）。
    incomplete_target_guard: bool = True
    # 原文去空白后短于该字符数不判（标题/标签类短单位本来常不带句号）
    incomplete_target_min_chars: int = Field(default=10, ge=0)
    # 悬空虚词收尾（2026-11-05）：译文以 and / of / the 这类虚词断在句尾即判不完整，
    # 不看原文是否带句末标点（defect-14-026:p9 的原文以「[待补]：」收尾，旧口径看不见）。
    # 自然语料 543 + 399 个判定单元新增拦截 0 / 0 条（开/关本项对比）。
    incomplete_target_dangling_tail: bool = True
    # 语义判别器（P2，2026-11-05）：本地 Qwen3.5-2B 判「译文里有没有原文没有的专名/实体」。
    # 只在语义分与全部规则守卫都放行后才调用；权重缺失或推理失败时不产生规则，
    # 结果写进 metadata 的 semantic_judge_status，绝不用默认值代替判别结果。
    semantic_judge: bool = False
    semantic_judge_model_path: Optional[str] = None
    # 译文去空白后短于该字符数不判（短答句里的名字无从核对）
    semantic_judge_min_chars: int = Field(default=12, ge=0)
    # 只校验记录声明语言与配置方向是否一致；真实语言检测仅作为规则证据
    language_check: bool = True

    # 本节点要求严格 UTF-8（上游已完成编码归一）
    input_encodings: List[str] = Field(default_factory=lambda: ["utf-8"])
    text_fields: List[str] = Field(default_factory=lambda: ["source", "target"])

    @model_validator(mode="after")
    def _check_consistency(self):
        if self.source_lang == self.target_lang:
            raise ValueError("source_lang 与 target_lang 不能相同")
        if self.length_ratio_min >= self.length_ratio_max:
            raise ValueError("length_ratio_min 必须小于 length_ratio_max")
        if not self.allowed_grades:
            raise ValueError("allowed_grades 不能为空")
        if self.scoring_unit == "parent" and not self.parent_fields:
            raise ValueError("scoring_unit=parent 时 parent_fields 不能为空")
        # 短单位阈值只能比主阈值宽松；比主阈值严会让短单位更容易被拦，与豁免目的相反
        if self.short_unit_max_chars > 0 and self.short_unit_threshold > self.threshold:
            raise ValueError("short_unit_threshold 不能大于 threshold")
        # 数字守卫依赖 number_check 的证据块；关掉 number_check 就等于关掉守卫
        if self.number_guard and not self.number_check:
            raise ValueError("number_guard 需要 number_check=true")
        if self.text_fields != ["source", "target"]:
            raise ValueError("本节点只按 source/target 计算语义等价，text_fields 必须为 [source, target]")
        return self
