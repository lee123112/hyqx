# 算子 19「筛选高置信样本入池」数据契约

> 正式编号 **19**（表1 v4.1）｜阶段 **S3 数据加工**｜类型 **规则型（模型列 = --）**
> 本文档与实际实现、样例、`map.json` 一一对应。文件名已按本算子正式编号命名，
> 不含任何旧编号或旧四接口残留。

## 1. 输入

### 1.1 入口文件

| 项 | 值 |
|---|---|
| 格式 | UTF-8 JSONL（一行一条 JSON 对象），流式处理 |
| 入口名 | 由压缩包内 `manifest.json` 的 `entrypoint` 指定；缺清单时必须**唯一**，且不能是 `19_sample_quarantine.jsonl` / `19_pending.jsonl` / `19_rejects.jsonl` |
| 记录形态 | ①扁平记录：`sample_id` + 上游命名空间；②成对记录（#11）：`pair_id` + `left`/`right` |
| 空行 | 计入 `blank_lines`，不报错 |
| 坏行 | 计入 `bad_lines` 与 `19_rejects.jsonl`，**绝不静默丢弃** |

### 1.2 本算子读取的上游字段（只读，不修改上游结论）

| 来源 | 位置（按优先级） | 用途 |
|---|---|---|
| #10 授权 | `admission.license` → `metadata.license` → `metadata.authorization` → `metadata.license_status` → `metadata.authorization_status` → `metadata.授权结论` → `authorization.status` → `authorization.license` → `license` → `license_status` → `authorization_status` → `授权结论` → `processing.10.license` | 「授权可训练」必过项 |
| #12 图文评分 | `processing["12"]`（缺则 `image_text_score`） | 图文路线是否开启/通过（读 `status`/`tier`） |
| #13 音文评分 | `processing["13"]` | 音文路线是否开启/通过（读 `route_enabled`/`status`/`passed`） |
| #14 视文评分 | `processing["14"]`（缺则顶层 `video_text_score`） | 视文路线是否开启/通过 |
| #16 整体对齐 | `alignment.whole.decision` → `processing["16"].status` → `whole_alignment.decision` | 「整体自动/人工对齐」必过项（另读 `human_veto_locked`） |
| #15 时序 | `temporal_alignment` → `operator_15` → `processing["15"]` | 「存在时间轴时序通过」必过项；无任何评分路线生效时，其「通过」结论还可作为 route 门兜底证据（`temporal_route_fallback`） |
| 时间轴存在性 | `time_index`/`timeline`/`time_range`/`bindings`/`subtitles`/`transcript_segments`/`video.frames[].timestamp`/`video.segments`/`audio.segments`，或 #15 节点判定为「适用」 | 决定时序必过项是否适用 |
| #17 局部标注 | `local_annotations.status` → `processing["17"].status` | 仅作证据；`incomplete` 视为上游已隔离 |
| #18 词-区域 | `processing["18"].status` | 仅作证据；`isolated` 视为上游已隔离 |
| #21 隔离标记 | `processing["21"].status == "isolated"` | 上游已隔离，不得加回 |
| 媒体引用 | `image` / `image_path` / `image_reference.path` / `media.{image,video,audio}` / `video.path` / `audio.path` / `annotations.image` / `paths.*` / `processing.17.{path,paths.*}` | 路径合法性扫描 + 尽力搬运 |

**路线状态归一化规则（`utils/upstream.py`）：**

- 「未生效」状态集合：`skipped` / `route_closed` / `not_applicable` / `passthrough` / `closed` / `disabled`；或显式 `route_enabled=false` → 该路线**不参与**判定（不算失败）。
- 路线通过：显式 `passed=true`；或 `tier ∈ {auto_aligned, pending_manual}`；或 `status == passed`。
- 路线失败：显式 `passed=false`；或 `tier == isolated`；或 `status ∈ {failed, isolated}`。
- 其余（含 `pending_model` / `pending`）→ **结论未定**（待定，不隔离）。
- **route 门兜底（`temporal_route_fallback`，默认 `true`）**：若三条评分路线都「未生效」，
  且 #15 结论为通过（`passed=true` 或 `status=passed`），则以 `enabled_routes=["15"]` 让
  route 门通过；`temporal` 门仍独立判定。#15 失败/不适用/待校准不补路线。

## 2. 输出

运行目录：`OUTPUT_DIR/<uniqueId>/high_confidence_pool/<runNNN>/`

