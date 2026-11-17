# AVP_ATTRIBUTE_DATA_008 · 数据契约（DATA_SCHEMA）

> 算子：差异归因（`avp_attribute_gap`）　阶段：S3 效果分析（1/2）
> 输入是 **007 的落库产物**，输出以《语音加扰算子开发规范》§8 的
> `008_attribution.json` 为最小契约；本文列出的附加字段只增不改。

---

## 1. 数据来源与边界

| 项 | 内容 |
|---|---|
| 输入 | 007 `007_eval_results.jsonl`（一行一条归一化评测结果） |
| 输入目录形态 | 传目录时优先找 `007_eval_results.jsonl`，否则取第一个 `.jsonl/.json` |
| 输入 JSON 形态 | JSONL；也兼容单文件 JSON 数组 / `{"records"｜"items"｜"results"｜"data": [...]}` |
| 输出 | `008_attribution.json`（逐条归因）、`008_alerts.json`（维度告警）、`008_version_compare.json`（版本对比） |
| 不做 | 不做评测、不改指标、不生成建议文案（009）、不看已回滚版本、不做跨版本发布判断 |

---

## 2. 输入字段（007 落库契约）

| 字段 | 类型 | 空值 | 本算子用途 |
|---|---|---|---|
| `eval_id` | string | 不可空 | 记录 ID、`record_id` 组成 |
| `external_task_id` | string | 不可空 | 推荐给 009 的任务号（`result.task_id`） |
| `version_id` | string | 不可空 | 版本对比基线选择 |
| `model` | string \| null | 允许空 | 跨模型对比的匹配键 |
| `metrics` | object | 不可空 | `{cer, wer, ser}` 百分比；默认取 `cer` 做差值 |
| `dimensions` | array | 可为空 | 元素 `{dimension, value, cer, wer, ser, samples, ratio}` |
| `error_mix` | object | 不可空 | `{sub, del, ins, unk}` 汇总错误条数（样本级缺失时的兜底） |
| `sample_results` | array | 不可空 | 元素 `{sample_id, ref_text, hyp_text, error_type, perturb}` |
| `sample_count` | int | 允许空 | 样本总数；缺失时按维度合计 / 样本条数推导 |
| `sample_results_mode` | enum | 允许空 | `full` / `subset`；影响置信区间是否可用 |
| `attributable` | bool | 允许空（默认 true） | **false 的记录直接跳过**（版本已回滚） |

`perturb` 快照（007 归一化后的结构，一条样本只有一个维度快照）：

```json
{"dimension": "噪声", "value": "人群嘈杂", "snr_db": 8.0, "speed": null, "asset": null}
```

> 字段级校验由 007 完成；008 只做输入契约的最低检查（缺 `error_mix` 等关键字段直接报
> `invalid_jsonl`，不猜、不默认补值）。

---

## 3. 基线选择规则

| `baseline_type` | `baseline_id` | 行为 |
|---|---|---|
| `model`（默认） | **必填**（对比对象模型名） | 基准 = 同 `model` 的记录；优先选与目标同 `version_id` 的一条；目标 = 其余模型里样本最多的一条 |
| `version` | 可选 | 给了 `baseline_id` → 基准取该版本；未给 → 按 `version_id` 字典序取**上一版本**为基准、最新版本为目标 |
| 其它 | — | `invalid_params:` |

- 同一版本/模型有多条记录时取 `sample_count` 最大的一条（同数按 `eval_id` 稳定排序）；
- 找不到基准（模型名/版本 ID 不存在）→ `invalid_params:`，**不退回「自己跟自己比」**；
- 只有一条可归因记录且 `baseline_type=version` → `invalid_params:`，提示补上一版本结果；
- 已回滚（`attributable=false`）记录在基线选择前即被剔除，并计入
  `statistics.skipped_not_attributable`。

---

## 4. 输出：`008_attribution.json`

### 4.1 契约根字段（§8，不可改名）

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `record_id` | string | 否 | `ATTR-{目标 eval_id}-{基准 eval_id}` |
| `eval_id` | string | 否 | 目标记录 ID |
| `baseline` | object | 否 | 基准信息，见 4.2 |
| `total_net_error` | int | 否 | 整体净多错条数（可负） |
| `main_cause` | string \| null | **是** | `维度/取值`；无显著主因时为 `null` |
| `share` | float \| null | **是** | 主因占比（0–1，4 位小数）；无主因时 `null` |
| `affected` | int \| null | **是** | 主因影响条数；无主因时 `null` |
| `confidence` | enum \| null | **是** | `低`/`中`/`高`；无主因时 `null` |
| `factors` | array | 否 | 维度因素列表，见 4.3 |
| `chain` | array | 否 | 归因链，见 4.4 |

