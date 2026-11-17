# 算子 #23 压缩交付对齐语料包 — 数据 Schema（与实际输出一致）

> 表1 v4.1 #23；第 6 环节「数据集交付」（stageId = **S6**）；P0 必须；系统功能型；**模型列 = `--`**。
> 本文描述**本算子实际读写**的字段；字段名与 `utils/delivery.py`、`inference.py`、样例
> `dataset/sample/` 一致，未使用旧编号或旧四接口写法。

---

## 1. 输入

### 1.1 主输入（业务入口，必需）

一个 JSONL 文件，即 **#22 输出的 Token 样本清单**（`entrypoint`，样例为 `demo.jsonl`）。

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `sample_id`（或 `id`） | string | 是 | 样本唯一标识；缺失 → 原始隔离区（`missing_field:sample_id`） |
| `token` / `tokens` / `text.token` | string \| string[] | 是 | #22 生成的训练文本（含 `<image>` / `<audio>` / `<video>` 标记）；为空 → 原始隔离区（`empty_field:token`） |
| `routes` / `route` / `processing["22"].route` | string[] \| string | 否 | 启用的路线（`image_text` / `audio_text` / `video_text`），用于标注模态适用性 |
| `modalities` | string[] | 否 | 原样保留（`text` / `image` / `audio` / `video`） |
| `media[].path` | string | 否 | 媒体**相对引用**；绝对路径/`..` 穿越/反斜杠 → 原始隔离区（`unsupported_media_path`）；`oss://`、`http(s)://` 记为外部引用（只登记不复制） |
| `image.path` / `images[].path` / `audio.path` / `video.path` / `frames[].path` / `files[].path` | string | 否 | 同上，按容器键递归收集 |
| `batch_id` / `scene_id` / `metadata` / `qc` / `alignment` / `processing` | 任意 | 否 | **全部原样保留**，只追加本算子命名空间，不覆盖上游结论 |
| `processing["21"]` | object | 否 | 上游隔离标记：`isolated=true` 或 `status ∈ {isolated, quarantined, …}` → 不进入交付包 |
| `metadata.retry_count` | int | 否 | 台账字段 `retry_count` 来源，缺省 0 |

### 1.2 材料输入（sidecar，自动发现或部署配置显式指定）

查找顺序（每个角色取**第一层命中**）：
`<输入包>/materials/` → `<输入包>/` → `<输入包>/` 递归；也可由 `configs/config.yaml`
的 `materials.<role>` 显式指定（**请求不可覆盖该段**）。

| 角色（role） | 文件名匹配 | 关键字段 | 判定作用 |
|---|---|---|---|
| `training_files` | 主输入自身 | 见 §1.1 | 门禁 1「训练文件已生成」 |
| `quality_report` | `*quality*`, `*qc_report*`, `*质检*` | `status`、`writeback.completed`、`written_back` | 门禁 2「质量报告已写回」 |
| `sampling_conclusion` | `*sampling*`, `*inspection*`, `*抽检*` | `status`、`writeback.completed` | 门禁 3「抽检已写回」 |
| `isolation_list` | `*isolation*`, `*quarantine*`, `*隔离*`（JSONL） | 每行 `sample_id`（或 `id`） | 隔离样本排除 + 包内必含材料 |
| `version_notes` | `*version*`, `*版本*` | **`enabled_routes`**、**`annotation_granularity`** | 声明启用路线与标注粒度；缺失/非法 → 材料无效 → 待发布 |

材料齐全性 = 上表 5 项全部 `found`（`training_files` 恒为入口文件）。

### 1.3 可选输入

| 文件 | 作用 |
|---|---|
| `manifest.json`（`crossmodal-manifest-v1`） | 声明 `entrypoint`，用于确定业务入口；缺失时按唯一 `.jsonl` 推断（排除隔离清单与本算子旁路台账） |

