# 算子5 数据契约：页眉页脚与时间戳清理（05_layout）

本文件描述算子 5 的**实际**输入输出，与 `processor.py` 实现、`dataset/sample/demo.jsonl`
样例和真实运行结果一致。上游为算子 4（`04_html_cleaned.jsonl`）。

## 1. 输入

- 路径：由工程侧传入（阶段首算子由 `/uploadFile` 提供，阶段内后续算子传上一中间响应的
  `data.outputFile`）。
- 格式：JSONL，一行一条记录，UTF-8（严格解码，可配置 `input_encodings`）。
- 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是（仅用于溯源，保留原值） | 记录标识 |
| `source` | string | 是，非空白 | 源文，可含 `\n` |
| `target` | string | 是，非空白 | 译文，可含 `\n` |
| `source_lang` | string | 否（原样保留） | 源语言 |
| `target_lang` | string | 否（原样保留） | 目标语言 |
| `metadata` | object | 否（缺省视为 `{}`） | 业务元数据；非对象即硬异常 |

## 2. 输出

### 2.1 `05_layout_cleaned.jsonl`

逐行 JSON，UTF-8，原子替换写入。除 `source`/`target` 清理结果与 `metadata` 追加字段外，
其余字段原样保留：

| 追加字段 | 类型 | 说明 |
| --- | --- | --- |
| `metadata.layout_cleaning.removed_lines` | int | 删除的整行数（含空白行） |
| `metadata.layout_cleaning.inline_removed` | int | 行内删除的片段数 |
| `metadata.layout_cleaning.removed_chars` | int | 删除字符总数 |
| `metadata.layout_cleaning.removed_by_kind` | object | `{timestamp, page_number, header_footer, repeated_line, blank_line}` 计数（仅出现非零项） |
| `metadata.layout_cleaning.fields_cleaned` | array | 内容发生变化的字段名 |
| `metadata.layout_cleaning.uncertain_matches` | int | 不确定性命中数 |
| `metadata.removed_patterns` | array | 删除证据数组，元素见下 |
| `metadata.cleaning_status` | string | `unchanged` / `cleaned` / `waiting_manual` |

`removed_patterns` 元素（**不含任何原文**）：

```json
{"field": "source", "kind": "timestamp", "pattern": "subtitle_range",
 "scope": "line", "line_index": 0, "region": "head", "hits": 1, "removed_chars": 29}
```

- `kind`：`timestamp` / `page_number` / `header_footer` / `repeated_line` / `blank_line`
- `scope`：`line`（整行删除，`hits` 恒为 1） / `inline`（行内删除，`hits` 为命中次数）
- `region`：`head` / `body` / `tail`
- `pattern`：配置中的模式名，或 `repeated_line` / `blank_line` / `field_protection`

### 2.2 `05_layout_rejects.jsonl`

逐行 JSON，UTF-8。含三类内容：

| `reject_type` | `reasons` 取值 | 说明 |
| --- | --- | --- |
| `hard` | `invalid_json` | 非法 JSON 行 |
| `hard` | `invalid_record` | JSON 合法但不是对象（如数组） |
| `hard` | `missing_field:<f>` | 缺 source/target |
| `hard` | `invalid_type:<f>` | 文本字段非 string 或 `metadata` 非对象 |
| `hard` | `empty_field:<f>` | 文本字段为空或纯空白 |
| `uncertain` | `waiting_manual:pattern_line_too_long` | 命中整行模式但整行过长，疑似正文 |
| `uncertain` | `waiting_manual:whole_field_would_be_empty` | 清理后整字段为空，受 `protect_whole_field` 保护 |

条目结构：

```json
{"line": 9, "record_id": "layout-0009", "record": { ...原文... },
 "reject_type": "uncertain", "reasons": ["waiting_manual:whole_field_would_be_empty"],
 "uncertain_matches": [{"field": "source", "kind": "field_protection",
   "reason": "whole_field_would_be_empty", "removed_lines": 1}]}
```

`uncertain` 条目的 `record` 是**未经任何修改的原文**，保证不丢数据。

### 2.3 `artifacts/stage_05_<uniqueId>.zip`（阶段产物）

【无血缘改造 2026-10-19】原 `lineage.json`（固定 10 字段数组：`operator`、`operator_version`、
`uniqueId`、`inputFile`、`outputFile`、`status`、`startedAt`、`finishedAt`、`config`、`statistics`）
已停用，任务目录不再产出任何血缘文件。阶段内中间路径通过 `/predict` 返回的 `data.outputFile`
传递；阶段结束前把主输出、异常清单与 `run.log` 打包为 `artifacts/stage_05_<uniqueId>.zip`，
响应返回 `stageZip` 与 `storageUri`。

`statistics` 全部来自真实处理结果（保留在处理器统计结果与 `run.log` 中）：

```
total / passed / rejected / hard_rejected / uncertain_records / records_waiting_manual
records_changed / lines_removed / inline_removed / chars_removed / suppressed_removals_in_rejects
removed_by_kind:{timestamp,page_number,header_footer,repeated_line,blank_line}
repeated_lines_detected / repeated_line_candidates / frequency_scan_records / frequency_scan_truncated
reason:<reason>（各硬异常与不确定原因计数）/ encoding
```

不变量：`passed + rejected == total`（`total` 为非空输入行数）。

### 2.4 `run.log`

每行 `[YYYY-MM-DD HH:MM:SS] [动作] 消息`，只含文件**名**、数量与状态，
不含原文、堆栈、Token 或大段 JSON。

## 3. 状态

`success` / `partial`（有硬异常） / `waiting_manual`（有不确定记录） / `failed`
（配置错误、空输入、编码失败、输入等于输出路径、读取失败）。
成功响应 `data` 固定为 `outputFile`、`rejectFile`、`stageZip`、`storageUri` 四字段：
`isLastOperator=false` 时 `outputFile` 是输出文件路径；`isLastOperator=true` 时
`outputFile` 是最终文件内容。

## 4. 样例

`dataset/sample/demo.jsonl` 共 9 条，覆盖：跨记录重复页眉（5/9 命中）、整行中文/英文/装饰页码、
整行与行内字幕时间戳、正文时间保留、正文裸数字保留、待人工记录。
预期结果（实测）：`total=9`、`passed=8`、`rejected=1`、`uncertain_records=1`、
`lines_removed=22`、`status=waiting_manual`。

## 5. 与其他算子的 schema 关系

本算子**只追加** `metadata` 下的 3 个键，不改动 `demo/DATA_SCHEMA.md` 定义的共享字段
（`id/source/target/source_lang/target_lang/metadata`），也不删除上游写入的其他 metadata 键。
未发现共享 schema 冲突。
