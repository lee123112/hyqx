# 算子 21 数据契约与样例说明（`mismatch_disposal` / 表1 #21）

本文件描述**输入契约、输出契约与仓库样例**，与实现
（`utils/schemas.py`、`utils/ledger.py`、`inference.py`）逐项对应。
不是旧编号/旧四接口文档：#21 只有 `/uploadFile`、`/predict`、`/removeFile` 三个接口。

## 0. 传输承载（demo 约定）

- 形态二 `data.outputFile`：`crossmodal-files-v1` 文件内容对象（v4.2 起中间/阶段末均返回）
  `{"format","entrypoint","files":[{"path","encoding","content"}]}`；
  `.json/.jsonl/.csv/.tsv/.txt/.md/.yaml/.yml/.log` 用 `utf-8` 文本承载，其余用 `base64`。
- 运行目录清单：`crossmodal-manifest-v1`（`manifest.json`），列出 `entrypoint` 与全部实际传递文件。
- 两者都**不是甲方正式协议**，正式二进制协议到位时只替换 `utils/transport.py`。

## 1. 输入契约

`inputFile` 指向单个 JSONL 文件，或含 `manifest.json` 的目录（按 `entrypoint` 定位入口）。
编码**必须严格 UTF-8**（拒绝 GBK 等，不做猜测/替换，整次运行结构化失败）。

每行一个 JSON 对象；`sample_id` 必须是**非空字符串**。允许两种形态：

### 1.1 上游台账条目（扁平，主用形态）

```json
{
  "sample_id": "s-pass-1",
  "label": "样本-评分：通过",
  "operator": "image_text_scoring",
  "stage": "S3",
  "action": "keep",
  "destination": "样本池",
  "event_time": "2026-10-07T09:00:00+08:00",
  "batch_id": "BATCH-21-A",
  "retry_count": 0,
  "requires_human_confirmation": false,
  "record": { "image": { "path": "media/pass_ok.png" } }
}
```

### 1.2 完整样本记录 + 嵌套 `disposition`

```json
{
  "sample_id": "n-1",
  "disposition": { "label": "样本-评分：通过", "action": "keep",
                   "destination": "样本池", "retry_count": 0 },
  "metadata": { "batch_id": "BATCH-21", "human_review_required": false },
  "image": { "path": "media/pass_ok.png" }
}
```

### 1.3 证据抽取兜底链（`utils/ledger.extract_evidence`）

| 证据 | 取值顺序 |
| --- | --- |
| `label` | 顶层 → `disposition` → `quality_report` → `qc` → `record`/`record.disposition`/`record.metadata.disposition` → `metadata.disposition` → `metadata` |
| `action` | 顶层 → `disposition` → `quality_report` → `record` → `record.disposition` → `metadata.disposition` |
| `destination` | 顶层 → `disposition` → `quality_report` → `record` → `record.disposition` → `metadata.disposition` |
| `batch_id` | 顶层 → `metadata` → `record` → `record.metadata` |
| `retry_count` | 顶层 → `metadata` → `disposition` → `record` → `record.metadata` → `record.disposition`（**仅非负整数**，布尔/字符串/负数按 0） |
| `return_count` | 仅 `operator_21.return_count`（自有历史命名空间，不做全局 `return_count` 兜底） |
| `requires_human_confirmation` | 顶层 / `metadata` / `record` 下的 `requires_human_confirmation`、`human_review_required`、`review_required` |

### 1.4 动作与去向归一

- `action`：`isolate`/`隔离`/`quarantine` → `isolate`；`return`/`reflow`/`退回`/`回流`/`rerun`/`重跑` → `return`；
  `lock`/`锁定` → `lock`；`keep`/`pass`/`通过`/`admit`/`release`/`放行`/`保留` → `keep`；
  `waiting_manual`/`manual`/`待人工` → `waiting_manual`。
- `destination`：`raw_isolation`/`原始隔离区` → `raw_isolation`；`sample_isolation`/`样本隔离库` → `sample_isolation`；
  `human_queue`/`人工队列`/`待人工` → `human_queue`；`样本池`/`产线`/`next_stage` → `pipeline`；
  `^^\d{2}_`（如 `09_准备候选描述`、`01_接入多模态批次`）→ `return_to_upstream`。

### 1.5 媒体引用

只允许**相对路径**（相对入口文件所在目录），拒绝：URL（`scheme://`）、绝对路径、`~`、`..` 逃逸、
反斜杠、符号链接逃逸。字段：`image`/`audio`/`video`，支持 `<name>.path` 或 `record.<name>.path`；
**裸字符串不算合法引用**（返回 `None`）。

### 1.6 分类判定顺序（表1 口径，`utils/ledger.classify`）

1. `label` 以 `原始文件-` 开头 → **原始隔离区**
2. `label` 是评分分层（含 `评分：` 或属于评分分层标签集）且 `scoring_tiers_not_isolated=true`
   → **流程状态**（通过/入池/CLIP分 → 放行；待人工 → 人工队列）
