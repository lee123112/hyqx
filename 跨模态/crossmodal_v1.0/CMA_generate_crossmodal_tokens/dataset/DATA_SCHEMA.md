# 算子 22「生成跨模态Token」数据契约（DATA_SCHEMA）

> 表1 v4.1 正式编号 **22**｜大环节 **5 样本构建**｜工具分类 **记录封装与输出**｜工具类型 **系统功能**
> 是否可跳过 **否（必须）**｜默认 **开启**｜优先级 **P0**｜模型列 **--（无模型）**｜阶段 `S5`
> 版本 `v1.0.0`｜输出目录 `OUTPUT_DIR/<uniqueId>/crossmodal_token/<runNNN>/`

本文件与 `configs/config.yaml`、`utils/names.py`、`utils/tokens.py`、`inference.py` 逐项对齐。
字段名或文件名如需变更，必须同步修改这三处 + 本文件 + `tests/`。

---

## 1. 输入契约

### 1.1 入口

- 编码 **UTF-8**，一行一条 JSON，**必须是 JSON 对象**；空行忽略并计数。
- 入口定位（`utils/transport.resolve_entrypoint`）：
  1. 存在 `manifest.json` → 采用其 `entrypoint`（必须是 `.jsonl`）；
  2. 否则取目录内**唯一**的非本算子旁路 `.jsonl`；多个候选时仅在
     `19_candidate_pool.jsonl` / `candidates.jsonl` / `samples.jsonl` 等已知主清单名中唯一命中才接受，否则报错。
- 本算子自己的派生产物（`22_*.jsonl`、`samples_<route>.jsonl`）**不会**被当成入口。

### 1.2 读取的上游字段（只读，绝不改写）

| 用途 | 读取位置（按优先级） | 来源 |
|---|---|---|
| 任务标识 | `pair_id` → `sample_id` | #11 / #09 |
| 成对记录 | `left.record` / `right.record` 展平（**左优先**），顶层字段最后覆盖 | #11/#12 |
| 模态 | `modalities[]`（别名归一见 `utils/upstream.MODALITY_ALIASES`）+ 引用推断 + 文本推断 | #08/#11 |
| 对齐文本 | `text.content` → `text`(字符串) → `metadata.description` → `description` | #09/#11 |
| 媒体引用 | `image/images/image_path`、`audio/audios/audio_path/voice`、`video/videos/video_path/movie`、`media.{image,audio,video}`、`paths.*`、`*_reference.path`、`annotations.image`、`processing.17.path`/`processing.17.paths.*` | #05–#07 |
| 质量结论（#20） | `processing["20"]` → `operator_20` → `quality_conclusion` → `qc.quality_conclusion` → `qc_conclusion` → `processing.qc` | **#20 质检对齐产物** |
| 隔离结论 | `processing["21"]`/`isolation` → `processing["20"].decision` → `processing["19"]` → `processing["17"].status==incomplete` → `processing["18"].status==isolated` | #21/#20/#19/#17/#18 |
| 授权结论 | `admission.license` → `metadata.license` → `authorization.status` → `license` → `processing["10"].license` 等（见 `_LICENSE_PATHS`） | #10 |
| 版本状态 | `metadata.version` → `version` → `metadata.corpus_version` → `processing["23"].version` | #23/工程侧 |
| 局部标注证据 | `local_annotations.status` → `processing["17"].status` → `processing["18"].status` → `phrase_box_relations[]` → `processing.17.regions/boxes` | #17/#18 |
| 批次/来源 | `metadata.batch_id` / `metadata.source` / `metadata.collected_at` | 工程侧 |

**媒体引用合法性**：必须是**可搬运的相对路径**。绝对路径 `/...`、盘符 `C:\`、反斜杠、
协议地址 `http://`、`~` 开头、`..` 越界一律判非法 → 原始隔离区（`unsupported_media_path`）。

---

## 2. 判定规则与去向

原始文件级问题优先于业务问题；顺序即优先级。

