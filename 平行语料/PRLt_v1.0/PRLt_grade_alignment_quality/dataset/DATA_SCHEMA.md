# 算子 13 数据 Schema（S/A/C 对齐质量分级）

## 输入：已评分句对（默认 `12_scored.jsonl`）

上游算子 12 的通过集，逐行一条已融合置信度的对齐记录。阶段内实际路径由 `/predict` 的
`inputFile` 指定（取上一中间算子返回的 `data.outputFile`）；默认文件名仅用于
CLI 与本地调试。

```json
{
  "id": "pair-0001-clause0001",
  "pair_id": "pair-0001",
  "source": "数据清洗很重要。",
  "target": "Data cleaning is important.",
  "source_lang": "zh",
  "target_lang": "en",
  "component_scores": {"clause_phrase": {"value": 0.95, "source_operator": "clause_phrase_alignment", "source_record_id": "pair-0001"}},
  "alignment_confidence": 0.95,
  "confidence_version": "v1.0.0",
  "review_status": "auto_pass",
  "metadata": {"granularity": "clause_or_phrase", "backend": "rule_degraded", "alignment_confidence": 0.95}
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` / `pair_id` | string | 记录标识；仅用于异常清单定位，不参与分级 |
| `source` / `target` | string | 源/目标文本，必填且非空（本节点不改写文本） |
| `source_lang` / `target_lang` | string | 声明语言；`language_check=true` 时若存在且与配置方向不一致即拒绝 |
| `alignment_confidence` | float | **首选**分数来源；`0<=N<=1` 按 0-1 比例乘 100，`1<N<=100` 视为百分制 |
| `confidence` | float | `alignment_confidence` 缺失时的回退字段（`confidence_fields` 可配） |
| `quality_score` | float | **可选**；已存在的 0-100 质量分，若存在必须与置信度换算值一致，否则 `score_conflict` |
| `component_scores` | object | 上游分项证据，**原样保留**，本节点不重算、不覆盖 |
| `metadata.*` | object | 上游元数据；本节点只**追加**分级字段，除 `review_status` 转存外不覆盖既有键 |

分数解析优先级（顶层优先，`read_metadata_fallback=true` 时 metadata 兜底）：

1. 置信度：`alignment_confidence` → `confidence`；
2. 已声明质量分：`quality_score`。

字段缺失、类型错误、NaN/Inf、越界或两分不一致都会进入异常清单，**不补分、不 clamp**。

## 分级规则

内部统一使用 0-100 的 `quality_score`；等级边界以 `DEVELOPMENT_PLAN.md` 的「验收」一行为准：

| 等级 | 区间 | 默认复核状态 | 可交付 |
| --- | --- | --- | --- |
| `S` | `90 < N <= 100` | `accepted_for_quality_check` | 是 |
| `A` | `80 <= N <= 90` | `accepted_for_quality_check` | 是 |
| `C` | `0 <= N < 80` | `waiting_manual` | 否（交付节点拦截） |

`N > 100` 视为越界（`score_out_of_range`）。等级判定不使用 `round`，`79.999` 与 `80.0`
严格落在不同等级。边界规则串写入每条记录 `grade_boundary_rule`：
`S:(90,100];A:[80,90];C:[0,80)`。

> 口径冲突：计划 §2 正文写「A 为 80<N<=90、C 为 N<=80」，与验收行「80 分为 A」冲突。
> 本实现取验收口径（`A` 的下界闭区间），已做成配置项 `grade_s_threshold` / `grade_a_threshold`。

## 输出：`13_graded.jsonl`（通过集，逐行一条已分级记录）

原始字段全部保留，追加以下顶层字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `quality_score` | float | 0-100 质量分（`alignment_confidence` 换算值或已有 `quality_score`） |
| `quality_grade` | string | 等级标签，默认 `S`/`A`/`C`（`grade_labels` 可改名） |
| `grade_rule_version` | string | 分级规则版本（默认 `v1.0.0`） |
| `review_status` | string | 等级对应的复核状态：`accepted_for_quality_check` / `waiting_manual` |
| `grade_boundary_rule` | string | 生效的边界规则串，用于结果可解释与回溯 |
| `score_source` | string | 实际使用的字段：`alignment_confidence` / `confidence` / `quality_score` / `both` |
| `score_scale` | string | 量纲：`ratio_0_1`（发生过 ×100 换算）或 `percent_0_100` |
| `raw_confidence` | float\|null | 原始置信度原值（未换算） |
| `raw_quality_score` | float\|null | 原始已声明质量分原值 |
| `score_conversion_version` | string | 量纲换算口径版本（默认 `v1.0.0`） |
| `delivery_eligible` | bool | 是否允许交付节点消费（S/A 为 true，C 为 false） |