3. `label` 属业务问题集（含 `评分低`）→ **样本隔离库**
4. `action=lock` → **锁定**（样本隔离库）
5. `action=return` → **回流**（优先于流程标签，避免被流程状态覆盖）
6. `label` 以 `流程-`/`流程状态-` 开头 → **流程状态**（留产线）
7. 其他 `样本-*` → 按业务问题处理（样本隔离库，保守口径）
8. `action=isolate` → 按去向进原始/样本隔离区
9. `action=waiting_manual`（或 `keep` + 人工去向）→ 人工队列；`action=keep` → 放行
10. 显式去向兜底 → 11. 无任何证据 → `not_applicable`（留产线）

### 1.7 退回与锁定预算（`decide_retry`）

```
next_return = return_count + 1 ; next_retry  = retry_count + 1
next_return > return_limit        → 锁定（reason=return_limit_exhausted，retry_count 不加）
next_retry  > max_retry_per_item  → 锁定（reason=retry_budget_exhausted，retry_count 不加）
否则                              → 回流（retry_count/return_count 各 +1）
```

## 2. 输出契约

运行目录：`OUTPUT_DIR/<uniqueId>/mismatch_disposal/runNNN/`（`runNNN` 取下一个未占用编号，不覆盖历史）。

| 文件 | 语义 |
| --- | --- |
| `21_passed.jsonl` | 放行样本（`next_stage` / `pipeline`），上游字段保留 + `operator_21` + `processing["21"]` |
| `21_ledger.jsonl` | 统一台账（有效记录 + 坏行），行数 = `valid_records + bad_lines` |
| `21_origin_quarantine.jsonl` | 原始隔离区清单 |
| `21_sample_quarantine.jsonl` | 样本隔离库清单（含锁定） |
| `21_return.jsonl` | 回流指令（`destination=return_to_upstream`，`upstream_destination` 保留原去向） |
| `21_pending_manual.jsonl` | 人工队列清单（`destination=human_queue`） |
| `21_rejects.jsonl` | 拒绝清单（`reason_code` ∈ `invalid_json`/`invalid_record`/`missing_sample_id`） |
| `manifest.json` | 运行清单（`crossmodal-manifest-v1` + 编号/阶段/口径说明） |
| `run.log` | 时间 + 动作 + 真实数量 |
| `media/**` | 仅放行样本的媒体（原相对路径原子复制；隔离样本媒体绝不复制） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py`、`utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 台账必需字段（9 个，测试逐项断言）

`sample_id`、`label`、`operator`、`stage`、`action`、`destination`、`event_time`、
`batch_id`、`retry_count`。

其中六类字段对应表1 输入“上游标签、工序、动作、去向、时间、批次”，
外加主键 `sample_id` 与退回计数 `retry_count`。附加字段见 README §4.2。

### 2.2 枚举

- `decision`：`pass` | `process_kept` | `not_applicable` | `waiting_manual` | `returned` |
  `locked` | `sample_isolated` | `raw_isolated` | `rejected`
- `destination`：`next_stage` | `pipeline` | `human_queue` | `return_to_upstream` |
  `sample_isolation` | `raw_isolation`
- `processing["21"].status`：`success` | `not_applicable` | `waiting_manual` | `returned` |
  `locked` | `isolated`
- 运行 `status`：`success` | `partial` | `degraded`
- `reason_code` 示例：`unreadable_file`、`encoding_error`、`empty_file`、`unparseable`、
  `sensitive`、`semantic_mismatch`、`low_score`、`mismatch`、`human_not_aligned`、
  `incomplete_annotation`、`scoring_tier`、`pending_manual`、`missing_description`、
  `unpaired`、`return_to_upstream`、`return_limit_exhausted`、`retry_budget_exhausted`、
  `explicit_lock`、`no_disposition_evidence`、`invalid_json`、`invalid_record`、
  `missing_sample_id`

### 2.3 明文保证

- 输出一律 UTF-8、`\n` 换行、每行一个 JSON（`ensure_ascii=false`，中文不转义）；
- 主产物**不含处理时刻**，同一输入的多次运行逐字节一致（可回归比对）；
- 不搬运隔离样本媒体（`isolated_media_carried == 0` 纳入守恒校验）；
- `human_verified` 恒为 `false`（无人工平台时不伪造人工结论）。

## 3. 样例目录

```
dataset/sample/
├── demo.jsonl         # 入口：18 行（14 条有效 + 1 空行 + 3 坏行）
├── manifest.json      # crossmodal-manifest-v1（entrypoint=demo.jsonl）
├── media/
│   ├── pass_ok.png        # 放行样本媒体（会随包搬运）
│   ├── pass_ok.wav        # 放行样本媒体（会随包搬运）
│   ├── origin_broken.png  # 原始文件-损坏（媒体**不搬运**）
│   └── sample_bad.png     # 样本-语义不通（媒体**不搬运**）
└── make_sample.py     # 纯标准库、无随机数、可重复生成
```

重建：`python dataset/sample/make_sample.py`（幂等，产出固定字节）。

## 4. 样例逐条期望（与 `tests/test_operator.py::EXPECTED` 完全一致）