| 顺序 | 条件 | 动作 | 标签 | 去向文件 | reason |
|---|---|---|---|---|---|
| 1 | 无 `sample_id` 且无 `pair_id` | `origin` | 原始文件-缺字段 | `22_rejects.jsonl` | `missing_field:identifier` |
| 2 | 媒体引用非法 | `origin` | 原始文件-路径非法 | `22_rejects.jsonl` | `unsupported_media_path` |
| 3 | 上游已隔离（#21/#20 隔离、#17 incomplete、#18 isolated、#19 isolated） | `isolated_skip` | 样本-已隔离 | `22_isolated_skipped.jsonl` | `upstream_isolated` |
| 4 | **没有质量结论**（#20 缺失或 `skipped`） | `pending` | 流程-待质量结论 | `22_pending.jsonl` | `no_quality_conclusion` |
| 5 | 授权结论缺失/无法判定 | `pending` | 流程-待补授权 | `22_pending.jsonl` | `license_missing` |
| 6 | 授权结论为不可训练 | `untrainable` | 样本-不可训练（`label_detail=样本-敏感`，建议去向 `sample_quarantine`） | `22_untrainable.jsonl` | `license_not_trainable` |
| 7 | 无对齐文本 | `pending` | 流程-待补文本 | `22_pending.jsonl` | `missing_text` |
| 8 | 模态不构成任何已知路线 | `pending` | 流程-无适用路线 | `22_pending.jsonl` | `no_applicable_route` |
| 9 | 适用路线全部未开启 | `pending` | 流程-路线未开启 | `22_pending.jsonl` | `route_disabled` |
| 10 | 标记与声明模态不一致（防御性，正常不触发） | `pending` | 流程-无适用路线 | `22_pending.jsonl` | `marker_validation_failed` |
| 11 | 其余（质检通过 + 可训练 + 有文本 + 路线已开启） | `generate` | 样本-样本构建：已生成Token | `samples.jsonl` + 路线文件 | `token_generated` |

### 2.1 路线（表1 配置项 1「没打开的路线不会生成对应文件」）

| 路线 | 条件 | 文件 | 默认 |
|---|---|---|---|
| `image_text` | `image` + `text` | `samples_image_text.jsonl` | **开启（不可关）** |
| `audio_text` | `audio` + `text` | `samples_audio_text.jsonl` | 关闭 |
| `video_text` | `video` + `text` | `samples_video_text.jsonl` | **开启（v1.0.1 起出厂默认）** |
| `local_annotation` | 局部标注证据（#17/#18）+ 至少一种媒体模态 | `samples_local_annotation.jsonl` | 关闭 |

- **未开启路线不产出文件**；开启但无样本时产出空文件（下游可据此判断路线已开）。
- 一条样本可命中多条路线（多模态）；`route` 字段记录**主路线**（按
  `image_text → audio_text → video_text → local_annotation` 取第一个），`routes[]` 记录全部。
- `samples.jsonl` 是**全部已生成样本**的并集（表1：样本文件名固定 `samples.jsonl`，隔离样本不写入）。

### 2.2 Token 文本

```
token_text = "<模态标记，按 image→audio→video 固定顺序，空格分隔>" + " " + 对齐文本
```

- 默认图文任务必然以 `<image>` 开头（表1 完成标准）。
- 标记值只读固定：`<image>` / `<audio>` / `<video>`（`markers` 配置改成其它值即配置失败）。
- **不产出子词 ID**：本算子模型列为 `--`，无分词器，`subword_token_ids` 与 `tokenizer` 恒为 `null`。
  「Token」指**模态标记封装**（F-ALIGN-058/059），绝不伪造分词结果。

---

## 3. 输出契约

`OUTPUT_DIR/<uniqueId>/crossmodal_token/<runNNN>/`（多次运行顺延，绝不覆盖历史运行）

