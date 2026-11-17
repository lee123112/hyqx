# 01 语料入库匹配校验 · 数据契约（DATA_SCHEMA）

> 算子：`avp_ingest_corpus`　目录：`AVP_INGEST_AUD_001`
> 上游：用户上传的 ZIP（音频 + 可选参考文本）　下游：02 语音标准化

---

## 1. 输入

### 1.1 上传包（`/uploadFile` 之后）

`inference.py` 已把 zip 解压到 `/data/{uniqueId}/input/extracted/`，**保留目录结构**。
本算子从该目录递归扫描音频。

| 支持形态 | 说明 |
|---|---|
| 同名 `.txt` 配对 | `a.wav` 旁边放 `a.txt`，整行内容为参考文本（推荐，`manifest_mode=none`） |
| 清单文件 | `.txt` / `.tsv` / `.csv` / `.jsonl`，通过 `manifest_mode=file` + `manifest_path` 指定 |

### 1.2 清单格式（`manifest_mode=file`）

| 格式 | 示例 | 解析口径 |
|---|---|---|
| TXT（空格分隔） | `BAC009S0002W0122 广州市交易会` | AISHELL / LibriSpeech 风格，第 1 列 id，其余拼接为文本 |
| TSV（制表符） | `path\tsentence` + 行 | Common Voice 风格，有表头则按列名猜 |
| CSV（逗号） | `path,sentence` + 行 | 同上 |
| JSONL | `{"audio":"a.wav","text":"..."}` | 按 key 猜（`path/audio/file/id/uttid/wav/wav` 与 `sentence/text/transcript/label/content/ref`） |

**分隔符自动识别**：按行内出现次数，优先级 **制表符 → 逗号 → 空格**。
可用 `delimiter` 参数强制覆盖（`auto` / `tab` / `comma` / `space`）。

**编码**：默认 UTF-8 并自动去 BOM。检测到 **GBK/GB18030 直接报错**
（`encoding_not_utf8`）要求转存 UTF-8，**不静默猜编码**。

**列指定**：`id_column` / `text_column` 为 0 基列号；不传则按表头列名猜，
仍猜不到就取「第 1 列 id、第 2 列起拼文本」。

### 1.3 uttid 口径（全产线统一）

```
uttid = 音频文件名去掉扩展名，casefold 归一化（大小写不敏感）
```

- 精确匹配，**不做模糊匹配**（原型研发标注明确「精确匹配，不做模糊匹配」）；
- ZIP 内**保留目录结构**，但取**文件名**做 uttid；
- 不同目录下同名文件 → **重复 uttid** → **阻断创建**。

---

## 2. 输出

### 2.1 `001_corpus.jsonl`（主产物，下游 02 的输入）

一行一条已入库样本，UTF-8、无 BOM、`\n` 换行。

| 字段 | 类型 | 含义 | 空值口径 |
|---|---|---|---|
| `dataset_id` | string | 数据集 ID，形如 `DS-20261102140030` | 不空（自动生成） |
| `dataset_name` | string | 数据集名称，2–50 字 | 不空 |
| `source` | string | 数据来源，4 选 1 | 不空 |
| `uttid` | string | 归一化音频 id | 不空，且全局唯一 |
| `audio_path` | string | 容器内音频绝对路径 | 不空 |
| `text` | string | 参考文本（已 `strip`） | 不空（空则不入库） |
| `duration` | float | 时长（秒，3 位小数） | 不空 |
| `sample_rate` | int | 音频原生采样率（Hz） | 不空 |
| `channels` | int | 声道数 | 不空 |
| `format` | string | libsndfile 格式名，**小写**（`wav`/`mp3`/`flac`） | 不空 |
| `created_at` | string | 入库时间，ISO8601 带时区 | 不空 |

> `duration` / `sample_rate` / `channels` / `format` 是**原生值**，标准化留给 02。

### 2.2 `001_rejects.jsonl`

只装**未入库**的样本（本算子仅一类：缺文本）。

| 字段 | 类型 | 含义 |
|---|---|---|
| `uttid` | string | 归一化 id |
| `file` | string | 原文件名（仅 basename） |
| `reason` | string | `missing_text` |
| `detail` | string | 人读说明（「清单无对应 id 或文本为空」/「文本仅含空白」） |

