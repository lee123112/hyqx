# AVP_EVAL_DATA_007 · 数据契约（DATA_SCHEMA）

> 算子：评测结果接收（`avp_ingest_eval_result`）　阶段：S2 评测执行（1/1，阶段末）
> 本文件是**输入 → 输出**的字段唯一真源；与《语音加扰算子开发规范》§8 冲突时以规范为准。

---

## 1. 数据来源与边界

| 项 | 内容 |
|---|---|
| 上游 | 外部「数据集评测」主动推送的评测结果（本系统不做评测执行） |
| 输入形态 | `.jsonl`（每行一条）为主；兼容单文件 `.json`（对象 / 数组 / `{"items"\|"records"\|"results": [...]}`） |
| 输入单位 | 一条记录 = 一个「评测任务 × 模型 × 版本」的结果集 |
| 输出 | `007_eval_results.jsonl`（落库）、`007_parse_failed.jsonl`（解析失败）、`007_eval_report.json`（汇总报告） |
| 不做 | 不做评测执行、不改指标数字、不补造缺失字段、不把失败记录「尽量落库」 |

---

## 2. 输入记录（外部回传）字段

### 2.1 必填六项（规范 §8，不可放宽）

| 字段 | 类型 | 空值 | 说明 |
|---|---|---|---|
| `external_task_id` | string(1–128) | 不可空 | 外部评测任务号；同一任务重推按 `last_wins` 覆盖 |
| `version_id` | string(1–128) | 不可空 | 版本 ID；**必须存在**于版本库，否则整条解析失败 |
| `metrics` | object | 不可空 | `{cer, wer, ser}` 三个指标，单位 **百分比 0–100**（不是 0–1 小数） |
| `dimensions` | array | 可为空数组 | 维度得分；每条样本只带一个 `perturb` 快照，因此各取值是**互斥分组**（占比合计 1） |
| `error_mix` | object | 不可空 | `{sub, del, ins, unk}` 非负整数条数（替换/删除/插入/未识别） |
| `sample_results` | array | 不可空数组 | 样本级结果；`full`=每样本一条，`subset`=只回传错误样本 |

### 2.2 可选字段

| 字段 | 类型 | 空值 | 说明 |
|---|---|---|---|
| `model` | string | 允许缺失 | 受测模型名；缺失只告警（下游按模型对比时无法匹配） |
| `total_samples` | int > 0 | 允许缺失 | 样本总数；缺失时按 `Σ dimensions[].samples` 推导，再退化到 `len(sample_results)` |
| `dimensions[].ratio` | float 0–1 | 允许缺失 | 样本占比；缺失时按 `samples / total_samples` 推算（4 位小数） |
| `sample_results[].perturb` | object | 允许缺失 | 加扰参数快照，见 §2.3 |

### 2.3 `dimensions[]` 元素

| 字段 | 类型 | 空值 | 说明 |
|---|---|---|---|
| `dimension` | string | 不可空 | 维度名（噪声 / 语速 / 环境音 …） |
| `value` | string | 不可空 | 维度取值（人群嘈杂 / 1.2x / 街道 …） |
| `cer` `wer` `ser` | float 0–100 | 不可空 | 该取值的指标（百分比） |
| `samples` | int ≥ 1 | 不可空 | 该取值样本数 |
| `ratio` | float 0–1 | 见上 | 占总样本比例 |

### 2.4 `sample_results[]` 元素与 `perturb` 快照

| 字段 | 类型 | 空值 | 说明 |
|---|---|---|---|
| `sample_id` | string | 不可空 | 样本 ID；也接受 `uttid` / `id` 作为别名 |
| `ref_text` | string | 允许缺失 | 参考文本；也接受 `ref` / `reference` |
| `hyp_text` | string | 允许缺失 | 识别文本；**空字符串表示「未识别」**（`unk`） |
| `error_type` | enum | 允许缺失 | `sub`/`del`/`ins`/`unk`/`none`；接受中文别名 替换/删除/插入/未识别/正确/无错误 |
| `errors` | object | 允许缺失 | 计数字典；缺 `error_type` 时按 `sub > del > ins > unk` 取主导类型 |
| `perturb` | object | 允许缺失 | 见下；也可用扁平写法 `dimension`/`value`/`snr_db`/`speed`/`asset` |

