# 算子 10 数据说明：核对输入对象准入（`input_admission`）

本文件只描述**本算子读写的字段、单位与状态口径**，以及本地样例的构成。
不在本目录重复跨算子总规范（见工程侧 `demo/DATA_SCHEMA.md` 与 `demo/DEVELOPMENT_CONTRACT.md`）。

- 表1 编号：**#10**（数据加工环节中间节点）
- 阶段：`S3`（表1 第 3 环节“数据加工”；demo 五阶段中属“跨模态数据对齐标注”）
- 上游：`#9 准备候选描述`（也核对 `#1 接入多模态批次` 登记的来源 ID/批次号）
- 下游：`#11 生成候选配对`（主清单）；`#21`（两类隔离台账）；`#9`/`#1`（退回清单）
- 算法后端：**纯规则，无模型、无权重、不需要 GPU、不联网**；不产生任何模型分数

---

## 1. 输入

### 1.1 入口

- `/predict` 的 `inputFile`（阶段内串联），或阶段首上传素材包后任务目录 `input/` 内的入口
  JSONL（`manifest.json` 的 `entrypoint` 优先；无 manifest 时只接受唯一候选）；
- **每行一条完整记录**（#9 输出形态），严格 UTF-8 解码；
- 坏行（JSON 解析失败 / 顶层不是对象）不中断流程，逐行进入原始隔离台账；
- 整文件非严格 UTF-8 → 结构化失败（`InputError`，HTTP 400），不猜编码、不静默丢弃。

### 1.2 本算子实际读取的字段

| 字段 | 用途 | 缺失 / 非法时 |
| --- | --- | --- |
| `sample_id` | 必填、去重计数、贯穿台账 | 缺失 → 原始隔离区（`missing_sample_id`） |
| `text.content`（或 `text` 为字符串） | 候选描述（#9 输出） | 空 → 退回 #9（`description_missing`） |
| `text.description_source` | 描述来源，必须不是“文件名/标签” | 命中禁用来源 → 退回 #9（`description_source_invalid`） |
| `metadata.description` + `metadata.description_source` | 音视频样本（无 text 模态）的描述承载位置 | 同上 |
| `processing["09"].description_source` / `.source` | 描述来源的兜底位置 | 无 |
| `image.path` / `video.path` / `audio.path` | 逐条核对“文件能否打开” | 见 §1.3 |
| `metadata.source_id`（或 `source`） | 来源 ID 核对 | 缺失 → 退回 #1 批次登记（`missing_source_id`） |
| `metadata.batch_id`（或 `batch`） | 批次号核对 | 缺失 → 退回 #1 批次登记（`missing_batch_id`） |
| `metadata.license`（或 `authorization`/`授权结论`） | 授权结论必须为“可训练” | 缺失 → 样本隔离库（`license_missing`）；非可训练 → `license_not_trainable` |
| `metadata.retry_count` | 已退回次数；达到上限即锁定 | 缺失按 0 |
| `metadata.image_tags` | 判定“描述写成了分类标签” | 缺失视为无标签 |
| `manifest.json` | 入口与随包文件定位 | 缺失时按唯一 JSONL 候选推断 |

> 上游字段只读取、不修改；本算子**只新增** `admission` 与 `processing["10"]`，
> 不触碰 `alignment`、`qc`、其它 `processing` 键与未知扩展字段（全部原样透传）。

### 1.3 模态判定与文件可打开口径

- 模态计数顺序固定 `text / image / video / audio`；`text` 计为存在当且仅当
  `text.content`（或字符串形态的 `text`）去空白后非空；媒体模态计为存在当且仅当
  `path` 非空。
- 每个媒体引用必须满足：相对 `manifest.json` 所在目录、正斜杠、无 `..`、无盘符、无 URL。
- 允许扩展名（表1 白名单 ∪ demo 共享 DATA_SCHEMA 白名单，且必须与所在模态字段匹配）：

  | 模态 | 允许扩展名 |
  | --- | --- |
  | `text` | `txt` `json` `csv` |
  | `image` | `jpg` `jpeg` `png` `webp` `bmp` `gif` |
  | `video` | `mp4` `mov` `avi` `mkv` |
  | `audio` | `wav` `mp3` `flac` `aiff` `aif` `m4a` |

- `file_check_mode=header`（默认，无第三方依赖）：存在性 → 非空 → 扩展名白名单与模态匹配 →
  头签名（PNG/JPEG/WEBP/BMP/GIF/MP4/MOV/M4A/AVI/MKV/WAV/MP3/FLAC/AIFF）；
  文本类追加“严格 UTF-8 可解码”。
