# 算子 8 数据说明（带时间索引的语音转写）

> 契约优先级：`../DATA_SCHEMA.md`（根级，冻结字段）> `../DEVELOPMENT_CONTRACT.md`（接口行为）
> > `TECHNICAL_DESIGN.md`（本算子设计）> 本文（落地细化）。参考算子 02 与平行语料 04/06
> 的代码含旧接口，仅作工程结构参考，字段与接口一律以根级文档和本文为准。

> **[v1.0.1 收缩]** 按表1 #8 边界，本算子只产出 **ASR 段落单元**（中文转写 +
> `start_ms`/`end_ms` + 音频片段 + 时间索引）。**不再组对、不再生成 `semantic_unit`**
> （字段固定 `null`，deprecated）；跨模态配对的 `semantic_units` 由 #12–#15 生成。

## 1. 范围与数据来源

- 输入：算子 02—06 处理后的**完整中间记录清单**，或（形态一/CLI）单音频、带音轨视频。
  转写来源：`audio` 字段，或**带音轨的 `video`**（前者优先）。
- 输出：跨模态候选清单 `07_candidates.jsonl` + 异常清单 `07_rejects.jsonl` +
  待补清单 `07_supplement_requests.jsonl`（只登记缺模态，不回采）+ 被引用媒体 +
  运行清单（`manifest.json`）。
- 本算子**不做跨模态推断**：不产生相似度/对齐分数，不裁剪或重编码视频/音频（仅按段落
  裁出 16kHz 单声道 WAV 片段）。
- 开关关闭时不 import/初始化/加载 Whisper（日志写 `whisper_not_loaded`）；打开后固定
  `whisper-base`、固定中文解码，不换更大模型。

## 2. 输入记录契约（post-06 形态）

每条记录为一行 JSON 对象，字段来源：

| 字段 | 来源 | 07 的处理 |
| --- | --- | --- |
| `sample_id` | 02 分配 | 唯一键；缺失/空/重复 → `07_rejects.jsonl` |
| `text` | 03 | `null`、字符串或对象 `{content, original, tokens, token_offsets, lang}`；仅用于模态统计 |
| `image` | 04 | `null`、路径字符串或对象；**不再校验尺寸/格式** |
| `video` | 05 | `null`、路径字符串或对象；**不再校验时长/帧时间戳**（带音轨时作为 ASR 来源） |
| `audio` | 06 | `null`、路径字符串或对象；**不再校验采样率/段区间**（作为 ASR 来源） |
| `processing` | 各算子 | 只增写 `processing["07"]`，其余节点结果原样保留 |
| `alignment` | 预留 | 缺失时初始化为 `{"whole": null, "local": []}`，已有值不覆盖（#17 会读） |
| `qc` | 04/05 等 | 缺失时初始化为 `{}`，已有值不覆盖 |
| `metadata` / 其他未知字段 | 上游/自定义 | **原样保留**（深拷贝透传，不裁剪不重排语义） |

记录级拒绝只剩：`sample_id` 缺失/空/重复、JSON 不可解析、媒体文件不存在/路径越界。
模态字段是路径字符串或元数据不全**不再拒绝**——缺字段由 #8 记录流程态（`not_applicable`
/ `no_speech` / `failed`），必要时登记待补清单。

## 3. 主清单 `07_candidates.jsonl`

