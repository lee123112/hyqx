# 05 样本质检 · 数据契约（DATA_SCHEMA）

> 算子：`avp_inspect_batch`　目录：`AVP_QC_AUD_005`
> 上游：03 噪声混音加扰 / 04 变速加扰（`003_batch.jsonl` / `004_batch.jsonl`，也可直接接 `005_batch_final.jsonl` 复检）
> 下游：06 样本版本（`005_batch_final.jsonl`）　旁路：前端复核队列（`005_review_queue.jsonl`）

---

## 1. 输入

### 1.1 输入来源

`inference.py` 把上传包解压到 `/data/{uniqueId}/input/extracted/`，本算子拿到的
`ctx.require_input()` 可能是：

| 形态 | 处理 |
|---|---|
| 单个 `.jsonl` 文件 | 直接读 |
| 目录 | 按 `io.batch_filename_candidates` 递归找 `005_batch_final.jsonl` → `004_batch.jsonl` → `003_batch.jsonl`；都没有时取「文件名含 batch 的唯一 JSONL」，仍不唯一则按路径排序取第一个并写警告日志 |
| 其他扩展名 | `missing_file:` |

> 不扫描音频目录、不做音频与文本配对 —— 那些是 01/03/04 的职责，本算子只认最终样本表。

### 1.2 输入行字段（§8 契约，字段只增不改名）

一行一条样本，UTF-8 无 BOM、`\n` 换行。

| 字段 | 类型 | 必填 | 含义 | 空值口径 |
|---|---|---|---|---|
| `sample_id` | string | ✅ | 样本唯一 ID（005 起的主键） | 不空；重复 → `invalid_jsonl:` |
| `source_uttid` | string | ✅ | 来源语料 uttid（01 生成） | 不空 |
| `dataset_id` | string | ✅ | 数据集 ID | 不空 |
| `audio_path` | string | ✅ | 音频路径，绝对路径或相对「输入目录 / 工作目录」 | 不空；文件不存在 → 该条完整性失败 |
| `text` | string | ✅ | 参考文本 | 不空；空/空白 → `missing_fields` |
| `duration` | float | ✅ | 时长（秒，3 位小数，沿用 03/04 快照） | 不空 |
| `perturb` | object | ✅ | 加扰快照，见 1.3 | 不空 |
| `qc` | object | 可选 | 若上游已带质检快照（复检场景）原样保留，本算子重算 | — |

`perturb` 快照结构：

```json
{
  "dimension": "noise",
  "value": "白噪",
  "snr_db": 20.0,
  "speed": null,
  "asset": "白噪"
}
```

| 键 | 类型 | 含义 | 空值口径 |
|---|---|---|---|
| `dimension` | string | 加扰维度：`noise` / `speed` / `mix` | 不空 |
| `value` | string | 维度取值（如 `白噪`、`1.2x`） | 不空 |
| `snr_db` | float\|null | 目标 SNR；非噪声维度为 `null` | 允许 null |
| `speed` | float\|null | 变速倍率；非变速维度为 `null` | 允许 null |
| `asset` | string\|null | 噪声素材名；无素材为 `null` | 允许 null |

### 1.3 干净参考（决定 SNR 是「实测」还是「盲估」）

按以下顺序在 **`perturb` 内** 找，找不到再在**行顶层**找（兼容 03/04 不同落库位置）：

```
clean_audio_path / clean_path / source_audio_path /
reference_audio_path / clean_ref / ref_audio_path
```

- 找到且能解码 → `A.snr_db(clean, mixed - clean)`，`snr_source="measured"`；
- 未找到或解码失败 → `A.estimate_snr_db()`，`snr_source="estimated"`；
- 明确写了参考但解码失败 → 报告 `snr.measure_fallback` 计数并记 warning，不静默当实测。

### 1.4 参数（`params`）

| 参数 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `mode` | string | 无（必填） | `start` 自动检查 + 抽样；`finalize` 回填人工判定 |
| `sample_ratio` | float | `0.05` | 抽样比例 |
| `min_samples` | int | `20` | 抽样下限 |
| `snr_threshold_db` | float | `15.0` | 信噪比阈值（dB） |
| `snr_below_ratio_max` | float | `0.2` | 低于阈值占比上限（分母 = 输入样本数） |
| `seed` | int | `20261102` | 抽样随机种子（确定性） |
| `review_decisions` | array | 无 | `finalize` 必填：[{sample_id, decision: pass\|reject}] |
| `batch_reject` | bool | `false` | `finalize` 整批退回 |

参数越界/非法一律 `invalid_params:`，不静默取默认值。

---

## 2. 自动检查口径（固定两项）

### 2.1 完整性（逐条全量，不做抽样）