### 2.3 `001_ingest_report.json`

| 字段 | 类型 | 含义 |
|---|---|---|
| `dataset_id` / `dataset_name` / `source` / `created_at` | — | 同语料表 |
| `manifest` | object | 清单解析元信息（`mode` / `format` / `encoding` / `delimiter` / `header` / `rows` / `id_column` / `text_column` / `bad_rows`） |
| `statistics` | object | 统计数（与 `data.statistics` **完全相同**） |
| `checks.rules` | string[] | 本次生效的检测规则文案 |
| `checks.note` | string | 「只提示不阻断」说明 |
| `checks.short_audio` | object[] | `{uttid, duration, threshold, problem:"短音频"}` |
| `checks.empty_audio` | object[] | `{uttid, rms, threshold, problem:"空音频"}` |
| `checks.duplicate_text` | object[] | `{text(截 80 字), uttids[], count}` |
| `checks.extra_text` | string[] | 清单有、音频无的 uttid（**只提示**） |
| `checks.unreadable` | object[] | `{uttid, file, reason}` |
| `checks.truncated` | object | 各列表被截断的条数（`io.max_listed`） |
| `rejects` | object[] | 同 `001_rejects.jsonl`（截断后） |
| `rechecked` | object | 仅 `action=recheck` 后出现：`{at, rows, corpus}`——重检时间、参与重检的入库条数、语料文件名 |
| `checks.rechecked_at` | string | 仅重检后出现：本次重检时间（ISO 8601，秒级） |
| `checks.recheck_note` | string | 仅重检后出现：说明「只复跑三项检查，未改写入库结果」 |
| `conclusion` | string | `success` / `partial` |

### 2.4 `statistics` 字段表（`data.statistics` 与报告一致）

| 键 | 含义 |
|---|---|
| `audio_files` | 扫描到的音频文件数 |
| `input_records` | 归一化后的 uttid 数（去重前按文件算） |
| `manifest_ids` | 参考文本条目数 |
| `matched` | 成功入库条数 |
| `missing_text` | 缺文本（未入库）条数 |
| `extra_text` | 文本多余条数（只提示） |
| `unreadable` | 无法解码条数 |
| `short_audio` | 短音频条数（只提示） |
| `empty_audio` | 空音频条数（只提示） |
| `duplicate_text_groups` | 文本重复**组**数 |
| `total_duration_sec` | 入库音频总时长（秒） |
| `avg_duration_sec` | 入库平均时长（秒） |

---

## 3. 业务判定表（只提示 vs 阻断）

| 情形 | 处置 | 是否入库 | 错误码 |
|---|---|---|---|
| 缺文本 / 文本为空或仅空白 | 列入 `rejects` | ❌ 不入库 | 无（记为 partial） |
| 文本多余 | 列入 `checks.extra_text` | — | 无 |
| 重复 uttid | **阻断整个创建** | ❌ 全批不入库 | `duplicate_uttid:` |
| 时长 < 2s | 列入 `checks.short_audio` | ✅ 仍然入库 | 无 |
| 全静音 | 列入 `checks.empty_audio` | ✅ 仍然入库 | 无 |
| 文本完全重复 | 列入 `checks.duplicate_text` | ✅ 仍然入库 | 无 |
| 音频无法解码 | 计入 `unreadable` | ❌ 不入库 | 无（记为 partial） |
| 无任何音频 | 直接失败 | — | `missing_file:` |
| 全部无文本 | 直接失败 | — | `missing_text:` |
| 清单 GBK 编码 | 直接失败 | — | `encoding_not_utf8` |

---

## 4. 样本目录说明

```
dataset/sample/
├── same_name_txt/     3 条 wav + 同名 txt（推荐形态，manifest_mode=none）
├── manifest_tsv/      2 条 wav + manifest.tsv（清单形态，manifest_mode=file）
└── edge_cases/        short_audio（0.6s，触发短音频提示）+ no_text（触发缺文本不入库）
```

样本由 `utils/make_samples.py` 程序化合成，**不是真实业务录音**。
合计约 473 KB，可随包交付。