| 文件 | 角色 | 内容 | 下游 |
|---|---|---|---|
| `19_candidate_pool.jsonl` | 主输出（入口） | **仅**四项必过项同时满足的入池样本；保留原记录全部字段并追加 `processing["19"]` 与 `operator_19` | #20、#22、#24 |
| `19_sample_quarantine.jsonl` | 旁路台账 | 必过项明确失败的样本（label `样本-语义不通`） | #21 样本隔离库 |
| `19_pending.jsonl` | 旁路台账 | 必过项结论缺失/待人工的样本（流程状态，label `流程-*`） | 留产线（不隔离） |
| `19_rejects.jsonl` | 旁路台账 | 坏行、非对象、编码异常、缺标识、重复标识、非法媒体路径（原始隔离区） | 原始文件排查 |
| `19_run.log` | 日志 | 逐行「时间 \| 动作 \| 数量」，不含原文/凭据/堆栈 | 运维 |
| `manifest.json` | 清单 | `crossmodal-manifest-v1`，登记本次全部产物 | 工程侧 |
| 声明的相对路径（如 `media/hc-0003.png`） | 媒体副本 | **完整保留声明的相对路径**；仅当该文件在输入目录/任务目录内确实存在时才搬运，缺失不报错（只计入 `media_missing`） | 工程侧 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py`、`utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 入池记录结构（`19_candidate_pool.jsonl` 行）

```json
{
  "sample_id": "hc-0001",
  "modalities": ["image", "text"],
  "admission": {"license": "可训练"},
  "processing": {
    "12": {"operator": "image_text_scoring", "status": "scored", "tier": "auto_aligned"},
    "16": {"operator": "whole_alignment", "status": "auto_aligned"},
    "19": {}
  },
  "operator_19": {}
}
```

`processing["19"]`（权威节点，全部字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `operator` | str | 固定 `high_confidence_pool` |
| `operator_number` | int | 固定 `19` |
| `operator_version` | str | `v1.0.0` |
| `stage_id` | str | `S3` |
| `source_reference` | str | 固定 `operator_19`（与 #15 的 `operator_15` 等前缀约定一致） |
| `identifier` | str\|null | `pair_id` 优先，否则 `sample_id` |
| `sample_id` / `pair_id` | str\|null | 原始标识 |
| `status` | int | 记录级结论：1=入池/待人工，0=不入池/原始隔离 |
| `process_status` | str | 原字符串流程态：`admitted` / `isolated` / `pending` / `origin` |
| `decision` | str | `admit` / `isolate` / `pending` / `origin` |
| `action` | str | `keep` / `isolate` / `waiting_manual` / `origin_quarantine` |
| `destination` | str | `candidate_pool` / `sample_quarantine` / `pipeline_wait` / `origin_quarantine` |
| `reason_code` | str | 见 §3 原因码表 |
| `label` | str | `样本-评分：入池` / `样本-语义不通` / `流程-*` / `原始文件-*` |
| `label_detail` | str\|null | 授权不可训练时为 `样本-敏感` |
| `detail` | str | 人类可读结论（含命中的门与依据） |
| `gates` | obj | 四道门逐项：`route` / `whole` / `license` / `temporal`，每项含 `required`、`passed`(true/false/null)、`reason`、`source` 及门专属字段；`route` 走兜底时 `enabled_routes=["15"]`，`route_results["15"]` 记录 #15 状态，`source` 注明「无生效路线时回退 processing["15"] 时序通过」 |
| `evidence` | obj | 判定证据（不含任何分数）：`enabled_routes`、`route_results`、`whole_decision`、`license_value`、`temporal_status`、`local_annotation`、`upstream_isolated`、`media_refs`、`media_missing`、`gates_failed`、`gates_pending` |
| `local_annotation_hard_requirement` | bool | 恒为 `false`（局部标注不作为入池硬条件） |
| `score_fields_produced` | bool | 恒为 `false`（本算子不产出任何分数） |
| `rule_based` | bool | 恒为 `true` |
| `model` | null | 恒为 `null`（表1 模型列 = --） |
| `human_verified` | bool | 入池时 `false`（留 #24 抽检回写） |
| `review_required` | bool | 恒为 `true` |
| `evaluated_at` | str | ISO8601 带时区 |