| 机器码 | 触发条件 |
|---|---|
| `missing_fields` | 必填字段缺失 / 全空白（`qc.required_fields`） |
| `audio_missing` | `audio_path` 指向的文件不存在 |
| `decode_failed` | 文件存在但无法解码 |
| `empty_audio` | 解码后 RMS < `audio.silence_rms`（默认 `1e-5`） |
| `zero_duration` | 实际解码时长 ≤ 0 |

### 2.2 信噪比

```
snr_db = 10 * log10( mean(signal²) / mean(noise²) )
        = 10 * log10( mean(clean²) / mean((mixed-clean)²) )   # 有参考
```

- 无参考时用盲估（谱减/帧能量迭代），结果写 `snr_source="estimated"`；
- `snr_db < snr_threshold_db` → 该条 `snr_below=true`；
- `below_ratio = 低于阈值条数 / 输入样本数`；`below_ratio > snr_below_ratio_max` → 自动检查不通过；
- 自动检查**不判定加扰可听性**：可听性只能人工试听，算子不伪造自动结论。

### 2.3 自动通过 vs 不通过

```
auto_passed = (integrity_failed == 0) and (below_ratio <= snr_below_ratio_max)
```

| 结果 | `status` | 结论 | 复核队列 |
|---|---|---|---|
| 通过 | `partial`（等人工） | `待复核` | 按公式抽样，可疑样本优先 |
| 不通过（完整性失败或 SNR 占比超限） | `partial` | `不通过` | **空**，整批退回重做 |

### 2.4 抽样公式（确定性）

```
sample_size = max(min_samples, ceil(N * sample_ratio))     # 默认 max(20, ceil(N*5%))
```

- 队列优先放「完整性通过但 SNR 存疑」的样本，剩余名额用 `numpy.random.default_rng(seed)` 补齐；
- 完整性不通过的样本不进复核队列（自动结论已是不通过）；
- `sample_size_actual = min(sample_size, 完整性通过样本数)`，真实队列行数等于 `sample_size_actual`。

---

## 3. 输出

### 3.1 `start` 产物

#### `005_qc_report.json`

| 字段 | 类型 | 含义 |
|---|---|---|
| `operator` / `operator_name` / `unique_id` | string | 算子与任务标识 |
| `mode` | string | `start` |
| `created_at` | string | ISO8601 带时区 |
| `batch.file` / `batch.input_samples` / `batch.fingerprint` | object | 输入文件名、条数、JSONL SHA-256（state 复用键） |
| `parameters` | object | 本次生效的抽样/阈值参数快照 |
| `statistics` | object | 与 `/predict` 的 `data.statistics` 完全一致 |
| `checks.items` | string[] | `["完整性","信噪比"]` |
| `checks.integrity` | object | `checked/failed/passed/problem_counts/failed_samples/truncated` |
| `checks.snr` | object | `checked/threshold_db/below/below_ratio/below_ratio_max/source_counts/measure_fallback/min_db/mean_db/max_db/below_samples/truncated` |
| `checks.auto_passed` | bool | 自动检查是否通过 |
| `checks.fail_reasons` | string[] | 不通过原因（人读） |
| `sample_size` / `sample_size_actual` | int | 公式抽样数 / 真实入队数 |
| `review_required` | bool | 是否需要人工 |
| `conclusion` | string | `待复核` / `不通过` |
| `review_queue` | object | `{file, size, note}` |

#### `005_review_queue.jsonl`

| 字段 | 类型 | 含义 |
|---|---|---|
| `sample_id` | string | 样本 ID（人工判定的 key） |
| `source_uttid` / `dataset_id` | string | 溯源 |
| `audio_path` | string | 原表里的路径（保持原样） |
| `audio_path_resolved` | string | 解析后的可播放绝对路径 |
| `duration` / `text` / `perturb` | — | 原样转发，供试听页展示 |
| `snr_db` | float\|null | 自动检查值 |
| `snr_source` | string | `measured` / `estimated` |
| `snr_below_threshold` | bool | 是否低于阈值 |
| `review_required` | string | 固定文案「人工试听判定加扰可听性（通过 / 剔除）」 |

#### `005_autocheck.json`（内部状态，非对外契约）

完整保存逐样本检查结果与输入指纹；`finalize` 用指纹判断能否复用，避免两次调用之间重复解码。
字段：`batch`、`parameters`、`samples{sample_id: {sample_id, source_uttid, audio_path, duration, integrity_ok, problems, snr_db, snr_source, snr_below}}`、`checks`、`conclusion`、`created_at`。

### 3.2 `finalize` 产物

#### `005_batch_final.jsonl`（下游 06 的输入）

03/04 行字段 + `qc`，`qc` **恰好 4 个键**：

