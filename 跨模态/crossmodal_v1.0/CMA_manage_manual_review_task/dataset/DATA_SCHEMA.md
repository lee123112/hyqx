# 算子 25「管理人工复核任务」数据契约（DATA_SCHEMA）

> 表1 v4.1 #25｜第 7 环节「人工抽检复核」｜stageId=`S7`｜operator_version=`v1.0.0`
> 本文与 `DEVELOPMENT_PLAN.md`、`TECHNICAL_DESIGN.md` 以及实际输出保持一致；
> 不复制旧编号（旧 `demo/15_标注员确认与标注指派` 保持不动）与旧四接口口径。

## 1. 输入

**入口**：任务目录 `OUTPUT_DIR/<uniqueId>/input/` 下的唯一 JSONL 入口
（按 `manifest.json` 的 `entrypoint` 定位；无清单时要求目录内恰好一个根级 `.jsonl`）。
阶段内链式续跑时 `inputFile` 传上一节点 `outputFile` 内容对象恢复出的任务内入口路径（必须位于同一 `uniqueId` 任务目录内）。

请求层只接受 `uniqueId`、`isLastOperator`（严格布尔）、`inputFile`、`config`；`config` 仅允许
`enabled`/`auto_dispatch`/`manual_platform_available`/`require_reviewer_identity`/`allow_degraded`，
`review_task_id`、`retry_limit` 等未声明字段一律拒绝（HTTP 422 + `code=1`）。

**编码**：严格 UTF-8（增量解码，`errors="strict"`）；非 UTF-8 直接结构化失败。

**记录字段**（上游字段全部保留，本算子只新增命名空间）：

| 字段 | 必要性 | 说明 |
|---|---|---|
| `sample_id` | 必需 | 缺失或空白 → 原始隔离台账；同一文件内重复 → 原始隔离台账 |
| `scene_id` / `scene` / `group_id` | 可选 | 场景信息，原样保留（本算子不做分层抽样） |
| `text` / `image` / `audio` / `video` | 可选 | 模态引用；本算子**不读媒体内容**（表1 模型列 `--`） |
| `metadata.batch_id` / `metadata.source_id` | 可选 | 台账 `batch_id` 取值来源，缺失记 `unknown` |
| `alignment` | 关键 | `status`/`type` 为 `uncertain`/`doubt` → 存疑证据；`auto_aligned=true` → 自动对齐 |
| `processing["16"]` | 关键 | `status`/`decision` 为 `uncertain` → 存疑证据 |
| `processing["12"/"13"/"14"/"15"]` | 关键 | `score` 落在 `[0.25, 0.32)` 或标签 `样本-评分：待人工` → 存疑证据 |
| `processing["24"]` | 关键 | `role=dispute` 或 `decision=rejected`，或 `decision=uncertain` 且 `sampled=true` → **抽检异议**；`sampled` 同时用于「不重复 #24 抽样」判定 |
| `processing["21"]` | 关键 | `action=return` / `decision=returned` / `status=returned` / `label=流程-退回重跑` / `returned=true` → **加工退回**；`isolated=true` / `action=isolate` → 上游已隔离（不适用） |
| `record.returned` / `record.return_reason` | 关键 | 加工退回的直接证据 |
| `record.retry_count` / `review_count` / `review_round` / `review_history[]` | 关键 | 历史复核轮数（「同一条最多复核 2 次」判定） |
| `review` / `human_review` / `manual_review` / `metadata.review` | 关键 | 人工结论证据（**唯一**结论来源） |
| `human_verdict` / `reviewer` / `reviewed_at` / `review_dimensions` | 可选 | 扁平人工结论兼容写法 |
| 其它未知字段 | 可选 | 原样保留并透传，不覆盖 |

**人工结论对象**（`review`）：

```json
{
  "verdict": "pass | fail | uncertain（支持 passed/通过/判错/存疑 等别名）",
  "reviewer": "复核人身份（默认必需，缺失即判待人工）",
  "reviewed_at": "2026-10-08T10:00:00+08:00",
  "round": 1,
  "dimensions": {"semantics": "fail", "temporal": "pass", "scene": "pass"}
}
```

- 维度只允许 `semantics` / `temporal` / `scene`；出现未知维度或非法取值 → 证据非法 → 待人工；
- 任一维度 `fail` → 整体判 `fail`，并置 `overridden_by_dimension=true` 留痕。

## 2. 输出

输出目录：`OUTPUT_DIR/<uniqueId>/manual_review_task_management/<runId>/`（同一任务重跑取新 `runNNN`，不覆盖）。

### 2.1 `25_review_candidates.jsonl`（主输出，入口）

逐条透传上游字段，并追加 `operator_25` 与 `processing["25"]`。**不含处理时刻**（保证逐字节可复现）。

| 字段 | 说明 |
|---|---|
| `operator` / `operator_label` / `operator_number` / `operator_version` | `manual_review_task_management` / 管理人工复核任务 / 25 / v1.0.0 |
| `stage_id` / `run_stage` | `S7` / `manual_review_task_management` |
| `status` / `process_status` | v4.2 双字段：`status` 为 int（1 = 正常流程态，0 = 隔离/锁定）；`process_status` 为原流程态：`not_applicable` / `not_eligible` / `not_triggered` / `not_dispatched` / `locked` / `passed` / `isolated` / `waiting_manual` |
| `category` | `uncertain` / `sampling_dispute` / `returned` / `null` |
| `decision` | `not_applicable` / `not_eligible` / `not_triggered` / `auto_aligned_not_sampled` / `max_review_reached` / `passed` / `rejected` / `pending_manual` / `uncertain` |
| `label` / `destination` / `action` | 统一台账字段（见 §2.3） |
| `reason_code` / `reason` / `evidence` | 可追溯原因与上游证据串 |
| `review_round` / `next_round` / `dispatched` / `max_review_count` / `trigger` | 轮次与派单信息 |
| `review` | 人工结论安全摘要（`verdict`/`reviewer`/`reviewed_at`/`round`/`dimensions`/`source`/`valid`/`reason`，**不含 notes 原文**） |
| `review_required` / `human_verified` / `locked` / `not_applicable` | 流程标志 |
| `source_reference` | 固定 `operator_25` |