一行一条，**完整记录透传 + 仅增写下列字段**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `semantic_unit` | `null` | **[deprecated]** 固定 `null`，绝不生成；此前的 `"<sample_id>:unit:0001"` 已废弃 |
| `alignment` | object | 上游已有则保留，缺失补 `{"whole": null, "local": []}`（#17 消费） |
| `qc` | object | 上游已有则保留，缺失补 `{}` |
| `processing["07"]` | object | 本节点结果，见下 |
| `transcription` | object | 转写载荷（有 ASR 来源时才写）；`status` 为产出态 |
| `audio_transcript` | `string \| null` | 中文转写全文；无文本/失败时为 `null`，绝不回填占位文本 |
| `time_index` | array | ASR 段落单元，见 §3.1；无段落时为 `[]` |
| `visual_time_index` | array | 无声视频的视觉兜底时间轴（`source=visual_frame` / `visual_segment`） |
| `transcription_gate` | object | `{transcript_required, transcript_available, direct_scoring_allowed, reason}` |
| `candidate_description_required` | bool | 转写失败/无语音/无音轨时为 `true`（进候选描述） |
| `missing_modality` | array | **仅缺模态时出现**，如 `["audio"]`（视频无音轨）；同时登记待补清单 |

`processing["07"]` 结构：

```json
{
  "status": 1,
  "process_status": "transcribed | skipped | not_applicable | no_speech | failed",
  "reason": null,
  "metrics": {
    "present_modalities": ["text", "image"],
    "media_references": 2,
    "asr_units": 0,
    "missing_modality": []
  }
}
```

`status` 为 int：**1 = 正常流程态**（含 `not_applicable`/`skipped`/`no_speech`），
**0 = 仅 `failed`**；`process_status` 承载记录级流程态。

### 3.1 ASR 段落单元 `time_index`

```json
{
  "index": 1,
  "start": 0.0,
  "end": 1.84,
  "start_ms": 0,
  "end_ms": 1840,
  "text": "这一段的中文转写",
  "segment_path": "06_audio/segments/xxx_0001.wav"
}
```

- `start_ms` / `end_ms` 为**整数毫秒**、左闭右开 `[start, end)`；由秒 ×1000 四舍五入
  （`floor(x*1000+0.5)`）得到，并保证按索引**单调、相邻不重叠**（重叠时后段起点前移到
  前段终点；等长时终点至少 +1ms）。
- `start` / `end` 秒字段**保留一版**给现有消费方（#17 优先读 ms，缺失时回落秒）。
- 每个条目必须有对应 `segment_path`（16kHz 单声道 PCM WAV，位于 `06_audio/segments/`）；
  裁片失败时不写 `segment_path`，但不得只写片段不写文本或反之。
- `transcription` 载荷固定含 `model` 与 `model_version`（同值 `whisper-base`）、
  `language: "zh"`；不伪装自动语种识别。
- `transcription.precheck`：正常转写路径为 `null`；预检命中时为
  `{enabled, duration_sec, snr_db, frame_cv, min_duration_seconds, min_snr_db}`
  （静音样本 `snr_db`/`frame_cv` 为 `null`），此时 `transcription.duration`
  同样写音频时长。`frame_cv` 是 20ms 帧 RMS 的波动系数（std/mean），
  `low_snr` 仅在 `frame_cv < 0.10`（接近稳态）时成立。

### 3.2 记录级分流

| 条件 | process_status | reason | 去向/附加 |
| --- | --- | --- | --- |
| 有音频/音轨且转写成功 | `transcribed` | `null` | 主清单，`direct_scoring_allowed=true` |
| 总开关或转写开关关闭 | `skipped` | `master_switch_off` / `transcription_disabled` | 主清单透传，`time_index=[]`，不加载模型 |
| 无音频来源（图片/纯文本） | `not_applicable` | `no_audio_input` | 主清单透传（不写 `transcription` 字段） |
| 视频无音轨 | `not_applicable` | `no_audio_track` | 主清单 + 视觉时间索引 + **待补清单** `missing_modality=["audio"]` |
| 有音轨但空转写 | `no_speech` | `empty_transcript` | 主清单，`candidate_description_required=true`，不伪造文本 |
| 送 ASR 前预检命中 | `no_speech` | `audio_silent` / `audio_too_short` / `low_snr`（低 SNR **且**稳态） | 主清单，`candidate_description_required=true`，不加载模型；`transcription.precheck` 写时长、SNR 与波动系数证据 |
| 解码失败/模型缺失/后端不可用 | `failed` | `audio_decode_failed` / `whisper_weights_missing` / … | 主清单，不能直接评分，**不重试、不联网** |
| `sample_id` 缺失/空、非非空字符串 | — | `invalid_record` | 异常清单 |
| `sample_id` 重复（同一素材内） | — | `duplicate_sample_id` | 异常清单（保留首条） |
| 引用的媒体文件不存在 | — | `media_missing` | 异常清单（`evidence.media_errors` 列出全部缺失路径） |
| 媒体路径越出运行目录或扩展名不受支持 | — | `unsupported_media_path` | 异常清单 |

