# 算子 13 评分音文语义一致性 —— 数据契约

> 表1 v4.2 第 13 行；阶段 S3（数据加工）；中间节点。
> 本文件与实际实现一致；如需变更先改 `utils/audio_text.py` 的常量，再同步本文件。

## 1. 输入

来源：#11「生成候选配对」输出的 `11_candidate_pairs.jsonl`（音文候选配对），
或等价的**单记录**形态（同一记录内同时含音频与对齐描述）。

### 1.1 配对形态（与 #11 同构，主形态）

```json
{
  "pair_id": "pair-01",
  "group_id": "grp-src-demo-A",
  "modality_pair": ["text", "audio"],
  "evidence": {"type": "A", "criterion": "same_source_id", "value": {"source_id": "src-demo-A"}},
  "evidence_priority": 1,
  "left": {
    "sample_id": "pair-01-audio",
    "modalities": ["audio"],
    "record": {
      "sample_id": "pair-01-audio",
      "source_id": "src-demo-A",
      "batch_id": "batch-demo",
      "audio": {
        "path": "media/seg_01.wav",
        "sample_rate": 16000,
        "channels": 1,
        "duration": 1.4,
        "segments": [],
        "transcript": "今天天气很好我们一起去公园散步吧",
        "transcript_source": "whisper-base"
      },
      "text": null,
      "processing": {"08": {"status": "success", "transcript": "…"}},
      "metadata": {"parent_asset_id": "asset-src-demo-A"}
    }
  },
  "right": {
    "sample_id": "pair-01-text",
    "modalities": ["text"],
    "record": {
      "sample_id": "pair-01-text",
      "source_id": "src-demo-A",
      "batch_id": "batch-demo",
      "audio": null,
      "text": {"content": "今天天气很好我们一起去公园散步吧", "original": "…", "lang": "zh"},
      "processing": {"09": {"status": "success", "description": "…"}},
      "metadata": {"description_source": "caption"}
    }
  },
  "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0",
               "uniqueId": "batch-demo_file-demo"}
}
```

### 1.2 字段解析顺序（实现口径）

| 目标 | 解析顺序（首个非空即用） |
| --- | --- |
| 音频段落 | `audio.segments[0]`（`path`/`start`/`end`） → `audio.path`（`start=0`，`end=audio.duration`） → `audio` 为纯字符串 |
| 转写文本 | `audio.transcript` → `audio.segments[0].transcript` → `text.transcript` → `processing.08.transcript` → `metadata.transcript` |
| 对齐描述 | `text.content` → `metadata.description` → `processing.09.description` → `text.original` |
| 记录标识 | `pair_id` → `left.sample_id` → `right.sample_id` → 两侧 `record.sample_id` → `sample_id` |
| 批次 | 记录自身 `batch_id`/`metadata.batch_id` → 文本侧/音频侧同名字段 |

音频与文本的角色说明：音频段落用于可读性和媒体可用性检查；文本侧复用上游 #10 的
Whisper-base 转写，与 #9 对齐描述计算字符级 CER。Wav2Vec2/BERT 裸余弦只能作为显式
实验诊断，不能参与生产门禁。

媒体路径约定：必须是相对素材包根的 POSIX 相对路径（如 `media/seg_01.wav`）；
拒绝绝对路径、`..`、反斜杠、`~`、URL。媒体解析根依次为：输入文件所在目录、其父目录、
`<task>/input`、任务根。

## 2. 输出

全部产物位于 `OUTPUT_DIR/<uniqueId>/audio_text_scoring/<runNNN>/`：

