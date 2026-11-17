# 算子 26「生成交付样例」数据 schema（与 plan/实际输出一致）

- 算子：`delivery_sample`｜正式编号 **#26**｜阶段 `S8`（表1 v4.2 第 8 环节「交付样例」，§4.26 修正原 S7）
- 版本：`v1.0.0`｜模型：`--`（系统功能，无模型、不产出任何分数）
- 上游：#23 可交付语料包（`train/samples.jsonl` + `manifest.json` + `version.json` + `materials/*`）
- 下游：验收人员（Excel 样例清单 + 样例报告）；工程侧（`26_samples.jsonl`，阶段内传递入口）
- 本文件描述**当前计划口径与实际产物**；不含任何旧编号、旧四接口残留。

---

## 1. 输入：包级元数据（可选，缺失时按记录级标记判定）

| 文件 | 字段 | 类型 | 用途 |
| --- | --- | --- | --- |
| `manifest.json`（`crossmodal-delivery-package-v1`） | `version_state` | str | `deliverable` / `pending_release` → 判定是否可交付 |
| `manifest.json` | `status` | str | `可交付` / `待发布`（报告与 Excel 展示） |
| `manifest.json` | `entrypoint` | str | 目录输入时的入口声明 |
| `version.json`（`crossmodal-delivery-version-v1`） | `version_state`、`status` | str | 同上门禁 |
| `version.json` / `materials/version_notes.json` | `enabled_routes` | list[str] | **定义「已开启路线」**（`route_source=package` 时为准） |
| `version.json` / `version_notes.json` | `dataset_version` | str | Excel「数据版本」列 |
| `version.json` / `version_notes.json` | `annotation_granularity` | str | 报告（`whole` / `sentence`） |
| `materials/isolation_list.jsonl` | `sample_id` | str | **只读**，命中即排除，不参与抽样 |

## 2. 输入：记录级字段（#22 Token 记录 + #23 交付标记，逐条保留上游全部字段）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `sample_id` | str | ✅ | 唯一标识；缺失 → `26_rejects.jsonl`（`missing_field:sample_id`） |
| `group_id` | str | ❌ | 分组标识，透传 |
| `routes` / `route` | list[str] / str | ✅（或可推导） | 归一为 `image_text`/`audio_text`/`video_text` |
| `modalities` | list[str] | ❌ | `image`/`audio`/`video`/`text`；缺失时由 `image`/`audio`/`video`/`text` 键推导 |
| `text.content` | str | ❌ | 描述来源之一（`description_summary`） |
| `text.description_source` | str | ❌ | 描述来源标注（如 `caption`），写入 `evidence.description_source` |
| `admission.license` | str | ❌ | 授权口径，写入 Excel「交付标记」前置于证据 |
| `metadata.batch_id` / `batch_id` | str | ❌ | 写入 Excel「来源批次」 |
| `metadata.retry_count` | int | ❌ | 透传到台账与命名空间 |
| `processing["12"/"13"/"14"]`、`operator_12/13/14` | object | ❌ | 评分结论来源（图文/音文/视文） |
| `image_text_score` 等 | object | ❌ | 评分结论兜底来源 |
| `processing["16"]`、`operator_16`、`alignment.whole` | object | ❌ | 标注结论来源 |
| `operator_23.decision` / `processing["23"].decision` | str | ✅（门禁） | `deliverable` 才可抽入样例 |
| `processing["21"]` / `isolated` / `status` | object/bool/str | ❌ | 隔离标记 → 排除 |
| 媒体引用（`image.path` 等） | str | ❌ | 必须为包内相对路径；绝对路径/`..`/网络地址 → `26_rejects.jsonl` |

## 3. 输出：`26_sample_list.xlsx`（主交付物）

工作表「交付样例」列（表1 要求 5 列在前，其余为可追溯附加列）：

| 列 | 类型 | 来源 |
| --- | --- | --- |
| 序号 | int | 输出顺序（先按路线 `image_text→audio_text→video_text`，再按 `sample_id`） |
| **样本ID** | str | `sample_id` |
| **模态** | str | 路线中文标签：图文 / 音文 / 视文 |
| **描述摘要** | str | 描述取值链，超 `description_summary_chars` 截断加 `…`；无则空 |
| **标注结论** | str | `人工确认：<结论>` / `<结论>（自动结论，未人工确认）` / `未提供` |
| **评分结论** | str | `0.412（样本-评分：通过；CLIP-ViT/B-32）` / 仅有分档时只写分档 / `未提供` |
| 路线 | str | `image_text` 等原始路线名 |
| 来源批次 | str | `batch_id` |
| 交付标记 | str | `operator_23` 交付标记 |
| 数据版本 | str | 包 `dataset_version` |

工作表「样例报告」列：`项目` / `取值`（算子、环节、任务标识、生成时间、来源包容器/入口/
版本状态/状态/数据版本、启用路线、表1 上限、实际上限、抽样模式/种子、抽样指纹、
输入记录数、可用记录数、样例条数、运行状态、样例生成失败、守恒校验、**路线不足**、
逐路线候选/已抽/上限、边界口径、已知限制）。

## 4. 输出：`26_samples.jsonl`（阶段内传递入口，UTF-8）

一行一条样例，**保留上游全部字段**并追加：