边界：不换更大模型、不补采、不打对齐分、不回填占位文本。

## 4. 待补清单 `07_supplement_requests.jsonl`

只登记、不回采（任务书「不补采」）。一行一个缺模态记录：

```json
{
  "sample_id": "demo_07_silent",
  "missing_modality": ["audio"],
  "reason": "no_audio_track",
  "source_reference": ["media/original/demo_07_silent.mp4"],
  "process_status": "not_applicable",
  "recorded_at": "2026-11-04T12:00:00+08:00",
  "operator": "multimodal_preprocessing"
}
```

文件总是产出（无缺模态时为空文件），在 `manifest.json` 中 `role=supplement`。

## 5. 异常清单 `07_rejects.jsonl`

| 字段 | 说明 |
| --- | --- |
| `operator` | 固定 `multimodal_preprocessing` |
| `reason` | 上表枚举，稳定字符串 |
| `message` | 可解释中文说明（含具体数值/字段名）|
| `line` | 输入行号（1-based）|
| `sample_id` | 可解析时给出，否则 `null` |
| `record` | **完整原始记录**（坏行只有行号），下游可一键复跑 |
| `source_reference` | 相关媒体相对路径数组 |
| `evidence.media_errors` | `[{"path": "...", "reason": "media_missing"}]` |

异常清单不计入主清单，也不随下游作为候选；它属于上位算子契约的“复查清单”旁路副本，
统计中 `rejected` 明确计数，不会静默消失。

## 6. 媒体搬运约定

- 记录中的媒体路径**相对运行目录**；07 以**同一相对路径**复制进本次 run 目录，因此
  **无需改写**记录中的 `path`（`inference._remap_record_media` 仍保留改写能力以兼容未来重命名）。
- 被引用的全部媒体都会搬运：`image.path`、`image.original_path`、`video.path`、
  `video.frames[].path`、`audio.path`、`audio.original_path`、`audio.segments[].path`。
- 同一路径多记录引用只复制一次；被拒绝记录的媒体**不复制**（避免孤儿文件）。
- 上游 review/rejects 旁路清单（`NN_rejects.jsonl` / `NN_review.jsonl`）若与输入同目录，
  随包携带一份（`role=sidecar`），保证上游处理结果可追溯。

## 7. 运行目录与本地控制文件

```
<OUTPUT_DIR>/<uniqueId>/
├── input/                                  # /uploadFile 解包结果（阶段首）
├── multimodal_preprocessing/<runId>/       # 本算子运行目录（runId=run001, run002, ...）
│   ├── 07_candidates.jsonl                 # 入口（main）
│   ├── 07_rejects.jsonl                    # 复查清单（review）
│   ├── 07_supplement_requests.jsonl        # 待补清单（supplement，只登记不回采）
│   ├── 04_images/ 05_frames/ 06_audio/ media/   # 被引用媒体（media）
│   ├── <NN>_rejects.jsonl                  # 上游旁路（sidecar，存在才复制）
│   ├── manifest.json                       # crossmodal-manifest-v1
│   └── run.log                             # 运行日志
└── .lock                                   # 任务锁
```