| 文件 | 内容 | 备注 |
|---|---|---|
| `samples.jsonl` | **主输出**：全部已生成样本，原记录字段原样保留 + `processing["22"]` + `operator_22` | 固定名 |
| `samples_image_text.jsonl` | 图文路线子集 | 路线开启时产出 |
| `samples_audio_text.jsonl` | 音文路线子集 | 仅路线开启时 |
| `samples_video_text.jsonl` | 视文路线子集 | 仅路线开启时 |
| `samples_local_annotation.jsonl` | 局部标注路线子集 | 仅路线开启时 |
| `22_tokens.jsonl` | Token 文件（标记 + token_text + 标记校验结果） | |
| `22_metadata_index.jsonl` | 元数据索引（模态索引 + 对象存储链接） | |
| `22_pending.jsonl` | 流程待处理台账（无质量结论/待补授权/缺文本/路线未开启/无适用路线） | 留产线，不隔离 |
| `22_flagged.jsonl` | **旁路 flagged 单列**：`processing["20"].decision == "flagged"` 的样本已生成 Token，同时单列复核 | #24 / 人工 |
| `22_untrainable.jsonl` | 不可训练样本清单（表1「不能训练的样本单独列出」） | |
| `22_isolated_skipped.jsonl` | 隔离区样本跳过台账 | 绝不进入 `samples.jsonl` |
| `22_rejects.jsonl` | 原始隔离（坏行、非对象、编码异常、缺标识、路径非法） | |
| `22_run.log` | 逐行「时间 \| 动作 \| 真实数量」 | |
| `manifest.json` | 运行清单（`crossmodal-manifest-v1`，登记运行目录内全部产物） | |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止写入血缘/阶段上下文、不再调用 `utils/mty_compliance.py`；`utils/lineage.py` 的时间戳/配置快照工具与 `utils/transport.py` 的响应打包仍在用，血缘写入代码保留存档）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

**媒体策略**：`media_link_policy = "original_files_link_only"`。
本算子**不搬运、不复制**任何媒体文件，只在记录中登记「对象存储链接」式引用；
`object_storage_connected = false` 表示当前 demo 环境未接入真实对象存储，
`link_status = "reference_only"`，`local_presence ∈ {present, absent, unknown}` 仅表示本地可见性。

### 3.1 `processing["22"]` / `operator_22` 字段

| 字段 | 说明 |
|---|---|
| `operator` / `operator_label` / `operator_number` / `operator_version` | `crossmodal_token` / 生成跨模态Token / 22 / v1.0.0 |
| `stage_id` / `run_stage` | `S5` / `sample_build` |
| `source_reference` | 恒为 `operator_22`（可追溯来源，不改写上游命名空间） |
| `identifier` / `sample_id` / `pair_id` | 标识 |
| `status` / `decision` / `action` / `destination` / `reason_code` / `label` | 见 §2 表 |
| `route` / `routes[]` / `applicable_routes[]` / `route_files` / `samples_file` | 路线归属与文件名 |
| `modalities[]` / `markers[]` / `token_text` / `token_char_count` | Token 内容 |
| `marker_validation` | `{valid, declared, required, missing, unexpected, duplicated, reference_ok, reference_missing}` |
| `text_reference` | `{field, length, sha256}`（只记引用与摘要，不复制原文到日志） |
| `media_links[]` | `{modality, path, object_key, storage, link_status, local_presence}` |
| `media_link_policy` / `media_copied` / `object_storage_connected` | 媒体策略三件套 |
| `quality_conclusion` | `{outcome, source, status, decision, flagged}`（#20 结论快照，只读回显） |
| `license` / `version` | 授权与版本快照 |
| `version_state_changed` / `compressed` / `packaged` | **恒为 false**（表1 边界：不压缩、不改版本状态） |
| `human_verified` / `review_required` / `subword_tokenizer` | `false` / `true` / `null` |
| `generated_at` | ISO8601（含时区） |

### 3.2 台账条目（pending / untrainable / isolated / flagged）

公共字段（含项目规则 §2.3 要求的统一台账字段）：
`line, identifier, sample_id, pair_id, operator, operator_number, operator_version,
stage_id, stage, source_reference, status, decision, action, destination,
recommended_destination, reason, label, label_detail, detail, routes, applicable_routes,
batch_id, human_verified, review_required, retry_count, event_time, detected_at, record`。

- `stage` 与 `stage_id` 同值（规则写作 `stage/operator`，本实现两者都给出）；
- `event_time` 与 `detected_at` 同值；
- `batch_id` 取自 `metadata.batch_id`；`retry_count` 取自 `metadata.retry_count`（缺省 0）；
- `recommended_destination` 仅在需要跨算子转交时给出（如不可训练样本 → `sample_quarantine`）；
- `record` 为原记录视图，超过 32768 字符时截断为 `{"_truncated": true, "original_chars": n, ...}`。

