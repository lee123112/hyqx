# 算子 12 数据 Schema（双语对齐置信度）

## 输入：子句/短语对齐结果（默认 `11_clause_pairs.jsonl`）

上游算子 11 的通过集，逐行一条子句/短语对齐关系。阶段内实际路径由 `/predict` 的
`inputFile` 指定（取上一中间算子返回的 `data.outputFile`）；默认文件名仅用于
CLI 与本地调试。

```json
{
  "id": "pair-0001-clause0001",
  "source": "数据清洗很重要。",
  "target": "Data cleaning is important.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {
    "granularity": "clause_or_phrase",
    "confidence": 0.911,
    "parent_confidence": 0.92,
    "paragraph_confidence": 0.85,
    "source_ref": "para-001-sent-001-clause-001",
    "target_ref": "para-001-sent-001-clause-001",
    "parent_pair_id": "pair-0001",
    "parent_source_ref": "para-001-sent-001",
    "candidates": []
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` / `pair_id` | string | 记录标识；`pair_id_fields` 决定取值优先级（默认 `pair_id` → `id`） |
| `source` / `target` | string | 源/目标子句或短语，必填且非空 |
| `source_lang` / `target_lang` | string | 声明语言；`language_check=true` 时若存在且与配置方向不一致即拒绝 |
| `metadata.granularity` | string | 粒度，默认 `clause_or_phrase`，原样保留 |
| `metadata.confidence` | float \| null | 子句/短语粒度置信度（`clause_phrase` 分项，最高优先级字段） |
| `metadata.parent_confidence` | float \| null | 父句（句子级）置信度（`sentence` 分项） |
| `metadata.paragraph_confidence` | float \| null | 段落级置信度（`paragraph` 分项） |
| `metadata.confidence_by_granularity` | object \| null | 显式粒度→分数映射，优先级高于单字段 |
| `metadata.component_scores` | object \| null | 显式分项结构，元素可为数值或 `{"value":..,"source_operator":..,"source_record_id":..}` |
| `metadata.source_ref` / `target_ref` | string | 对齐引用；`require_refs=true` 时缺失即 `schema_error` |
| `metadata.candidates` | array \| null | 多候选：每项可含自己的 `confidence`/`component_scores`/`source_ref`/`target_ref` |

分项读取优先级（存在且非 `null` 即采用）：

- `clause_phrase`：`metadata.confidence_by_granularity.clause_phrase` →
  `metadata.component_scores.clause_phrase` → `metadata.clause_phrase_confidence` →
  `metadata.confidence` → `confidence`
- `sentence`：`metadata.confidence_by_granularity.sentence` →
  `metadata.component_scores.sentence` → `metadata.sentence_confidence` →
  `metadata.parent_confidence` → `parent_confidence`
- `paragraph`：`metadata.confidence_by_granularity.paragraph` →
  `metadata.component_scores.paragraph` → `metadata.paragraph_confidence` → `paragraph_confidence`

非法输入不中断流程，按行写入异常清单：`invalid_json`、`invalid_record`、`invalid_type:metadata`、
`missing_field:<字段>`、`invalid_type:<字段>`、`empty_source`、`empty_target`、
`language_declaration_mismatch`、`schema_error:missing_source_ref`、
`schema_error:missing_target_ref`、`invalid_type:<粒度>`、`score_out_of_range:<粒度>`、
`score_missing`。

## 输出：`12_scored.jsonl`（通过集，逐行一条已评分记录）

原始字段全部保留，追加以下顶层字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `pair_id` | string | 记录标识（`pair_id` → `id` → `parent_pair_id`） |
| `granularity` | string | 输入粒度，默认 `clause_or_phrase` |
| `source_ref` / `target_ref` | string | 对齐引用 |
| `component_scores` | object | 每个参与分项：`{value, source_operator, source_record_id, source_field}`，可追溯到上游算子与记录 id |
| `confidence_components` | object | 与 `component_scores` 同内容（`DEVELOPMENT_PLAN.md` 命名别名） |
| `weights` | object | 配置中的完整权重 |
| `used_weights` | object | 实际参与归一后的权重（缺失分项剔除后重新归一） |
| `alignment_confidence` | float | 加权融合分 `Σ(value_i × used_weight_i)`，0–1 |
| `confidence_version` | string | 置信度口径版本（默认 `v1.0.0`） |
| `conflict_candidates` | array | 多候选及其融合分：`{index, confidence, source_ref, target_ref}`；无候选为 `[]` |
| `review_status` | string | `auto_pass`（≥ review_threshold）/ `needs_review` / `waiting_manual`（并列冲突） |

