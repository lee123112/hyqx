# 算子 01「接入多模态批次」数据契约（DATA_SCHEMA）

- 算子编号：**#01**（表1 v4.1 第 1 环节「输入核验」，`stageId=S1`，**阶段首算子**）
- 目录：`跨模态/demo_10.6/01_接入多模态批次`
- 产物前缀：`01_`（与正式编号一致）
- 编码：全部 UTF-8（`\\n` 换行，写入采用 `*.partial` + `os.replace` 原子替换）
- 传输格式：`crossmodal-files-v1` 内容对象（形态一/形态二统一）；运行目录清单 `manifest.json`（`crossmodal-manifest-v1`）
- 媒体策略：**只登记原文件链接，不复制媒体**（`operator_01.media_policy = link_only_no_copy`）

---

## 1. 输入

### 1.1 入口文件

| 场景 | 入口 |
| --- | --- |
| 阶段首（工程侧上传后调用） | `OUTPUT_DIR/<uniqueId>/input/` 下**唯一**的根级 `*.jsonl`（有 `manifest.json` 时以清单 `entrypoint` 为准） |
| 阶段内续跑 | `/predict` 的 `inputFile` 指向上一响应 `outputFile` 内容对象恢复出的任务内路径（必须位于本任务目录内） |
| CLI | `--input <path>`（默认允许任务目录外路径，`--strict-input` 可禁止） |

### 1.2 输入记录（JSONL，一行一条）

必须是一行一个 JSON **对象**；其余形态（数组、字符串、坏 JSON、非 UTF-8 字节）进原始隔离台账的拒绝子类。

```json
{
  "sample_id": "demo-img-local",
  "source": "采集平台批次",
  "source_id": "SRC-DEMO-2026",
  "batch_id": "BATCH-20261001-01",
  "upstream_version": "pipeline-1.4.2",
  "license": "可训练",
  "files": [
    {"modality": "image", "path": "media/demo-img-local.png", "size_bytes": 16}
  ],
  "processing": {"00": {"status": "uploaded"}},
  "qc": {"verdict": null}
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `sample_id` | 是 | 对象标识；缺失/非字符串 → 【原始文件-无法解析】原始隔离区 |
| `source` | 是 | 表1 冻结五类来源（中/英写法均可）：`采集平台批次`、`预训练文本产线交付`、`预训练图像产线交付`、`预训练音频产线交付`、`预训练视频产线交付`（代码：`collection_platform` / `text_pipeline` / `image_pipeline` / `audio_pipeline` / `video_pipeline`） |
| `source_id` | 是 | 上游来源 ID；缺失 → 【流程-待补登记】退回 |
| `batch_id` | 是 | 批次号；缺失 → 【流程-待补登记】退回 |
| `upstream_version` | 是 | 上游产线版本；缺失 → 【流程-待补登记】退回 |
| `license` | 是 | 授权结论；只有 `可训练`（或 `trainable`）可进入加工 |
| `files` / `media` / `objects` | 二选一 | 文件登记列表；每项含链接与可选 `modality`、`size_bytes` |
| `sample_id` 以外的单文件简写 | 否 | 记录级 `file_path`/`oss_uri`/`uri`/`url`/`path`/`link`/`file` + `modality`，等价于单元素 `files` |
| `processing` / `alignment` / `qc` / `metadata` | 否 | 上游字段，**原样透传**；`processing` 内的其他算子命名空间不会被覆盖 |
| 其他未知字段 | 否 | 原样透传（不丢字段） |

文件登记项链接取值优先级：`oss_uri` > `oss` > `uri` > `url` > `link` > `path` > `file_path` > `file` > `source_file`。
链接形态：远端（`oss://`、`s3://`、`gs://`、`http(s)://` 等带 `scheme://`）只登记链接；相对路径必须是包内可搬运相对路径（禁绝对路径、盘符、`..`、反斜杠、URL 写法）。

### 1.3 授权结论取值