| 文件 | 何时产生 | 内容 |
| --- | --- | --- |
| `13_audio_text_scores.jsonl` | `enabled=true` | **通过**记录（交 #19），含 `processing["13"]` 全量分数与结论 |
| `13_passthrough.jsonl` | `enabled=false` | 全部有效记录原样透传，仅追加 `processing["13"]`（`status=1`、`process_status=skipped`） |
| `13_sample_quarantine.jsonl` | 总是 | 不通过样本（交 #21），条目含 `audio_text` 与完整 `record` |
| `13_pending.jsonl` | 总是 | 流程待处理（缺转写/缺描述/无音文对/评分前置不满足），**不隔离** |
| `13_rejects.jsonl` | 总是 | 坏行 + 原始文件问题（无法解析/缺字段/编码异常/空/损坏） |
| `manifest.json` | 总是 | `crossmodal-manifest-v1`，列出全部传递文件 |
| `run001.log` | 总是 | 逐行日志：`[时间] [动作] 消息` |
| `media/**` | 总是（尽力） | 被引用音频按原相对路径搬运，供可交付与本算子复算 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py`、`utils/mty_compliance.py` 的血缘/归档接口，代码保留存档；`utils/transport.py` 仅保留 `crossmodal-files-v1` 文件对象与 `manifest.json` 打包能力）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 记录内节点 `processing["13"]`

```json
{
  "status": 1,
  "process_status": "passed",
  "route_enabled": true,
  "passed": true,
  "reason": "passed",
  "label": "样本-评分：通过",
  "action": "通过并交下游",
  "destination": "#19",
  "cosine_similarity": null,
  "raw_cosine_similarity": null,
  "edit_distance": 2,
  "edit_distance_ratio": 0.0909,
  "edit_distance_ratio_homophone": 0.0909,
  "edit_distance_ratio_max": 0.3,
  "homophone_tolerance": true,
  "homophone_tolerated": false,
  "metric": "CER",
  "threshold_source": "task_config",
  "cer": 0.0909,
  "wer": 0.0,
  "scoring_method": "character_edit_distance_ratio",
  "scoring_version": "v2",
  "transcript_model": "whisper-base",
  "transcript_model_version": null,
  "candidate_pool_eligible": true,
  "audio_segment": {"path": "media/seg_01.wav", "start": 0.0, "end": 1.4},
  "transcript_source": "audio.transcript",
  "description_source": "text.content",
  "transcript_chars": 16,
  "description_chars": 16,
  "audio_duration_seconds": 1.4,
  "audio_truncated": false,
  "scored_at": "2026-10-06T15:47:31+08:00"
}
```

`status` 为 int（1=通过/不隔离，0=不通过/隔离）；原字符串流程态 `passed` / `failed` / `pending` / `skipped` 迁入 `process_status`。

`edit_distance_ratio` 是严格字符口径；`edit_distance_ratio_homophone` 把「替换成同音字」的
代价记为 0 后按同一分母重算（`homophone_tolerance: true` 时才有值），判定取两者较小值与同一
`edit_distance_ratio_max` 比较。`homophone_tolerated=true` 表示严格口径超限、容错口径通过
（此时 `reason=passed_homophone_tolerant`）。

同音容错开启时拼音表是**硬依赖**：整次运行启动前校验，缺失即 `ConfigError`（不写任何产物）。
若运行期表仍不可用（异常场景），严格 CER 超阈值的记录判
`not_applicable`（`reason=pinyin_table_unavailable`，留产线、不隔离），并写
`pinyin_table_unavailable=true` / `pinyin_table_error`；严格口径本就在阈值内的记录照常通过，
但同样带该标记，避免「静默按严格口径误隔离」。
生产路线不加载独立基座模型，因此 `cosine_similarity` 和 `audio_model`/`text_model` 始终为
`null`；若显式注入实验 scorer，诊断值只写入 `raw_cosine_similarity`。

### 2.2 台账条目（`13_sample_quarantine.jsonl` / `13_pending.jsonl` / `13_rejects.jsonl`）

```json
{
  "line": 3,
  "sample_id": "pair-02-audio",
  "pair_id": "pair-02",
  "reason": "edit_distance_exceeded",
  "label": "样本-语义不通",
  "stage": "13",
  "operator": "audio_text_scoring",
  "action": "记录并隔离",
  "destination": "样本隔离库",
  "event_time": "2026-10-06T15:47:31+08:00",
  "batch_id": "batch-demo",
  "retry_count": 0,
  "audio_text": { "...": "与 2.1 相同的节点" },
  "record": { "...": "完整记录（隔离/待处理条目才有，便于 #21 与人工复核）" }
}
```

`reason` 枚举：`json_parse_error`、`record_not_object`、`encoding_error`、
`missing_field: sample_id`、`unsupported_media_path`、`audio_missing`、`audio_unreadable`、
`audio_silent`、`no_audio_text_pair`、`missing_transcript`、`missing_description`、
`edit_distance_exceeded`、`score_error`。

### 2.3 标签与去向

| 场景 | 标签 | 去向 |
| --- | --- | --- |
| 判定通过 | `样本-评分：通过` | `#19`（主产物） |
| 判定不通过（编辑距离超标） | `样本-语义不通` | 样本隔离库（交 #21） |
| 缺转写 / 缺描述 / 无音文对 | `流程-待转写` / `流程-待补描述` / `流程-无音文对` | 产线待处理（**不隔离**） |
| 路线关闭 | `流程-音文路线未启用` | 原样透传 |
| 损坏/空/无法解析/缺字段/编码异常 | `原始文件-损坏` / `原始文件-空` / `原始文件-无法解析` / `原始文件-缺字段` / `原始文件-编码异常` | 原始隔离区 |

## 3. 传输契约

- 入口 JSONL 由 `resolve_entrypoint` 定位：优先 `manifest.json` 的 `entrypoint`，
  否则只接受唯一候选（`demo.jsonl` / `samples.jsonl` 优先）；
- 形态二响应中的 `data.outputFile` 为 `crossmodal-files-v1` 文件内容对象：
  `{"format","entrypoint","files":[{"path","encoding":"utf-8"|"base64","content"}]}`，
  文本以 UTF-8 承载、媒体以 base64 承载；`files` 必含 `entrypoint`；
- `data.result` 为业务摘要：`{runId, runDir, entrypoint, files{outputFile,rejectsFile,sampleQuarantineFile,pendingFile}, statistics}`，
  其中 `runDir` 与 `files.*` 均为任务内相对路径；
- 阶段内后续算子使用上一运行目录内的入口文件绝对路径作为 `inputFile`；
  v4.2 起**中间与阶段末响应同构**，均返回上述 `outputFile` + `result`，不再返回 `lineageFile` 或阶段血缘数组。

## 4. 统计与守恒

`statistics` 全部来自真实计数，且必须满足：

```
input_records = valid_records + bad_lines
valid_records = origin_quarantined + sample_isolated + pending_records + passed_records + skipped_records
sample_isolated = edit_distance_exceeded
scored_records  = passed_records + edit_distance_exceeded
pending_records = missing_transcript + missing_description + no_audio_text_pair
                + score_error_records
```

`conservation_ok` 为上述全部等式的合取，写入 `data.result.statistics` 与 `run.log`。
