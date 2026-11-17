# 算子说明书 · 处置错配样本

| 项 | 内容 |
| --- | --- |
| 算子名称 | 处置错配样本（登记表 C 列） |
| 正式编号 | **#21**（表1 v4.1 第 21 行） |
| 目录 | `CMA_dispose_mismatched_sample`（登记表 D 列；来源 `demo_10.6/21_处置错配样本`） |
| 所属阶段 | **S3 数据质量评估**（阶段内 2/2） |
| 节点位置 | 阶段内**中间节点**（阶段末由工程侧以 `isLastOperator=true` 标记；v4.2 起中间/阶段末响应同构） |
| 算子 id | `mismatch_disposal` |
| 版本 | `v1.0.0` |
| 端口 | `8121` |
| 可跳过 | **否**（表1「必须开启」，置 `false` 会配置校验失败） |
| 优先级 | 高（质量门禁，影响下游样本池） |

## 1. 职责

统一处置跨模态产线中的**异常与错配样本**，并维护**统一台账**：

- 原始文件问题 → **原始隔离区**；
- 内容/错配问题 → **样本隔离库**；
- **评分分层 / 流程状态不进隔离区**（表1 明令）；
- 退回与锁定预算：单条最多退回重跑 **1** 次、全产线最多 **2** 次，第 3 次**锁定**且不再回流；
- **锁定与隔离样本不得进入 Token / 交付**；
- 人工复核平台未接入时：**默认 `allow_degraded=false`** → `DependencyError` 结构化失败
  （CLI 退出码 1、`/predict` HTTP 400）；测试/联调可 `--override '{"allow_degraded": true}'`
  显式降级，届时需人工确认的记录置 `waiting_manual` / `degraded` 留产线，**绝不伪造人工结论**。

## 2. 表1 冻结参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 必须开启；置 `false` 直接配置失败 |
| `return_limit` | `1` | 单条允许退回重跑次数上限（本算子维度） |
| `max_retry_per_item` | `2` | 全产线单条最多重跑次数；第 3 次锁定样本隔离库 |
| `raw_issue_destination` | `raw_isolation` | 只读：原始文件问题 → 原始隔离区 |
| `quality_issue_destination` | `sample_isolation` | 只读：治理不达标 → 样本隔离库 |
| `scoring_tiers_not_isolated` | `true` | 只读：评分分层 / 流程状态不进隔离区 |
| `manual_platform_available` | `false` | 人工复核平台接入状态（demo 环境未接入） |
| `allow_degraded` | `false` | `true` → 相关记录置 `waiting_manual` 并显式 `degraded`；`false` → 结构化失败 `DependencyError` |
| `max_embedded_record_bytes` | `32768` | 隔离台账内嵌原记录的字节上限（超出只留计数与哈希） |
| 模型/权重 | 无 | 纯规则型：无模型、无权重、不联网 |

请求不可覆盖 `operator`、`stage_id`、`enabled`、去向与评分分层口径（见 `utils/schemas.py`）。

## 3. 输入

- 主输入：**M20 质量报告**（`20_alignment_qc.jsonl`）。结论既读扁平摘要
  `operator_20`，也读权威节点 `processing["20"]`；两处都有时以 `operator_20` 为准。
- 兼容输入：各评分算子（#12/#13/#14/#15）与标注算子（#16/#17/#18）产出的
  隔离记录、`pending`/`return` 记录与上游流转台账（顶层 `label`/`action`/
  `destination`，或 `disposition`/`quality_report`/`qc` 嵌套）。
- 关键字段：`label`、`decision`、`action`、`destination`、`retry_count`、`reason_code`、
  `stage_id`、`evaluated_at`（后四项分别落到台账的 `stage`/`event_time` 与证据链）。

M20 结论到处置的映射（表1「通过样本才可进入 M22」）：

| M20 `label` / `decision` | 本算子处置 | 去向 |
|---|---|---|
| `样本-质检通过` / `accepted` | `pass`（放行） | `next_stage` |
| `流程-待人工复核` / `flagged` | `waiting_manual`（人工平台未接入时显式降级） | `human_queue` |
| `原始文件-*` | `raw_isolated` | `raw_isolation` |
| `样本-语义不通` 等业务问题 | `sample_isolated` | `sample_isolation` |

