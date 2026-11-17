# 算子 #17 标注局部对整体 —— 数据契约（DATA_SCHEMA）

> 口径来源：表1 #17 行、`DEVELOPMENT_PLAN.md`、`TECHNICAL_DESIGN.md`、
> `demo/DEVELOPMENT_CONTRACT.md`（阶段 S3 数据加工中间节点）。
> 本文只描述本算子**实际读写**的字段；未知扩展字段一律原样保留。

## 1. 输入

### 1.1 入口

- 单个 UTF-8 JSONL（每行一个 JSON 对象），入口由上传包的 `manifest.json` 指定（默认 `demo.jsonl`）。
- 媒体（`media/*`）与转写 sidecar 以**相对入口清单根**的路径引用；禁用绝对路径、`..`、反斜杠逃逸与 URL。
- 编码严格 UTF-8，不做隐式猜测；非法编码/超长行按「原始隔离」处理。

### 1.2 记录字段

| 字段 | 必需 | 说明 |
|---|---|---|
| `sample_id` / `sampleId` / `id` | 是 | 样本标识；缺失 → 原始隔离（`missing_sample_id`） |
| `modality` / `modalities` | 否 | 如 `["image","text"]`、`["audio","text"]`；用于判定适用性 |
| `text.content` / `metadata.description` / `description` / `caption` | 否 | 描述文本；用于「通用类别 ∩ 描述」匹配与 `max_text_chars` 截断 |
| `image` / `images` / `image_path` / `imagePaths` | 条件 | 图像引用；可为字符串或 `{"path"\|"file"\|"uri"\|"src"}` |
| `video` / `videos` / `video_path` / `videoPaths` | 条件 | 视频引用 |
| `audio` / `audios` / `audio_path` / `audioPaths` | 条件 | 音频引用 |
| `transcript` / `transcript_segments` / `transcript_data` | 否 | 内联转写：`[{start,end,text}]` 或 `{segments:[...]}` 或单段 `{start,end}` |
| `transcript_file` / `transcript_path` / `transcriptFile` | 否 | 随包 sidecar JSON（同上结构） |
| `human_annotations` / `human_confirmation` / `local_human_annotations` | 否 | 人工确认证据（见 1.3） |
| `alignment` | 否 | 上游整体对齐结论，如 `{"whole":{"status":"aligned\|not_aligned"}}` |
| `processing` | 否 | 上游命名空间；本算子**只写** `processing["17"]`，不覆盖其他键 |
| 其他（`batch_id`、`qc`、`metadata` 等） | 否 | 原样保留并随主输出透传 |

### 1.3 人工确认证据

```json
{
  "human_verified": true,
  "complete": true,
  "annotator": "annotator-01",
  "boxes": [
    {"id": "b1", "label": "person",
     "bbox": [0.10, 0.20, 0.40, 0.75]}
  ],
  "time_ranges": [
    {"id": "r1", "start_sec": 0.0, "end_sec": 1.2, "text": "设备开始运转"}
  ]
}
```

- `boxes[].bbox` 支持 `[x_min,y_min,x_max,y_max]`、`{x_min,y_min,x_max,y_max}`、`{x,y,w,h}`，
  一律要求**归一化且落在 [0,1] 内、且 x_min<x_max、y_min<y_max**，否则原始隔离。
- `time_ranges[]` 要求 `0 <= start_sec < end_sec`。
- `human_verified`/`complete` 必须是真正的布尔值，其他类型 → 原始隔离。
- **只有** `human_verified=true` 且标注完整（图像有框、音视频有时间段，或 `complete` 未置 false）
  才可交付；否则只能 `pending_manual_review` 或样本隔离。

## 2. 输出

### 2.1 运行目录（`OUTPUT_DIR/<uniqueId>/<operator_id>/<runId>/`）

| 文件 | 角色 | 说明 |
|---|---|---|
| `17_local_annotations.jsonl` | main（开启） | 开启路线主输出（保留全部有效记录并追加本算子命名空间） |
| `17_passthrough.jsonl` | main（关闭） | 关闭路线主输出（原样透传，零框零时间戳） |
| `17_pending.jsonl` | pending | 待人工确认旁路副本（与主记录不重复计数） |
| `17_sample_quarantine.jsonl` | sample_quarantine | 样本隔离台账（标签 `样本-语义不通`，交 #21） |
| `17_rejects.jsonl` | review | 原始隔离/坏行台账 |
| 搬运的媒体 / sidecar | media | 与输入相对路径一致；缺失媒体从活跃引用移除并计数 |
| `manifest.json` | sidecar | `crossmodal-manifest-v1`，列出**实际传递**的文件 |
| `run.log` | 本地控制 | 逐行结构化运行日志（`run_start`/`run_end`/逐条路线） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py`、`utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.2 主输出记录追加字段