`metadata` 中同步镜像：`quality_score`、`quality_grade`、`grade_rule_version`、
`grade_boundary_rule`、`review_status`、`score_source`、`score_scale`、
`score_conversion_applied`、`delivery_eligible`；上游 `review_status` 与等级状态不同时，
转存到 `metadata.upstream_review_status` 保留证据（不覆盖真实值）。

## 异常输出：`13_rejects.jsonl`

```json
{
  "line": 7,
  "id": "pair-0007-clause0001",
  "pair_id": "pair-0007",
  "reasons": ["score_missing"],
  "record": {"...": "原始记录（损坏行时为 null）"}
}
```

`reasons` 取值：

`invalid_json`、`invalid_record`、`invalid_type:metadata`、`missing_field:source`、
`missing_field:target`、`invalid_type:source`、`invalid_type:target`、`empty_source`、
`empty_target`、`language_declaration_mismatch`、`invalid_type:alignment_confidence`、
`invalid_type:confidence`、`invalid_type:quality_score`、`score_not_finite`、
`score_out_of_range`、`score_conflict`、`score_missing`。

## 与算子 20（人工抽检复核）的回流字段

本节点**不伪造人工结果**。C 级记录写入 `review_status=waiting_manual` 且
`delivery_eligible=false`，工程侧/算子 20 复核后按以下字段回写（本节点只读不写）：

| 回流字段 | 取值 | 说明 |
| --- | --- | --- |
| `manual_review_status` | `accepted` / `rejected` / `pending` | 人工复核结论 |
| `quality_grade` | `S` / `A` / `C` | 复核后可上调/下调等级 |
| `quality_score` | float | 复核后的最终质量分（如需） |
| `review_status` | `accepted_for_quality_check` / `rejected_by_manual` | 更新后的流转状态 |
| `reviewer` / `reviewed_at` / `review_comment` | string | 留痕 |

本节点一旦发现 `manual_review_status` 存在，只做记录并在统计中计数，不重新分级、不覆盖。

## 阶段产物：`artifacts/stage_13_<uniqueId>.zip`

【无血缘改造 2026-10-19】原 `lineage.json`（JSON 数组，按执行顺序追加，含 operator /
operator_version / uniqueId / inputFile / outputFile / status / startedAt / finishedAt /
config / statistics）已停用，任务目录不再产出任何血缘文件。阶段内中间路径通过 `/predict`
返回的 `data.outputFile` 传递；阶段结束前把主输出、异常清单与 `run.log` 打包为
`artifacts/stage_13_<uniqueId>.zip`，响应返回 `stageZip` 与 `storageUri`。

### `statistics` 字段（保留在处理器统计结果与 `run.log` 中）

| 字段 | 说明 |
| --- | --- |
| `input_records` | 处理的非空输入行数（含非法行） |
| `graded` | `13_graded.jsonl` 实际行数 |
| `rejected_records` | `13_rejects.jsonl` 实际行数 |
| `S_count` / `A_count` / `C_count` | 各等级记录数，三者之和等于 `graded` |
| `waiting_manual_count` | 进入 `waiting_manual` 的记录数（默认等于 `C_count`） |
| `accepted_for_quality_check_count` | 进入质量检测通过集的记录数（默认 S+A） |
| `score_conversion_applied_count` | 发生 `0-1 => 0-100` 换算的记录数 |
| `min_quality_score` / `max_quality_score` | 通过集质量分实际最小/最大值 |
| `score_source:<来源>` | 各分数来源的记录数 |
| `reason:<原因>` | 各异常原因计数 |
| `encoding` | 生效输入编码 |
| `grade_rule_version` / `grade_boundary_rule` / `score_scale` / `score_conversion_version` | 生效口径 |

状态优先级：`failed`（配置/编码/空输入/路径冲突）→ `partial`（存在异常条目）→
`waiting_manual`（无异常但存在 C 级）→ `success`。
`graded + rejected_records == input_records`。

## 编码约定

- 全链路严格 UTF-8：输入按 `input_encodings`（默认仅 `utf-8`）增量严格解码，失败即 `failed`。
- 输出先写 `*.jsonl.tmp` 再 `os.replace` 原子替换；输出（通过集、异常清单、阶段产物与日志）
  统一 UTF-8，逐行流式写入，不使用本地默认编码。

## 配置

```yaml
source_lang: zh
target_lang: en
language_check: true
input_file_name: 12_scored.jsonl
confidence_fields: [alignment_confidence, confidence]
declared_score_fields: [quality_score]
read_metadata_fallback: true
score_scale: auto              # auto | ratio_0_1 | percent_0_100
score_consistency_tolerance: 0.01
grade_s_threshold: 90.0
grade_a_threshold: 80.0
grade_labels: {S: S, A: A, C: C}
grade_status_map: {S: accepted_for_quality_check, A: accepted_for_quality_check, C: waiting_manual}
delivery_eligible_grades: [S, A]
grade_rule_version: v1.0.0
score_conversion_version: v1.0.0
input_encodings: [utf-8]
```
