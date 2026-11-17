# AVP_PERTURB_AUD_003 · 数据契约（DATA_SCHEMA）

> 输入来自 02 算子 `002_std.jsonl`；输出 `003_batch.jsonl` + `003_perturb_report.json`，
> `003_batch.jsonl` 是 05 样本质检的输入。时间单位一律为**秒（float，3 位小数）**。

## 1. 输入

| 来源 | 形态 | 说明 |
|---|---|---|
| `inputFile` | `002_std.jsonl` 文件 | 正常链路：02 算子 `data.lineageFile.outputFile` |
| `inputFile` | 目录 | 目录内优先找 `002_std.jsonl`；否则取唯一的 `*.jsonl`（zip 解压 / CLI 场景） |
| 未传 `inputFile` | — | 依次尝试 `work_dir/input/extracted`、`work_dir/input` |

### 1.1 `002_std.jsonl` 行字段（本算子实际读取的部分）

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `uttid` | string | 是 | 样本 ID → 输出 `source_uttid`。缺失 → 该行计入 `rejects(missing_field)` |
| `std_audio_path` | string | 二选一 | 标准化后音频路径（优先使用） |
| `audio_path` | string | 二选一 | 原音频路径；**仅在 `std_audio_path` 字段不存在时**兜底（兼容 01 产物直连）。若 `std_audio_path` 存在但文件缺失，按 `audio_not_found` 记失败，**不静默回退** |
| `dataset_id` | string | 否 | 原样透传；缺失输出 `""` |
| `text` | string | 否 | 原样透传；缺失输出 `""` |
| `duration` / `std_duration` | float | 否 | 本算子不依赖（时长以重新解码的真实值为准） |

路径解析规则：绝对路径直接用；相对路径依次相对语料表目录、`work_dir/input`、`work_dir`。

### 1.2 噪声素材（默认，来自仓库）

```
weight/assets/
├── noise/白噪.wav、人群嘈杂.wav、交通噪声.wav
└── env/街道.wav、车内.wav
```

- 默认取 `weight/assets/<dimension>/<取值>.wav`；
- 也支持 `params.assets_dir`（相对任务工作目录）指向自定义素材目录，此时取
  `<assets_dir>/<取值>.wav`；
- **取值找不到素材 → `invalid_params`（整批拒绝）**，不静默换素材、不产出半批假数据；
- 素材全静音/无法解码 → `silent_audio:` / `decode_failed:`（整批报错）。

## 2. 参数（`params`，以 `map.json` `paramsSpec` 为准）

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `dimension` | enum | 是 | — | `noise` / `env`；**口音不是加扰维度**，传口音直接 `invalid_params` |
| `values` | string[] | 是 | — | 维度取值；不得为空、不得重复；每个取值必须在素材目录存在同名 `.wav` |
| `snr_db` | float[2] | 是 | — | `[下界, 上界]`，逐条在区间内取目标 SNR；必须落在配置 `[snr_min_db, snr_max_db]` 内 |
| `scope_mode` | enum | 否 | `all` | `all` = 全部样本；`count` = 指定条数 |
| `scope_count` | int | 否 | — | `scope_mode=count` 时必填，`1 ≤ count ≤ 输入样本数`；按输入顺序取前 N 条（确定性，便于复现） |
| `assets_dir` | string | 否 | `weight/assets/<dimension>` | 自定义素材目录（相对任务工作目录或绝对路径） |
| `seed` | int | 否 | `20261102` | 目标 SNR 采样与噪声裁剪位置的可复现种子 |
| `per_value` | bool | 否 | `false` | `true` = 每条语音 × 每个取值各出一条；`false` = 每条语音随机取一个取值 |

## 3. 输出

