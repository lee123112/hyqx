# 算子 20 数据 Schema（人工抽检复核）

> 编号 20，算子 id `manual_spot_check_review`，阶段「人工抽检子流程」（本阶段只有这一个算子，阶段末必须传 `isLastOperator=true`）。
> 本文件与 `DEVELOPMENT_PLAN.md` / `TECHNICAL_DESIGN.md` 及实际输出保持一致。

## 1. 输入

### 1.1 候选文件（主输入）

工程侧传入的待复核 JSONL/manifest，每个非空行是一个 JSON 对象；可引用 13 的 C 级记录、14–17 的低置信或冲突记录。

```json
{
  "id": "r02",
  "source": "源语言文本",
  "target": "目标语言文本",
  "source_lang": "zh",
  "target_lang": "en",
  "quality_grade": "C",
  "alignment_confidence": 0.55,
  "metadata": {}
}
```

- 记录标识字段按 `record_id_fields`（默认 `[record_id, id]`）依次取值，两个都没有即 `missing_field:record_id`。
- `source` / `target` 只用于统计长度（`source_chars` / `target_chars`），**不会写入队列或已复核记录**。
- 支持顶层字段与 `metadata` 同名键；证据字段顶层优先。
- 重复 `record_id` → `duplicate_record_id`；损坏 JSON → `invalid_json`；非对象行 → `invalid_record`。

### 1.2 人工结果文件（可选侧输入）

由工程侧显式提供，通过 `/predict` 的 `config.review_payload_file`（任务目录内相对路径或工程侧绝对路径）或 `config.review_payload`（内联数组）传入。不提供时本节点只生成队列：存在未完成强制复核时返回 `waiting_manual`；仅有普通风险抽检时返回 `success`，但队列记录仍保持 `review_status=waiting_manual`。

```json
{
  "record_id": "r02",
  "review_status": "accepted",
  "reviewer_id": "reviewer_1001",
  "reviewed_at": "2026-10-05T10:00:00+08:00",
  "review_reason": "人工确认译文与源文语义一致",
  "reviewed_grade": "A"
}
```

必填：`record_id`、`review_status`、`reviewer_id`、`reviewed_at`、`review_reason`；`reviewed_grade` 可选。
`review_status` 只允许 `accepted | rejected | needs_revision | waiting_manual`，其他值一律拒绝，**绝不自动改写为 accepted**。

## 2. 输出

写入 `OUTPUT_DIR/<uniqueId>/`，全部为 UTF-8，`.tmp` + 原子替换。

| 文件 | 内容 |
|---|---|
| `20_review_queue.jsonl` | 待人工复核队列（本算子主输出，阶段末 `data.outputFile` 内容） |
| `20_reviewed.jsonl` | 已收到合法人工结果的记录 |
| `20_review_rejects.jsonl` | 输入结构异常、人工结果非法、目标不存在、冲突等拒绝/审计条目 |
| `artifacts/stage_20_<uniqueId>.zip` | 阶段产物（队列、已复核、异常清单与 `run.log`），2026-10-19 起取代 `lineage.json` |
| `run.log` | 逐行 `[时间] [动作] 数量`，不含人工备注原文、凭据与完整堆栈 |
| `review_state.json` | 任务目录内的复核状态（跨次运行幂等基础，非交付内容） |

### 2.1 队列记录

```json
{
  "record_id": "r02",
  "review_status": "waiting_manual",
  "trigger_reasons": ["grade_c", "low_confidence"],
  "selection_source": "triggered",
  "evidence_summary": {"trigger_reasons": ["grade_c"], "quality_grade": "C",
                        "alignment_confidence": 0.55, "source_chars": 17, "target_chars": 62,
                        "language_pair": "zh-en"},
  "source_ref": {"input": "demo.jsonl", "line": 2, "record_id": "r02"},
  "queued_at": "2026-10-05T13:28:09+08:00",
  "operator": "manual_spot_check_review",
  "operator_version": "v1.1.0",
  "sampling_config": {"sample_ratio": 0.1, "min_sample_count": 1,
                       "max_sample_count": 0, "random_seed": 20261005}
}
```

- 队列记录不复制完整原文，通过 `source_ref`（输入文件名 + 行号 + record_id）回查。
- `review_status` 恒为 `waiting_manual`；队列 = 被抽中且尚未收到合法人工结果的记录。

### 2.2 已复核记录

在队列字段基础上替换/新增：

