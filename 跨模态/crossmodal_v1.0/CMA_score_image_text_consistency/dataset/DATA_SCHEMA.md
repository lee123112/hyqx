# 算子 #12 评分图文语义一致性 数据契约

编号：正式 **#12**（目录前缀 `12_`，与表1 v4.1 及本目录计划一致）
算子 ID：`image_text_semantic_scoring`　版本：`v1.0.0`　阶段：`S3`
端口：`8112`　入口文件：`12_image_text_scores.jsonl`

本文件与 `DEVELOPMENT_PLAN.md`、`TECHNICAL_DESIGN.md`、`configs/config.yaml`
以及**实际输出**保持一致；不得用于其他编号或旧版四接口。

---

## 1. 输入

### 1.1 主输入

| 项 | 说明 |
| --- | --- |
| 文件 | 上游 **#11 生成候选配对** 的 `11_candidate_pairs.jsonl`（JSONL，UTF-8） |
| 媒体 | 同包内相对路径媒体（如 `media/xxx.png`），相对输入文件所在目录解析 |
| 上传 | `/uploadFile` 接收 ZIP（含 `manifest.json` + 入口 JSONL + 媒体）或单个 JSONL |

### 1.2 输入记录（候选对）字段

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `pair_id` | string | 是 | 配对唯一标识；缺失或重复 → 原始隔离区（`原始文件-缺字段`） |
| `group_id` | string | 否 | 分组标识，原样透传 |
| `modality_pair` | string[] | 否 | 模态组合，如 `["text","image"]`；决定是否进入图文评分 |
| `evidence` | object | 否 | 上游证据（类型/准则/来源/取值），原样透传 |
| `evidence_priority` | int | 否 | 证据优先级，原样透传 |
| `left` / `right` | object | 是 | 两侧记录：`{sample_id, modalities, record}` |
| `left.record` / `right.record` | object | 是 | 样本记录，含 `sample_id`、`text`、`image`、`video`、`audio`、`metadata` |

文本描述取值顺序（与 #10 保持一致）：`text.content` → `metadata.description`；
来源取 `text.description_source` → `processing["09"].description_source` → `metadata.description_source`。
图像引用取值：`image.path` / `image.file` / `image.file_path` / `image.uri`，或 `image` 为字符串。

### 1.3 输入不被接受的形态

| 情况 | 处理 | 标签 / 目的地 |
| --- | --- | --- |
| 非 UTF-8 字节 | 异常清单 | `原始文件-编码异常` / 原始隔离区 |
| JSON 语法错误、非对象行 | 异常清单 | `原始文件-无法解析` / 原始隔离区 |
| 缺 `pair_id`、`pair_id` 重复 | 异常清单 | `原始文件-缺字段` / 原始隔离区 |
| 媒体路径为绝对路径/URL/UNC/含 `..` | 异常清单 | `原始文件-无法解析` / 原始隔离区 |
| 媒体文件不存在 | 异常清单 | `原始文件-损坏` / 原始隔离区 |
| 图片无法解码 | 异常清单 | `原始文件-损坏` / 原始隔离区 |

---

## 2. 输出

一次运行目录：`OUTPUT_DIR/<uniqueId>/image_text_semantic_scoring/runNNN/`

| 文件 | 角色 | 内容 |
| --- | --- | --- |
| `12_image_text_scores.jsonl` | 入口/主清单 | 已评分且 `score >= isolation_threshold` 的记录（人工队列 + 自动对齐），交 #16/#19 |
| `12_isolation.jsonl` | 复核清单 | `score < isolation_threshold` 的隔离台账，交 #21 |
| `12_not_applicable.jsonl` | 复核清单 | 无图文组合或路线关闭的台账（留产线，不隔离） |
| `12_pending_model.jsonl` | 复核清单 | 模型不可用时的"待模型"记录（无分数，不隔离） |
| `12_rejects.jsonl` | 复核清单 | 坏行/缺字段/媒体问题台账（原始隔离区） |
| `manifest.json` / `config.snapshot.json` / `run.log` | 副产物 | 清单、配置快照、日志 |
| `media/**` | 媒体 | 本次记录真实引用的媒体副本（相对路径保持不变） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`utils/lineage.py`、`utils/mty_compliance.py` 代码保留存档）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 主清单记录字段