`operator_19`（扁平摘要，便于下游快速过滤）：`operator_number`、`operator_version`、`stage_id`、
`identifier`、`status`、`process_status`、`decision`、`action`、`destination`、`reason_code`、`label`、`label_detail`、
`gates_passed`、`enabled_routes`、`local_annotation_hard_requirement`、`score_fields_produced`、
`human_verified`、`review_required`、`source_reference`、`evaluated_at`。

### 2.2 台账条目结构（`19_sample_quarantine.jsonl` / `19_pending.jsonl` / `19_rejects.jsonl`）

```json
{
  "line": 7,
  "identifier": "hc-0005",
  "sample_id": "hc-0005",
  "pair_id": null,
  "operator": "high_confidence_pool",
  "operator_number": 19,
  "operator_version": "v1.0.0",
  "stage_id": "S3",
  "source_reference": "operator_19",
  "status": 0,
  "process_status": "isolated",
  "decision": "isolate",
  "action": "isolate",
  "destination": "sample_quarantine",
  "reason": "whole_not_aligned",
  "label": "样本-语义不通",
  "label_detail": null,
  "detail": "必过项失败：整体结论非自动/人工对齐（not_aligned）",
  "gates": {},
  "evidence": {},
  "local_annotation_hard_requirement": false,
  "score_fields_produced": false,
  "human_verified": false,
  "review_required": true,
  "detected_at": "2026-10-07T10:00:00+08:00",
  "record": {}
}
```

- 隔离 / 待人工条目带 `record`（原记录，超过 32 KiB 时替换为 `{"_truncated": true, ...}` 摘要）。
- 原始隔离条目额外带 `raw`（原始行文本，截断到 512 字符），不带 `record`。
- 编码异常条目 `detail` 说明编码原因；解析在异常处停止并记录 `fileReadIncomplete`。

## 3. 原因码与去向

| `reason_code` | 分类 | label | destination | 触发条件 |
|---|---|---|---|---|
| `all_gates_passed` | 入池 | 样本-评分：入池 | candidate_pool | 四道门全部 `passed=true` |
| `route_failed` | 隔离 | 样本-语义不通 | sample_quarantine | 已开启且生效的路线中存在失败（AND 语义） |
| `whole_not_aligned` | 隔离 | 样本-语义不通 | sample_quarantine | 整体结论为 `not_aligned` 等明确不对齐 |
| `whole_not_required_type` | 隔离 | 样本-语义不通 | sample_quarantine | 整体结论不在 `required_alignment` 允许集合内 |
| `license_not_trainable` | 隔离 | 样本-语义不通（`label_detail=样本-敏感`） | sample_quarantine | 授权结论非「可训练」 |
| `temporal_failed` | 隔离 | 样本-语义不通 | sample_quarantine | 存在时间轴且时序不通过 |
| `upstream_isolated` | 隔离 | 样本-语义不通 | sample_quarantine | 尊重上游 `#21 isolated` / `#17 incomplete` / `#18 isolated` |
| `no_scoring_route_enabled` | 待人工 | 流程-无评分路线 | pipeline_wait | 三条路线都未开启/未生效，且未走 #15 时序兜底（`temporal_route_fallback=false`，或 #15 非「通过」） |
| `insufficient_enabled_routes` | 待人工 | 流程-无评分路线 | pipeline_wait | 生效路线数 < `min_enabled_routes` |
| `route_pending` | 待人工 | 流程-待人工确认 | pipeline_wait | 生效路线结论未定（如 `pending_model` / 缺转写） |
| `whole_alignment_missing` | 待人工 | 流程-待人工对齐 | pipeline_wait | 读不到 #16 结论 |
| `whole_pending_manual_review` | 待人工 | 流程-待人工对齐 | pipeline_wait | 整体结论为 `pending_manual_review` 等 |
| `whole_uncertain` | 待人工 | 流程-待人工对齐 | pipeline_wait | 整体结论为 `uncertain`（交 #25） |
| `license_missing` | 待人工 | 流程-待补授权 | pipeline_wait | 读不到授权结论 |
| `temporal_missing` | 待人工 | 流程-待时序判定 | pipeline_wait | 存在时间轴但读不到 #15 结论 |
| `temporal_pending` | 待人工 | 流程-待时序判定 | pipeline_wait | 时序为 `pending_calibration` 等 |
| `missing_field:identifier` | 原始隔离 | 原始文件-缺字段 | origin_quarantine | 既无 `sample_id` 也无 `pair_id` |
| `duplicate_identifier` | 原始隔离 | 原始文件-重复标识 | origin_quarantine | 标识与先前记录重复 |
| `unsupported_media_path` | 原始隔离 | 原始文件-路径非法 | origin_quarantine | 媒体引用为绝对路径 / 穿越 / 协议地址 / 反斜杠 |
| `json_parse_error` | 原始隔离 | 原始文件-无法解析 | origin_quarantine | 行不是合法 JSON |
| `record_not_object` | 原始隔离 | 原始文件-无法解析 | origin_quarantine | 行是合法 JSON 但不是对象 |
| `encoding_error` | 原始隔离 | 原始文件-编码异常 | origin_quarantine | 文件不是 UTF-8（解析停止） |

