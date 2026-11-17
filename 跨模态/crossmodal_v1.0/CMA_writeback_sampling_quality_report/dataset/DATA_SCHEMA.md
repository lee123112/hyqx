# 算子 24 数据契约（`抽检并回写质量报告` v1.0.0，stageId=S7）

本文件描述算子 24 的**真实输入输出字段**。对应表1 #24（第 7 环节「人工抽检复核」）。
上游为 #19 候选池 / #20 待回写质量报告，下游为 #23（按回写状态二次封版）、
#21/#25（承接判错隔离与异议）。

---

## 1. 输入

### 1.1 入口文件

- 一个 UTF-8 编码的 JSONL 文件，逐行一条候选池记录；**严格 UTF-8**，不允许 BOM/GBK
  （编码非法 → 结构化失败 `InputError`，不做编码猜测）。
- 定位规则：
  - 阶段首：`OUTPUT_DIR/<uniqueId>/input/` 下按 `manifest.json` 的 `entrypoint`，
    无清单时要求**恰好一个**根级 `*.jsonl`；
  - 阶段内：`/predict` 的 `inputFile` 传上一算子 `outputFile` 内容对象恢复出的任务内入口路径（必须在本任务目录内）。
- 行级异常处理（**不静默丢弃**）：
  | 情况 | 去向 | `reason` |
  | --- | --- | --- |
  | 空行 | 计入 `blank_lines` | — |
  | JSON 解析失败 | `24_rejects.jsonl` | `json_parse_error` |
  | 非 JSON 对象 | `24_rejects.jsonl` | `record_not_object` |
  | 缺 `sample_id` | `24_original_quarantine.jsonl` | `missing_sample_id` |
  | `sample_id` 重复 | `24_original_quarantine.jsonl` | `duplicate_sample_id` |

### 1.2 记录字段（读取，不改写）

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `sample_id` | ✅ | 唯一标识；空/重复即原始隔离 |
| `scene_id` / `scene` / `group_id` / `metadata.scene*` | ➖ | 场景分层维度，缺失记 `unknown` |
| `text` / `image` / `audio` / `video` | ➖ | 模态存在性判定（存在即计入路由），缺失记 `unknown` |
| `alignment.status` / `alignment.type` / `processing["16"].*` | ➖ | 对齐类型分层维度 + 是否自动对齐 |
| `processing["21"].*` / `isolated` | ➖ | 上游已隔离标记 → 记录保留但**不进入抽样框** |
| `metadata.batch_id` / `source_id` | ➖ | 台账 `batch_id` 来源 |
| 其它任意字段 | ➖ | 原样保留并透传到主输出 |

> 算子 24 **不读取媒体文件内容**（可打开性/解码由 #20 负责），因此样例无需真实媒体，
> 也不在算子间复制媒体（#22/#23 按引用交付）。

### 1.3 人工复核结论（**唯一结论来源**）

表1 #24 的复核由资深标注员完成，结论随记录回传。算子 24 只做归一化与校验，
**绝不生成**结论或分数。按以下优先级取第一个存在的来源：

1. `processing["24"].review`（重跑幂等）
2. `spot_check.review`
3. `human_review` / `review` / `manual_review`（记录级）
4. `metadata.human_review` / `metadata.review`
5. 扁平字段 `human_verdict` + `reviewer` + `reviewed_at` + `review_dimensions`

结论对象结构：

```json
{
  "verdict": "pass | fail | uncertain",
  "reviewer": "reviewer-07",
  "reviewed_at": "2026-10-07T10:00:00+08:00",
  "dimensions": {"semantics": "pass", "temporal": "fail", "scene": "pass"},
  "notes": "可选，不写入台账"
}
```

- `verdict` 允许别名（`aligned`/`ok`/`通过`→pass；`not_aligned`/`判错`/`不对齐`→fail；
  `存疑`→uncertain）；
- `dimensions` 仅允许 `semantics` / `temporal` / `scene`；任一维度 `fail` → 整体判 **fail**
  （`overridden_by_dimension=true` 留痕）；
- **默认要求 `reviewer` 非空**（`require_reviewer_identity=true`），缺失即按待人工处理，
  并在 `reason_code` 中注明 `review_invalid:missing_reviewer`。

### 1.4 上游待回写质量报告

按 `report_source_names` 顺序在入口文件同目录查找，默认
`20_quality_report.json` → `quality_report.json` → `24_quality_report.json`。