- `file_check_mode=decode`：在上面的基础上追加**真实解码**（图片 Pillow、视频 OpenCV、
  `wav` 用标准库 `wave`）。只判断“能否打开”，**不做任何画质评分**（表1 明确禁止越界）。
  没有内置解码器的格式（`mp3`/`flac`/`aiff`/`aif`/`m4a`）在 decode 模式下：
  - `allow_degraded=false`（默认）→ 结构化失败 `DependencyError`，不伪造“已解码”；
  - `allow_degraded=true` → 该文件标 `status=1`、`process_status=header_only`，运行状态置 `degraded`。

### 1.4 判定优先级（统计唯一归口）

```
结构(sample_id) → 文件可打开 → 来源ID/批次号 → 描述 → 授权 → 模态数 → 通过
```

原始文件问题排在最前：文件都打不开的记录无法被任何下游消费。

---

## 2. 输出

### 2.1 四路产物与行数守恒

全部写在 `OUTPUT_DIR/<uniqueId>/input_admission/<runId>/`：

| 文件 | 用途 | 去向 |
| --- | --- | --- |
| `10_admitted.jsonl` | 合格待配对对象池（入口） | #11 生成候选配对 |
| `10_return.jsonl` | 退回/待补台账 | #9 准备候选描述、#1 接入多模态批次 |
| `10_origin_quarantine.jsonl` | 原始隔离台账（含坏行） | #21 |
| `10_sample_quarantine.jsonl` | 样本隔离库台账（含锁定） | #21 |

同目录另写 `manifest.json`、`run.log`，以及随包搬运的 `media/`（仅主清单仍引用的媒体）
与上游旁路清单。

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`lineage.py`/`mty_compliance.py` 代码保留存档）；原血缘中的统计/配置快照等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

守恒式（写入 `statistics.conservation_ok`，不成立即结构化失败并清理产物）：

```
input       = valid_records + bad_lines
valid_records = admitted + returned + origin_quarantined + sample_quarantined
origin_entries = bad_lines + origin_quarantined
```

### 2.2 `10_admitted.jsonl`——每条记录新增的 `admission` 命名空间

```json
{
  "admission": {
    "operator": "input_admission",
    "formalOperatorNumber": 10,
    "version": "v1.0.0",
    "passed": true,
    "modalities": ["text", "image"],
    "modality_count": 2,
    "description": {"source": "图注", "chars": 10, "valid": true},
    "license": "可训练",
    "source_id": "SRC-A",
    "batch_id": "BATCH-2026-10",
    "file_checks": {
      "image": {"path": "media/img_ok.png", "ext": "png", "bytes": 136,
                "mode": "header", "status": 1, "process_status": "checked",
                "checker": "signature"}
    },
    "duplicate": false,
    "admittedAt": "2026-10-06T15:11:54+08:00"
  },
  "processing": {
    "10": {"status": 1, "process_status": "success", "action": "admit",
           "modalities": ["text", "image"],
           "metrics": {"modality_count": 2, "description_chars": 10, "files_checked": 1},
           "finished_at": "2026-10-06T15:11:54+08:00"}
  }
}
```

[v4.2] `file_checks.<模态>.status` 为 int（1=可打开/已检查，0=失败），原字符串
`checked`（头签名）/ `decoded`（真实解码）/ `header_only`（降级）/ `failed` 迁入 `process_status`。

### 2.3 台账条目结构（三个旁路清单共用）

```json
{
  "sample_id": "s-orq-1", "line": 8,
  "label": "原始文件-损坏",
  "operator": "input_admission", "formalOperatorNumber": 10, "stage": "S3",
  "action": "isolate", "destination": "原始隔离区",
  "reason": "file_missing",
  "message": "image：文件不存在或不是普通文件",
  "event_time": "2026-10-06T15:11:54+08:00",
  "batch_id": "BATCH-2026-10", "retry_count": 0, "locked": false,
  "evidence": {"file": {"path": "media/nonexistent.png", "status": 0,
                        "process_status": "failed", "...": "..."}},
  "record": {"sample_id": "s-orq-1", "...": "上游原样记录"}
}
```

- 坏行条目只保留 `sample_id=null`、`line`、`reason`、`message`，不保存超长原始坏行；
- 有效记录条目保留原记录（`record`），便于 #21 复核与回流。

### 2.4 标签与去向（表1 质量三场景）

