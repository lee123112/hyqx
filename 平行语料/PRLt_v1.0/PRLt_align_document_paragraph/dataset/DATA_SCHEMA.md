# 算子9 数据契约（文档/段落级对齐）

## 1. 输入

### 1.1 对齐计划 `10_align_plan.json`（算子10 产物，必填）

`/predict` 的 `inputFile` 指向该文件；未传时按任务目录下 `10_align_plan.json`
查找。支持 JSON 对象或 JSONL（取首个有效对象记录）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `plan_version` | string | 否 | 计划版本，仅记录 |
| `source.path` | string | 是 | 源文档路径（相对或绝对） |
| `source.lang` | string | 否 | 源语言，默认 `zh`（只影响可比单元口径） |
| `target.path` | string | 是 | 目标文档路径 |
| `target.lang` | string | 否 | 目标语言，默认 `en` |
| `granularity` | string | 否 | 计划粒度，记录到统计 |
| `stages[]` | array | 否 | 阶段路由；给出时必须含 `operator=9` 或 `granularity=paragraph` |
| `fallback_policy` | string | 否 | `rule_degraded`（默认）/ `fail`；`fail` 且无语义模型时结构化失败 |
| `window` | int | 否 | 计划侧窗口，本算子以配置 `candidate_window` 为准 |

### 1.2 源/目标双语文本（必填）

UTF-8 纯文本，`\n\n`（允许空行内空格/制表符）分隔段落。段落首尾空白裁剪，
空段落直接丢弃并计入 `empty_paragraphs_skipped`。文件必须严格按
`input_encodings` 解码，解码失败即结构化失败（不做编码猜测）。

### 1.3 路径口径

- 路径允许根：任务目录 `OUTPUT_DIR/<uniqueId>`、其 `input/` 子目录、计划文件
  所在目录（再叠加 `/predict` 从 `inputFile` 推导的目录）。
- 默认 `allow_external_document_paths=false`：绝对路径也必须位于上述允许根内，
  否则返回 `path_out_of_scope`；相对路径越界同样返回 `path_out_of_scope`。

## 2. 输出

### 2.1 `09_paragraph_pairs.jsonl`（通过输出，流式）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | `para-p<源起始>-t<目标起始>`，如 `para-p0003-t0002` |
| `source` | string | 源段落文本；多段合并时以 `\n\n` 连接 |
| `target` | string | 目标段落文本；多段合并时以 `\n\n` 连接 |
| `source_lang` / `target_lang` | string | 语言标记 |
| `metadata.granularity` | string | 固定 `paragraph` |
| `metadata.source_ref` / `target_ref` | string | 段落引用，多段用 `+` 连接 |
| `metadata.source_refs` / `target_refs` | array | 段落引用数组，可回指原文段落 |
| `metadata.relation` | string | `1:1` / `1:2` / `2:1` |
| `metadata.confidence` | float | 加权总分（多段关系已扣 `group_penalty`），[0,1] |
| `metadata.scores` | object | `length` / `position` / `overlap`，`overlap` 不可比时为 `null` |
| `metadata.score_breakdown` | object | `weighted`（各分项加权值）、`weights`（实际权重）、`penalty` |
| `metadata.length_evidence` | object | `unit`（`cjk`/`word`）、`source_chars`、`target_chars`、`source_units`、`target_units` |
| `metadata.backend` | string | 本次固定 `rule_degraded`（未使用任何语义模型分数） |
| `metadata.low_confidence_reason` | string\|null | 通过输出中为 `null`，被标记多候选时为 `conflict_multi_candidate` |
| `metadata.ambiguity` | bool | 是否存在同样合理的第二优对齐方案 |
| `metadata.ambiguity_evaluated` | bool\|null | 是否做了第二优对齐评估（规模过大时为 `false`） |
| `metadata.ambiguity_gap` | float\|null | 最优与第二优对齐的总分差 |
| `metadata.ambiguity_runner_up` | object\|null | 竞争关系的引用、关系类型与置信度 |

### 2.2 `09_paragraph_rejects.jsonl`（异常清单，流式，逐行独立）

| `reason` | 含义 | 关键字段 |
| --- | --- | --- |
| `low_confidence` | 置信度低于 `min_confidence`，未写入通过输出 | `source_refs`、`target_refs`、`relation`、`confidence`、`scores`、`length_evidence`、`text_preview` |
| `conflict_multi_candidate` | 存在接近的第二优对齐方案（关系仍通过） | `source_refs`、`target_refs`、`confidence`、`ambiguity_gap`、`runner_up` |
| `unaligned_paragraph:source` / `:target` | 该段落没有通过阈值的关系 | `ref`、`text_preview` |
| `plan_not_found` / `plan_empty` / `plan_invalid_json:lineN` / `plan_not_object` | 计划缺失或损坏 | `detail` |
| `plan_missing_document_path` / `path_out_of_scope` / `document_not_found` | 计划路径缺失或越界 | `detail` |
| `stage_not_in_plan` | 计划未路由到段落级阶段 | `detail` |
| `model_unavailable` | 允不允许降级的场景下没有可用语义模型 | `detail` |
| `empty_document:source` / `:target` | 文档没有有效段落 | `detail` |
| `input_encoding_invalid` | 文档无法按配置编码解码 | `detail` |
| `alignment_error` / `output_write_error` / `plan_read_error` | 计算或 IO 异常 | `detail` |

### 2.3 `statistics` 口径

`source_paragraphs` / `target_paragraphs`（有效段落数）、
`empty_paragraphs_skipped`、`candidates_scored`（候选关系数）、`pairs` 与
`pairs_written`（通过输出行数，二者相等）、`pair_1_to_1`/`pair_1_to_2`/
`pair_2_to_1`、`low_confidence`、`multi_candidate`、`unaligned_source`、
`unaligned_target`、`rejected`（异常清单行数）、`total_score`（DP 总分）、
`reason:*`（各失败原因的条数）。

约束：每个源/目标段落恰好被归属一次——通过输出、`low_confidence` 或
`unaligned_paragraph` 三者之一，不静默丢弃。

## 3. 打分公式

- 可比单元 `units`：中文/日文/韩文（`unit=cjk`）= 汉字数 + 非汉字词数；
  其他语言（`unit=word`）= 词数。
- `length = 1 - |units_s - units_t| / max(units_s, units_t)`（中英字符数天然
  不同，因此用可比单元而非原始字符数）。
- `position = 1 - |center_s - center_t|`，`center = (起始索引 + 跨度/2) / 文档段落数`。
- `overlap` = 双方 ASCII 词/数字 token 的 Jaccard；任一侧没有可比 token 时为
  `null`，此时长度与位置权重重新归一化（不把零词汇重叠当作必然失败）。
- `confidence = 权重加权和 - penalty`（`1:2`/`2:1` 关系扣 `group_penalty`）。


