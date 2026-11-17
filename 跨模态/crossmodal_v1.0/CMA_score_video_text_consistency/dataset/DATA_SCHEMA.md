# 算子 14 数据契约（与 `inference.py` 实际输出一致）

- 算子：`video_text_scoring` / 正式编号 **14** / 阶段 **S3** / 版本 `v1.0.0`
- 输出根：`OUTPUT_DIR/<uniqueId>/runs/<runId>/`（**只写任务目录内**，不同 `uniqueId` 互不可见）
- 编码：UTF-8（无 BOM，`\n` 换行）；JSONL 全流程流式读写
- 分数语义：余弦相似度，保留 6 位小数
- 帧预算：`max_frames_per_segment`（1–8）是**整段视频合计**上限，多语义段共享该预算；
  0 帧段段分记无证据、不参与隔离判定（`frames_used` 恒 ≤ 上限）
- 本文件描述**当前实现真实产出**的字段；未产出的字段不在此声明。

---

## 1. 输入

入口为一条 `crossmodal-manifest-v1` 清单所指向的 JSONL（或素材包内唯一的 `.jsonl`）。
媒体（抽帧图像）以**相对素材包根**的路径引用：`frames/xxx.png`。

### 1.1 配对形态（`#11_生成候选配对` 产物）

```jsonc
{
  "pair_id": "pair-001",                 // 必需：业务标识（缺失 → 原始隔离区）
  "group_id": "grp-src-demo-A",
  "modality_pair": ["text", "video"],
  "evidence": {"type": "A", "criterion": "same_source_id", "value": {"source_id": "src-demo-A"}},
  "evidence_priority": 1,
  "left":  {"sample_id": "...", "modalities": ["text", "video"], "record": { /* 记录 */ }},
  "right": {"sample_id": "...", "modalities": ["text", "video"], "record": { /* 记录 */ }},
  "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0"}
}
```

**选侧规则**：含 `video` 的一侧为视频侧，另一侧为文本侧；两侧都有 `video` 时取 `left`。

### 1.2 扁平形态

同一记录内同时含 `video` 与 `text`，业务标识为 `sample_id`。

### 1.3 `video` 字段

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `path` | string | 视频引用 | 记入节点 `video_path`；**不解码** |
| `duration` | number | 否 | 秒 |
| `fps` | number | 否 | 仅信息性 |
| `frames[]` | array | 否 | 抽帧序列；**缺失/为空 → 待处理台账** |
| `frames[].path` | string | 是 | 相对素材包根；禁绝对/`..`/反斜杠/URL |
| `frames[].timestamp` | number | 否 | 秒；缺省则整段共享全部候选帧 |
| `frames[].frame_index` | integer | 否 | 仅信息性 |
| `segments[]` | array | 否 | 语义段；缺省则整视频为一段 |
| `segments[].start` / `.end` | number | 否 | 取帧区间 `[start, end)` |
| `segments[].text` / `.description` / `.transcript` / `.caption` | string | 否 | 段级文本，优先于记录级 |
| `segments[].frames[]` | array | 否 | 段自带帧，优先于整视频抽帧 |

`video` 也可为纯字符串路径（此时无抽帧 → 待处理台账）。

### 1.4 文本字段（按优先级）

`text.content` → `text.description` → `text.text` → `text.caption` →
`metadata.description` → `metadata.caption` → `metadata.title` →
`processing.<n>.description`（`<n>` 按字典序，跳过 `14`）。

节点 `description_source` 记录实际来源（如 `text.content`）。全部缺失 → 待处理台账。

---

## 2. 输出文件