### 3.1 `003_batch.jsonl`（每行一个加扰样本，字段固定）

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `sample_id` | string | 否 | 批次内唯一 ID：`<uttid>__<dimension>__<4 位序号>`；文件名安全化（中文保留） |
| `source_uttid` | string | 否 | 来源样本 uttid（对应 002 的 `uttid`） |
| `dataset_id` | string | 否 | 来源数据集 ID（缺失为 `""`） |
| `audio_path` | string | 否 | 混音后 WAV 真实路径（`003_audio/<sample_id>.wav`，16k/8k 等与输入同采样率，PCM16 单声道） |
| `clean_audio_path` | string | 否 | **契约外附加字段**：参与混音的**干净参考**音频真实路径（即 02 产出的标准化音频）。05 质检据此用 `noise = mixed − clean` **实测** SNR 并标 `snr_source="measured"`；缺了它，加扰样本的信噪比只能盲估，与「加扰样本必须实测」的业务口径不符 |
| `text` | string | 否 | 参考文本（缺失为 `""`） |
| `duration` | float | 否 | 混音后音频真实时长（秒，3 位小数；由落盘文件头读出） |
| `perturb` | object | 否 | 参数快照，见 §3.2 |

### 3.2 `perturb` 参数快照

| 字段 | 类型 | 本算子取值 | 说明 |
|---|---|---|---|
| `dimension` | string | `"noise"` / `"env"` | 加扰维度 |
| `value` | string | 取值 | 实际使用的维度取值 |
| `snr_db` | float | 区间内数值 | 该样本的**目标 SNR**（1 位小数） |
| `speed` | null | `null` | 本算子不变速，固定 `null`（04 算子用） |
| `asset` | string | 素材路径 | 实际使用的噪声素材文件路径 |

> 「实测 SNR」与「目标误差」不放进 batch 行（batch 只放参数快照），放在报告
> `mix.snr_validation` 与 `samples[]` 里，供质检/追溯使用。

### 3.3 `003_perturb_report.json`

| 字段 | 含义 |
|---|---|
| `operator` / `params` | 算子标识与本次生效参数快照（含素材目录、seed、per_value、scope） |
| `statistics` | 与 `run()` 返回的 `statistics` 完全一致 |
| `mix.assets[]` | 每个取值的 `{value, asset, count, snr_target_mean_db, snr_error_max_abs_db}` |
| `mix.snr_validation` | `{definition, mean_abs_error_db, max_abs_error_db, threshold_db, violations}` |
| `samples[]` | 逐条 `{sample_id, source_uttid, value, snr_target_db, snr_applied_db, snr_error_db, sample_rate, duration, audio_path}`，最多 `io.max_listed` 条 |
| `rejects[]` | 未产出样本明细（静音/解码/写盘失败），最多 `io.max_listed` 条 |
| `truncated` | 明细截断条数（统计数仍全量） |
| `conclusion` | `success` / `partial` |

### 3.4 SNR 口径（重要）

- **定义**：`SNR(dB) = 10*log10(P_signal / P_noise)`，`P` 为均方值；
- **实测方式**：`common.audio.merge_at_snr` 在**实际参与混音的干净信号参考**（可能已按峰值同比缩放）
  与**缩放后的噪声分量**上计算 `applied_snr_db`，不是把 `target` 原样抄回；误差
  `= applied - target`，报告给均值/最大值；
- **不要把「混音文件功率 / 噪声功率」当 SNR**：那测的是 `(S+N)/N`，会在 8 dB 目标上读到约 8.6 dB，
  属于口径错误；
- 阈值：`mix.max_snr_error_db = 0.5`，超过即 `snr_error_violations > 0`，批次 `partial`。

## 4. `dataset/sample/` 说明

- `002_std.jsonl`：2 条 16k 单声道 PCM16 标准语料（2.5s / 3.0s），`std_audio_path` 用相对路径
  `std/utt_00X.wav`，随目录整体搬运；
- 用 `dimension=noise`、`values=["白噪"]`、`snr_db=[5,15]`、`per_value=true` 跑，产出 2 条混音样本；
- CLI 重放命令见 `README.md` §5（`--input` 直接指向本目录）。
