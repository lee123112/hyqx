# 算子说明书 · 24 抽检并回写质量报告

## 1. 表1 基准（第 7 环节「人工抽检复核」）

| 项目 | 表1 口径 | 本算子实现 |
| --- | --- | --- |
| 配置 | 默认开启；否（**必须**）；系统功能；P0 | `enabled` 必须为 `true`，配置成 `false` 直接报错 |
| 模型 | `--`（无模型） | 无模型、无外部服务；不做任何打分 |
| 输入 | 候选池（含自动对齐样本）、场景/模态/对齐类型分层信息 | 入口 JSONL + 同目录上游待回写质量报告 |
| 处理边界 | 按层抽 10%–20%，资深标注员复核语义/时序/场景；判错取消自动对齐并隔离；结论写回报告 | 见 §2–§5 |
| 抽多少 | 默认 10%，可选 10%–20%，按层分开抽 | `sample_rate` ∈ [0.10, 0.20]；配额按层比例分配，总量夹在区间内 |
| 输出和下游 | 抽检结论、已回写质量报告、异议清单；回写状态交 #23；异议交 #21/#25 | 7 个 JSONL/JSON 产物 + 报告；`handover` 显式标注 |
| 质量处置 | 单条判错：【样本-语义不通】，隔离，样本隔离库，并取消自动对齐 | `label=样本-语义不通`、`destination=样本隔离库`、`action=isolate`、改写 `alignment` |
| 回写未完成 | 不拦其余样本，版本保持待发布 | 待人工/存疑样本留在主输出；报告 `waiting_writeback` + `pending_release` |

## 2. 处理流程

```text
校验 uniqueId / 入口清单 / 路径安全 / 严格 UTF-8
      ↓  （第一遍流式扫描）
空行 / 坏行 / 非对象 → 24_rejects.jsonl
缺 sample_id / 重复 sample_id → 24_original_quarantine.jsonl
其余 → 抽样框（记录体写临时文件，内存只留 sample_id + 分层键）
      ↓
按 (scene, modality, alignment_type) 分层：比例取整 + 最大余数分配配额
层内按 blake2b(salt|sample_id) 升序取前 k 条（确定性、与输入顺序无关）
      ↓  （第二遍流式扫描）
判定人工复核结论 → 主输出 / 抽检样本 / 样本隔离 / 异议 / 待人工 / 原始隔离 / 坏行
      ↓
回写质量报告（保留上游字段）+ 运行清单 + 日志
      ↓
统计守恒校验（不通过则 statistics.conservation_ok=false 并写日志）
```

## 3. 分层与抽样

- **场景**：`scene_id` → `scene` → `group_id` → `metadata.scene*`；取不到记 `unknown`（维度标记不适用，记录仍保留）；
- **模态**：记录里真实存在的模态字段（`text/image/audio/video`）按字母序拼成 `image+text` 形式；无模态字段记 `unknown`；
- **对齐类型**：`alignment.status/type` → `processing["16"]` → `metadata.alignment_*`，归一化为
  `auto_aligned / manual_aligned / not_aligned / uncertain / pending_manual / other`；
  取不到记 `unknown`；
- **配额**：`target = clamp(round(rate*N), [ceil(0.10N), floor(0.20N)])`，层间用最大余数法分配；
  当百分比区间为空（`floor(0.20N) < ceil(0.10N)`）时至少抽 1 条；
- **层内选取**：`blake2b(salt|sample_id)` 升序取前 `quota` 条 —— **无随机数**，
  同一批样本重跑得到同一抽检集合；
- **抽样框**：不按对齐类型排除任何层（表1「自动对齐也进入抽样框」）；
  上游已隔离的记录保留在主输出但**不进入**抽样框。

## 4. 人工复核结论（唯一结论来源）

优先级：`processing["24"].review` → `spot_check.review` → `human_review`/`review`/`manual_review`
→ `metadata.human_review`/`metadata.review` → 扁平 `human_verdict`。

校验规则：取值必须归一化成功；`dimensions` 只允许 `semantics/temporal/scene`；
任一维度 `fail` → 整体判 **fail**；默认要求 `reviewer` 非空。
任一项不满足 → 结论**无效**，按待人工处理，`human_verified=false`。

## 5. 判定与去向

| 判定 | `label` | `action` | `destination` | 主输出 | 交接 |
| --- | --- | --- | --- | --- | --- |
| 判对（passed） | `流程-抽检通过` | `keep` | 交付候选 | ✅ | — |
| 判错（rejected） | `样本-语义不通` | `isolate` | 样本隔离库 | ❌（移出） | #21 |
| 存疑（uncertain） | `流程-抽检存疑` | `waiting_manual` | 人工复核队列 | ✅ | #21/#25 |
| 待人工（pending） | `流程-抽检待人工` | `waiting_manual` | 人工复核队列 | ✅ | #25 |
| 未抽中 | `null` | `keep` | 交付候选 | ✅ | — |
| 上游已隔离 | `流程-上游已隔离` | `keep` | 样本隔离库 | ✅ | — |

判错时**取消自动对齐**：仅当原本确为自动对齐才改写上游 `alignment` 字段
（`status=not_aligned`、`auto_aligned=false`、`cancelled_by=operator_24`，并保留 `previous_status`）；
人工对齐记录只登记 `human_alignment_judgement`，不改写上游字段。

## 6. 回写状态机

```text
抽检样本全部有最终结论（passed/rejected）
        ├── 是 → status=written_back     version_state=deliverable   （#23 可二次封版）
        └── 否 → status=waiting_writeback version_state=pending_release（未回写不得封版）

0 条抽检样本（空输入/无可抽检样本）→ 视为回写完成，但 gate.no_sample=true（待主 agent 决策）
缺上游报告 → `allow_degraded=true` 时运行 status=degraded；`false`（默认）时结构化失败
```

## 7. 统计守恒

```text
input_lines = input + blank_lines
input       = valid + rejected
valid       = output + origin_quarantined + sample_quarantined
output      = sampled_in_output + not_sampled + not_applicable_records
sampled     = sampled_in_output + sampled_rejected
sampled     = sampled_passed + sampled_rejected + sampled_uncertain + sampled_pending
sample_quarantined      = sampled_rejected
disputes                = sampled_rejected + sampled_uncertain
pending_manual          = sampled_pending
human_verified_records  = sampled_passed + sampled_rejected
review_required_records = sampled_uncertain + sampled_pending
```

旁路清单（抽检样本/异议/待人工）是主输出的线索视图，**不重复加总**。

## 8. 降级与禁止事项

- 禁止：固定分数、随机分数、`模型不可用但仍标 model`、把待人工当人工确认、
  吞掉异常行、把文件大小当业务统计、按旧提示词实现四接口或 `/downloadFile`；
- 降级只有一种：**未接入人工平台** → 全部抽检样本 `pending_manual`，
  报告保持 `waiting_writeback` 并在日志与统计中如实体现；
- 输入编码非法、文件缺失、上游报告缺失且禁止降级 → 结构化失败（`code=1`）。