**主输出包含**：不适用、非三类、未触发、自动对齐未抽中、通过、待人工、存疑的记录。
**主输出不含**：复核不通过（→ 样本隔离库）与复核超限锁定（→ 样本隔离库）的记录。

### 2.2 旁路台账

| 文件 | 内容 | 去向 |
|---|---|---|
| `25_review_tasks.jsonl` | 派单任务（`task_id`、`round`、`dispatch.state`、`dispatch.assignee`、`priority=P1`） | 人工复核平台 |
| `25_review_conclusions.jsonl` | 结论回写（`conclusion.verdict`、`round`、`human_verified`、`handover`） | 平台/审计 |
| `25_sample_pool_return.jsonl` | 复核通过、回候选池的样本 | `operator_19`、`operator_20` |
| `25_sample_quarantine.jsonl` | 复核不通过（`action=isolate`）与超限锁定（`action=lock`）的样本 | `operator_21` |
| `25_pending_manual.jsonl` | 无结论 / 结论非法 / 结论存疑的队列记录 | `operator_25_platform` |
| `25_not_dispatched.jsonl` | 未生成任务的记录（非三类 / 未触发 / 自动对齐未抽中 / 复核超限 / 上游已隔离） | 审计 |
| `25_original_quarantine.jsonl` | 缺 `sample_id`、重复 `sample_id`（保留原记录） | 原始隔离区 |
| `25_rejects.jsonl` | 坏行（JSON 解析失败、非对象），只保留 ≤200 字符预览 | 原始隔离区 |
| `25_review_report.json` | 任务/结论/轮次/交接汇总报告 | 审计 |

所有台账行都含统一字段：`label`、`stage`、`action`、`destination`、`uniqueId`、`runId`、`event_time`、`retry_count`、`sample_id`、`batch_id`。

### 2.3 统一台账取值

| `label` | `action` | `destination` | 触发条件 |
|---|---|---|---|
| `流程-复核通过` | `keep` | `候选池` | 人工结论 `pass` |
| `样本-语义不通` | `isolate` | `样本隔离库` | 人工结论 `fail` |
| `流程-复核存疑` | `waiting_manual` | `人工复核队列` | 人工结论 `uncertain` |
| `流程-复核待人工` | `waiting_manual` | `人工复核队列` | 无结论 / 结论非法 |
| `流程-复核次数超限` | `lock` | `样本隔离库` | 已完成 2 次复核 |
| `流程-无复核任务` | `keep` | `产线保留` | 非三类样本 |
| `流程-未触发复核` | `keep` | `产线保留` | `enabled=false` 或 `auto_dispatch=false` |
| `流程-自动对齐未抽中` | `keep` | `产线保留` | 自动对齐且未被 #24 抽中 |
| `流程-上游已隔离` | `keep` | `样本隔离库` | 上游已判定隔离（不适用） |
| `原始文件-缺字段` / `原始文件-重复标识` | `isolate` | `原始隔离区` | 缺 / 重复 `sample_id` |
| `原始文件-坏行` | `isolate` | `原始隔离区` | JSON 解析失败 / 非对象 |

> 说明：`not_applicable` 不计失败；`locked`（超限锁定）已在 `sample_quarantined` 中登记，
> 不再与 `output` 或 `concluded_rejected` 重复加总。

## 3. 统计与守恒

`statistics` 全部来自真实记录计数：

```
input_lines = input + blank_lines
input       = valid + rejected
valid       = output + origin_quarantined + sample_quarantined
valid       = not_dispatched + tasks_generated + origin_quarantined
not_dispatched = not_applicable_records + not_eligible + not_triggered
                 + auto_aligned_not_sampled + max_review_reached
sample_quarantined = concluded_rejected + max_review_reached
locked      = max_review_reached
queued      = tasks_generated = concluded_passed + concluded_rejected
                               + concluded_pending + concluded_uncertain
tasks_generated = dispatched + waiting_dispatch
returned_to_pool = concluded_passed
conclusions_written = human_verified_records = concluded_passed + concluded_rejected
review_required_records = concluded_pending + concluded_uncertain
```

附加只读字段：`empty_input`、`rounds`（按轮次分布）、`trigger`、`enabled`、
`platform_available`、`dispatched_sample_digest`（派单样本 id 的 blake2b 摘要）、
`report_status`、`conservation_ok`、`conservation_failures`（违反项，正常为空数组）。

## 4. 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

## 5. 样例

| 文件 | 说明 |
|---|---|
| `dataset/sample/demo.jsonl` | 28 条记录 + 5 行异常（空行 / 坏 JSON / 非对象 / 重复 id / 缺 id），含通过、不通过、存疑、待人工、超限、自动对齐未抽中、上游隔离 |
| `dataset/sample/demo_pending.jsonl` | 同结构的 28 条队列记录，但**不含任何人工结论**（模拟平台未接入 → 全部待人工） |
| `dataset/sample/manifest.json` | `crossmodal-manifest-v1` 运行清单 |
| `dataset/sample/make_sample.py` | 确定性样例生成脚本（纯标准库，无随机数，无外部下载） |

样例：`python dataset/sample/make_sample.py`（重复运行产出字节一致）。