上游候选对字段（`pair_id`、`group_id`、`modality_pair`、`evidence`、`left`、`right`、
`producer` 等）**原样保留**，新增以下字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text_sample_id` / `image_sample_id` | string | 本次评分的文本侧 / 图像侧样本 ID |
| `text_reference` | object | `{side, sample_id, chars, description_source}` |
| `image_reference` | object | `{side, sample_id, path}`，path 为本运行目录内相对路径 |
| `image_text_score` | object | 业务结果（见下表） |
| `scoring` | object | 统一台账字段（operator/status/process_status/label/action/destination/event_time/batch_id/retry_count） |
| `processing["12"]` | object | 在既有 `processing` 下追加本算子节点状态 |

`image_text_score` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `clip_score` | float \| null | **真实余弦相似度**，保留 6 位小数；待模型时为 `null` |
| `score_semantics` | string \| null | 恒为 `cosine`（待模型时为 `null`） |
| `tier` | string \| null | `auto_aligned` / `pending_manual` / `isolated`；待模型时为 `null` |
| `label` | string | `样本-评分：通过` / `样本-评分：待人工` / `样本-语义不通` / `流程-待模型评分` |
| `action` | string | `auto_align` / `queue_manual_review` / `isolate` / `wait_model` |
| `destination` | string | `样本池` / `人工队列` / `样本隔离库` / `留产线（样本池）` |
| `status` | int | 1=不过滤（自动对齐/待人工）/ 0=过滤（隔离）；待模型恒为 1 |
| `process_status` | string | `scored` / `pending_model` |
| `reason` | string \| null | 未评分原因（仅待模型） |
| `orientation` | string | `left_text_right_image` / `right_text_left_image` / `left_internal_text_image` / `right_internal_text_image` |
| `orientation_alternatives` | string[] | 该配对可用的全部取向（审计用） |
| `thresholds` | object | `{isolation, auto_align}` 实际生效阈值 |
| `model` | object \| null | 模型身份（见 2.4）；待模型时为 `null` |
| `detector` | object | YOLOv8 辅助证据，恒含 `tier_unchanged: true` |
| `event_time` / `batch_id` / `retry_count` | string/string/int | 台账口径 |

### 2.2 分档口径（表1 #12 冻结）

| 条件 | label | action | destination |
| --- | --- | --- | --- |
| `score < 0.25`（可配 0.20–0.40） | `样本-语义不通` | `isolate` | `样本隔离库`（交 #21） |
| `0.25 <= score < 0.32` | `样本-评分：待人工` | `queue_manual_review` | `人工队列`（留样本池，不拦） |
| `score >= 0.32`（可配 0.25–0.50） | `样本-评分：通过` | `auto_align` | `样本池` |

YOLOv8 辅助默认关闭；即使开启也**不改变** `tier`，只写 `detector.labels` /
`detector.matched_keywords` 供 #13/#14 参考。

### 2.3 台账（隔离 / 不适用 / 待模型 / 异常清单）字段

`line`、`pair_id`、`sample_id`、`reason`、`label`、`stage`、`operator`、`action`、
`destination`、`event_time`、`batch_id`、`retry_count`、`evidence`，可选 `record`（原始记录）。

### 2.4 模型身份 `model`

`{name: "CLIP-ViT/B-32", version, library, weights, device, degraded, real_backend,
untrained_architecture_only, score_semantics}`；降级时为
`{name, version, library, weights: null, device: null, degraded: true, real_backend: false,
degrade_reason}`，且**不写任何分数**。

---

## 3. 统计守恒

`statistics` 中所有计数来自真实处理结果，并满足：

```
input == output + isolated + pending_model + not_applicable + rejected
rejected == bad_lines + structural_rejects + media_rejected
pairs_total == image_text_pairs + not_applicable_pairs
image_text_pairs == scored + media_rejected
```

`conservation_ok` 为上式成立的真实布尔结果；`empty_input` 标识 0 行输入；
`degraded` / `runtime_degraded` / `degradation_reason` 描述模型可用性。

---

## 4. 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

`isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；v4.2 起中间与阶段末响应同构，均返回
`{operator, status, process_status, outputFile, result}`，`result` 为
`{runId, runDir, files, statistics, config, stageId}`。