`metadata` 中同步镜像：`alignment_confidence`、`confidence_version`、`review_status`、
`low_confidence`、`evidence_incomplete`、`component_scores`、`used_weights`。

## 异常输出：`12_rejects.jsonl`

硬异常（不可评分）与低置信/冲突审计条目逐行列出：

```json
{
  "line": 3,
  "id": "pair-0003-clause0001",
  "pair_id": "pair-0003",
  "source_ref": "para-003-sent-001-clause-001",
  "target_ref": "para-003-sent-001-clause-001",
  "reasons": ["low_confidence"],
  "confidence": 0.633333,
  "component_scores": {"...": "..."},
  "review_status": "needs_review"
}
```

`reasons` 取值：`invalid_json`、`invalid_record`、`invalid_type:metadata`、`missing_field:<字段>`、
`invalid_type:<字段>`、`empty_source`、`empty_target`、`language_declaration_mismatch`、
`schema_error:missing_source_ref`、`schema_error:missing_target_ref`、`score_missing`、
`invalid_type:<粒度>`、`score_out_of_range:<粒度>`、`low_confidence`、`conflict_multi_candidate`。

- 低置信与并列冲突默认 `low_confidence_policy=mark_and_keep`：通过集保留，同时写审计条目；
  改为 `move_to_rejects` 后仅写异常清单并从通过集移除。
- 硬异常（JSON/类型/缺字段/缺分/越界）永远只进异常清单。

## 阶段产物：`artifacts/stage_12_<uniqueId>.zip`

【无血缘改造 2026-10-19】原 `lineage.json`（JSON 数组，按执行顺序追加，含 operator /
operator_version / uniqueId / inputFile / outputFile / status / startedAt / finishedAt /
config / statistics）已停用，任务目录不再产出任何血缘文件。阶段内中间路径通过 `/predict`
返回的 `data.outputFile` 传递；阶段结束前把主输出、异常清单与 `run.log` 打包为
`artifacts/stage_12_<uniqueId>.zip`，响应返回 `stageZip` 与 `storageUri`。

### `statistics` 字段（保留在处理器统计结果与 `run.log` 中）

| 字段 | 说明 |
| --- | --- |
| `input_records` | 处理的非空输入行数（含非法行） |
| `scored` | `12_scored.jsonl` 实际行数 |
| `rejected` | `12_rejects.jsonl` 实际行数 |
| `hard_rejected` | 不可评分（JSON/类型/缺字段/缺分/越界）的行数 |
| `low_confidence` | 低置信记录数（`alignment_confidence < low_confidence_threshold`） |
| `waiting_manual` | 并列冲突进入待人工的记录数 |
| `conflicts` | 多候选并列冲突数 |
| `moved_to_rejects` | `move_to_rejects` 策略下被移出通过集的低置信记录数 |
| `evidence_incomplete` | 存在启用粒度分项缺失的记录数 |
| `component_available:<粒度>` | 各分项成功读取的记录数 |
| `reason:<原因>` | 各异常/审计原因计数 |
| `encoding` / `confidence_version` / `weights` / `enabled_granularities` | 生效编码、版本、权重与粒度 |
| `low_confidence_threshold` / `review_threshold` | 生效阈值 |
| `evidence_index_sentence` / `evidence_index_paragraph` | 旁路证据索引条目数（配置时） |

状态口径：`failed`（配置/编码/空输入）→ `partial`（有硬异常或被移出的低置信）→
`degraded`（无硬异常但存在低置信/冲突/证据不全）→ `success`（无上述情况）。
`scored + hard_rejected + moved_to_rejects == input_records`。

## 编码约定

- 全链路严格 UTF-8：输入按 `input_encodings`（默认仅 `utf-8`）增量严格解码，失败即 `failed`。
- 输出（通过集、异常清单、阶段产物与日志）统一 UTF-8，逐行流式写入，不使用本地默认编码。

## 配置

```yaml
source_lang: zh
target_lang: en
input_file_name: 11_clause_pairs.jsonl
pair_id_fields: [pair_id, id]
require_refs: true
language_check: true
enabled_granularities: [clause_phrase, sentence, paragraph]
component_weights: {clause_phrase: 0.6, sentence: 0.3, paragraph: 0.1}
low_confidence_threshold: 0.7
review_threshold: 0.9
ambiguity_margin: 0.005
low_confidence_policy: mark_and_keep   # mark_and_keep | move_to_rejects
confidence_version: v1.0.0
sentence_evidence_file: null
paragraph_evidence_file: null
input_encodings: [utf-8]
```