| 字段 | 类型 | 含义 |
|---|---|---|
| `qc.status` | string | `pass` / `rejected` |
| `qc.snr_db` | float\|null | 自动检查的 SNR |
| `qc.snr_source` | string\|null | `measured` / `estimated` |
| `qc.reason` | string | 判定依据（人工通过/人工剔除/自动不通过原因） |

被剔除的样本**也保留**在 `005_batch_final.jsonl` 里（`status=rejected`），由 06 打包时排除 —— 这样版本对比能解释「为什么少了一条」。

#### `005_qc_final.json`

| 字段 | 类型 | 含义 |
|---|---|---|
| `operator` / `operator_name` / `unique_id` | string | 标识 |
| `mode` | string | `finalize` |
| `batch` | object | 同 start 的指纹信息 |
| `conclusion` / `qc_conclusion` | string | `通过` / `不通过`（两键同值，兼容上下游命名） |
| `review.queue_size/reviewed/passed/rejected/complete/batch_reject/status/note` | object | 人工复核结果 |
| `auto_checks` | object | 自动检查摘要 |
| `snr` | object | `threshold_db/measured/estimated/below` |
| `statistics` | object | 与 `/predict` 完全一致 |
| `sample_count` | object | `{input, version, rejected}` |
| `batch_final` | object | `{file, row_count}` |

### 3.3 `statistics` 字段（`/predict` 与报告一致）

| 键 | 出现在 | 含义 |
|---|---|---|
| `input_samples` | start/finalize | 输入条数 |
| `total_duration_sec` | start/finalize | 总时长（秒，3 位小数） |
| `integrity_checked` | start/finalize | 完整性检查条数 |
| `integrity_failed` | start/finalize | 完整性失败条数 |
| `snr_checked` | start/finalize | 参与 SNR 检查条数 |
| `snr_measured` / `snr_estimated` | start/finalize | 实测 / 盲估条数 |
| `snr_below_threshold` | start/finalize | 低于阈值条数 |
| `snr_below_ratio` | start/finalize | 低于阈值占比（4 位小数） |
| `sample_size` / `sample_size_actual` | start/finalize | 公式抽样数 / 真实入队数 |
| `review_required` | start/finalize | 是否需人工 |
| `review_queue` / `reviewed` / `review_passed` / `review_rejected` | finalize | 复核队列与判定数 |
| `auto_rejected` | finalize | 自动检查不通过而直接剔除的条数 |
| `passed` / `rejected` | finalize | 最终通过 / 剔除条数 |
| `version_sample_count` | finalize | 可进入版本的条数（= passed） |
| `conclusion` | start/finalize | `待复核` / `通过` / `不通过` |

---

## 4. 时间、数值、空值口径

| 项 | 口径 |
|---|---|
| 时间 | ISO8601 带时区（`2026-11-02T14:15:40+08:00`） |
| 时长 | 秒，float；写入时沿用上游 3 位小数，`statistics` 汇总四舍五入 3 位 |
| `snr_db` | dB，float，3 位小数；无法计算为 `null` |
| 占比 | float，4 位小数（如 `0.6667`） |
| 空值 | 统一 `null`，不写 `""` 冒充；`qc.snr_db` / `qc.snr_source` 允许 `null` |
| 编码 | UTF-8，无 BOM；JSONL 每行一个 JSON 对象 |

---

## 5. 失败与错误码

| 场景 | 错误码 / 行为 |
|---|---|
| 无输入 / 输入文件不存在 | `missing_file:` |
| 输入不是 JSONL / 空表 | `invalid_jsonl:` / `missing_file:` |
| JSONL 坏行 / 重复 `sample_id` | `invalid_jsonl:` |
| params 非法（mode、比例、阈值、seed、decision、未知 sample_id） | `invalid_params:` |
| `finalize` 时复核未判定完 | `human_review_incomplete:`（**不允许提交**） |
| `finalize` 找不到 start 状态且输入已变化 | 自动重算并写 warning，不报错 |
| 自动检查不通过 | `code=0`、`status=partial`、结论 `不通过`（业务结果，不是系统错误） |
| 未预期异常 | `internal_error:` |

---

## 6. 样例目录

```
dataset/sample/
├── 005_batch.sample.jsonl          3 条样本（2 条实测 SNR + 1 条盲估，均高于阈值）
└── audio/
    ├── clean.wav                   干净参考（供实测 SNR）
    ├── mix_20db.wav                20 dB 混音
    ├── mix_22db.wav                22 dB 混音
    └── mix_blind_25db.wav          25 dB 混音（不带参考，走盲估）
```

样本由 `tools/fixture_lib.py` 程序化合成，**不是真实业务录音**；合计约 257 KB。
