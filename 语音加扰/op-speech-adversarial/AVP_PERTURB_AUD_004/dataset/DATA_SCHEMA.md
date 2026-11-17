# AVP_PERTURB_AUD_004 · 数据契约（DATA_SCHEMA）

> 输入来自 02 算子 `002_std.jsonl`；输出 `004_batch.jsonl` + `004_speed_report.json`，
> `004_batch.jsonl` 是 05 样本质检的输入。时间单位一律为**秒（float）**，误差单位为**百分比（%）**。

## 1. 输入

| 来源 | 形态 | 说明 |
|---|---|---|
| `inputFile` | `002_std.jsonl` 文件 | 正常链路：02 算子 `data.lineageFile.outputFile` |
| `inputFile` | 目录 | 目录内优先找 `002_std.jsonl`；否则取唯一的 `*.jsonl`（zip 解压 / CLI 场景） |
| 未传 `inputFile` | — | 依次尝试 `work_dir/input/extracted`、`work_dir/input` |

### 1.1 `002_std.jsonl` 行字段（本算子实际读取的部分）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `uttid` | string | 是 | 样本 ID → 输出 `source_uttid`；缺失 → `rejects(missing_field)` |
| `std_audio_path` | string | 二选一 | 标准化后音频路径（优先使用） |
| `audio_path` | string | 二选一 | 原音频路径；**仅在 `std_audio_path` 字段不存在时**兜底。若 `std_audio_path` 存在但文件缺失 → `audio_not_found`，**不静默回退** |
| `dataset_id` | string | 否 | 原样透传；缺失输出 `""` |
| `text` | string | 否 | 原样透传；缺失输出 `""` |
| `duration` / `std_duration` | float | 否 | 本算子不依赖（时长以重新读取的文件头为准） |

路径解析规则：绝对路径直接用；相对路径依次相对语料表目录、`work_dir/input`、`work_dir`。

## 2. 参数（`params`，以 `map.json` `paramsSpec` 为准）

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `values` | string[] | 是 | — | **只能来自 `["0.8x", "1.0x", "1.2x"]`**；大小写不敏感（`1.2X` → `1.2x`）；不得为空、不得重复；其它值 → `invalid_params` |
| `scope_mode` | enum | 否 | `all` | `all` = 全部样本；`count` = 指定条数 |
| `scope_count` | int | 否 | — | `scope_mode=count` 时必填，`1 ≤ count ≤ 输入样本数`；按输入顺序取前 N 条 |
| `seed` | int | 否 | `20261102` | 目标倍率随机挑选（`per_value=false` 时）的可复现种子 |
| `per_value` | bool | 否 | `true` | `true` = 每条语音 × 每个倍率各出一条；`false` = 每条语音随机取一个倍率 |

## 3. 输出

### 3.1 `004_batch.jsonl`（每行一个变速样本，字段固定）

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `sample_id` | string | 否 | 批次内唯一 ID：`<uttid>__speed__<倍率>__<4 位序号>`（`.` 写成 `p`，如 `1p2x`） |
| `source_uttid` | string | 否 | 来源样本 uttid（对应 002 的 `uttid`） |
| `dataset_id` | string | 否 | 来源数据集 ID（缺失为 `""`） |
| `audio_path` | string | 否 | 变速后 WAV 真实路径（`004_audio/<sample_id>.wav`，采样率/声道/格式与输入一致） |
| `text` | string | 否 | 参考文本（缺失为 `""`） |
| `duration` | float | 否 | 输出音频真实时长（秒，3 位小数；由落盘文件头读出） |
| `perturb` | object | 否 | 参数快照，见 §3.2 |

### 3.2 `perturb` 参数快照

| 字段 | 类型 | 本算子取值 | 说明 |
|---|---|---|---|
| `dimension` | string | `"speed"` | 加扰维度 |
| `value` | string | `"0.8x"` / `"1.0x"` / `"1.2x"` | 语速倍率取值 |
| `snr_db` | null | `null` | 本算子不混音，固定 `null`（03 算子用） |
| `speed` | float | `0.8` / `1.0` / `1.2` | 倍率数值 |
| `asset` | null | `null` | 本算子无素材，固定 `null` |

### 3.3 `004_speed_report.json`

| 字段 | 含义 |
|---|---|
| `operator` / `params` | 算子标识与本次生效参数快照（values/scope/seed/per_value） |
| `statistics` | 与 `run()` 返回的 `statistics` 完全一致 |
| `speed.by_value[]` | 每个倍率的 `{value, speed, count, duration_error_max_pct, passthrough_checked, passthrough_identical}` |
| `speed.duration_error` | `{definition, unit, mean_pct, max_pct, threshold_pct, violations}` |
| `speed.passthrough` | `{checked, identical, rate, definition}`（1.0× 逐字节透传核对） |
| `samples[]` | 逐条 `{sample_id, source_uttid, value, speed, source_duration, expected_duration, duration, duration_exact, duration_error_pct, sample_rate, passthrough, passthrough_identical, audio_path}`，最多 `io.max_listed` 条 |
| `rejects[]` | 未产出样本明细（解码/写盘失败），最多 `io.max_listed` 条 |
| `truncated` | 明细截断条数（统计数仍全量） |
| `conclusion` | `success` / `partial` |

### 3.4 两个硬口径（重要）

**1.0× 原样透传**

- 1.0× 分支**直接复制 02 写出的 WAV PCM16 文件字节**，不重新解码、不重新量化；
- 因此输出文件与输入文件**逐字节一致**（sha256 相同），只换文件名；
- 报告 `speed.passthrough.rate` 给出核对通过率，CLI/测试均按文件字节比对；
- 若输入不是 16bit PCM WAV（理论上 02 不会产出），退化为「解码 → `time_stretch(1.0)` → 重新编码」，
  标记 `passthrough_identical=false` 并让批次 `partial`，**不伪装成逐字节一致**。

**输出时长误差**

```
expected = 源时长 / 倍率
error_pct = |实际输出时长 - expected| / expected × 100%
```

- 1.0× 理论为 0；
- 相位声码器输出帧数取整会引入一个到几个采样点的误差，16k 下最大约 0.002%；
- 阈值 `speed.max_duration_error_pct = 1.0`；超过即 `duration_error_violations > 0`，批次 `partial`。

## 4. `dataset/sample/` 说明

- `002_std.jsonl`：2 条 16k 单声道 PCM16 标准语料（2.5s / 3.0s），`std_audio_path` 用相对路径
  `std/utt_00X.wav`，随目录整体搬运；
- 用 `values=["0.8x","1.0x","1.2x"]`、`per_value=true` 跑，产出 6 条变速样本；
- CLI 重放命令见 `README.md` §5（`--input` 直接指向本目录）。