### 4.2 `baseline`

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `type` | enum | 否 | `model` / `version` |
| `id` | string | 否 | 基准模型名或基准版本 ID |
| `eval_id` | string | 否 | 基准记录 ID |
| `external_task_id` | string | 允许空 | 基准外部任务号 |
| `version_id` | string | 否 | 基准版本 |
| `model` | string \| null | 是 | 基准模型 |
| `metrics` | object | 否 | 基准整体指标 |
| `sample_count` | int | 否 | 基准样本数 |

### 4.3 `factors[]`（每个维度取值一条）

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `dimension` / `value` | string | 否 | 维度名 / 取值 |
| `in_scope` | bool | 否 | 是否在归因维度白名单内 |
| `samples` | int | 否 | 该取值样本数（决定置信度与影响条数分母） |
| `ratio` | float \| null | 是 | 样本占比 |
| `target_metric` / `baseline_metric` | float \| null | 是 | 目标/基准的同指标值（默认 CER） |
| `target_metrics` / `baseline_metrics` | object | 否 | cer/wer/ser 三项 |
| `baseline_samples` / `baseline_ratio` | int/float \| null | 是 | 基准侧样本数/占比 |
| `diff_pp` | float \| null | **是** | 差值 pp（目标 − 基准，4 位小数）；不可比时 null |
| `deltas_pp` | object | 否 | cer/wer/ser 三项差值（可比时） |
| `affected` | int \| null | **是** | 影响条数（可负，四舍五入） |
| `share` | float | 否 | 影响条数 / 总净多错；总净多错 ≤0 时为 0 |
| `confidence` | enum \| null | 是 | 该维度样本数对应置信档（白名单内才给） |
| `comparable` | bool | 否 | 目标与基准同维度同取值且指标齐全 |
| `baseline_present` | bool | 否 | 基准是否有该取值 |
| `sample_trace` | object | **是** | 样本级证据，见 4.5；无样本级结果时 null |

### 4.4 `chain[]`（归因链，逐层可追）

| `level` | `ref` 关键字段 | 说明 |
|---|---|---|
| `record` | eval_id / external_task_id / version_id / model / sample_count / sample_results_mode / metric / value | 目标记录 |
| `baseline` | type / eval_id / version_id / model / sample_count / metric / value | 基准记录 |
| `summary` | main_cause / reason（+`trace_for=top_factor`） | 仅无主因时出现 |
| `dimension` | dimension / value / samples / ratio / target_metric / baseline_metric / diff_pp / affected / share / confidence / in_scope | 被追踪的维度因素 |
| `error_type` | source / error_samples / error_counts / ci_half_pp / top_snr_bucket | 错误形态与证据强度 |
| `sample` | count / sample_ids / truncated / note | 错误样本 ID（最多 `max_trace_samples` 条） |

无主因时，链路仍会挑「占比最高的可比维度」追到样本级并标 `trace_for=top_factor`，
保证「无主因」也能解释到样本；若没有任何可比维度，链路止于 `summary`。

### 4.5 `sample_trace`（证据强度）

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `status` | enum | 否 | `exact`（可完整回溯）/ `partial` / `unavailable` |
| `sample_results_mode` | enum | 否 | `full` / `subset` |
| `listed_samples` / `tagged_samples` | int | 否 | 样本级条数 / 落在该维度的条数 |
| `target_samples` | int | 否 | 该维度样本数（来自 dimensions） |
| `error_samples` | int \| null | 是 | 该维度错误条数；无法完整回溯时 null |
| `error_rate` | float \| null | 是 | 错误率（error_samples / 样本数） |
| `ci_half_pp` | float \| null | 是 | 95% 置信区间半宽（pp）：`1.96·√(p(1−p)/n)·100` |
| `error_counts` | object | 否 | `{sub, del, ins, unk}` 样本级构成 |
| `snr_buckets` | array | 否 | `[{label, count}]`，按配置边界分桶 |
| `top_snr_bucket` | string \| null | 是 | 错误最集中的 SNR 桶 |
| `sample_ids` | string[] | 否 | 错误样本 ID（截断） |
| `sample_ids_truncated` | int | 否 | 被截断条数 |
| `note` | string \| null | 是 | 降级原因 |

### 4.6 附加字段（只增）

