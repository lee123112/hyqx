# 算子 #09 准备候选描述 数据 Schema

> 版本：v1.0.0；日期：2026-10-06；对应表1第 09 行「数据加工 / 准备候选描述」。
> 本算子不引入 `/downloadFile`，也不改动其他算子的命名空间。

## 1. 入口（输入 JSONL）

入口为 UTF-8 JSONL，每行一个对象，必须包含唯一 `sample_id`。上游 `processing`、`alignment`、
`qc`、`metadata` 及未知扩展字段原样保留，本算子只写 `description*` 与 `processing["09"]`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sample_id` | string | 是 | 样本唯一标识；缺失记入 `09_rejects.jsonl` |
| `caption` | string / null | 否 | 图注（来源优先级第 1） |
| `alt` / `alt_text` | string / null | 否 | Alt 文本（第 2） |
| `title` | string / null | 否 | 标题（第 3） |
| `subtitle` | string / array / null | 否 | 字幕（第 4）；数组元素取 `text` 并拼接 |
| `transcript` | string / object / null | 否 | 语音转写（第 5）；亦可放 `audio.transcript`、`audio.segments[].text` |
| `manual_description` | string / null | 否 | 人工补写（第 6，仅在回写后存在） |
| `image` / `video` / `audio` | string 或 `{path:...}` / null | 否 | 媒体相对路径，用于文件名/标签排除与随包携带 |
| `metadata` | object | 否 | 支持 `caption`/`alt`/`title`/`subtitle`/`transcript`/`manual_description`/`image_tags` |

同样字段也支持写在 `image.*`、`video.*`、`audio.*`、`metadata.*` 命名空间下，解析顺序见
`utils/description.py` 的 `SOURCE_FIELDS`。

**重要边界**：表1未把上游通用 `text` 字段列入描述来源。记录仅有 `text`（无论字符串或
`{"content": ...}`）时，本算子不会把它当描述，而是生成人工补写任务；这是表1口径，不是缺陷。

## 2. 描述的合法性与优先级

1. 按 `caption → alt → title → subtitle → transcript → manual` 取第一条满足条件的文本。
2. 文本 `strip()` 后长度必须 ≥ `min_length`（默认 5，范围 5–20），否则顺延下一来源并记录
   `too_short`。
3. 文本等于任一媒体文件名（含词干与扩展名）时无效，记录 `equals_filename`；等于
   `metadata.image_tags` / `metadata.tags` 中任一标签时无效，记录 `equals_tag`。
4. 超过 `max_length`（默认 500，范围 100–500）时截断到 500，并置 `description_truncated=true`。
5. 六个来源都无有效文本时，生成人工补写任务，**不进入准入、也不进入任何隔离区**。

本算子不调用图像描述模型（无 BLIP / LLaVA / OCR / CLIP），不存在模型分数或降级分数。

## 3. 主产物 `09_descriptions.jsonl`（交 #10）

每行是在上游记录上追加描述字段后的完整记录：

| 字段 | 类型 | 说明 |
|---|---|---|
| `description` | string | 有效候选描述（已 strip、必要时截断） |
| `description_source` | string | `caption`/`alt`/`title`/`subtitle`/`transcript`/`manual` |
| `description_source_label` | string | 中文标签：图注 / Alt / 标题 / 字幕 / 语音转写 / 人工补写 |
| `description_length` | int | 最终描述字符数（截断后） |
| `description_original_length` | int | 截断前字符数 |
| `description_truncated` | bool | 是否发生截断 |
| `description_word_count` | int | 词数（拉丁按单词、中文按汉字计），用于质量判定 |
| `description_quality` | string | `ok`（≥`min_words`）/ `short`（词数不足，未阻塞） |
| `description_lang` | string | 按字符脚本判定的描述语言：`zh`/`en`/`ja`/`ko`/`und` |
| `description_lang_source` | string | `detected`（脚本判定）/ `declared`（记录声明） |
| `description_alternate_index` | int | 换用同源候选图注时，该候选在 `metadata.alt_candidates` 中的下标（仅换用时出现） |
| `processing["09"]` | object | `{"status": 1, "process_status": "success", "operator", "metrics":{description_source, description_length, truncated, word_count, quality, lang, lang_source, alternate_used}}` |

[v1.0.1] `text` 为对象且 `text.lang` 与内容明显矛盾时，本算子把 `text.lang` 纠正为实际语言，
原值写入 `text.lang_declared`，并置 `text.lang_source=detected`。

## 4. 人工补写任务 `09_rewrite_tasks.jsonl`（交 #10 / 人工队列）

每行一条任务，动作是"等待回写"，不是隔离：

| 字段 | 说明 |
|---|---|
| `task_id` | `rewrite-<sample_id>` |
| `sample_id` | 样本 ID |
| `reason` | 固定 `no_valid_description` |
| `label` | 流程状态-待人工补写（**不是**质量隔离标签） |
| `action` / `destination` | `waiting_manual` / `pipeline_wait` |
| `status` | `pending` |
| `required_action` | `manual_fill_description` |
| `is_bare_image` | 是否存在图像模态但无有效文本 |
| `modalities` / `media` | 记录中实际存在的模态及相对路径 |
| `skipped_sources` | 各来源被跳过的原因（`empty`/`too_short`/`equals_filename`/`equals_tag`） |
| `source_priority` | 冻结的六来源顺序 |
| `model_used` | 恒为 `null`，明确未使用任何描述模型 |

回写流程：人工把 `manual_description` 写回记录后重跑本算子，`description_source` 记为
`manual`，该样本即可进入 #10 准入。

## 5. 拒绝清单 `09_rejects.jsonl`

只承接原始文件问题（不静默丢弃）：

```json
{"line": 13, "reason": "json_parse_error", "label": "原始文件-无法解析",
 "action": "隔离", "destination": "原始隔离区", "operator": "candidate_description",
 "event_time": "2026-10-06T12:00:00+08:00"}
```

`reason` 取值：`json_parse_error`、`record_not_object`、`missing_field: sample_id`。
这些是**格式问题**，与"无描述"的流程状态严格区分。

## 6. 统计守恒

`statistics` 字段：`total_lines`、`records`、`bad_lines`、`described`、`pending_manual`、
`missing_sample_id`、`rejected`、`truncated`、`source_counts`。

- `records = total_lines - bad_lines`
- `records = described + pending_manual + missing_sample_id`
- `total_lines = described + pending_manual + rejected`，其中 `rejected = bad_lines + missing_sample_id`

## 7. 输出文件对象 `crossmodal-files-v1`（中间/阶段末同构）

[v4.2] `isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；中间与阶段末响应同构，
均返回 `outputFile` + `result`。`entrypoint` = `09_descriptions.jsonl`；`files` 含入口、
`09_rewrite_tasks.jsonl`、`09_rejects.jsonl`、`manifest.json` 及主产物引用的全部媒体
（文本 utf-8、媒体 base64）。`manifest.json` 的 `artifacts` `role` 依次为
`main` / `review` / `sidecar` / `media`。本格式是 demo 约定，工程侧解码后再重新上传。
