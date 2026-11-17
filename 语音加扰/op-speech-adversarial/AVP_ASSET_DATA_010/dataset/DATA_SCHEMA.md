# 数据资产统计（AVP_ASSET_DATA_010）输入输出契约

本文件说明：输入来自哪个文件、输出到哪个文件、每个字段的类型/含义/空值口径。
字段名与《语音加扰算子开发规范》§8 对齐：`010_asset.json` 必含
`{total_samples, dataset_count, new_in_range, per_dataset, trend}`，其余字段为只增不改名的补充字段。

---

## 1. 输入

### 1.1 输入来源与优先级

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `params.registry_path` | 样本注册表路径（素材库数据集 + 入库记录）。绝对路径直接用；相对路径相对 `ctx.work_dir` 解析 |
| 2 | `ctx.input_file`（文件） | 单文件：登记表 JSON / 记录 JSONL / 006 单版本产物 JSON |
| 3 | `ctx.input_file`（目录） | 递归扫描 `io.input_exts`（`.jsonl` / `.json`），逐个解析后合并 |

`params.registry_path` 与 `inputFile` 同时存在时以 `registry_path` 为准（007/006 产物的典型接入方式）；
两者都没有时抛 `missing_file:`。

### 1.2 登记表 JSON（推荐格式）

```json
{
  "datasets": [
    {
      "dataset_id": "DS-20261020-001",
      "name": "客服对话语音原始集-V2",
      "source": "业务回流",
      "sample_count": 3,
      "created_at": "2026-10-20",
      "updated_at": "2026-10-28 14:32"
    }
  ],
  "records": [
    {"dataset_id": "DS-20261020-001", "uttid": "BAC009S0001", "created_at": "2026-10-27T09:00:00+08:00"}
  ]
}
```

`datasets[]` 字段（键名大小写不敏感，按别名顺序取第一个非空值）：

| 字段 | 别名 | 类型 | 必填 | 空值口径 |
|---|---|---|---|---|
| dataset_id | `datasetId` / `id` | string | 否 | 缺省时用 `name` 生成稳定 ID；仍无则归入 `__unregistered__` |
| name | `dataset_name` / `datasetName` / `title` | string | 否 | 缺省显示 `dataset_id` |
| source | `data_source` / `dataSource` | string | 否 | 缺省输出 `null` |
| sample_count | `sampleCount` / `count` / `total` | int ≥ 0 | 否 | 缺省时用该数据集的入库记录条数 |
| created_at | `createdAt` / `create_time` / `registered_at` | string（见 §1.5） | 否 | 缺省时用该数据集最早一条记录的入库日作为「创建日」代理 |
| updated_at | `updatedAt` / `update_time` | string（见 §1.5） | 否 | 缺省时用最新记录日；再缺用创建日；仍无输出 `null` |

`records[]` / 记录 JSONL 每行字段：

| 字段 | 别名 | 类型 | 必填 | 空值口径 |
|---|---|---|---|---|
| dataset_id | `datasetId` | string | 否 | 缺省时按 `dataset_name` 归集；仍无归入 `__unregistered__` |
| dataset_name | `datasetName` | string | 否 | 仅用于回填数据集名称 |
| uttid | `uttId` / `sample_id` / `id` | string | 否 | **仅用于日志与来源说明，不参与去重** |
| created_at | `createdAt` / `ingest_time` / `ingested_at` | string（见 §1.5） | 否 | 缺失/不可解析 → 计入总量、不计入新增，状态置 `partial` 并告警 |
| audio_path / text / duration / source | 同 001 契约 | — | 否 | 忽略，不影响统计 |

### 1.3 记录 JSONL（001/002 语料直接接入）

每行一条记录（`001_corpus.jsonl` 契约），字段同上；总样本数 = 行数（不去重）。
同一 `uttid` 出现在两个数据集时各计一次。

### 1.4 006 单版本产物兼容

```json
{"version_id": "V-20261028-001", "version_name": "客服场景-加扰集 v1.2",
 "sample_count": 1200, "published_at": "2026-10-28T12:00:00+08:00"}
```

命中 `version_id` 且带 `sample_count` 时视为一个数据集：
`dataset_id = version_id`、`name = version_name`、`sample_count` 原值、`created_at/updated_at = published_at`。
此时没有入库明细，`new_in_range = 0`，状态 `partial` 并告警「没有入库记录」。

### 1.5 时间解析口径

- 时区：`asset.timezone`（默认 `Asia/Shanghai`）。本机无 tzdata 时用 `asset.utc_offset_hours`（默认 8）固定偏移兜底。
- 带偏移的 ISO 字符串按自身偏移换算；`Z` 后缀按 UTC；无时区字符串按上述本地时区解释。
- 可接受格式见 `time.formats`（`YYYY-MM-DDTHH:MM:SS+08:00`、`YYYY-MM-DD HH:MM:SS`、`YYYY-MM-DD` 等）。
- 所有输出时间统一为 `time.output_formats.datetime`（`%Y-%m-%d %H:%M:%S`）+ 日期 `%Y-%m-%d`。

---

## 2. 输出 `010_asset.json`

