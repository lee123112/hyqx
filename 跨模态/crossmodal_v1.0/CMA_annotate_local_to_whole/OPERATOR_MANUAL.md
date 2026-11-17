# 算子说明书 · 17 标注局部对整体

> 文档版本：v1.0.0　|　对应表1：数据加工环节 · #17　|　阶段：S3　|　默认：关闭　|　必选性：可选　|　优先级：P1

## 1. 表1 口径对照

| 表1 字段 | 口径 | 本实现落点 |
|---|---|---|
| 流程环节 | 3 数据加工 | `stage_id: S3`（与 demo 既有 S3 算子一致） |
| 开关 | 默认关闭 | `configs/config.yaml: enabled: false`；`operator.settings` 留档冻结设置 |
| 是否必须 | 可选 | 关闭路线完整可用（原样透传、不隔离、不产框） |
| 优先级 | P1 | 不阻塞上游；资源不可用时优先显式降级/待人工 |
| 模型/后端 | YOLOv8n（仅通用类别预填；关闭时不加载） | `utils/detectors.py::Yolov8nBackend`；关闭路线 `_prepare_detector` 直接返回、不加载 |
| 旧目录关系 | 旧 `14_半自动化候选标注` 含历史混合覆盖，本目录正式单列 #17 | 只读参考历史目录，未改动旧目录 |

**表1 研发测关键技术和边界**（逐条实现）：
1. 图像仅保留 **YOLOv8n 官方通用类别（COCO-80）** 且 **与描述可对上** 的框 → `filter_general_boxes`；
2. **专用设备不预填**，由标注员手画 → 非 COCO-80 类别名不进入候选；
3. 音视频 **沿用转写起止时间预填**，转写按规范 §4.17「音视频引用 M08 时间索引」优先读
   **M08 的 `time_index`**；无声视频（M08 判 `no_audio_track`）退回 M08 的
   **`visual_time_index`**（视觉分段，来源 `m08_visual_index`，时间段 `source=visual_segment`，
   无文本）；再其次记录内联转写，最后 sidecar（`transcript_file` 等）
   → `build_time_ranges`（`video_timestamp_source: transcript` 冻结），
   `source_reference.transcript_source` 标记实际来源（`m08_time_index` / `m08_visual_index` /
   `inline` / sidecar 路径）；
4. **不得部署 Grounding DINO、不训练专用检测模型** → 无相关依赖与代码路径；
5. **不做词-区域一一对应** → 只做「类别名/别名是否出现在描述里」的词边界匹配
   （英文按整词及单/复数形式、中文按子串，见 `detectors.class_matches_description`），输出不含词-区域配对。

## 2. 处理流程

1. **输入校验**：`uniqueId` 合法性 → 入口（manifest/唯一 JSONL）→ 编码（严格 UTF-8）→ 必需字段 → 路径安全。
2. **模态与适用性**：声明模态与媒体引用共同决定 `applicable`；纯文本/无图像无音视频 → `not_applicable`（留样本池、不隔离）。
3. **整体对齐前置**：`require_whole_aligned=true` 且上游整体对齐为否定（`not_aligned` 等）→ `not_applicable`。
4. **候选预填**：
   - 图像：YOLOv8n 推理 → 归一化 xyxy →「通用类别 ∩ 描述」过滤（记录丢弃数）；
     匹配按 COCO-80 中英文别名表做**词边界**判定（英文 `woman` 不会命中 `business`，
     `kid` 命中 `kids`；中文别名如 `人/工人/行人` 命中 `person`），命中不到描述即丢弃；
   - 音视频：**先取 M08 `time_index`** 的转写起止时间，缺失时退回 M08
     `visual_time_index`（无声视频的视觉分段）→ 内联转写 → sidecar
     → 时间段（真实时间，无任何打分）；四处均无时间轴才记 `no_transcript_time_axis`。
5. **人工确认判定**：无证据或未确认 → 待人工；已确认但标注不完整 → 样本隔离库；已确认且完整 → 可交付。
6. **产物书写**：保留原字段，追加 `local_annotations`、`operator_17`、`processing["17"]`；旁路清单与主输出分别落盘。
7. **统计与日志**：`run.log` + `manifest.json` + 真实计数 + 守恒校验（v4.2 起不再写 `lineage.json` / `stage_context.json`）。