## 4. 输出

| 产物 | 说明 |
| --- | --- |
| `21_passed.jsonl` | 主产物：通过、继续向下游流转的记录 |
| `21_ledger.jsonl` | **统一台账**（每条处置决策 + 去向 + 计数） |
| `21_origin_quarantine.jsonl` | 原始隔离区（`raw_isolation`） |
| `21_sample_quarantine.jsonl` | 样本隔离库（`sample_isolation`） |
| `21_return.jsonl` | 退回重跑（`return_to_upstream`） |
| `21_pending_manual.jsonl` | 人工队列（`human_queue`） |
| `21_rejects.jsonl` | 坏行 / 无法解析 |

台账决策取值：`pass` / `process_kept` / `not_applicable` / `waiting_manual` /
`returned` / `locked` / `sample_isolated` / `raw_isolated` / `rejected`。

## 5. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/uploadFile?uniqueId=` | 上传 zip 素材包或单文件 |
| POST | `/predict` | 执行处置。形态一 `?detection=dispose_mismatched_sample`（`multipart/form-data` 单文件，处理型 1 成功/0 失败，返回 `status`/`disposal_records`/`token_eligible`）；形态二 JSON（`isLastOperator` 标记阶段末，中间/阶段末响应同构，均返回 `outputFile`+`result`） |
| POST | `/removeFile?uniqueId=` | 幂等清理任务目录 |

**无 `/downloadFile`**。外层响应固定 `{"code","msg","data"}`；v4.2 起不再返回 `lineageFile`，也不再写 `lineage.json`/`stage_context.json` 等血缘/阶段归档文件。

## 6. 依赖

- 运行：`fastapi`、`uvicorn`、`pydantic>=2`、`PyYAML`、`python-multipart`；
- 测试：`pytest`、`httpx`；
- **无模型依赖、无权重、无外部服务**，运行期不联网。

## 7. 资源

CPU 2 核以上 / 内存 ≥ 2 GB；无需 GPU；磁盘按台账规模计。

## 8. 质量标签与去向

| 场景 | 标签 | 去向 |
| --- | --- | --- |
| 通过 | `样本-质检通过`（M20）、`样本-评分：通过` 等上游结论 | 下一阶段（`next_stage`） |
| 内容/错配 | `样本-语义不通` 等 `样本-` 前缀 | 样本隔离库（`sample_isolation`） |
| 原始文件问题 | `原始文件-无法解析` 等 `原始文件-` 前缀 | 原始隔离区（`raw_isolation`） |
| 评分分层 / 流程状态 | `流程-` / `流程状态-` 前缀 | **不进隔离区**，保留产线 |
| 通过（判定优先级） | 含 `质检通过`/`对齐通过`/`审核通过` 且不含 `未通过`/`待复核` → 先于 `样本-` 隔离判定，记为 `pass` | `next_stage` |
| 预算内退回 | 台账 `destination=return_to_upstream` | 退回上游重跑 |
| 超预算 | 台账 `locked` | 样本隔离库，**不再回流** |
| 需人工确认 | 台账 `waiting_manual` | 人工队列（`human_queue`） |

**禁止负样本工厂**：不复制隔离样本。

## 9. 与其他算子的关系

- **上游**：#12/#13/#14/#15（评分路线）、#16/#17/#18（标注路线）产出隔离与流转记录；
- **下游**：锁定/隔离样本**不进入** #22 生成跨模态Token 与 #23 交付打包；
- **同阶段关系**：#20 质检对齐产物（阶段 1/2）负责质检结论，本算子（阶段 2/2）负责按结论处置；
- **旧目录关系**：旧 `demo/21_图像视频数据质检` 含部分隔离能力，本目录补齐表1 #21，旧目录保持不动；
- **跨算子约定**：退回目标 `destination` 沿用上游算子名称（`01_接入多模态批次`、`09_准备候选描述`），
  并受 `^^\d{2}_` 命名校验约束——该取值属跨算子业务契约，不随目录英文重命名而改动。