| 文件 | 路线关闭 | 路线开启 | 说明 |
| --- | --- | --- | --- |
| `14_passthrough.jsonl` | ✅ 入口 | — | 原样透传 + `skipped` 节点 |
| `14_video_text_scores.jsonl` | — | ✅ 入口 | 通过记录 + 待处理记录 + 时序不适用透传记录 |
| `14_rejects.jsonl` | ✅ | ✅ | 原始隔离区（含读入阶段坏行） |
| `14_sample_quarantine.jsonl` | ✅（空） | ✅ | 样本隔离库（交 #21） |
| `14_not_applicable.jsonl` | ✅（空） | ✅ | 时序路线不适用台账（带 timeline 的记录不评分、不隔离） |
| `14_pending.jsonl` | ✅（空） | ✅ | 待处理台账 |
| `manifest.json` | ✅ | ✅ | `crossmodal-manifest-v1` |
| `frames/*.png` | 不产生 | ✅ | 被引用抽帧；同名不同内容追加 8 位摘要后缀 |
| `14_run.log` | ✅ | ✅ | `[时间] [动作] 真实数量` |

> 路线关闭时**不产生** `frames/`：表1 要求「关闭时不生成视文产物」。

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py`、`utils/mty_compliance.py` 的血缘/归档接口，代码保留存档；`utils/transport.py` 仅保留 `crossmodal-files-v1` 文件对象与 `manifest.json` 打包能力）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

---

## 3. 主输出行结构

```jsonc
{
  // …原始记录字段原样保留…
  "processing": {
    "14": { /* 视文节点，见 §4 */ }
  },
  "video_text_score": { /* 与 processing["14"] 同一对象 */ },
  "video_text_score_value": 0.4123   // 便捷标量（可为 null）
}
```

### 3.1 `skipped` 节点（路线关闭）

```jsonc
{
  "status": 1, "process_status": "skipped", "route_enabled": false, "passed": null,
  "reason": "route_disabled", "label": "流程-视文路线未启用",
  "action": "passthrough", "destination": "产线待处理",
  "video_text_score": null, "segment_mean": null, "min_segment_score": null,
  // 回填本次配置生效值（不再硬编码 0.25），示例为 configs/config.yaml 默认 0.20
  "low_score_threshold": 0.20, "segment_count": 0, "total_segments": 0,
  "scored_frames": 0, "frames_used": 0,
  "max_frames_per_segment": 8, "frames_within_budget": true,
  "segments_without_frames": [],
  "frame_budget_note": "整段视频合计最多 8 帧；未分配到帧的段记无证据、不参与隔离判定",
  "clip_model": "CLIP-ViT/B-32",
  "clip_model_version": null, "model": null, "segments": [], "degraded": false
}
```

---

## 4. 视文节点（`processing["14"]`）字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | integer | 记录级结论：1=通过/不隔离，0=过滤/隔离（`failed` 为 0，其余流程态为 1） |
| `process_status` | `passed` \| `failed` \| `pending` \| `degraded` \| `skipped` | 原字符串流程态（v4.2 起由 `status` 迁入本字段） |
| `route_enabled` | boolean | 本次运行是否开启路线 |
| `passed` | boolean \| null | 通过结论；未评分时为 `null`（**不使用 `false` 代替缺失**） |
| `reason` | string | 见 §6 原因表 |
| `label` | string | 见 §6 标签表 |
| `action` | `pass` \| `isolate` \| `origin` \| `pending` | 分流动作 |
| `destination` | string | `#19` / `样本隔离库` / `原始隔离区` / `产线待处理` |
| `video_text_score` | number \| null | **视文分数** = 全部被选中帧的余弦均值（不再是段均值的均值） |
| `segment_mean` | number \| null | 同 `video_text_score` 的别名（便于 #19/#16 读取） |
| `min_segment_score` | number \| null | **最低段分**（只统计有证据的段） |
| `low_score_threshold` | number | 本次生效阈值（配置默认 0.20） |
| `segment_count` | integer | 参与判定（有证据）的段数 |
| `total_segments` | integer | 语义段总数（含 0 帧段） |
| `scored_frames` | integer | 实际得到分数的帧数 |
| `frames_used` | integer | 被选中参与评分的帧数，**恒 ≤ `max_frames_per_segment`** |
| `max_frames_per_segment` | integer | 本次生效的**整段视频合计**帧预算（1–8） |
| `frames_within_budget` | boolean | `frames_used <= max_frames_per_segment`（证据自检） |
| `segments_without_frames` | integer[] | 0 帧段序号（该段段分记无证据、不参与隔离判定） |
| `frame_budget_note` | string | 帧预算与 0 帧段口径说明 |
| `clip_model` | string | 固定 `CLIP-ViT/B-32` |
| `clip_model_version` | string \| null | 权重文件名 |
| `model` | object \| null | **真实模型身份**；未使用模型时严格为 `null` |
| `segments[]` | array | 段明细，见 §5 |
| `video_path` | string \| null | 视频引用路径（不解码） |
| `description_source` | string \| null | 描述字段来源 |
| `degraded` | boolean | 是否显式降级 |
| `scored_at` | string | ISO8601（+08:00） |
| `failing_segments` | integer[] | 仅不通过样本：低于阈值的段序号 |
| `detail` | string | 仅异常/降级样本：可读原因（不含原文与堆栈） |