`perturb` 快照结构（与 003/004 产物保持一致，**一条样本只有一个维度快照**）：

| 字段 | 类型 | 单位/口径 | 空值 |
|---|---|---|---|
| `dimension` | string | 维度名 | 与 `value` 同时缺省 |
| `value` | string | 维度取值 | 与 `dimension` 同时缺省 |
| `snr_db` | float | dB（可负） | 允许空 |
| `speed` | float > 0 | 倍速系数（1.0=原速） | 允许空 |
| `asset` | string | 噪声/环境素材名 | 允许空 |

> 时间口径：本算子不处理音频时长；`received_at` 为带时区的 ISO8601（`+08:00`）。时长类字段（秒，float，3 位小数）属于音频算子，本算子不产出。

### 2.5 版本库（判定版本 ID 是否存在）

三种来源按优先级合并：`known_versions`（参数）→ `registry_path`（参数）→ `configs/config.yaml` 的 `versions.default_registry_path`。

注册表文件支持四种形态（006 产物兼容）：

```json
{"version_id": "V-1", "status": "已发布"}
[{"version_id": "V-1", "status": "已发布"}]
{"versions": [{"version_id": "V-1", "status": "已回滚"}]}
{"V-1": {"status": "已发布"}}
```

- **不存在** → 整条解析失败（`version_not_found`），不落库；
- **存在且状态 = 已回滚**（或列在 `rolled_back_versions`）→ 落库，`attributable=false`；
- 只在 `known_versions` 里、无状态 → 推定 `version_status=已发布`（能被评测拉取说明当时已发布）。

---

## 3. 输出文件

### 3.1 `007_eval_results.jsonl`（落库）

一行一条归一化记录，字段如下（**契约字段一个不改名，只增不改**）：

| 字段 | 类型 | 空值 | 说明 |
|---|---|---|---|
| `eval_id` | string | 否 | 内部结果 ID，规则 `{ids.eval_id_prefix}-{external_task_id}`（重推幂等） |
| `external_task_id` | string | 否 | 外部评测任务号 |
| `version_id` | string | 否 | 版本 ID |
| `model` | string \| null | 是 | 受测模型名 |
| `metrics` | object | 否 | `{cer, wer, ser}` 百分比 |
| `dimensions` | array | 否 | 元素含 `dimension/value/cer/wer/ser/samples/ratio` |
| `error_mix` | object | 否 | `{sub, del, ins, unk}` 非负整数 |
| `sample_results` | array | 否 | 元素含 `sample_id/ref_text/hyp_text/error_type/perturb` |
| `attributable` | bool | 否 | `false` = 版本已回滚，不参与归因与建议 |
| `received_at` | string | 否 | 接收时间 ISO8601（+08:00） |
| `sample_count` | int | 否 | 样本总数（推导见 §2.2） |
| `sample_results_mode` | enum | 否 | `full` / `subset`（subset 时下游不算置信区间） |
| `version_status` | string | 否 | `已发布` / `已回滚` |
| `warnings` | array | 否 | 一致性问题（只告警不改数），元素 `{code, detail}` |

`warnings.code` 取值：`ratio_mismatch`、`dimension_ratio_sum_mismatch`、`dimension_samples_mismatch`、`error_mix_mismatch`、`missing_model`。

### 3.2 `007_parse_failed.jsonl`（解析失败，**绝不落库**）

| 字段 | 说明 |
|---|---|
| `source_line` | 输入文件行号（从 1 开始） |
| `external_task_id` / `version_id` | 能解析出来就带，便于对方排查 |
| `reason_code` | 见下表 |
| `reason` | 人类可读原因（单条主因） |
| `problems` | 该条命中的全部问题 `[{code, field, message}]` |
| `raw_preview` | 原始 JSON 行预览（≤ `io.raw_preview_len` 字符） |

