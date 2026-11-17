# AVP_NORMALIZE_AUD_002 · 数据契约（DATA_SCHEMA）

> 输入来自 01 算子 `001_corpus.jsonl`；输出 `002_std.jsonl` / `002_dropped.jsonl` /
> `002_normalize_report.json`，其中 `002_std.jsonl` 是 03 / 04 算子的输入。
> 时间单位一律为**秒（float，3 位小数）**；路径为任务工作目录下的真实路径（采样样例里用相对路径）。

## 1. 输入

| 来源 | 形态 | 说明 |
|---|---|---|
| `inputFile` | `001_corpus.jsonl` 文件 | 正常链路：01 算子 `data.lineageFile.outputFile` |
| `inputFile` | 目录 | 目录内优先找 `001_corpus.jsonl`；否则取目录里唯一的 `*.jsonl`（zip 解压 / CLI 冒烟场景） |
| 未传 `inputFile` | — | 依次尝试 `work_dir/input/extracted`、`work_dir/input` |

### 1.1 `001_corpus.jsonl` 行字段

| 字段 | 类型 | 必填 | 含义 / 空值口径 |
|---|---|---|---|
| `dataset_id` | string | 是 | 数据集 ID，原样透传到输出 |
| `uttid` | string | 是 | 样本 ID（全产线唯一；01 已阻断重复）。缺失 → 该行计入 `rejects(reason=missing_field)` |
| `audio_path` | string | 是 | 原音频路径。绝对路径直接用；相对路径依次相对语料表目录、`work_dir/input`、`work_dir` 解析。找不到 → `rejects(reason=audio_not_found)` |
| `text` | string | 是（01 保证） | 参考文本，原样透传；本算子不校验文本 |
| `duration` | float | 是（01 保证） | **原始音频时长（秒）**，原样保留，不覆盖 |
| `created_at` | string | 是 | ISO8601 时间，原样透传 |
| `sample_rate` / `channels` / `format` | int / int / string | 否 | 01 记录的原音频元数据；标准化后会被目标值覆盖（见 §2.1） |

## 2. 输出

### 2.1 `002_std.jsonl`（每行 = 001 字段 + 标准化字段）

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `dataset_id` `uttid` `audio_path` `text` `created_at` | 同输入 | 否 | 原样透传（`audio_path` 仍指**原音频**） |
| `duration` | float | 否 | **原始音频时长**，原样保留（不覆盖 001 语义） |
| `sample_rate` | int | 否 | 标准化后采样率：`16000` / `8000`（= `params.sample_rate`） |
| `channels` | int | 否 | 标准化后声道数：固定 `1` |
| `format` | string | 否 | 标准化后格式：固定 `"wav_pcm16"`（WAV 容器 + PCM 16bit） |
| `std_audio_path` | string | 否 | 标准化后 WAV 的路径（`002_audio/<uttid>.wav`，重名自动加 `_2` 后缀） |
| `std_duration` | float | 否 | 标准化后音频的真实时长（秒，3 位小数），由**解码后的采样点数**计算 |

> 说明：`format` 取接口 `paramsSpec` 的规则值 `wav_pcm16`，不是 `soundfile` 的容器名 `wav`；
> 两者关系固定为 `wav_pcm16 = wav 容器 + pcm_16 子类型`，报告 `normalize.target` 里同时给出。

### 2.2 `002_dropped.jsonl`（超范围剔除，不裁剪）

| 字段 | 类型 | 含义 |
|---|---|---|
| `dataset_id` / `uttid` / `text` | string | 原样透传 |
| `audio_path` | string | 原音频路径 |
| `duration` | float | 解码后的真实原时长（秒，3 位小数） |
| `reason` | string | `below_min_duration`（< 下限）或 `above_max_duration`（> 上限） |
| `min_duration` / `max_duration` | float | 本次任务的时长范围规则 |

### 2.3 `002_normalize_report.json`

| 字段 | 类型 | 含义 |
|---|---|---|
| `operator` | object | `{id, name}` |
| `params` | object | 本次生效参数快照（sample_rate/channels/format/min_duration/max_duration/remove_source） |
| `statistics` | object | 与 `run()` 返回的 `statistics` 完全一致（同一真相源） |
| `normalize.target` | object | 目标 `{sample_rate, channels, format, container, subtype}` |
| `normalize.duration_range` | array | `[min_duration, max_duration]` |
| `normalize.duration_tolerance_sec` | float | 边界判定容差（只吸收浮点/帧换算噪声） |
| `normalize.format_compliance` | object | **写盘后复核**的 `{checked, compliant, rate}` |
| `normalize.duration_error` | object | `{mean_sec, max_sec, max_pct}`：标准化前后时长误差 |
| `normalize.snr_estimated` | object | `{snr_source:"estimated", count, mean_db, min_db, max_db, note}`；无干净参考，只能盲估 |
| `samples` | array | 每条成功样本的明细（最多 `io.max_listed` 条） |
| `dropped` / `rejects` | array | 剔除 / 失败明细（最多 `io.max_listed` 条） |
| `truncated` | object | 上述明细被截断的条数（统计数仍是全量） |
| `conclusion` | string | `success` / `partial` |

### 2.4 样本明细（`samples[]`）

```json
{
  "uttid": "utt_001",
  "source_audio_path": "/data/task/input/audio/utt_001.wav",
  "std_audio_path": "/data/task/tmp/002/002_audio/utt_001.wav",
  "source_sample_rate": 22050,
  "source_channels": 1,
  "source_format": "wav",
  "source_duration": 2.0,
  "std_duration": 2.0,
  "duration_error_sec": 0.000045,
  "duration_error_pct": 0.0023,
  "snr_db": 81.37,
  "snr_source": "estimated",
  "format_compliant": true
}
```

## 3. `dataset/sample/` 说明

- 3 条程序化合成语音（2.5s / 3.0s / 1.5s，16kHz 单声道 PCM16，单文件 ≤ 100KB）；
- `001_corpus.jsonl` 里 `audio_path` 用**相对路径** `audio/utt_00X.wav`，随目录整体搬运；
- 用 `min_duration=2.0`、`max_duration=10.0` 跑，会得到「2 条输出 + 1 条剔除（1.5s < 2s）」，
  正好演示「超范围直接剔除、不裁剪」；
- 重放方式见 `README.md` §5 的 CLI 冒烟命令（`--input` 直接指向本目录）。