### 4.1 `model` 对象（真实身份，非伪造）

```jsonc
{
  "name": "CLIP-ViT/B-32", "arch": "ViT-B-32",
  "version": "ViT-B-32.pt",
  "library": "open_clip", "weights": "<算子目录>/weight/clip/ViT-B-32.pt",
  "weights_sha256": "9f1c2ab34d5e6f78",        // 16 位前缀；>64MB 时为 null
  "device": "cpu", "batch_size": 8, "embed_dim": 512,
  "degraded": false, "real_backend": true,
  "untrained_architecture_only": false,         // true 表示只构建架构、未载权重（不可用于评分）
  "score_semantics": "cosine", "test_double": false
}
```

---

## 5. 段明细 `segments[]`

```jsonc
{
  "index": 0,
  "start": 0.0, "end": 2.0,                    // 无分段时为 null
  "text_source": "video.segments[0].text",     // 无描述时为 null
  "text_chars": 17, "text_truncated": false,
  "frame_quota": 4,                            // 该段分到的帧配额（整段视频预算的一部分）
  "frame_count": 4, "scored_frames": 4,
  "segment_mean": 0.5144,                      // 段内被选中帧的分数均值；0 帧段为 null
  "passed": true,                              // segment_mean >= threshold；0 帧段为 null
  "has_evidence": true,                        // 0 帧段为 false
  "no_evidence_reason": null,                  // 0 帧段：frame_quota_zero / no_candidate_frames
  "frames": [{"path": "pair-002_0001.png", "score": 0.501, "timestamp": 0.0}]
}
```

`frames[].path` 为**文件名**（运行目录内 `frames/` 下的相对名）；
`score` 为该帧与段文本的 CLIP 余弦相似度；`timestamp` 取自输入抽帧（可为 `null`）。

---

## 6. 台账条目结构（`14_rejects.jsonl` / `14_sample_quarantine.jsonl` / `14_pending.jsonl`）

```jsonc
{
  "line": 6,                                   // 输入文件行号（1 起）
  "stage": "14", "operator": "video_text_scoring", "operator_version": "v1.0.0",
  "sample_id": "flat-006", "pair_id": null, "batch_id": "batch-demo",
  "retry_count": 0,
  "reason": "frame_missing", "label": "原始文件-无法解析",
  "action": "origin", "destination": "原始隔离区",
  "event_time": "2026-10-06T17:34:18+08:00",
  "video_path": "video/flat-006.mp4",
  "video_text": { /* 视文节点（§4） */ },
  "record": { /* 仅 pass / isolate 携带完整记录，作为隔离证据 */ }
}
```

### 6.1 读入阶段（`14_rejects.jsonl`）原因

| `reason` | `label` | 去向 |
| --- | --- | --- |
| `json_parse_error` | 原始文件-无法解析 | 原始隔离区 |
| `record_not_object` | 原始文件-无法解析 | 原始隔离区 |
| `missing_field: sample_id` | 原始文件-缺字段 | 原始隔离区 |
| `duplicate_identifier` | 原始文件-缺字段 | 原始隔离区 |
| `encoding_error` | 原始文件-编码异常 | 原始隔离区 |

### 6.2 业务判定原因

