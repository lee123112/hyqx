# 算子15 译文流畅度检测 数据契约

> 适用范围：平行语料 demo 流水线「双语质量检测过滤」阶段的节点 15。
> 上游：算子 14 语义等价性检测（默认 `14_semantic_checked.jsonl`）。
> 下游：算子 16/17/18（领域识别、实体一致性、数据集打包）。

## 1. 输入

JSONL，每行一个 dict。本节点只读取 `target`，**不改写 source/target 原文**。

```json
{
  "id": "task_file-000001",
  "source": "数据清洗很重要。",
  "target": "Data cleaning is important.",
  "source_lang": "zh",
  "target_lang": "en",
  "semantic_status": "passed",
  "semantic_score": 0.94,
  "metadata": {"granularity": "sentence", "confidence": 0.93}
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 否（建议） | 记录标识 |
| `target` | **是** | 待检测译文；缺字段/非字符串/去空白后为空 → 硬异常 |
| `target_lang` | 否 | 声明目标语言；存在且与配置 `target_lang` 不一致 → 硬异常 |
| `semantic_status` / `semantic_score` | 否 | 上游语义质控状态，只透传不改写 |
| `metadata` | 否 | 对象；非对象 → 硬异常。原有键全部保留 |

编码：严格 UTF-8（上游算子 3/4 已完成编码归一）。

## 2. 输出

### 2.1 `15_fluency_checked.jsonl`

上游字段原样保留，并追加（同时写入顶层与 `metadata`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `fluency_score` | number \| null | `scoring_mode=defect_count_v1` 时为由**已核验缺陷**折算的 0–100 展示分（见 2.5）；降级/模型错误时为 `null` |
| `fluent` | bool \| null | `score >= threshold`；无分数时为 `null` |
| `fluency_status` | string | `accepted` / `low_fluency` / `waiting_calibration` / `model_unavailable` / `waiting_manual` / `model_error` |
| `fluency_score_semantics` | string \| null | 分数语义；当前为 `defect_count_v1`（规则计数映射值，**不是概率**）；正式门禁要求已校准概率百分制 |
| `fluency_business_gate_approved` | bool \| null | 当前校准器是否获业务门禁批准 |
| `fluency_backend` | string | `model` / `rule_degraded` |
| `model_version` | string \| null | 模型返回的版本；降级时为 `null` |
| `grammar_errors` | array | 模型提出的原始错误片段（未核验）；降级时为 `[]` |
| `fluency_evidence` | array | 证据列表，每项 `{type, detail}`；模型证据 + 规则证据 |
| `fluency_threshold` | number | 本次生效阈值快照 |
| `fluency_checked_at` | string | 检测时间（+08:00） |

### 2.1.1 `defect_count_v1` 附加字段

`scoring_mode=defect_count_v1`（默认）时额外输出：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `fluency_scoring_mode` | string | 本条实际生效的口径：`defect_count_v1` / `rubric_v2` |
| `fluency_defects` | array | **已核验**缺陷：`{type, span, fix, severity, source}`；`source` 为 `model` / `rule` |
| `fluency_suspects` | array | 未通过核验的主张（带 `reason`），**不计分**，供人工复核 |
| `fluency_defect_counts` | object | 按类型的已核验缺陷计数，如 `{"dup_word": 1}` |
| `fluency_defect_rate_per_100_tokens` | number | 每 100 词加权错误数 —— 下游应优先用这个，而不是 `fluency_score` |
| `fluency_tokens` | int | 译文词数 |
| `fluency_token_denominator` | int | 实际计分分母 `max(词数, token_floor)` |
| `fluency_verified_ratio` | number | 已核验主张 / 全部主张 |
| `fluency_low_confidence_length` | bool | 词数低于 `low_confidence_length_tokens`，长度不足以判断 |

`suspect.reason` 取值：`span_not_found`（定位不在原文）、`non_minimal_edit`（fix 缺失、
与 span 相同或改动过大）、`type_not_scored`（类型不在 `scored_defect_types`）、
`mechanical_check_failed`（机械校验未通过）。

规则证据类型（只描述问题，**不含任何分数**）：
`empty_text`、`invalid_text`、`invisible_characters`、`replacement_character`、
`mojibake_pattern`、`untranslated_source_script`、`abnormal_punctuation`、
`unbalanced_brackets`、`unbalanced_quotes`、`repeated_token`、`suspiciously_short`。

### 2.2 `15_rejects.jsonl`

两类条目共用同一文件：

- 硬异常：`invalid_json`、`invalid_record`、`missing_field:target`、`invalid_type:target`、
  `empty_target`、`invalid_type:metadata`、`language_declaration_mismatch`；
- 审计条目：非 `accepted` 记录（`low_fluency` / `waiting_calibration` / `model_unavailable` / `waiting_manual` /
  `model_error`），与 `low_fluency_policy` / `degraded_policy` 无关，**必经写入**。

条目结构：

```json
{
  "line": 7,
  "id": "task_file-000007",
  "target_lang": "en",
  "target_excerpt": "This result is very good!!! Really!!!",
  "reasons": ["low_fluency"],
  "fluency_score": 41.5,
  "fluency_status": "low_fluency",
  "fluency_backend": "model"
}
```

`target_excerpt` 最多 40 字符（超出以 `…` 截断），不落盘完整原文。

### 2.3 `artifacts/stage_15_<uniqueId>.zip`（阶段产物）

【无血缘改造 2026-10-19】原 `lineage.json`（JSON 数组，按执行顺序追加，含
`operator`、`operator_version`、`uniqueId`、`inputFile`、`outputFile`、`status`、
`startedAt`、`finishedAt`、`config`、`statistics`）已停用，任务目录不再产出任何血缘文件。
阶段内中间路径通过 `/predict` 返回的 `data.outputFile` 传递；阶段结束前把主输出、
异常清单与 `run.log` 打包为 `artifacts/stage_15_<uniqueId>.zip`，响应返回 `stageZip` 与
`storageUri`。

`statistics` 关键字段（保留在处理器统计结果与 `run.log` 中）：
`input_records`、`passed_records`（= `15_fluency_checked.jsonl` 行数）、
`rejected_records`（= `15_rejects.jsonl` 行数）、`hard_rejected_records`、
`moved_to_rejects`、`scored_records`、`accepted_records`、`low_fluency_records`、
`degraded_records`、`model_unavailable_records`、`waiting_manual_records`、
`model_error_records`、`threshold`、`model_version`、`backend`、`encoding`、`reason:*`，
以及 `defect_count_v1` 口径下的 `scoring_mode`、`defect_verified_records`、
`defect_clean_records`、`verified_defect_total`、`suspect_defect_total`、
`defect_type_counts`。

恒等式：`input_records == passed_records + hard_rejected_records + moved_to_rejects`。

### 2.4 `run.log`

逐行 `[YYYY-MM-DD HH:mm:ss] [动作] 消息`；不输出密码、Token、完整原文与完整堆栈。

## 3. 过滤口径

| 条件 | `fluency_status` | `fluent` | 落入 `15_fluency_checked.jsonl` | 落入 `15_rejects.jsonl` |
| --- | --- | --- | --- | --- |
| `score >= threshold` | `accepted` | `true` | 是 | 否 |
| `score < threshold` | `low_fluency` | `false` | 默认是（`mark_and_keep`） | 是（审计） |
| 未校准或校准器仅供评价 | `waiting_calibration` | `null` | 是 | 是（审计） |
| 后端降级（无分数） | `model_unavailable` / `waiting_manual` | `null` | 默认是（`mark_and_keep`） | 是（审计） |
| 单条模型错误 | `model_error` | `null` | 默认是（`mark_and_keep`） | 是（审计） |
| 结构/声明语言异常 | — | — | 否 | 是（硬异常） |

`low_fluency_policy` / `degraded_policy` 设为 `move_to_rejects` 时，对应记录只写入异常清单，
并从 `15_fluency_checked.jsonl` 移除，计入 `moved_to_rejects`。

## 4. 不伪造分数的硬约束

`scoring_mode=defect_count_v1`（默认）：`fluency_score` 不是模型给的，而是
**已核验缺陷条数的确定性函数**（见 2.5）。模型只提 `{type, span, fix}` 主张，
主张是否成立由算子核验；核验不通过的只进 `fluency_suspects`，不影响分数。

`scoring_mode=rubric_v2`（历史口径，保留兼容）：`fluency_score` 直接来自模型。

两种情况共同的硬约束：

- 后端不可用或返回非法值时，`fluency_score` 必须为 `null`，状态为
  `model_unavailable` / `waiting_manual` / `model_error`；
- 规则型证据不得凭空变成模型分数（`defect_count_v1` 下规则缺陷以 `source=rule`
  显式标注，并同样受 `max_defects` 约束）；
- 不得复制上游 `semantic_score` 到流畅度字段。

## 5. 计分口径（`defect_count_v1`）

```
tokens       = 译文词数
denominator  = max(tokens, token_floor)              # 默认 token_floor=20
weight       = Σ severity_weights[severity]          # hard=1.0 / minor=0.4
rate         = weight / denominator × 100            # 每 100 词加权错误数
score        = 100 × max(0, 1 − rate / score_scale)  # 默认 score_scale=12.5
```

`score_scale=12.5` 的含义：每 100 词 12.5 个加权错误 = 0 分；60 分线 =
**每 100 词不超过 5 个加权错误**。这是先验定义，不随数据回调。

两条结构保证：

1. **0 条已核验缺陷恒等于 100 分** —— 「文本没错误却被打低分」不可能发生；
2. **未核验的主张不影响分数** —— 模型编造的错误无法压低分数。

`defect_count_v1` 不是校准概率，因此不在 `accepted_score_semantics` 白名单内，
记录一律为 `waiting_calibration`，只作辅助证据（规范 4.15）。