### 3.3 原始隔离条目（rejects）

字段：`line, identifier, sample_id, pair_id, operator, operator_number, operator_version,
stage_id, source_reference, status, decision, action, destination, reason, label, detail,
human_verified, review_required, detected_at`；坏行额外带 `raw`（截断至 512 字符）。
**不携带 `record`**，避免把无法解析的内容当业务记录扩散。

---

## 4. statistics 与守恒等式

`conservation_ok = true` 要求以下等式**全部成立**（右侧均以**磁盘产物真实行数**校验，不用内存计数自证）：

1. `input_records == valid_records + bad_lines`
2. `valid_records == generated_records + pending_records + untrainable_records + isolated_skipped_records + origin_quarantined`
3. `generated_records == samples_records == tokens_records == metadata_records`
4. `generated_records == sum(primary_by_route)`
5. `by_route[route] == route_<route>_records`（各路线文件真实行数）
6. `sum(by_route) == generated_records + multi_route_records`
7. `pending_records == pending_reason_total == pending 文件行数`
8. `untrainable_records == 文件行数 == untrainable_reason_total`
   - 旁路 flagged：`flagged_records == 22_flagged.jsonl 行数` 且 `flagged_records <= generated_records`
     （flagged 属「不拦样本」的旁路子集：**仍生成 Token**，但按表1/计划「旁路 flagged 单列」另列复核）
9. `isolated_skipped_records == 文件行数 == isolated_reason_total`
10. `origin_quarantined == origin_reason_total`
11. `bad_lines == bad_line_reason_total`
12. `rejects_entries == rejects 文件行数 == bad_lines + origin_quarantined`
13. `marker_validation_failures == 0`
14. `media_links_unresolved <= media_links`；`marker_reference_missing <= generated_records`

规则型标记：`model = null`、`models_available = false`、`rule_based = true`、
`score_fields_produced = false`、`subword_tokenizer = null`、`compressed = false`、`version_state_changed = false`。

其他关键统计：`blank_lines, empty_input, encoding_error, file_read_incomplete, media_links,
media_links_unresolved, multi_route_records, local_annotation_records, duplicate_identifier_records,
flagged_records,
routes_enabled, samples_file, missing_quality_policy, by_route, primary_by_route, *_by_reason, file_counts`。

### 4.1 运行状态

| 状态 | 条件 | 含义 |
|---|---|---|
| `success` | 守恒通过且 `generated_records > 0` | 正常产出 |
| `degraded` | 守恒通过、`generated_records == 0` 且 `pending_records > 0` | **没有 Token 产出是因为上游尚无质量结论**，不伪造结果 |
| `failed` | 守恒失败 / 有效记录为 0 且存在坏行 / `marker_validation_failures > 0` / 严格模式下存在待处理 | 需人工介入 |

`missing_quality_policy=failed` 为严格模式（投产前口径校验用），默认 `waiting_manual`
完全对应表1「还没有质量结论：不拦，留产线中，不生成Token」。

---

## 5. 接口返回形态（v4.2，血缘输出停用）

### 5.1 接口返回形态

- **形态一** `POST /predict?detection=generate_crossmodal_tokens`（`multipart/form-data`，`file` 必传）：
  `{"status":1,"token_records":"22_tokens.jsonl","samples_jsonl":"samples.jsonl","manifest":"manifest.json","sample_count":3}`；
  业务失败 `{"status":0,"msg":"..."}`。
- **形态二** `POST /predict?operatorId=m22`（`application/json`；`uniqueId` 与严格布尔 `isLastOperator` 必传）：
  `data = {"operator","status","outputFile","result"}`，其中 `result` 含
  `uniqueId`/`runDir`/`inputFile`/`outputFile`/`runId`/`statistics`/`startedAt`/`finishedAt`。