- `run.log` 是**本地旁路文件**，不进入 `artifacts`；
- 文件内容对象额外携带 `manifest.json`，下游恢复后无需依赖上游目录即可定位入口；
- v4.2 起不再写 `lineage.json`/`stage_context.json`，血缘/阶段上下文输出已停用。
- `manifest.json` 字段：`format`、`entrypoint`、`artifacts[{path, role}]`（role ∈ main/media/
  sidecar/review/**supplement**）、`operator`、`run_id`、`unique_id`、`created_at`、`notes`。

## 8. 统计字段（`statistics`）

`input`、`input_lines`、`blank_lines`、`valid_records`、`bad_lines`、`output`、`rejected`、
`success`、`duplicates`、`media_copied`、`media_bytes`、`media_missing`、`sidecar_files`、
`empty_input`、`conservation_ok`、`artifact_files`、`reject_reasons{}`、请求参数快照。

记录级流程态计数（五桶互斥，每条主清单记录恰好落一个）：

`transcribed`、`transcription_skipped`、`not_applicable`、`no_speech`、`transcription_failed`，
以及 `not_applicable_reasons{}`、`no_speech_reasons{}`、`transcription_fail_reasons{}`、
`transcription_skip_reasons{}`。

ASR 口径计数：`audio_units`、`transcription_segments`、`segments_written`、
`transcription_model_loaded`、`transcription_loaded`、`supplement_requests`、
`visual_time_index_built` / `visual_time_index_entries` / `visual_time_index_frame_entries`。

预检计数（`no_speech` 子集，不参与五桶守恒）：`precheck_skipped`（命中数）、
`precheck_reasons{}`（按 `audio_silent` / `audio_too_short` / `low_snr` 分布）。

`success` = `transcribed`（真有转写产出的记录数；配对单元已不在本节点生成）。

守恒（不满足即整次运行失败，不产出半成品）：

```
input  == output + rejected
output == transcribed + transcription_skipped + not_applicable + no_speech + transcription_failed
```

## 9. 配置与覆盖

`configs/config.yaml`：`operator{name,version}`、`stage_id`、`enabled`（总开关）、
`transcription{enabled, model, language, model_root, model_dir, device,
max_segment_seconds, paragraph_silence_seconds, beam_size, initial_prompt,
precheck, min_duration_seconds, min_snr_db, fail_action}`；运行统计的
`statistics.transcription` 快照回显 `precheck` / `min_duration_seconds` / `min_snr_db`。

- 请求 `config` 只能覆盖业务参数；`operator`/`stage_id` 与未知键 → 失败信封；
  请求可切换 `transcription_enabled`，但不能改模型/语言（固定项）。
- **[deprecated]** `unit_policy`、`max_av_duration_diff_ratio`、`av_mismatch_action`、
  `validate_frame_timestamps` 仍可被解析（旧配置/旧请求不报错），但**不再参与任何判定**。
- `require_existing_media=false` 一律拒绝：run/HTTP/CLI 都强制执行媒体存在性校验。
- 未知枚举、越界数值、字符串型数值、非有限数（NaN/Inf）全部报错，不静默回退默认值。

## 10. fixture 数据

`dataset/sample/`（由 `dataset/sample/make_sample.py` 生成，`input_source=fixture`）：
5 条记录覆盖 text+image（含/不含原图）、video+audio、仅文本；转写默认关闭时
带音频来源的 2 条为 `skipped`、无音频来源的 3 条为 `not_applicable`。
媒体为真实 PNG/WAV 与占位 MP4 容器。详见 `sample/README.md`。

## 11. 与上下游对接注意

- 下游（08 阶段末）应使用 `inputFile` 指向本算子 `07_candidates.jsonl`，或恢复阶段末
  文件内容对象后按 `manifest.json` 的 `entrypoint` 定位；**不要**依赖目录内“唯一 JSONL”
  自动推断（本算子会同时产出清单与异常清单两个 JSONL）。
- 07 的输出记录保留了全部被引用媒体，因此恢复后的目录**自包含**，可脱离上游目录继续处理。