> v4.2 起不再读取上游 `lineage.json`/`lineage_history.json`/`stage_context.json`（血缘输入已停用，相关文件即使存在也被忽略）。

---

## 2. 输出（`OUTPUT_DIR/<uniqueId>/delivery_package/runNNN/`）

| 文件 | 角色 | 说明 |
|---|---|---|
| `23_delivery_package.jsonl` | **主输出 / entrypoint** | 入包记录（保留原字段 + `operator_23` 命名空间 + `processing["23"]`） |
| `23_excluded_isolated.jsonl` | 旁路台账 | 已在隔离区的样本（含统一台账字段），**不进交付包、不与主输出重复加总** |
| `23_rejects.jsonl` | 原始隔离台账 | 坏行、非对象、编码错误、缺标识、非法媒体路径、空 Token |
| `23_materials.json` | 控制明细 | 材料发现/校验结果、三项门禁、版本状态、缺失清单 |
| `23_deliverable_package.zip` | 交付包（可交付） | 三项门禁全部满足时的封版包 |
| `23_pending_package.zip` | 交付包（待发布） | 材料不齐或抽检未回写时的待发布包 |
| `manifest.json` | 运行清单 | `crossmodal-manifest-v1`，登记**运行目录内**全部产物 |
| `23_run.log` | 日志 | 每行「时间 \| 动作 \| 真实数量」，不写原文/凭据 |
| `media/**` | 媒体副本 | 随包携带的记录相对引用（保持声明路径；`include_media=false` 时不复制） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止写入血缘/阶段上下文、不再调用 `utils/mty_compliance.py`；`utils/lineage.py` 的时间戳/配置快照工具与 `utils/transport.py` 的响应打包仍在用，血缘写入代码保留存档）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 追加字段（`operator_23` 命名空间，不覆盖上游）

| 字段 | 类型 | 说明 |
|---|---|---|
| `operator` | string | 固定 `delivery_package` |
| `operator_number` | int | 固定 `23` |
| `operator_version` | string | `v1.0.0` |
| `source_reference` | string | 固定 `operator_23` |
| `status` | string | `packaged` |
| `decision` | string | `deliverable`（可交付）/ `pending_release`（待发布） |
| `label` | string | `样本-已入交付包` |
| `action` / `destination` | string | `package` / `delivery_package` |
| `reason_code` | string | `packaged_into_delivery_package` |
| `applicability` | string | `applicable` / `not_applicable`（不在版本说明声明的启用路线内）/ `unknown`（未声明或无 route） |
| `member` | string | 包内成员路径，固定 `train/samples.jsonl` |
| `token_present` | bool | Token 非空真实性 |
| `evidence` | object | `{member, route, token_present, media_refs, external_refs, applicability, materials_source}` |
| `human_verified` | bool | **恒为 false**（本算子不产生任何人工确认） |
| `review_required` | bool | 恒为 true（交付前需工程侧/人工复核） |
| `event_time` / `batch_id` / `sample_id` / `retry_count` / `route` | - | 台账与追溯字段 |

### 2.2 旁路台账字段（`23_excluded_isolated.jsonl` / `23_rejects.jsonl`）

统一台账：`label`、`stage`（`S6`）、`operator`（`operator_23`）、`action`、`destination`、
`reason_code`、`detail`、`event_time`、`batch_id`、`sample_id`、`retry_count`、`line`；
记录内容另附 `record`（超过 `max_embedded_record_bytes` 时改为 `record_omitted` + `record_bytes`）；
原始隔离条目附 `raw_text_preview` 与 `raw_text_truncated`。

| 标签 | 去向 | 触发原因码 |
|---|---|---|
| `样本-已入交付包` | `delivery_package` | `packaged_into_delivery_package` |
| `样本-语义不通` | `sample_isolation_library` | `upstream_isolated`（上游 `reason_code` 记入 `upstream_reason_code`） |
| `原始文件-无法解析` | `origin_quarantine` | `json_parse_error`、`record_not_object`、`missing_field:sample_id`、`duplicate_identifier`、`unsupported_media_path` |
| `原始文件-空` | `origin_quarantine` | `empty_field:token` |
| （编码异常） | `origin_quarantine` | `encoding_error`（非 UTF-8，停止解析并记 `file_read_incomplete`） |

