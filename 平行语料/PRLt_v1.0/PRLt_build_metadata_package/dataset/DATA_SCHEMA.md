# 算子 18 数据 Schema（元数据构建与数据集打包）

> 适用范围：平行语料 demo 流水线「数据集交付」阶段节点 18。
> 上游：算子 16 领域识别与筛选（默认 `16_domain_checked.jsonl`）；2026-10-19 无血缘改造后不再读取上游阶段血缘文件。
> 下游：算子 19 数据质量报告。

编号、输出前缀与 `../DATA_SCHEMA.md`（平行语料 demo 数据 Schema 契约）一致：`18_dataset.jsonl`、`18_dataset.tsv`、`18_manifest.json`、`18_delivery.zip`，另有 `18_rejects.jsonl`、`artifacts/stage_18_<uniqueId>.zip`、`run.log`（2026-10-19 起不再产出 `lineage.jsonl`）。

## 1. 输入

严格 UTF-8 的 JSONL，每行一个 dict。阶段内实际路径由 `/predict` 的 `inputFile` 指定
（取上一算子中间响应的 `data.outputFile`）；默认文件名仅用于 CLI 与本地调试。

```json
{
  "id": "del-0001",
  "source": "数据清洗很重要。",
  "target": "Data cleaning is important.",
  "source_lang": "zh",
  "target_lang": "en",
  "granularity": "sentence",
  "source_ref": "doc-001-sent-001",
  "quality_grade": "S",
  "quality_score": 95.0,
  "alignment_confidence": 0.95,
  "domain": "technology",
  "quality_status": "accepted",
  "fluency_status": "accepted",
  "domain_status": "accepted",
  "review_status": "accepted_for_quality_check",
  "delivery_eligible": true,
  "metadata": {"granularity": "sentence", "source_type": "public_web"}
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 交付记录唯一标识；`duplicate_id_policy=reject` 时重复 id 进入异常清单 |
| `source` / `target` | 是 | 源/目标文本；本节点**不改写**文本，只做透传与打包 |
| `source_lang` / `target_lang` | 是 | 语言声明（上游 02 已校验方向）；用于派生 `language_pair` |
| `language_pair` | 否 | 已存在即原样保留；缺失时按 `source_lang + "-" + target_lang` 构建 |
| `quality_grade` / `grade` | 是 | 上游 13 的等级；默认只放行 `S`/`A` |
| `quality_score` | 是 | 0–100 质量分；越界/非数值进异常清单，不 clamp |
| `alignment_confidence` | 是 | 0–1 对齐置信度；越界/非数值进异常清单 |
| `domain` | 是 | 上游 16 的主领域 |
| `source_ref` | 是 | 对齐引用（回指段落/句子/子句） |
| `quality_status` / `semantic_status` | 是 | 上游 14 语义质控状态；`waiting_quality` 视为待人工 |
| `fluency_status` | 是 | 上游 15 流畅度状态 |
| `fluency_business_gate_approved` | 否 | 上游 15 是否已获业务校准批准；仅明确 `false` + `waiting_calibration` 可作辅助证据 |
| `domain_status` | 是 | 上游 16 领域筛选状态 |
| `review_status` | 否 | `waiting_manual` 时记录为待人工 |
| `delivery_eligible` | 否 | 上游 13 标记；`false` 时默认拦截 |
| `metadata.*` | 否 | 上游元数据；本节点只**追加/镜像**交付字段，不覆盖既有键 |

字段读取顺序：**顶层优先，`metadata` 同名键兜底**。

## 2. 入库门禁

默认（`configs/config.yaml`）：

| 条件 | 结果 |
| --- | --- |
| `quality_grade ∈ {S, A}` | 通过 |
| `quality_grade = C` | 拦截（`grade_not_allowed`），除非 `allow_c=true` |
| `delivery_eligible = false` | 拦截（`not_delivery_eligible`） |
| `quality_status/fluency_status/domain_status` 不在接受集合 | 拦截（`<name>_status_not_accepted`） |
| 例外：`fluency_status=waiting_calibration` 且 `fluency_business_gate_approved=false` | 辅助证据放行，不记为拒收；记录 `fluency_gate` / `fluency_evidence_only` |
| 状态字段缺失 | 拦截（`missing_status:<name>`），`require_status_fields=false` 可放宽 |
| `review_status` 或任一状态为 `waiting_manual`/`waiting_quality` | 拦截（`waiting_manual`），除非 `allow_waiting_manual=true` |

`allow_waiting_manual=true` 时，任务明确许可的待人工记录**跳过业务门禁**进入交付集，
并标记 `delivery_status="waiting_manual"`；但**元数据完整性门禁仍然生效**。

## 3. 元数据构建与完整性

**上游必须已提供**（缺失写 `missing_metadata`，逐字段记入 `missing_fields`，不用空值补齐）：
`id`、`source`、`target`、`source_lang`、`target_lang`、`quality_grade`、`quality_score`、
`alignment_confidence`、`domain`、`source_ref`。

**本节点构建**：

| 字段 | 构建方式 |
| --- | --- |
| `language_pair` | 已存在则保留；否则由 `source_lang`/`target_lang` 派生，并置 `language_pair_derived=true` |
| `created_at` | 本次打包时间（+08:00），全批一致 |
| `operator_versions` | 仅来自显式配置 `operator_versions` 的 `operator→operator_version`；【无血缘改造 2026-10-19】不再读取上游阶段血缘，未显式配置时为 `{}` 且 `operator_versions_source="unavailable"`，**绝不伪造版本号** |
| `lineage_refs` | 字段保留兼容；无血缘改造后恒为空数组，`lineage_refs_source="unavailable"` |
| `package_version` / `delivery_status` / `delivery_checked_at` | 交付元数据 |

**冲突处理**：顶层字段与 `metadata` 同名键同时存在且不一致时，`metadata_conflict_policy`：
`reject`（默认，记 `metadata_conflict`）｜`prefer_top_level`｜`prefer_metadata`。
非 reject 策略会在记录与 `metadata` 写入 `metadata_conflict_fields` 与 `metadata_conflict_policy`，
属**显式消解**而非静默覆盖。

## 4. 输出

### 4.1 `18_dataset.jsonl`（交付数据集）

上游字段原样保留，追加（同时镜像进 `metadata`）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `language_pair` | string | 语言对，如 `zh-en` |
| `language_pair_derived` | bool | 是否为本次派生 |
| `operator_versions` | object | 上游算子版本快照（仅来自显式配置；无血缘不伪造） |
| `operator_versions_source` | string | `config` / `unavailable`（`upstream_lineage` 已停用） |
| `lineage_refs` | array | 上游对齐引用兼容字段；无血缘改造后恒为空数组 |
| `lineage_refs_source` | string | 恒为 `unavailable`（`upstream_lineage` 已停用） |
| `created_at` | string | 打包时间（+08:00） |
| `package_version` | string | 数据集包版本 |
| `delivery_status` | string | `delivered` / `waiting_manual` |
| `delivery_checked_at` | string | 门禁检查时间 |
| `fluency_gate` | string | 仅在辅助证据放行时出现，固定为 `waiting_calibration_until_gold_calibrated` |
| `fluency_evidence_only` | bool | 仅在辅助证据放行时出现；`true` 表示流畅度未作为交付门禁 |
| `metadata_conflict_fields` | array | 仅在冲突被显式消解时出现 |

### 4.2 `18_dataset.tsv`

列顺序由 `tsv_fields` 决定（默认 12 列）：
`id, source, target, source_lang, target_lang, language_pair, quality_grade, quality_score,
alignment_confidence, domain, source_ref, created_at`。
容器值序列化为紧凑 JSON；含制表符/换行/引号的单元格按 CSV 规则引用，可无损回读。

### 4.3 `18_manifest.json`

```json
{
  "manifest_version": "v1.0.0",
  "package_version": "v1.0.0",
  "pipeline_version": "v1.0.0",
  "task_id": "smoke_cli",
  "created_at": "2026-10-05T09:35:54+08:00",
  "unit_type": "sentence",
  "language_pair": "zh-en",
  "source_types": {"public_web": 2, "licensed": 1},
  "model_versions": {"domain_classification": "BAAI/bge-m3@demo"},
  "rule_versions": {},
  "record_count": 3,
  "fluency_gate": {
    "mode": "evidence_only_until_gold",
    "evidence_only_records": 0,
    "blocking": false
  },
  "statistics": {"...": "与运行统计一致（不含 package 自身哈希）"},
  "files": [{"name": "18_dataset.jsonl", "bytes": 5493, "sha256": "…", "records": 3}],
  "package": {"name": "18_delivery.zip", "members": ["18_dataset.jsonl", "18_dataset.tsv", "18_manifest.json"]},
  "indexes": {"mongodb": {"enabled": false}, "elasticsearch": {"enabled": false}, "parquet": {"enabled": false}}
}
```

`counts`、`bytes`、`sha256` **全部从真实落盘文件计算**。清单位于 zip 内部，无法包含
zip 自身哈希，故 zip 的 `sha256` 只在运行统计 `statistics.package_sha256` 中计算；
无血缘改造后不再写入任何血缘文件。

### 4.4 `18_delivery.zip`

成员由 `zip_members` 决定（默认 `18_dataset.jsonl`、`18_dataset.tsv`、`18_manifest.json`）。
如需把异常清单一并交付，把 `18_rejects.jsonl` 加入 `zip_members` 即可。

### 4.5 `18_rejects.jsonl`

```json
{
  "line": 4,
  "id": "del-0004",
  "source_ref": "doc-004-sent-001",
  "reasons": ["grade_not_allowed", "not_delivery_eligible", "waiting_manual"],
  "status_values": {"semantic": "accepted", "fluency": "accepted", "domain": "accepted"},
  "record": {"...": "原始记录（损坏行时为 null）"}
}
```

稳定原因码：

`invalid_json`、`invalid_record`、`invalid_type:metadata`、`invalid_type:<数值字段>`、
`score_out_of_range:<数值字段>`、`score_not_finite:<数值字段>`、`missing_metadata`（附 `missing_fields`）、
`metadata_conflict`（附 `conflicts`）、`missing_status:<semantic|fluency|domain>`、
`semantic_status_not_accepted`、`fluency_status_not_accepted`、`domain_status_not_accepted`、
`grade_not_allowed`、`not_delivery_eligible`、`waiting_manual`、`duplicate_id`。

> 计划 §1 提到的 `out_of_scope`/`low_fluency`/`semantic_not_equivalent` 是**上游状态值**，
> 本节点以 `<name>_status_not_accepted` 作为稳定原因码，并把原值写入 `status_values`
> 与统计的 `status_value_counts`（如 `domain=out_of_scope`），便于回溯。

### 4.6 `artifacts/stage_18_<uniqueId>.zip` 与 `run.log`

【无血缘改造 2026-10-19】原 `lineage.jsonl`（JSON 数组，按执行顺序追加，含 `operator`、
`operator_version`、`uniqueId`、`inputFile`、`outputFile`、`status`、`startedAt`、
`finishedAt`、`config`、`statistics`）已停用：任务目录不再产出任何血缘文件，
`18_delivery.zip` 也不再包含 `lineage.jsonl`。阶段内中间路径通过 `/predict` 返回的
`data.outputFile` 传递；阶段结束前把主输出、异常清单与 `run.log` 打包为
`artifacts/stage_18_<uniqueId>.zip`，响应返回 `stageZip` 与 `storageUri`。
日志逐行 `[YYYY-MM-DD HH:mm:ss] [动作] 消息`，不记录完整原文、Token 与完整堆栈。

### 4.7 `statistics` 关键字段

`input_records`、`accepted_records`、`rejected_records`、`delivery_records`、
`S_count` / `A_count` / `C_count`（**输入侧**有效记录的等级分布，`S+A+C` 可小于 `input_records`）、
`grade_counts`、`delivery_grade_counts`（交付集等级分布）、`waiting_manual_count`、
`delivered_waiting_manual_records`、`duplicate_id_records`、`metadata_conflict_records`、
`missing_metadata_records`、`operator_versions_unavailable`、
`bytes`/`sha256`（数据集）、`tsv_bytes`/`tsv_sha256`、`reject_bytes`/`reject_sha256`、
`manifest_bytes`/`manifest_sha256`、`package_bytes`/`package_sha256`、
`package_version`、`pipeline_version`、`manifest_version`、`language_pair`、`unit_type`、
`unit_type_counts`、`source_types`、`model_versions`、`rule_versions`、`zip_members`、
`encoding`、`operator_versions_source`、`status_value_counts`、`reason:*`。

恒等式：`delivery_records == accepted_records`，且
`delivery_records + rejected_records == 输入中「结构有效 + 结构损坏」的行数`。
状态优先级：`failed`（配置/编码/空输入/路径冲突/空交付且策略为 failed）→
`partial`（存在拦截）→ `waiting_manual`（无拦截但交付集含许可的待人工记录）→ `success`。

## 5. 接口与阶段传输

只实现 `POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`，
**不实现 `/downloadFile`**。`/predict` 必传 `uniqueId` 与布尔 `isLastOperator`，
阶段内后续算子另传 `inputFile`；可选传 `operatorId`（传了必须等于 `"18"`）与
`params`/`config` 覆盖项（`params` 优先）。

- 成功响应 `data` 固定为 `outputFile`、`rejectFile`、`stageZip`、`storageUri` 四字段。
- `isLastOperator=false`：`data.outputFile` 为 `OUTPUT_DIR/<uniqueId>/18_dataset.jsonl` 的路径，
  供算子 19 作为 `inputFile`。
- `isLastOperator=true`：`data.outputFile` 为 `18_dataset.jsonl` 的 **UTF-8 文本内容**
  （zip 为二进制，不作为内容返回）。阶段末 `data.outputFile` **不是**下一算子的路径；
  不再返回 `lineageFile`。

## 6. 样例与配置

`dataset/sample/demo.jsonl`：8 条样例，覆盖 S/A 交付、C 级拦截、低流畅、缺领域、
元数据冲突、领域范围外 6 类判定。
`dataset/sample/lineage.json`：历史样例上游阶段血缘，**已停用**（2026-10-19 无血缘改造后
处理器不再读取）；保留仅供回溯，CLI 冒烟不再依赖该文件。

配置项见 `configs/config.yaml`，可通过 `/predict` 的 `config` 对象覆盖。