| 字段 | 类型 | 含义 | 空值口径 |
|---|---|---|---|
| total_samples | int | 资产总量 = 各数据集样本数之和（**不去重**） | 输入无数据集 → `0` |
| dataset_count | int | 数据集对象个数 | 无 → `0` |
| new_in_range | int | 近 N 天新增样本（按 `created_at`） | 无可解析时间 → `0` + 告警 |
| time_range_days | int | 生效的 N（7/30/90） | 默认 7 |
| range.start / range.end | string | 窗口首尾日期（含今天，共 N 天） | — |
| per_dataset[] | array | 数据集列表（**存量**，不随 N 变化；关键词过滤时同口径） | 无 → `[]` |
| per_dataset[].dataset_id | string | 数据集 ID | — |
| per_dataset[].name | string | 数据集名称 | 缺省用 ID |
| per_dataset[].source | string\|null | 来源 | 缺省 `null` |
| per_dataset[].sample_count | int | 该数据集样本数 | 缺省用记录条数 |
| per_dataset[].new_in_range | int | 该数据集近 N 天新增 | 无 → `0` |
| per_dataset[].updated_at | string\|null | 最后更新时间（`%Y-%m-%d %H:%M:%S`） | 无 → `null` |
| trend[] | array | **连续 N 天**的逐日趋势，缺日补 0 | 无 → 全 0 数组（长度仍为 N） |
| trend[].date | string | 日期 `%Y-%m-%d` | — |
| trend[].new_samples | int | 当日新增样本数 | 0 |
| trend[].new_datasets | int | 当日新增数据集数（按创建日代理，见 §3） | 0 |
| filter.keyword | string\|null | 生效的关键词 | 无 → `null` |
| sources[] | array | 实际读取的输入文件及类型/条数（溯源自证） | — |
| caliber | object | 口径说明快照（前端可直接展示） | — |
| warnings[] | array | 数据质量问题（最多 `io.max_warnings` 条） | `[]` |
| generated_at | string | 生成时间（本地时区，`%Y-%m-%d %H:%M:%S`） | — |

`per_dataset` 排序：`sample_count` 降序 → `name` → `dataset_id`，保证同输入同输出。

---

## 3. 统计口径

1. **资产总量不去重**：同一音频属于两个数据集时计两次（规范 §11 第 11 条）。
2. **近 N 天新增**：窗口 = `[今天-(N-1), 今天]`，按入库时间 `created_at` 聚合；无数据日补 0。
3. **trend 连续**：固定 N 个日期元素，缺日补 `{new_samples:0, new_datasets:0}`，前端可直接画折线/柱状。
4. **新增数据集**：`new_datasets` 按数据集创建日聚合（代理口径）：
   优先用登记表 `created_at`；缺失时退化为该数据集最早入库日。均早于窗口 → 当日为 0。
5. **关键词过滤**：作用于整个结果（指标卡 + 列表 + 趋势），保证「列表合计 = 指标卡」。
6. **partial 判定**（不静默降级）：
   - 存在记录缺少可解析入库时间 → `partial` + 告警；
   - 登记表声明 `sample_count > 0` 但没有任何入库记录 → `partial` + 告警（该数据集新增恒为 0）；
   - 单文件无法识别为登记表/记录/版本产物 → 抛 `invalid_params:`；
   - 输入 JSONL 坏行 → 抛 `invalid_jsonl:`。

---

## 4. 错误码

| 前缀 | 触发条件 |
|---|---|
| `missing_file:` | 没有可用输入（无 `registry_path` 且无 `inputFile`，或路径不存在） |
| `invalid_params:` | `time_range` 不在 7/30/90；JSON 无法识别为登记表/记录/版本产物 |
| `invalid_jsonl:` | JSONL 存在坏行（非 JSON 或非对象） |
| `internal_error:` | 未预期异常（由 `inference.py` 统一收口） |

---

## 5. 示例

输入：`dataset/sample/asset_registry_sample.json`（2 个数据集、7 条记录，DS-1 与 DS-2 共用 `BAC009S0001`）。

输出（节选，`time_range=7`，生成日 2026-11-02）：

```json
{
  "total_samples": 7,
  "dataset_count": 2,
  "new_in_range": 5,
  "time_range_days": 7,
  "range": {"start": "2026-10-27", "end": "2026-11-02"},
  "per_dataset": [
    {"dataset_id": "DS-20261018-002", "name": "AISHELL-3公开数据集", "source": "公开采购",
     "sample_count": 4, "new_in_range": 2, "updated_at": "2026-11-02 10:30:00"},
    {"dataset_id": "DS-20261020-001", "name": "客服对话语音原始集-V2", "source": "业务回流",
     "sample_count": 3, "new_in_range": 3, "updated_at": "2026-10-28 14:32:00"}
  ],
  "trend": [
    {"date": "2026-10-27", "new_samples": 1, "new_datasets": 0},
    {"date": "2026-10-28", "new_samples": 2, "new_datasets": 0},
    {"date": "2026-10-29", "new_samples": 0, "new_datasets": 0},
    {"date": "2026-10-30", "new_samples": 0, "new_datasets": 0},
    {"date": "2026-10-31", "new_samples": 0, "new_datasets": 0},
    {"date": "2026-11-01", "new_samples": 0, "new_datasets": 0},
    {"date": "2026-11-02", "new_samples": 2, "new_datasets": 0}
  ]
}
```

`BAC009S0001` 在 DS-1、DS-2 各出现一次 → 总量按 7 计（3+4），没有按 uttid 去重成 6。
