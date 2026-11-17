# 算子 7 数据 Schema

## 输入

输入是算子 6（语言检测）产生的 UTF-8 JSONL，文件名默认 `05_layout_cleaned.jsonl`，每个非空行是一个 JSON 对象：

```json
{
  "id": "len-0001",
  "source": "欢迎使用平行语料系统。",
  "target": "Welcome to the parallel corpus system.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {}
}
```

必填字段：`id`、`source`、`target`。`source`、`target` 必须是字符串；`metadata` 必须是对象或缺省。
缺字段、类型错误、损坏 JSON 行不会静默丢弃，全部进入过滤清单。

本节点不改写任何文本，只读取 `source`、`target` 做长度判定。

## 长度定义

1. 先对字段做 Unicode **NFC** 归一（`é` 的组合形式与预组合形式计为同一长度）；
2. 再按 **Python 字符数**统计（不是字节数）；
3. **不删除**标点、空格和换行，`"a b"` 长度为 3；
4. 空字符串长度为 0，触发 `empty_source` / `empty_target`；
5. 长度比例 `length_ratio = target_chars / max(source_chars, 1)`，源文为空时用 1 兜底，不会除零。

## 阈值规则

所有阈值都是**闭区间**（等于边界视为通过）：

| 规则 | 原因名 | 触发条件 |
|---|---|---|
| 源文为空 | `empty_source` | `source_chars == 0` |
| 源文过短 | `source_too_short` | `0 < source_chars < min_source_chars` |
| 源文过长 | `source_too_long` | `source_chars > max_source_chars` |
| 目标文为空 | `empty_target` | `target_chars == 0` |
| 目标文过短 | `target_too_short` | `0 < target_chars < min_target_chars` |
| 目标文过长 | `target_too_long` | `target_chars > max_target_chars` |
| 比例过小 | `length_ratio_below_min` | 两端非空且 `length_ratio < min_length_ratio` |
| 比例过大 | `length_ratio_above_max` | 两端非空且 `length_ratio > max_length_ratio` |

任一端为空时不判定比例，避免同一条记录因为兜底比例被重复计数。

## 输出

- `06_length_filtered.jsonl`：通过（或 `warn` 模式下带警告保留）的 UTF-8 JSONL，原记录字段全部保留。
- `06_length_rejects.jsonl`：过滤清单，每行 `{"line": 行号, "record": 原记录或 null, "reasons": [...], "length_stats": {...}}`。
- `artifacts/stage_06_<uniqueId>.zip`：阶段产物（主输出、异常清单与 `run.log`），2026-10-19 起取代 `lineage.json`。
- `run.log`：带时间和动作标签的日志。

每条通过记录的 `metadata` 增加：

```json
{
  "length_stats": {"source_chars": 11, "target_chars": 39, "length_ratio": 3.545455}
}
```

`warn` 模式下违规但被保留的记录额外写入：

```json
{"length_warnings": ["length_ratio_below_min"]}
```

语言对软告警命中时还会写入可追溯策略：

```json
{
  "length_warnings": ["length_ratio_above_max"],
  "length_ratio_policy": {
    "language_pair": "zh-en",
    "base_min_length_ratio": 0.2,
    "base_max_length_ratio": 5.0,
    "hard_min_length_ratio": 0.1,
    "hard_max_length_ratio": 12.5,
    "action": "warn"
  }
}
```

原有 metadata 字段（例如算子 6 写入的 `detected_source_lang`、`language_detection_backend`）原样保留。

## 统计字段

`statistics` 全部来自真实处理结果：

- `total` / `passed` / `rejected` / `warned`；
- `language_pair_soft_warnings` / `hard_ratio_rejections` / `warning_pair:*`；
- 每类原因计数：`empty_source`、`empty_target`、`source_too_short`、`source_too_long`、`target_too_short`、`target_too_long`、`length_ratio_below_min`、`length_ratio_above_max`；
- 通用原因计数：`reason:invalid_json`、`reason:invalid_record`、`reason:missing_field:*`、`reason:invalid_type:*`；
- 实际观测极值：`min_observed_source_chars`、`max_observed_source_chars`、`min_observed_target_chars`、`max_observed_target_chars`、`min_observed_length_ratio`、`max_observed_length_ratio`；
- 配置回显：`on_violation`、`encoding`。

一致性要求：`06_length_filtered.jsonl` 行数 == `statistics.passed`；`06_length_rejects.jsonl` 行数 == `statistics.rejected`（忽略空行）。

## 配置

```yaml
min_source_chars: 1
max_source_chars: 5000
min_target_chars: 1
max_target_chars: 5000
min_length_ratio: 0.2
max_length_ratio: 5.0
on_violation: reject        # reject | warn
language_pair_ratio_policies:
  zh-en:
    hard_min_length_ratio: 0.1
    hard_max_length_ratio: 12.5
    soft_violation_action: warn
text_fields: [source, target]
input_encodings: [utf-8]
```

负数、`min > max`、比例非正数、硬边界落在基础告警区间内部、`on_violation` 非法枚举、未知字段、`text_fields` 不是 `[source, target]` 都会直接报配置错误并以 `failed` 结束，不会带默认值继续跑。