### 2.3 交付包成员（ZIP 内固定相对路径）

| 成员 | 作用 |
|---|---|
| `train/samples.jsonl` | **包内训练文件**（与主输出 `23_delivery_package.jsonl` 内容一致，已剔除隔离样本） |
| `materials/quality_report.json` | 已回写质量报告（原样复制） |
| `materials/sampling_conclusion.json` | 抽检结论（原样复制） |
| `materials/isolation_list.jsonl` | 隔离清单（原样复制） |
| `materials/version_notes.json` | 版本说明（原样复制） |
| `manifest.json` | 包清单 `crossmodal-delivery-package-v1`：逐文件 `sha256`/`size`、材料状态、门禁结果、真实统计、外部引用 |
| `version.json` | 封版说明：`enabled_routes`、`annotation_granularity`、`status`（待发布/可交付）、`dataset_version` |
| `media/**` | 随包携带的记录相对媒体（保持声明路径） |

---

## 3. 统计与守恒（`data.result.statistics` 全部取自真实结果）

```
input_records  = valid_records + bad_lines
valid_records  = packaged_records + excluded_isolated + origin_quarantined
rejects_entries= bad_lines + origin_quarantined
bad_lines      = Σ bad_line_by_reason
origin_quarantined = Σ origin_by_reason
excluded_isolated  = Σ excluded_by_reason
train_records_in_package = packaged_records          # 包内训练文件真实行数
applicable_records + not_applicable_records + applicability_unknown_records
               = packaged_records
media_carried + media_missing + media_oversized + media_skipped_by_config
               = media_distinct
materials_found_count + len(materials_missing) + len(materials_invalid) = 5
zip_entries ≥ 1
```

任一等式不成立 → `status=failed`，并把失败项写入 `statistics.conservation_failures`。
`not_applicable_records` 只是**标注**，仍留在主输出，**不与拒绝重复加总**。

### 交付门禁（只读，三项缺一不可）

| 键 | 含义 |
|---|---|
| `training_files_present` | 入口存在且 `packaged_records > 0` |
| `quality_report_written_back` | 质量报告 `status=written_back` 或 `writeback.completed=true` |
| `sampling_written_back` | 抽检结论回写完成 |

三项全真且 5 项材料齐全 → `version_state=deliverable`（`可交付`）；
否则 → `version_state=pending_release`（`待发布`），运行 `status=degraded`，
缺失项逐条写入 `23_materials.json`、包内 `manifest.json` 与 `data.result.statistics`。
`require_deliverable=true`（严格模式）时待发布将显式 `failed`。

---

## 4. 阶段说明

- 本算子为表1 第 6 环节唯一算子：`stageId = S6`；`isLastOperator` 仍必传（严格布尔）仅标记阶段末调用，
  v4.2 起中间与阶段末响应同构，均返回 `crossmodal-files-v1` 文件内容对象 `outputFile` + `result`，不再返回 `lineageFile`。
- 阶段内后续算子只使用上一节点 `data.result.outputFile` 作为 `inputFile`；
  材料仍从任务输入包（`OUTPUT_DIR/<uniqueId>/input/`）只读回退查找一次（`materials_fallback_root` 标记）。
- 形态一支持两阶段发布：`publish_phase=prepare`（允许待发布，等价 `require_deliverable=false`）/
  `publish_phase=finalize`（严格，等价 `require_deliverable=true`，不可交付即 `status=0`）。
- `uniqueId` 命名沿用在线业务键 `任务id_文件id`（同任务串行、不同 `uniqueId` 并发）。