```json
{
  "sample_id": "cm-i-0001",
  "group_id": "grp-cm-i-0001",
  "routes": ["image_text"], "route": "image_text",
  "modalities": ["image", "text"],
  "text": {"content": "一只橘猫坐在木质窗台上，午后光线斜照", "language": "zh"},
  "admission": {"license": "可训练"},
  "metadata": {"batch_id": "batch-20261001", "retry_count": 0},
  "processing": {"12": {"...": "上游评分结论原样保留"},
                 "26": {"decision": "exported", "status": 1, "process_status": "sampled"}},
  "operator_26": {
    "operator": "delivery_sample", "operator_label": "生成交付样例",
    "operator_number": 26, "operator_version": "v1.0.0", "stage_id": "S8",
    "status": 1, "process_status": "sampled", "decision": "exported", "label": "样例-已导出",
    "action": "export", "destination": "delivery_sample",
    "reason_code": "sampled_into_delivery_sample",
    "sample_id": "cm-i-0001", "seq": 1,
    "route": "image_text", "routes": ["image_text"],
    "modal": "图文", "modalities": ["image", "text"],
    "description_summary": "一只橘猫坐在木质窗台上，午后光线斜照",
    "annotation_conclusion": "对齐（自动结论，未人工确认）",
    "score_conclusion": "0.412（样本-评分：通过；CLIP-ViT/B-32）",
    "evidence": {
      "description_field": "text.content", "description_source": "caption",
      "description_chars": 18, "description_stored_chars": 18,
      "description_truncated": false, "description_sha256": "…",
      "annotation_source": "processing.16", "annotation_conclusion": "对齐",
      "score_source": "processing.12", "score_value": 0.412,
      "score_tier": "auto_aligned", "score_model": "CLIP-ViT/B-32",
      "delivery_marker": "deliverable", "multi_route": false,
      "enabled_routes": ["image_text", "audio_text", "video_text"],
      "source_package": {"version_state": "deliverable", "status": "可交付",
                         "dataset_version": "v1.0.0-demo", "entrypoint": "demo.jsonl"}
    },
    "human_verified": false, "review_required": true,
    "event_time": "2026-10-08T16:01:44+08:00", "batch_id": "batch-20261001",
    "retry_count": 0, "max_samples_per_route": 10,
    "sampling_mode": "random", "sampling_seed": 26
  }
}
```

`operator_26.status` / `processing["26"].status` 为 v4.2 双字段：int `status`（1 = 样例已导出）
与 `process_status`（`sampled`）；运行级 `report.status` 仍为字符串契约（`success`/`degraded`/`failed`）。

## 5. 输出：台账（每条记录必有唯一下去，不静默丢弃）

| 文件 | 字段 | 去向 `destination` | `reason_code` |
| --- | --- | --- | --- |
| `26_skipped.jsonl` | `label,stage,operator,action,destination,reason_code,detail,event_time,batch_id,sample_id,retry_count,line,record/record_omitted,record_bytes` | `sample_pool` | `route_not_enabled`、`no_applicable_route`、`route_quota_exhausted` |
| `26_excluded.jsonl` | 同上 + `isolation_source` | `sample_isolation_library` / `sample_pool` | `upstream_isolated`、`record_not_deliverable`、`source_not_deliverable` |
| `26_rejects.jsonl` | `label,stage,operator,action,destination,reason_code,detail,detected_at,event_time,sample_id,line,raw_text_preview,raw_text_truncated` | `origin_quarantine` | `json_parse_error`、`record_not_object`、`encoding_error`、`missing_field:sample_id`、`unsupported_media_path`、`duplicate_identifier` |

## 6. 输出：`26_sample_report.json` 关键字段

| 字段 | 说明 |
| --- | --- |
| `status` | `success` / `degraded` / `failed` |
| `sample_generation_failed` / `degrade_reason` | 样例生成失败标记与原因（`source_not_deliverable`、`no_deliverable_sample`、`disabled_by_config`、`all_records_unusable`、`conservation_violation`、`file_read_incomplete`） |
| `source` | 容器、入口、版本状态、状态、数据版本、启用路线、可交付证据 |
| `routes` | 逐路线 `available/sampled/limit/exhausted/insufficient` |
| `routes_insufficient` | 候选不足上限的路线（已按实际条数导出） |
| `table1_max_samples_per_route` | 恒为 10（只读） |
| `boundary` / `notes` | 边界口径与本次说明 |
| `statistics` | 与 `data.result.statistics` 同源的完整真实统计 |

## 7. 统计守恒（每次运行校验，失败即 `failed`）

```text
input_records        = valid_records + bad_lines + origin_quarantined
valid_records        = sampled_records + route_disabled_records
                       + route_exhausted_records + excluded_records
bad_lines            = Σ bad_line_by_reason
origin_quarantined   = Σ origin_by_reason
rejects_entries      = bad_lines + origin_quarantined
excluded_records     = Σ excluded_by_reason
route_disabled_records = Σ route_disabled_by_reason
samples_jsonl_rows   = sampled_records
Σ route_sampled      = sampled_records = Excel 数据行数
```

## 8. 命名空间与版本

- 本算子写入记录的命名空间：`operator_26`（同时写 `processing["26"]`）；
- `source_reference = operator_26`，`operator_version = v1.0.0`；
- 输出目录：`OUTPUT_DIR/<uniqueId>/delivery_sample/runNNN/`（每次运行新建 run，任务级锁串行）。