| # | `sample_id` | 证据 | 分类 → 决策 | 去向 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 1 | `s-pass-1` | `样本-评分：通过` + keep | 评分分层 → `pass` | `next_stage` | 放行；媒体 `media/pass_ok.png` 搬运 |
| 2 | `s-pass-2` | `样本-评分：入池` + keep | 评分分层 → `pass` | `next_stage` | 放行；媒体 `media/pass_ok.wav` 搬运 |
| 3 | `s-proc-1` | `样本-评分：待人工` + keep | 评分分层 → `waiting_manual`（degraded） | `human_queue` | 平台未接入 → 人工队列 |
| 4 | `s-proc-2` | `流程-未配对` + keep | 流程状态 → `process_kept` | `pipeline` | 留产线，**不进隔离区** |
| 5 | `s-na-1` | 无任何处置证据 | `none` → `not_applicable` | `pipeline` | 不拦，留产线 |
| 6 | `s-origin-1` | `原始文件-损坏` | 原始文件 → `raw_isolated` | `raw_isolation` | 媒体 `origin_broken.png` **不搬运** |
| 7 | `s-origin-2` | `原始文件-编码异常` | 原始文件 → `raw_isolated` | `raw_isolation` | — |
| 8 | `s-sample-1` | `样本-语义不通` | 业务问题 → `sample_isolated` | `sample_isolation` | 媒体 `sample_bad.png` **不搬运** |
| 9 | `s-sample-2` | `样本-敏感` | 业务问题 → `sample_isolated` | `sample_isolation` | — |
| 10 | `s-lock-1` | `return` + `retry_count=2` | 退回预算耗尽 → `locked` | `sample_isolation` | 第 3 次 → 锁定且不再回流；标签强制 `样本-语义不通` |
| 11 | `s-ret-1` | `return` + `retry_count=0` | 预算内 → `returned` | `return_to_upstream` | `upstream_destination=09_准备候选描述` |
| 12 | `s-ret-2` | `return` + `retry_count=1` + `operator_21.return_count=1` | 单条退回次数耗尽 → `locked` | `sample_isolation` | `reason=return_limit_exhausted` |
| 13 | `s-manual-1` | `样本-评分：待人工` + `requires_human_confirmation=true` | 评分分层 → `waiting_manual`（degraded） | `human_queue` | 需人工确认且平台未接入 |
| 14 | `s-dup` | `样本-评分：通过`，与 #1 引用同一媒体 | 评分分层 → `pass` | `next_stage` | 同源重复样本：仍提交下游，媒体只搬一次 |
| 15 | — | 空行 | 不计入任何清单 | — | `blank_lines=1` |
| 16 | — | 截断 JSON `{"sample_id": "broken-1", ...` | `rejected`（`invalid_json`） | `raw_isolation` | 进拒绝清单 |
| 17 | — | `[1, 2, 3]` | `rejected`（`invalid_record`） | `raw_isolation` | 非对象 |
| 18 | — | 缺 `sample_id` 的对象 | `rejected`（`missing_sample_id`） | `raw_isolation` | 带行号 18 |

### 4.1 期望统计（`tests/test_operator.py::EXPECTED`）

```json
{
  "input_lines": 18, "blank_lines": 1, "input_records": 17,
  "valid_records": 14, "bad_lines": 3,
  "released": 7, "passed": 3, "process_kept": 1, "not_applicable": 1,
  "waiting_manual": 2, "pending_process": 0,
  "origin_isolated": 2, "sample_isolated": 4, "returned": 1, "locked": 2,
  "main_entries": 7, "ledger_entries": 17,
  "origin_entries": 2, "sample_entries": 4, "return_entries": 1,
  "pending_entries": 2, "reject_entries": 3,
  "media_carried": 2, "isolated_media_carried": 0
}
```

`status=degraded`（存在人工平台降级）；`conservation_ok=true`。

> `duplicates` 在样例中为 0（样例 `sample_id` 唯一）。重复样本标记路径由
> `tests/test_ledger.py::test_evaluate_record_marks_duplicates_without_dropping_or_isolating`
> 覆盖：重复样本**既不丢弃也不隔离**，只标 `duplicate=true`。

## 5. 与上游/下游的衔接

- **上游输入**：#12/#13/#14/#15/#16/#17/#19/#20/#24/#25 的标签、工序、动作、去向、时间、批次、退回次数；
  原始文件问题（#20 IT 报告）与业务问题（#16/#19/#24/#25）分别落到两个隔离区。
- **下游输出**：`21_passed.jsonl` 交 **#22 生成跨模态Token**；`21_return.jsonl` 交对应上游重跑；
  锁定与隔离样本**不得**进入 #22/#23 的 Token 与交付包。
- **阶段内**：`isLastOperator` 仍必传（严格布尔），仅标记阶段末调用；v4.2 起中间与阶段末响应同构，
  均返回 `outputFile`（`crossmodal-files-v1` 文件内容对象）+ `result`（业务摘要），不再返回 `lineageFile`。