- 找到：作为回写基底，**上游字段只增不改**（`checks`/`metrics`/`statistics` 原样保留）；
- 未找到：`allow_degraded=true` → 本次运行 `status=degraded`，仍产出自己的报告，
  并在 `source_report.available=false` 中如实声明；`allow_degraded=false` → 结构化失败
  （绝不伪造「回写成功」）。

---

## 2. 输出（全部写入 `OUTPUT_DIR/<uniqueId>/spot_check_writeback/<runId>/`）

| 文件 | 内容 |
| --- | --- |
| `24_spot_check.jsonl` | **主输出**：全量有效记录（判错隔离除外），逐条追加 `processing["24"]` + `operator_24` |
| `24_sampled.jsonl` | 分层抽检样本明细 + 人工结论（= 表1「分层抽检样本 / 抽检结论」） |
| `24_sample_quarantine.jsonl` | 判错样本 → #21 样本隔离库（标签【样本-语义不通】，已取消自动对齐） |
| `24_disputes.jsonl` | 异议清单（判错 + 存疑）→ #21/#25 |
| `24_pending_manual.jsonl` | 待人工清单（尚无合法复核结论）→ #25 |
| `24_original_quarantine.jsonl` | 原始隔离（缺标识/重复标识），保留原记录 |
| `24_rejects.jsonl` | 坏行（仅截断预览，不写大段原文） |
| `24_quality_report.json` | **已回写质量报告**（`status=written_back` 或 `waiting_writeback`） |
| `manifest.json` | 运行清单（`crossmodal-manifest-v1`，`entrypoint=24_spot_check.jsonl`） |
| `run.log` | 规范日志（每行含时间/动作/真实数量，不含原文与堆栈） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py`、`utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 记录级注解 `processing["24"]`

```json
{
  "operator": "spot_check_writeback",
  "operator_label": "抽检并回写质量报告",
  "operator_number": 24,
  "operator_version": "v1.0.0",
  "stage_id": "S7",
  "run_stage": "manual_spot_check_review",
  "status": 1,
  "process_status": "success",
  "action": "keep | isolate | waiting_manual",
  "decision": "passed | rejected | uncertain | pending_manual | not_sampled | skipped",
  "label": "流程-抽检通过 | 流程-抽检存疑 | 流程-抽检待人工 | 样本-语义不通 | 流程-上游已隔离 | null",
  "destination": "交付候选 | 人工复核队列 | 样本隔离库 | 原始隔离区",
  "reason_code": "human_judged_consistent | human_judged_mismatch | human_judged_uncertain | no_human_conclusion | review_invalid:<reason> | not_sampled | upstream_isolated",
  "reason": "中文说明",
  "sampled": true,
  "stratum": {"scene": "scene-hyd", "modality": "image+text", "alignment_type": "auto_aligned"},
  "sample_rate": 0.1,
  "sampling_salt": "operator24",
  "review": {"verdict": "fail", "reviewer": "reviewer-07", "reviewed_at": "...",
             "dimensions": {"semantics": "fail"}, "source": "review",
             "valid": true, "reason": null},
  "review_required": false,
  "human_verified": true,
  "human_alignment_judgement": "not_aligned",
  "auto_alignment_cancelled": true,
  "writeback": {"report": "24_quality_report.json", "written": true},
  "checked_at": "2026-10-08T12:57:01+08:00",
  "retry_count": 0,
  "source_reference": "operator_24",
  "not_applicable": false
}
```

`status` 与 `process_status` 是 v4.2 的双字段：`status` 为 int（1 = 保留在产线，0 = 过滤/隔离），
`process_status` 为原流程态（`success`/`isolated`/`waiting_manual`/`not_sampled`/`skipped`）。

同一条记录还会写一份扁平摘要 `operator_24`（`decision`/`label`/`destination`/`action`/
`sampled`/`human_verified`/`review_required`/`cancelled_auto_alignment`/`checked_at`）。

判错时还会**取消自动对齐**（仅当原对齐确为自动对齐时改写上游字段，
不改写人工对齐记录）：

```json
"alignment": {"status": "not_aligned", "auto_aligned": false, "cancelled": true,
              "cancelled_by": "operator_24", "cancelled_at": "...",
              "previous_status": "auto_aligned", "cancel_reason": "human_span_check_mismatch"}
```

### 2.2 旁路台账通用字段