| 字段 | 类型 | 含义 |
|---|---|---|
| `generated_at` | string | 生成时间 ISO8601（+08:00，秒级） |
| `metric` | string | 本次归因使用的指标（默认 `cer`） |
| `external_task_id` / `version_id` / `model` | string | 目标记录身份（便于 009 / 011 直接引用） |
| `sample_count` / `sample_results_mode` | int / enum | 目标样本规模与模式 |
| `metrics` / `baseline_metrics` | object | 目标/基准整体指标 |
| `error_mix` | object | 目标整体错误形态汇总（样本级缺失时的兜底） |
| `main_cause_dimension` / `main_cause_value` | string \| null | 主因拆开存放 |
| `main_cause_reason` | string | 主因判定/未判定原因（「无显著主因：…」） |
| `main_cause_detail` | object \| null | 主因的样本数、差值、占比、置信度、错误构成、CI、SNR 桶 |
| `alert_count` | int | 告警条数 |
| `alert_threshold_pp` | float | 本算子实际告警阈值（009 在拿不到 alerts 文件时用它兜底） |
| `scope` | object | `{dimensions, requested, fallback}` 实际参与与是否退化 |
| `warnings` | string[] | 基线选择/白名单退化等提示（不影响结论） |

---

## 5. 输出：`008_alerts.json`

```json
{
  "generated_at": "2026-11-02T14:00:00+08:00",
  "record_id": "ATTR-EV-EVT-E2E-001-EV-EVT-E2E-001-BL",
  "eval_id": "EV-EVT-E2E-001",
  "external_task_id": "EVT-E2E-001",
  "metric": "cer",
  "threshold_pp": 1.5,
  "count": 2,
  "alerts": [
    {"code": "dimension_diff_gt_threshold", "dimension": "噪声", "value": "人群嘈杂",
     "metric": "cer", "diff_pp": 7.3, "threshold_pp": 1.5, "samples": 120,
     "affected": 9, "in_scope": true,
     "message": "噪声/人群嘈杂 CER 差值 7.30pp > 告警阈值 1.50pp"}
  ]
}
```

告警条件：**可比维度**的 `diff_pp` 严格大于 `alert_pp`（等于不告警）；不限于白名单，
白名单外维度也会列出并标 `in_scope=false`，便于发现白名单漏配。

---

## 6. 输出：`008_version_compare.json`

| 字段 | 类型 | 空值 | 含义 |
|---|---|---|---|
| `applicable` | bool | 否 | 仅 `baseline_type=version` 时为 true |
| `baseline_type` / `baseline_id` | string | 否 | 对比类型与基准标识 |
| `target` / `baseline` | object | 否 | `{eval_id, version_id, model, metrics, sample_count}` |
| `overall_delta_pp` | object | **是** | cer/wer/ser 整体差值 pp；不可比时为空对象 |
| `dimension_deltas` | array | 否 | 可比维度的差值、样本数、影响条数、`in_scope` |
| `note` | string | 否 | 说明「model 类型不是版本对比」 |

> `baseline_type=model` 时 `applicable=false`：模型对比也会写目标/基准真实数据，但
> **不冒充版本结论**（版本比较只认跨版本对比）。

---

## 7. 计算口径（与规范 §11.9 一一对应）

| 量 | 口径 |
|---|---|
| 维度差值 | `diff_pp = 目标指标 − 基准指标`（pp，4 位小数） |
| 影响条数 | `affected = half_up(diff_pp / 100 × samples)`；负差值保留负号 |
| 总净多错 | `total_net_error = half_up((目标整体指标 − 基准整体指标) / 100 × 目标样本数)` |
| 占比 | `share = affected / total_net_error`（总净多错 >0；否则 0） |
| 主因 | 白名单内、可比、`affected>0` 的因素里，`share` 最高且 ≥`main_cause_share`；并列或低于阈值 → null |
| 置信度 | 主因样本数 <100 低 / 100–200 中 / >200 高 |
| 告警 | 可比维度 `diff_pp > alert_pp`（严格大于） |
| 舍入 | 一律 half-up（2.5→3、−2.5→−3），不用银行家舍入 |
| 置信区间 | 主因维度错误率 p 的 95% 半宽 `1.96·√(p(1−p)/n)·100`，仅样本可完整回溯时给 |

时间口径：本算子不处理音频时长；`generated_at` 为带时区 ISO8601。

---

## 8. 与 009 的衔接

- 009 读 `008_attribution.json`（单个对象）与 `008_alerts.json`，用 `factors[].diff_pp`、
  `share`、`samples`、`sample_trace`、`main_cause*` 生成建议；
- `attributable=false` 的记录在 008 已剔除，009 不需要重复过滤；
- 008 的 `total_net_error`、`main_cause`、`share`、`affected`、`confidence` 是 009
  「事实」段与「证据强度」段的唯一数据来源，009 不得重算或改写。