| `label` | `destination` | 触发 `reason` | 语义 |
| --- | --- | --- | --- |
| `原始文件-损坏` | 原始隔离区 | `file_missing` `file_unreadable` `file_signature_mismatch` `decode_failed` | 文件打不开 |
| `原始文件-空` | 原始隔离区 | `file_empty` | 空文件 |
| `原始文件-无法解析` | 原始隔离区 | `format_not_allowed` `media_path_invalid` `text_decode_failed` `missing_sample_id` `invalid_json` `invalid_record` | 格式/路径/结构问题 |
| `样本-敏感` | 样本隔离库 | `license_not_trainable` `license_missing` | 授权不是“可训练”或无法确认 |
| `样本-语义不通` | 样本隔离库 | `modality_insufficient` `return_limit_exceeded` | 模态不足；或退回超限被锁定 |
| `流程-待补描述` | `09_准备候选描述` | `description_missing` `description_source_invalid` `description_is_filename` `description_is_tag` `description_too_short` `description_too_long` | **不隔离**，退回补描述 |
| `流程-待补登记` | `01_接入多模态批次` | `missing_source_id` `missing_batch_id` | **不隔离**，退回补登记 |

> “尚无描述”属于流程状态，按表1 只退回不隔离；只有同一记录退回次数达到
> `max_return_count`（默认 2，即第 3 次）才锁定进样本隔离库（`action=lock`、`locked=true`）。

### 2.5 `statistics` 字段（全部来自真实处理结果）

| 字段 | 含义 |
| --- | --- |
| `input_lines` / `blank_lines` / `input` | 物理行数 / 空行 / 参与处理的数据行 |
| `valid_records` / `bad_lines` | 可作为对象处理的记录 / 坏行 |
| `admitted` / `returned` / `origin_quarantined` / `sample_quarantined` | 四路计数 |
| `returned_description` / `returned_registration` | 退回去向拆分 |
| `origin_entries` | 原始隔离台账实际条目数（= `bad_lines` + `origin_quarantined`） |
| `locked` / `duplicates` | 锁定条数 / 重复 `sample_id` 条数 |
| `description_missing` / `description_invalid` | 描述问题拆分 |
| `license_trainable` / `license_not_trainable` / `license_missing` | 授权结论拆分 |
| `files_checked` / `files_missing` / `files_broken` | 文件核对拆分 |
| `modality_counts` / `modality_pairs` | 按模态数与模态组合的分布（键为字符串） |
| `return_reasons` / `origin_reasons` / `sample_reasons` | 各分流的原因分布 |
| `media_copied` / `media_bytes` / `sidecar_files` / `artifact_files` | 随包产物情况（字节数只描述产物体积，**不是业务记录数**） |
| `file_check_mode` / `degraded_mode` | 文件核对模式 / 是否发生降级 |
| `empty_input` / `conservation_ok` / `entrypoint` | 空输入 / 守恒校验 / 入口文件名 |

`backends`（探测到的解码后端）只放在运行结果与日志里，不进入 `statistics`，避免与业务统计混淆。

### 2.6 `manifest.json`

`crossmodal-manifest-v1` + `entrypoint: "10_admitted.jsonl"`，并列出全部随包文件
（四类清单、`media/`、上游旁路清单），附加 `operator`、`formal_operator_numbers: [10]`、
`run_id`、`unique_id`、`created_at`、`stage_id`、`notes`。

---

## 3. 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。`isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；v4.2 起中间与阶段末响应同构，均返回 `{operator, status, outputFile, result}`。

---

## 4. 本地样例

`dataset/sample/`（`python dataset/sample/make_sample.py` 可重复生成，无随机数）：

| 文件 | 说明 |
| --- | --- |
| `demo.jsonl` | 16 行：4 合格 / 3 退回 / 3 原始隔离 / 4 样本隔离（含 1 条锁定）+ 2 坏行 |
| `media/img_ok.png` | 合法 PNG（64×64，纯 Python 生成，可被 Pillow 解码） |
| `media/img_broken.png` | 扩展名 `.png` 但内容非 PNG → 头签名不符 |
| `media/img_empty.png` | 0 字节 → 原始文件-空 |
| `media/audio_ok.wav` | 16bit 单声道 PCM WAV（标准库 `wave` 生成） |
| `media/video_ok.mp4` | 优先用 OpenCV 写真实可解码 mp4；无 OpenCV 时退回最小 `ftyp` 头 |
| `manifest.json` | `crossmodal-manifest-v1`，入口 `demo.jsonl` |

样例核实结果（本机实测，见 README「真实测试结果」）：

```
input=16 valid_records=14 bad_lines=2
admitted=4 returned=3 origin_quarantined=3 sample_quarantined=4 locked=1
origin_entries=5  files_checked=13  conservation_ok=true
```