```json
{
  "record_id": "r02",
  "review_status": "accepted",
  "reviewer_id": "re***01",
  "reviewed_at": "2026-10-05T10:00:00+08:00",
  "review_reason": "人工确认译文与源文语义一致",
  "review_reason_truncated": false,
  "reviewed_grade": "A",
  "reviewed": true,
  "review_conflict": false,
  "review_source": "review_payload",
  "trigger_reasons": ["grade_c"],
  "evidence_summary": {"...": "..."},
  "source_ref": {"input": "demo.jsonl", "line": 2, "record_id": "r02"},
  "reviewed_recorded_at": "2026-10-05T13:28:10+08:00",
  "operator": "manual_spot_check_review",
  "operator_version": "v1.1.0"
}
```

`reviewer_id` 默认脱敏（保留首尾各 2 字符，中间 `***`），可用 `mask_reviewer_id: false` 关闭。

### 2.3 异常/拒绝清单

每条为 `{"line", "record_type", "record", "reasons"}`，`record_type` 取 `input | payload`。稳定原因码：

`invalid_json`、`invalid_record`、`missing_field:record_id`、`duplicate_record_id`、`invalid_review_record`、
`missing_field:review_status`、`invalid_review_status:<值>`、`missing_field:reviewer_id`、
`missing_field:reviewed_at`、`invalid_reviewed_at`、`missing_field:review_reason`、
`review_target_not_found`、`review_conflict`、`review_payload_not_found`。

## 3. 抽样口径

- 触发命中的记录（`trigger_reasons` 与配置 `trigger_reasons` 有交集）**一定入队**，不受 `sample_ratio` 影响。
- 其余候选按 `sample_ratio` 随机抽样：`k = ceil(候选池 * sample_ratio)`。
- 总量尽量满足 `min_sample_count`（不足时按 `(record_id, line)` 顺序补足）。`max_sample_count`（0 = 不限）限制普通抽样预算，但绝不截断强制复核项；强制项本身超过上限时，实际队列可以超过该值。
- 随机抽样使用 `random.Random(random_seed)`，候选先按 `(record_id, line)` 排序，因此**抽样集合可复算**，与文件遍历顺序无关。

触发原因识别：

| 原因 | 判定 |
|---|---|
| `grade_c` | `grade_fields`（默认 `quality_grade`/`grade`）命中 `c_grade_values`（默认 `C`） |
| `low_confidence` | `low_confidence_fields` 为真，或 `confidence_fields` 数值 < `low_confidence_threshold`（默认 0.7） |
| `conflict` | `conflict_fields`（默认 `review_conflict`/`conflict_flag`/`conflict`）为真 |
| `review_required` | `review_required_field` 为真 |
| 显式原因 | `trigger_fields` 中的非空字符串/列表值原样作为原因 |

## 4. 统计（`statistics`）

`input_records`、`candidate_count`、`triggered_count`、`sample_pool_count`、`random_sampled_count`、
`sampled_count`、`queue_written_count`、`mandatory_review_count`、`audit_sample_count`、
`blocking_pending_count`、`nonblocking_audit_pending_count`、`reviewed_count`、`accepted_count`、`rejected_count`、
`needs_revision_count`、`reviewed_waiting_manual_count`、`waiting_manual_count`（= 待人工队列长度）、
`pending_manual_count`、`conflict_count`、`input_rejected_count`、`payload_entry_count`、
`payload_rejected_count`、`duplicate_review_ignored`、`reviewed_status_counts`、`rejected_total`、
`encoding`、`reason:<原因码>`。

全部来自真实读写计数；文件大小不参与任何业务统计。

## 5. 状态与系统边界

`status` 取值：

- `success`：无拒绝且没有未完成的强制复核；允许存在尚未回写的非阻塞普通风险抽检队列；
- `waiting_manual`：无拒绝，但仍有 `delivery_blocking=true` 的强制复核未完成；
- `partial`：存在输入异常、非法人工结果或冲突；
- `failed`：配置错误、编码错误、空输入、输入与输出同路径。

本 demo **不实现**标注工作台、账号认证和人工身份真实性校验；没有工程侧 review payload 时只生成队列，绝不伪造审核完成。冲突结果不自动覆盖，只标 `review_conflict`，原状态保留在 `review_state.json`。

`risk_review_v2` 下，只有 `delivery_blocking=true` 的强制复核未完成时返回
`waiting_manual`。普通随机抽检记录携带 `review_requirement=audit_sample` 和
`delivery_blocking=false`，可以在不伪造人工接受结论的前提下作为交付后审计队列。