```jsonc
{
  // ...原有字段全部保留...
  "local_annotations": {
    "operator_number": 17,
    "operator_version": "v1.0.0",
    "annotation_type": "local-to-whole",
    "status": 1,
    "process_status": "confirmed",
    "decision": "confirmed",
    "deliverable": true,
    "human_verified": true,
    "confirmed_by": "annotator-01",
    "coordinates": "normalized-xyxy",
    "boxes": [ /* 人工确认框（归一化 xyxy） */ ],
    "time_ranges": [ /* 人工确认时间段 */ ],
    "prefilled": {
      "boxes": [ /* YOLOv8n 通用类别 ∩ 描述 的候选框（human_verified=false） */ ],
      "time_ranges": [ /* 转写起止时间预填 */ ],
      "box_count": 1,
      "time_range_count": 2,
      "confirm_required": false
    },
    "candidate_count": 3,
    "confirmed_count": 1,
    "matched_classes": ["person"],
    "dropped_boxes": 1,
    "detector": { /* 真实后端状态；degraded/real_backend/reason 见下 */ },
    "modality": ["image", "text"],
    "description_used": true,
    "transcript_source": "m08_time_index|m08_visual_index|inline|sidecar|null",
    "reason_code": "human_confirmed",
    "label": "样本-局部标注：已人工确认",
    "action": "keep",
    "destination": "sample_pool",
    "review_required": false,
    "event_time": "2026-10-07T10:00:00+08:00",
    "algorithm": "yolov8n_general_prefill_v1+transcript_time_prefill_v1"
  },
  "operator_17": { /* 精简证据视图：status/process_status/decision/reason_code/evidence */ },
  "processing": {
    "17": {
      "status": 1,
      "process_status": "confirmed",
      "deliverable": true,
      "boxes_source": "human_confirmed",
      "regions": [ /* 下游 #18 直接消费的局部框（人工确认优先） */ ],
      "boxes": [ /* 同 regions */ ],
      "local_regions": [ /* YOLOv8n 预填候选 */ ],
      "candidate_boxes": [ /* 同 local_regions */ ],
      "confirmed_boxes": [ /* 人工确认框 */ ],
      "time_ranges": [ /* 确认时间段 */ ],
      "prefilled_time_ranges": [ /* 转写预填时间段 */ ],
      "path": "media/demo.png",
      "paths": {"image": "media/demo.png", "images": ["media/demo.png"],
                "video": null, "videos": [], "audio": null, "audios": [],
                "transcript": null},
      "modality": ["image", "text"]
    }
  }
}
```

> 记录级 `status` 为 v4.2 int（1=正常流程态/待人工，0=隔离/失败），原字符串迁入
> `process_status`；`local_annotations`、`operator_17` 与 `processing["17"]` 同构。

### 2.3 下游 #18 消费的框格式

`regions`/`boxes`/`candidate_boxes`/`confirmed_boxes` 统一为**归一化 xywh**：

```json
{"box_id": "box-001",
 "bbox": {"x": 0.10, "y": 0.20, "w": 0.30, "h": 0.55, "coord": "xywh-normalized"},
 "class_name": "person",
 "confidence": 0.91,
 "source": "yolov8n",
 "human_verified": false}
```

- `regions` = 人工确认框优先，否则预填候选；`boxes_source` 取值 `human_confirmed|yolov8n_prefill|none`。
- `candidate_boxes`、`confirmed_boxes` 单列，**任何预填框的 `human_verified` 恒为 `false`**。
- 专用设备类别不由本算子预填（留给标注员手画），因此不会出现在 `candidate_boxes` 中。

## 3. 路线、标签与去向

| 路线 | process_status | 质量标签 | action | destination | 主输出 | 旁路 |
|---|---|---|---|---|---|---|
| 人工确认完成 | `confirmed` | `样本-局部标注：已人工确认` | `keep` | `sample_pool` | 是（可交付） | — |
| 未人工确认/证据未确认 | `pending_manual_review` | `流程-局部标注：待人工确认` | `waiting_manual` | `manual_queue` | 是（不可交付） | `17_pending.jsonl` |
| 已确认但标注不完整 | `incomplete` | `样本-语义不通` | `isolate` | `sample_quarantine` | **否** | `17_sample_quarantine.jsonl` |
| 模态不适用 / 整体对齐为否定 | `not_applicable` | `流程-局部标注不适用` | `keep` | `sample_pool` | 是 | — |
| 工具关闭 | `skipped` | `流程-工具关闭：原样透传` | `keep` | `sample_pool` | 是 | — |
| 原始文件/字段/编码问题 | — | `原始隔离-*` | `isolate` | `origin_quarantine` | 否 | `17_rejects.jsonl` |

`not_applicable` 与 `skipped` 都是**主输出子集**，不计失败；`incomplete` 只进样本隔离台账，
不留在可交付链上，也不计入主输出。

## 4. 统计口径（真实计数，来自实际文件）

`statistics` 至少包含：`input_records`、`blank_lines`、`valid_records`、`bad_lines`、
`confirmed_records`、`pending_records`、`incomplete_records`、`not_applicable_records`、
`skipped_records`、`origin_quarantined`、`main_records`、`pending_sidecar_entries`、
`sample_quarantine_entries`、`boxes_prefilled`、`boxes_confirmed`、`boxes_dropped_unmatched`、
`time_ranges_prefilled`、`time_ranges_confirmed`、`media_carried`、`media_missing`、
`conservation_ok`、`conservation`。

守恒校验（`conservation_ok`）必须同时成立：

1. `input_records == valid_records + bad_lines`
2. `valid_records == confirmed + pending + incomplete + not_applicable + origin_quarantined + skipped`
3. `rejects_entries == bad_lines + origin_quarantined`
4. `main_records == 主输出应保留的路线数`
5. `pending_sidecar_entries == pending_records`

「一条记录多个框/时间段」的数量只体现在 `boxes_*` / `time_ranges_*`，**不冒充样本数**。

## 5. 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

## 6. 样例

`dataset/sample/`：`demo.jsonl`（8 条，覆盖全部路线）、`media/*`（真实可解码 PNG/WAV）、
`manifest.json`、`make_sample.py`（可复现重建全部样例）。