| `reason` | `label` | 去向 | 是否隔离 |
| --- | --- | --- | --- |
| `passed` | 样本-评分：通过 | `#19` | 否 |
| `segment_below_threshold` | 样本-语义不通 | 样本隔离库（#21） | ✅ |
| `frame_missing` | 原始文件-无法解析 | 原始隔离区 | 否 |
| `frame_unreadable` | 原始文件-损坏 | 原始隔离区 | 否 |
| `unsupported_media_path` | 原始文件-无法解析 | 原始隔离区 | 否 |
| `no_video_text_pair` | 流程-无视频文本对 | 产线待处理 | 否 |
| `no_video_frames` | 流程-无视频帧 | 产线待处理 | 否 |
| `missing_description` | 流程-待补描述 | 产线待处理 | 否 |
| `model_unavailable` | 流程-待评分 | 产线待处理 | 否 |
| `score_error` | 流程-待评分 | 产线待处理 | 否 |
| `temporal_route_not_applicable` | 流程-时序路线不适用视文评分 | 主产物 + `14_not_applicable.jsonl`（判据交 #15） | 否 |

---

## 7. 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

运行级 `status`（内部契约）仍为 `success`（全部处理完）/ `degraded`（显式降级）/ `failed`（有效记录为 0 且存在异常行），可在 `result.status` 与 `manifest.json` 读到；记录级 `processing["14"]` 使用 int `status` + `process_status`。

---

## 8. `statistics` 字段与守恒

```jsonc
{
  "route_enabled": true,
  "input_records": 7, "valid_records": 7, "blank_lines": 0, "bad_lines": 0,
  "passed_records": 0, "sample_isolated": 0, "origin_quarantined": 1,
  "pending_records": 6, "skipped_records": 0, "scored_records": 0,
  "segment_below_threshold": 0,
  "no_video_text_pair": 1, "no_frames": 1, "missing_description": 1,
  "model_unavailable": 3, "score_error_records": 0,
  "frames_carried": 16, "frames_missing": 1, "media_carried": 16, "media_missing": 1,
  "models_available": false, "model_source": null, "clip_model": "CLIP-ViT/B-32",
  "low_score_threshold": 0.20, "max_frames_per_segment": 8,
  "degraded": true, "encoding_error": false, "file_read_incomplete": false,
  "empty_input": false,
  "conservation_ok": true, "conservation_failures": []
}
```

**守恒等式**（任一不成立则 `conservation_ok=false` 并列出 `conservation_failures`）：

1. `input_records == valid_records + bad_lines`
2. `valid_records == origin_quarantined + sample_isolated + pending_records + passed_records + skipped_records`
3. `sample_isolated == segment_below_threshold`
4. `scored_records == passed_records + segment_below_threshold`
5. `pending_records == no_video_text_pair + no_frames + missing_description + model_unavailable + score_error_records`

所有计数均为**业务记录条数/帧数**，绝非文件字节数。

---

## 9. 接口返回（共享契约 §4/§5）

```jsonc
{"code": 0, "msg": "…", "data": { … }}
```

- `POST /uploadFile?uniqueId=` → `data = {uniqueId, entrypoint, files[], mediaFiles[], sizeBytes}`
- `POST /predict`（形态一，`multipart/form-data` + `?detection=`）→ `{"status": 1, "uniqueId", "pair_id", "segment_scores", "decision", "model_version", "process_status"}`；失败 `{"status": 0, "msg"}`
- `POST /predict`（形态二，`application/json`）→ `data = {operator, status, outputFile(crossmodal-files-v1), result{uniqueId, runId, entrypoint, status, message, statistics}}`；`isLastOperator` 仅作阶段末标记，中间/阶段末响应同构
- `POST /removeFile?uniqueId=` → `data = {uniqueId, removed}`
- 形态二失败：`code=1`，`msg` 前缀为异常类名（`InputError` / `ConfigError` / `ModelUnavailable` / `TaskBusyError`），参数校验失败为 HTTP 422
- **不存在** `/downloadFile`（返回 404）