| `reason_code` | 触发场景 |
|---|---|
| `invalid_json` | 该行不是合法 JSON |
| `not_object` | 该行是 JSON 但不是对象 |
| `missing_field` | 必填字段/子字段缺失（`field` 指明位置，如 `metrics.cer`） |
| `type_error` | 类型不符（指标不是数字、`samples` 不是整数等） |
| `range_error` | 越界（指标不在 0–100、`samples` < 1、`error_mix` 为负等） |
| `empty_field` | 必填容器为空（如 `sample_results=[]`） |
| `duplicate_sample` | 同一条回传里 `sample_id` 重复 |
| `version_not_found` | 版本 ID 不在版本库 → 整条失败 |

### 3.3 `007_eval_report.json`（汇总）

```json
{
  "operator": "评测结果接收",
  "operator_id": "avp_ingest_eval_result",
  "generated_at": "2026-11-02T14:00:00+08:00",
  "input_file": "external_eval_results.jsonl",
  "version_bank": {"source": "param_list", "path": null, "count": 3, "rolled_back": 1, "known_versions": 2},
  "parameters": {"known_versions_provided": ["..."], "rolled_back_versions_provided": ["..."],
                 "registry_path": null, "extra_required_fields": [], "duplicate_policy": "last_wins"},
  "checks": {"required_fields": ["external_task_id", "..."], "rules": ["..."], "duplicate_policy": "last_wins"},
  "statistics": {"input_lines": 5, "blank_lines": 0, "valid_records": 3, "parse_failed": 2,
                 "deduplicated": 0, "attributable": 2, "not_attributable": 1,
                 "total_samples": 630, "dimension_entries": 10, "sample_result_entries": 630,
                 "warnings": 0, "known_versions": 3, "rolled_back_versions": 1},
  "parse_failed_by_reason": {"version_not_found": 1, "missing_field": 1},
  "warnings_by_code": {},
  "parse_failed_detail": [], "parse_failed_truncated": 0,
  "warnings_detail": [], "warnings_truncated": 0,
  "duplicate_events": [],
  "conclusion": "partial",
  "results_preview": [{"eval_id": "EV-EVT-E2E-001", "version_id": "V-20261101-01", "cer": 8.0}]
}
```

`conclusion`：`success`（全部通过）/ `partial`（部分失败仍有落库）/ `failed`（全部失败、未落库）。
`/predict` 的 `code` 仍为 0（批量业务结果不是服务错误），`data.status` 与 `conclusion` 同步。

---

## 4. 与下游的衔接

- **008 差异归因** 的输入就是 `007_eval_results.jsonl`：只用 `attributable=true` 的记录；
  按 `version_id` + `model`（或 `version_id`）配对目标与基准记录。
- 008 需要的 `sample_count`、`sample_results_mode`、`dimensions[].ratio`、`sample_results[].perturb`
  均由本算子归一化好，008 **不再猜字段**。
- `attributable=false` 的记录下游必须跳过（版本已回滚不参与归因与建议生成）。

---

## 5. 一致性告警（只告警不阻断）

| 告警 | 判定 | 处理 |
|---|---|---|
| `ratio_mismatch` | \|ratio − samples/total\| > `validation.ratio_tolerance` | 保留外部给的比例，只告警 |
| `dimension_ratio_sum_mismatch` | 全部取值 ratio 合计偏离 1 | 只告警 |
| `dimension_samples_mismatch` | Σ samples ≠ 样本总数 | 只告警 |
| `error_mix_mismatch` | error_mix 某类条数 ≠ 样本级该形态计数 | 只告警（汇总口径可能是字符级） |
| `missing_model` | 回传无 model | 落库，`model=null` |

告警不改变任何数字，也不把记录踢出落库；下游可据此判断数据质量。