| 输入 | 归一值 | 判定 |
| --- | --- | --- |
| `可训练` / `trainable` / `可训练数据` | `trainable` | 通过 |
| `不可训练` / `untrainable` / `非可训练` / `not_trainable` | `untrainable` | 【样本-敏感】样本隔离库 |
| `待确认` / `pending` / `to_be_confirmed` / `待定` | `pending` | 【样本-敏感】样本隔离库 |
| 缺失 / 空 | `missing` | 【样本-敏感】样本隔离库 |
| 其他任意值 | `unknown` | 【样本-敏感】样本隔离库 |

`license` 也接受嵌套对象（会依次读 `conclusion`/`license`/`verdict`/`value`/`result`/`status`）。

---

## 2. 输出（全部写入 `OUTPUT_DIR/<uniqueId>/multimodal_batch_intake/<runId>/`）

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `01_workitems.jsonl` | `main` | 可加工对象清单（入口产物，交 #2（开启时）或 #3） |
| `01_origin_quarantine.jsonl` | `origin_quarantine` | 原始隔离记录（含坏行/非对象/编码错误/超长行的拒绝子类） |
| `01_sample_quarantine.jsonl` | `sample_quarantine` | 样本隔离记录（样本-敏感） |
| `01_return.jsonl` | `return` | 待补登记退回清单 |
| `01_batch_registry.json` | `batch_registry` | 批次台账（按 来源+批次号 汇总） |
| `manifest.json` | `manifest` | 运行清单（`crossmodal-manifest-v1`，供下游/工程侧定位入口） |
| `01_run.log` | `log` | 规范日志（逐行 时间+动作+真实数量，无原文/无密钥） |
| `failure.json` | `failure` | 仅失败时产生（结构化失败原因） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py`、`utils/mty_compliance.py` 代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 `01_workitems.jsonl`（主输出，一行一个「文件登记项」）

上游字段原样透传，**只追加**本算子命名空间：

```json
{
  "sample_id": "demo-img-local",
  "source": "采集平台批次",
  "source_id": "SRC-DEMO-2026",
  "batch_id": "BATCH-20261001-01",
  "upstream_version": "pipeline-1.4.2",
  "license": "可训练",
  "files": [{"modality": "image", "path": "media/demo-img-local.png", "size_bytes": 16}],
  "processing": {
    "00": {"status": "uploaded"},
    "01": {
      "status": 1, "process_status": "registered",
      "label": "流程-可加工", "action": "register",
      "destination": "工作集", "source": "collection_platform",
      "batch_id": "BATCH-20261001-01", "license": "可训练",
      "modality": "image", "reg_format": "png", "file_check": "local_verified"
    }
  },
  "operator_01": {
    "operator": "multimodal_batch_intake", "operator_label": "接入多模态批次",
    "operator_number": 1, "operator_version": "v1.0.0",
    "stage": "S1", "stage_label": "输入核验",
    "sample_id": "demo-img-local", "lineno": 1,
    "source": "collection_platform", "source_label": "采集平台批次",
    "source_id": "SRC-DEMO-2026", "batch_id": "BATCH-20261001-01",
    "upstream_version": "pipeline-1.4.2", "license": "可训练",
    "modality": "image", "format_ext": "png",
    "file_link": "media/demo-img-local.png", "file_check": "local_verified",
    "size_bytes": 16, "media_policy": "link_only_no_copy",
    "status": 1, "process_status": "registered",
    "label": "流程-可加工", "action": "register",
    "destination": "工作集", "registered": true,
    "uniqueId": "smoke_cli", "runId": "run001"
  }
}
```

- `file_link`：通过项**唯一**的文件指针（远端链接或包内相对路径），下游按此取原始媒体；
- `file_check`：`local_verified`（本地文件存在且文件头与扩展名一致）/ `link_only`（远端链接或 `whitelist` 模式下只登记链接）；
- 本算子**不复制媒体字节**到运行目录。

### 2.2 `01_origin_quarantine.jsonl`（【原始文件-无法解析】→ 原始隔离区）

业务隔离行与拒绝行共用一份台账：

```json
{
  "stage": "S1", "stage_label": "输入核验",
  "operator": "multimodal_batch_intake", "operator_number": 1, "operator_version": "v1.0.0",
  "uniqueId": "smoke_cli", "runId": "run001", "lineno": 7,
  "role": "origin_quarantine", "sample_id": "demo-bad-format",
  "label": "原始文件-无法解析", "action": "isolate", "destination": "原始隔离区",
  "reason_code": "format_not_allowed", "reason": "扩展名 .gif 不在表1 白名单",
  "source": "image_pipeline", "source_raw": "预训练图像产线交付",
  "source_id": "SRC-DEMO-2026", "batch_id": "BATCH-20261001-01",
  "upstream_version": "pipeline-1.4.2", "license_state": "trainable",
  "file_link": "oss://demo/img/demo-bad-format.gif", "modality": null,
  "declared_modality": "image", "format_ext": "gif", "file_check": "", "file_index": 0,
  "original_record": {"...": "原始记录全文，便于回溯，不静默丢失"}
}
```

拒绝行（坏行/非对象/编码错误/超长行）字段子集：`lineno`、`role=origin_quarantine`、
`label=原始文件-无法解析`、`action=isolate`、`destination=原始隔离区`、`reason_code`、
`reason`、`preview`（≤200 字符，禁止记录完整原文）、`preview_truncated`。

`reason_code` 取值：`missing_sample_id`、`file_entry_not_object`、`link_not_transportable`、
`format_not_allowed`、`file_empty`、`file_unreadable`、`file_signature_mismatch`、
`file_not_accessible`、`json_parse_error`、`record_not_object`、`invalid_utf8`、`line_too_long`。

### 2.3 `01_sample_quarantine.jsonl`（【样本-敏感】→ 样本隔离库）

字段同 2.2，差异为：`role=sample_quarantine`、`label=样本-敏感`、`destination=样本隔离库`。
`reason_code` 取值：`license_untrainable`、`license_pending`、`license_missing`、`license_unknown`。

### 2.4 `01_return.jsonl`（【流程-待补登记】→ 采集平台批次登记）

字段同 2.2，差异为：`role=returned`、`label=流程-待补登记`、`action=return`、`destination=采集平台批次登记`，
并带 `missing_fields`。
`reason_code` 取值：`incomplete_registration`（`missing_fields` 列出 `source`/`source_id`/`batch_id`/`upstream_version`）、
`missing_file_link`、`modality_mismatch`、`source_modality_mismatch`、`duplicate_registration`。

### 2.5 `01_batch_registry.json`（批次台账）

```json
{
  "format": "crossmodal-batch-registry-v1",
  "operator": "multimodal_batch_intake", "operator_label": "接入多模态批次",
  "operator_number": 1, "operator_version": "v1.0.0",
  "stageId": "S1", "uniqueId": "smoke_cli", "runId": "run001",
  "generatedAt": "2026-10-08T17:16:23+08:00",
  "entrypoint": "01_workitems.jsonl",
  "enabled": true, "file_check_mode": "header",
  "batches": [
    {
      "source": "image_pipeline", "source_label": "预训练图像产线交付",
      "source_id": "SRC-DEMO-2026", "batch_id": "BATCH-20261001-01",
      "upstream_version": "pipeline-1.4.2", "license": "可训练",
      "registered": 1, "origin_quarantined": 1, "sample_quarantined": 1, "returned": 1,
      "object_count": 4,
      "modalities": {"image": 1},
      "file_links": ["oss://demo/img/demo-img-oss.jpg"]
    }
  ],
  "totals": {"batches": 6, "objects": 12, "registered": 7, "origin_quarantined": 2,
             "sample_quarantined": 1, "returned": 2, "rejected": 2}
}
```

未登记来源/批次号的对象归入 `source="unknown"` / `batch_id="unknown"` 分组（不丢数）。

### 2.6 统计与守恒（`statistics`，出现在 `data.result` 与 CLI 输出）

| 键 | 含义 |
| --- | --- |
| `input_lines` | 物理行数 |
| `blank_lines` | 空行数 |
| `input` | 非空行数 |
| `rejected` | 坏行/非对象/编码错误/超长行数 |
| `valid` | 解析成功的记录数 |
| `objects` | 展开后的文件登记项数 |
| `registered` | 可加工对象数（主输出行数） |
| `origin_quarantined` | 原始隔离数 |
| `sample_quarantined` | 样本隔离数 |
| `returned` | 待补登记数 |
| `duplicates` | 被去重退回的登记项数 |
| `link_only` / `local_verified` | 走到文件检查步骤的登记项中，只登记链接 / 本地文件头校验通过的项数 |
| `batches` | 批次台账分组数 |
| `records_with_multiple_files` / `records_without_files` | 多文件记录数 / 无文件链接记录数 |
| `by_source` / `by_modality` / `by_format` | 通过项分布 |
| `license_counts` | 所有登记项的授权归一值分布 |
| `origin_reasons` / `sample_reasons` / `return_reasons` / `reject_reasons` | 各台账原因码分布 |
| `enabled` / `file_check_mode` | 本次运行生效配置快照 |
| `empty_input` / `conservation_ok` / `conservation_failures` / `report_status` | 空输入标记、守恒结果与运行状态 |

守恒式（全部成立，否则结构化失败）：

```
input_lines = input + blank_lines
input       = valid + rejected
objects     = registered + origin_quarantined + sample_quarantined + returned
sum(by_source)   = registered
sum(by_modality) = registered
```

`report_status`：`success`（全部通过）/ `partial`（有隔离或退回）/ `empty`（无输入）/ `disabled`（总开关关闭）/ `failed`（守恒不成立）。

### 2.7 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

---

## 3. 接口契约

| 接口 | 请求 | 响应 `data` |
| --- | --- | --- |
| `POST /uploadFile?uniqueId=` | `file`（单文件或 zip）或 `files`（多文件） | `{uniqueId, entrypoint, files[], fileCount}` |
| `POST /predict` | JSON `{uniqueId, isLastOperator(bool), inputFile?, config?}`；或 multipart `?detection=M01` + `file` | `{operator, status, outputFile, result}`（[v4.2] 不再返回 `lineageFile`）；形态一为 `{status, uniqueId, batch_id, accepted, isolation_records, process_status}` |
| `POST /removeFile?uniqueId=` | — | `{uniqueId, removed: bool}`（幂等） |

- 外层信封统一 `{"code": 0 或 1, "msg": "...", "data": {...}}`；
- 未匹配路径（含 `/downloadFile`）→ HTTP 404 + `code=1`；
- `isLastOperator` 必须是布尔（`"false"`/`1` 等 → HTTP 422 + `code=1`）；
- `config` 只允许 `enabled` / `file_check_mode` / `dedupe` / `check_source_modality`，其余键（含表1 冻结项）一律 `code=1`。

## 4. 配置（`configs/config.yaml`）

| 键 | 默认 | 可否覆盖 | 说明 |
| --- | --- | --- | --- |
| `operator.*` / `stage_id` | 身份 | 否 | 编号/名称/版本/阶段冻结 |
| `enabled` | `true` | 是 | 表1 总开关（默认开启） |
| `file_check_mode` | `header` | 是 | `whitelist` / `header` / `strict` |
| `dedupe` | `true` | 是 | 同一 `sample_id`+链接只保留首次 |
| `check_source_modality` | `false` | 是 | 来源产线模态一致性校验（表1 未明确，待主 agent 决策） |
| `allowed_license` | `可训练` | 否 | 表1 冻结 |
| `sources` | 五类来源 | 否 | 表1 冻结 |
| `file_formats` | 四组白名单 | 否 | 表1 冻结 |
| `transport.format` | `crossmodal-files-v1` | 否 | 响应内容对象格式（中间/阶段末同构） |
| `transport.max_response_bytes` | `67108864` | 否 | 响应上限，超限失败不截断 |

## 5. 与上下游的边界

- 上游：工程侧批次清单/ZIP 解压后文件清单、采集平台批次与四条预训练产线交付的登记信息；
- 下游：`01_workitems.jsonl` 交 **#02（开启时）** 或 **#03**；两类隔离台账分别交原始隔离区与样本隔离库；
- 本算子不判定内容质量、不生成标签、不做去重抽样；只做**来源/格式白名单/授权**三项准入校验与登记。