## 3. 配置项（`configs/config.yaml`）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 表1 冻结：默认关闭 |
| `detector_backend` | `auto` | `auto`（本地权重缺失→按 `allow_degraded`）/ `yolov8n`（强制真实后端，缺失即显式失败）/ `none`（不加载） |
| `model_name` | `YOLOv8n` | 仅接受 YOLOv8n 及其别名，其他模型名直接配置错误 |
| `detector.weights` | `YOLOv8n/yolov8n.pt` | 本地权重路径（文件或含 `yolov8n.pt` 的目录），相对 `<本算子目录>/weight` 或 `MODEL_ROOT` 解析；**绝不下载** |
| `detector.device` | `cpu` | 部署口径，请求侧不可改写 |
| `detector.conf_threshold` / `iou_threshold` / `max_boxes` / `image_size` | `0.25` / `0.45` / `50` / `640` | 可在请求 `config` 内微调 |
| `detector.general_classes_only` | `true` | **冻结为 true**（专用设备不预填） |
| `allow_degraded` | `true` | `true` 显式降级（零框）；`false` 显式失败 |
| `require_whole_aligned` | `true` | 整体对齐为否定时判 `not_applicable` |
| `video_timestamp_source` | `transcript` | **冻结** |
| `max_text_chars` / `max_time_ranges` / `max_images_per_record` | `500` / `50` / `4` | 截断与容量上限，不改变判定口径 |
| `transport.format` / `max_response_bytes` | `crossmodal-files-v1` / `67108864` | demo 约定 |

请求侧白名单：`enabled`、`allow_degraded`、`require_whole_aligned`、`max_text_chars`、
`max_time_ranges`、`max_images_per_record`、`detector.{conf_threshold,iou_threshold,max_boxes,image_size}`。
其余键（含任何 `weights`/`device`/`detector_backend`/路径）一律拒绝。

## 4. 状态、标签、动作、去向

| 路线 | process_status | 质量标签 | action | destination | 可交付 | 旁路文件 |
|---|---|---|---|---|---|---|
| 人工确认完成 | `confirmed` | `样本-局部标注：已人工确认` | `keep` | `sample_pool` | 是 | — |
| 未人工确认 | `pending_manual_review` | `流程-局部标注：待人工确认` | `waiting_manual` | `manual_queue` | 否 | `17_pending.jsonl` |
| 已确认但标注不完整 | `incomplete` | `样本-语义不通` | `isolate` | `sample_quarantine` | 否 | `17_sample_quarantine.jsonl` |
| 模态不适用 | `not_applicable` | `流程-局部标注不适用` | `keep` | `sample_pool` | 否 | — |
| 整体对齐未对齐 | `not_applicable` | `流程-局部标注不适用` | `keep` | `sample_pool` | 否 | — |
| 工具关闭 | `skipped` | `流程-工具关闭：原样透传` | `keep` | `sample_pool` | 否 | — |
| 原始文件/字段/编码/媒体 | — | `原始隔离-无法解析 / 缺关键字段 / 字段非法 / 路径不安全 / 文件损坏` | `isolate` | `origin_quarantine` | 否 | `17_rejects.jsonl` |

记录级 `processing["17"].status` 为 v4.2 int（1=正常流程态/待人工，0=隔离/失败），上表
`process_status` 列为原字符串值。

原因码（`reason_code`）示例：`human_confirmed`、`annotation_pending`、`annotation_incomplete`、
`not_applicable_modality`、`whole_alignment_not_aligned`、`no_general_class_match`、
`detector_unavailable`、`no_transcript_time_axis`、`route_disabled`、`json_parse_error`、
`missing_sample_id`、`image_missing`、`image_unreadable`、`transcript_sidecar_missing`、
`transcript_unreadable`、`transcript_invalid`、`human_annotation_invalid`、
`too_many_time_ranges`、`media_path_unsafe`。

## 5. 失败与降级

| 情形 | 行为 |
|---|---|
| 权重未配置 / 找不到 / 库缺失 / 加载失败 且 `allow_degraded=true` | `status=degraded`、`detector.degraded=true`、**零框零时间戳**、记录进 `pending_manual_review`（原因含具体缺项） |
| 同上但 `allow_degraded=false` 或 `detector_backend=yolov8n` | 显式失败：`code=1`，不产出任何结果文件 |
| 推理期异常 | 显式失败（不降级为假结果） |
| 坏行 / 非对象记录 | 计入 `bad_lines`，写 `17_rejects.jsonl`（带行号），继续处理后续行 |
| 媒体缺失 / 不可解码 / 转写 sidecar 不可读 | 原始隔离，并计数 `media_missing` / `images_unreadable` |
| 空输入 | `empty_input=true`、`code=0`，`statistics` 明确说明，不当作崩溃 |
| 同一 `uniqueId` 并发 | `409` + `task_busy`（锁文件在任务目录之外） |

## 6. 质量门禁

- **人工确认是唯一交付前提**：`human_verified` 由输入证据显式给出，算子不自行置真。
- **预填 ≠ 确认**：所有预填框 `human_verified=false`、`source=yolov8n`，并单独记录在 `prefilled`/`candidate_boxes`。
- **不完整不交付**：`incomplete` 只写样本隔离台账，主输出不含该记录。
- **守恒**：`statistics.conservation_ok` 为 true 才会是 `success`/`partial` 的正常结果路径；
  校验项见 `dataset/DATA_SCHEMA.md` §4。
- **可追溯**：每条旁路台账均带 `uniqueId`、`runId`、`line_no`、`event_time`、
  `source_reference`（`表1 #17 标注局部对整体（跨模态 demo_10.6）`）。