`line` / `sample_id` / `batch_id` / `role` / `decision` / `label` / `stage` / `stage_id` /
`action` / `destination` / `reason_code` / `reason` / `stratum` / `stratum_label` /
`sampled` / `human_verified` / `review_required` / `review` / `cancelled_auto_alignment` /
`handover` / `operator` / `operator_number` / `operator_version` / `uniqueId` / `runId` /
`event_time` / `retry_count` / `source_reference` / `record`。

`handover`：样本隔离 → `["operator_21"]`；异议 → `["operator_21","operator_25"]`；
待人工 → `["operator_25"]`。

### 2.3 `24_quality_report.json`（已回写报告）

在上游报告基础上新增（不改上游字段）：

```json
{
  "status": "written_back | waiting_writeback",
  "original_status": "上游原状态，例如 waiting_writeback",
  "source_report": {"name": "20_quality_report.json", "available": true,
                    "status": "...", "operator": "alignment_qc",
                    "operator_version": "v1.0.0", "runId": "run001"},
  "writeback": {
    "required": true, "writeback_by": "operator_24", "writeback_completed": true,
    "version_state": "deliverable | pending_release",
    "writeback_at": "...", "writeback_operator": "spot_check_writeback",
    "writeback_operator_number": 24, "uniqueId": "...", "runId": "...",
    "sampling": {"sample_rate": 0.1, "salt": "operator24",
                 "strata": ["scene","modality","alignment_type"],
                 "target_total": 4, "achieved_rate": 0.1},
    "conclusions": {"passed": 3, "rejected": 1, "uncertain": 0,
                    "pending_manual": 0, "not_sampled": 36, "sampled_total": 4},
    "cancelled_auto_alignment": ["sp-img-07"],
    "disputes": 1, "pending_manual": 0,
    "gate": {"all_sampled_concluded": true, "sampled_total": 4,
             "no_sample": false, "reason": null}
  },
  "spot_check": {"sampling": {...}, "strata": {...}, "conclusions": {...}, "summary": {...}},
  "operator_24": {"operator": "...", "artifacts": {...}, "statistics": {...}},
  "run_status": "partial"
}
```

`status` 规则（表1「回写未完成：不拦其余样本，版本保持待发布」）：

| 条件 | `status` | `version_state` |
| --- | --- | --- |
| 抽检样本全部有最终结论（通过/判错） | `written_back` | `deliverable` |
| 存在存疑或待人工样本 | `waiting_writeback` | `pending_release` |

> `gate.reason`：待回写未完成 = `sampled_without_final_conclusion`；
> 本次 0 条抽检样本 = `no_sample`（此时按空集视为回写完成，是否以此封版**待主 agent 决策**）。

### 2.4 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

---

## 3. 统计口径与守恒

```text
input_lines = input + blank_lines
input       = valid + rejected
valid       = output + origin_quarantined + sample_quarantined
output      = sampled_in_output + not_sampled + not_applicable_records
sampled     = sampled_in_output + sampled_rejected
sampled     = sampled_passed + sampled_rejected + sampled_uncertain + sampled_pending
sample_quarantined = sampled_rejected
disputes    = sampled_rejected + sampled_uncertain
pending_manual = sampled_pending
human_verified_records = sampled_passed + sampled_rejected
review_required_records = sampled_uncertain + sampled_pending
```

- `not_applicable_records`：上游已隔离，保留在主输出但不进入抽样框；
- `24_sampled.jsonl` / `24_disputes.jsonl` / `24_pending_manual.jsonl` 是主输出的**旁路线索**，
  不重复加总；
- 任一等式不成立时 `statistics.conservation_ok=false` 且
  `conservation_failures` 列出实际差异，日志同步记录。

## 4. 其它约束

- 输出统一 UTF-8 无 BOM，落盘用同目录临时文件 + `os.replace` 原子替换；
- 只写 `OUTPUT_DIR/<uniqueId>/`；`uniqueId` 拒绝空/绝对路径/`..`/分隔符/保留设备名；
- 不同 `uniqueId` 使用独立目录与独立任务锁，互不污染；
- `isLastOperator` 仍必传（严格布尔），用于标记阶段末调用；v4.2 起中间与阶段末响应同构，
  均返回 `crossmodal-files-v1` 内容对象 `outputFile` + `result`，不再用 `false` 只回血缘对象；
- 不实现 `/downloadFile`。