**主因优先级**：结构化检查 → 门失败（`route` → `whole` → `license` → `temporal`，取首个）→ 上游隔离 → 门待定（同序）。

## 4. statistics 字段（真实统计，逐项守恒）

| 字段 | 说明 |
|---|---|
| `input_records` | 非空行数（含坏行与不可解码行） |
| `valid_records` | 成功解析为 JSON 对象的记录数 |
| `blank_lines` / `bad_lines` | 空行数 / 坏行数 |
| `admitted_records` / `sample_isolated` / `pending_records` / `origin_quarantined` | 四类去向的记录数 |
| `admitted_via_temporal_route` | 入池记录中 route 门走 #15 兜底（`enabled_routes=["15"]`）的条数；是 `admitted_records` 的子集，不参与守恒等式 |
| `rejects_entries` | `19_rejects.jsonl` 实际行数 = `bad_lines + origin_quarantined` |
| `isolated_by_reason` / `pending_by_reason` / `origin_by_reason` / `bad_line_by_reason` | 各分类的原因码计数 |
| `gate_counts` | 四道门各自的 `passed` / `failed` / `pending` 记录数 |
| `media_refs` / `media_missing` / `media_carried` | 声明媒体引用数 / 找不到数 / 实际搬运数 |
| `conservation_ok` / `conservation_failures` | 守恒校验结果与失败说明列表 |
| `empty_input` / `encoding_error` | 空输入标记 / 编码异常标记 |
| `model` / `models_available` / `rule_based` / `score_fields_produced` | 恒为 `null` / `false` / `true` / `false` |
| `local_annotation_hard_requirement` | 恒为 `false` |
| `degraded` | 运行状态是否为 `degraded` |
| `enabled` / `required_alignment` / `require_temporal_if_present` / `temporal_route_fallback` / `training_authorization` / `min_enabled_routes` / `missing_upstream_policy` | 生效配置回显 |

**守恒等式（任一不成立即 `conservation_ok=false` 且运行状态 `failed`）**

1. `input_records == valid_records + bad_lines`
2. `valid_records == admitted_records + sample_isolated + pending_records + origin_quarantined`
3. `sample_isolated == sum(isolated_by_reason)`
4. `pending_records == sum(pending_by_reason)`
5. `origin_quarantined == sum(origin_by_reason)`
6. `bad_lines == sum(bad_line_by_reason)`
7. `rejects_entries == bad_lines + origin_quarantined`
8. `media_missing <= media_refs`

## 5. 运行状态

| status | 条件 |
|---|---|
| `success` | 守恒成立，且（候选池非空 或 无待人工记录） |
| `degraded` | 守恒成立，但候选池为空且存在待人工记录（全部结论未定，等待上游/人工补充） |
| `failed` | 守恒失败；或全部行都坏（`valid_records==0` 且 `bad_lines>0`）；或严格模式（`missing_upstream_policy=failed`）下存在待人工记录 |

`degraded` 与 `failed` 都不会伪造模型分数：本算子始终把 `model` 记为 `null`，
把无法判定的样本显式登记为 `pipeline_wait`。

## 6. 样例

`dataset/sample/`（由 `make_sample.py` 生成，可重复执行）：

| 文件 | 用途 | 结果（实测） |
|---|---|---|
| `demo.jsonl` | 主样例：21 非空行（19 有效 + 2 坏行）+ 1 空行 | 入池 6 / 隔离 6 / 待人工 4 / 原始隔离 3；`conservation_ok=true`；`status=success` |
| `demo_degraded.jsonl` | 全部结论未定（3 条） | 入池 0 / 待人工 3 → `status=degraded` |
| `demo_strict.jsonl` | 配合 `missing_upstream_policy=failed`（2 条） | 入池 0 / 待人工 2 → `status=failed` |
| `manifest.json` | 样例包清单 | `entrypoint=demo.jsonl` |