- `isLastOperator` 仅标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile`+`result`，
  不再用 `false` 只回血缘对象；链式调用取 `data.result.outputFile` 作为下一算子的 `inputFile`。
- 正式接口只有 `POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`；
  **不实现 `/downloadFile`**（访问 `/downloadFile` 返回 404）。

### 5.2 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

---

## 6. 配置

`configs/config.yaml`；未知键一律失败（`extra=forbid`）。请求级覆盖白名单仅
`enabled` / `routes` / `missing_quality_policy`。

| 键 | 默认 | 约束 |
|---|---|---|
| `operator.number` | 22 | 必须是 22，否则配置失败 |
| `operator.id` / `operator.name` | `crossmodal_token` | 固定 |
| `operator.version` | `v1.0.0` | 非空 |
| `operator.enabled` / `enabled` | `true` | **不能关**（置 false 配置失败），两者必须一致 |
| `transport.format` | `crossmodal-files-v1` | 固定，禁止改动字段语义 |
| `transport.max_response_bytes` | 67108864 | ≥ 1024 |
| `stage_id` | `S5` | 非空 |
| `exclude_isolated` | `true` | **必须 true**（隔离样本不进入样本文件） |
| `samples_file_name` | `samples.jsonl` | **固定**，改名即失败 |
| `markers.{image,audio,video}` | `<image>` / `<audio>` / `<video>` | 只读固定值 |
| `routes.image_text` | `true` | **必须 true**（默认图文） |
| `routes.audio_text` / `routes.local_annotation` | `false` | 可开启 |
| `routes.video_text` | `true` | v1.0.1 起出厂默认开启；显式 `false` 可关闭（视频样本转 `route_disabled` 待处理） |
| `missing_quality_policy` | `waiting_manual` | `waiting_manual` / `failed` |

环境变量：`OUTPUT_DIR`（输出根目录，默认算子目录下 `output/`）、`OPERATOR_CONFIG`（配置路径，
默认 `configs/config.yaml`）、`PORT`（默认 8122）、`OPERATOR_LOG_LEVEL`。

---

## 7. 待确认口径（本算子实现所取的解释）

1. 「按路线分文件写 samples.jsonl」——本实现取：`samples.jsonl` 为**全部已生成样本的并集**（固定名，
   满足表1 配置项 2 与「隔离样本不写入本文件」），同时按路线分文件写 `samples_<route>.jsonl`。
   若主 agent 认定 `samples.jsonl` 只应装默认图文路线，只需把 `samples.jsonl` 的写入源改为
   `outcome["routes"] == ["image_text"]` 一处（`inference._process`）。
2. 「文字里用什么标记模态」控件只读——本实现把三个标记值写成**强制校验**（改值即失败），
   并按默认显示执行；未做「任务级放开标记」的开关。
3. 「不能训练的样本单独列出」——本实现按**授权结论**判定，明确不可训练 → `22_untrainable.jsonl`
   且不进入 `samples.jsonl`；授权结论缺失按流程状态进 `22_pending.jsonl`（不隔离、不生成 Token）。
4. 「原文件仍只存链接」——本实现不搬运媒体，只登记 `object_key` 与 `link_status=reference_only`；
   demo 环境未接入真实对象存储，故 `object_storage_connected=false`。
5. 重复 `sample_id` 的输入不在表1 描述内，本实现按**原始隔离**登记
   （`duplicate_identifier`，计入 `origin_quarantined` 与 `rejects`），而不是静默去重。
6. 规则 §2.3「同一条全产线最多 2 次重跑，第 3 次锁定样本隔离库」——本算子**只尊重上游写下的
   显式锁定标记**（`locked` / `retry_locked` / `isolation_locked`，含 `metadata.*`），
   不自行按 `retry_count` 推断锁定阈值，避免与 #19/#21 的口径冲突；
   若主 agent 要求由 #22 按次数锁定，改动点是 `utils/upstream.py::resolve_isolation` 顶部。
7. 规则 §2.1/§2.3 把「授权不可训练」归为业务问题（去向样本隔离库），表1 #22 又要求
   「不能训练的样本单独列出」——本实现写 `22_untrainable.jsonl` 并标注
   `label_detail=样本-敏感`、`recommended_destination=sample_quarantine`，
   是否直接并入 #21 的样本隔离库由主 agent 决定（本算子不越权改写 #21 台账）。
