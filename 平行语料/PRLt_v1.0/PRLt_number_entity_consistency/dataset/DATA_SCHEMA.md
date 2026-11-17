# 算子17 数据契约（数字与实体一致性检测）

- 算子编号：17
- 算子代码：`entity_consistency`
- 所在阶段：双语质量检测过滤（阶段内节点，**非阶段首算子**）
- 默认输入：`16_domain_checked.jsonl`
- 产出：`17_entity_checked.jsonl`、`17_entity_rejects.jsonl`、`artifacts/stage_17_<uniqueId>.zip`、`run.log`
  （2026-10-19 起不再产出 `lineage.json`）
- 字符集：输入输出均为严格 UTF-8

## 1. 输入记录（JSONL，每行一个对象）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 否 | 记录标识；拒绝清单会原样回填用于定位（`_id` 不带原文） |
| `source` | string | 是 | 源语言文本（字段名可由配置 `text_fields[0]` 改名） |
| `target` | string | 是 | 目标语言文本（字段名可由配置 `text_fields[1]` 改名） |
| `language_pair` | string | 是（`language_check=true` 时） | 语言方向，如 `zh-en`；也接受 `zh_en`、`zh→en`、`zh-cn-en-us` |
| `source_lang` / `target_lang` | string | 否 | 未提供 `language_pair` 时由二者拼接 |
| `metadata` | object | 否 | 透传元数据；非对象会被拒绝 |

本算子只读上述字段，**不改写原文**。

### 硬拒绝（只进拒绝清单，不进主输出）

| reasons 取值 | 触发条件 |
| --- | --- |
| `invalid_json` | 该行不是合法 JSON |
| `invalid_record` | 该行 JSON 不是对象 |
| `missing_field:<字段>` | 缺少 `source` / `target` |
| `invalid_type:<字段>` | `source` / `target` 不是字符串，或 `metadata` 不是对象 |
| `empty_<字段>` | `source` / `target` 去空白后为空 |
| `missing_language_pair` | 开启语言校验但无法解析语言方向 |
| `language_pair_mismatch` | 记录的 language_pair 与配置方向不一致 |

> `metadata` 非对象时 reason 固定为 `invalid_type:metadata`。

## 2. 主输出记录（`17_entity_checked.jsonl`）

在原记录基础上**追加**下列字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `entity_consistency_status` | string | `consistent` / `inconsistent` / `waiting_manual` |
| `entity_evidence` | object | 抽取与匹配证据，见 §3 |
| `entity_backend` | string | `rule`（默认）或 `model`（模型后端实际生效） |
| `entity_rule_version` | string | 规则口径版本，当前 `v1.0.0` |
| `entity_model_version` | string\|null | 模型后端真实版本；规则后端为 `null`（**不伪造**） |
| `entity_model_confidence` | number\|null | 模型真实置信度；规则后端为 `null` |
| `entity_conflict_reasons` | string[] | 冲突原因码，如 `missing_in_target:number`、`model_error` |
| `entity_checked_at` | string | 检测时间（ISO 8601，UTC+8） |
| `review_status` | string | `accepted`（consistent）/ `waiting_manual` |
| `metadata` | object | 保留原字段并镜像上述摘要（`entity_*_total`、`review_status` 等） |

### 状态判定口径

| 状态 | 条件 |
| --- | --- |
| `inconsistent` | 存在至少一处 `missing_in_target` / `extra_in_target` / `value_mismatch` |
| `waiting_manual` | 无冲突，但存在 `unparsed`（畸形数字/日期等）或字段超出 `max_text_scan_chars` 被截断 |
| `consistent` | 无冲突且无 unparsed |

冲突优先级高于无法解析（可定位的缺陷优先暴露）。

## 3. `entity_evidence` 结构

```json
{
  "rule_version": "v1.0.0",
  "backend": "rule",
  "model_version": null,
  "model_confidence": null,
  "model_verdict": null,
  "category_counts": {
    "number":     {"source": 1, "target": 1, "matched": 0, "missing_in_target": 0, "extra_in_target": 0, "value_mismatch": 1, "unparsed": 0},
    "date":       {"source": 1, "target": 1, "matched": 1, "missing_in_target": 0, "extra_in_target": 0, "value_mismatch": 0, "unparsed": 0}
  },
  "totals": {
    "source": 2, "target": 2, "matched": 1,
    "missing_in_target": 0, "extra_in_target": 0,
    "value_mismatch": 1, "unparsed": 0
  },
  "conflicts": [
    {
      "category": "number",
      "type": "value_mismatch",
      "source": "1234.5",
      "target": "1243.5",
      "source_span": [10, 16],
      "target_span": [12, 18]
    }
  ],
  "conflicts_truncated": 0,
  "unparsed": [
    {
      "side": "source", "field": "source", "category": "number",
      "reason": "unparsed_number", "hash": "9f2c1d...", "length": 5, "span": [3, 8]
    }
  ],
  "unparsed_truncated": 0,
  "conflict_reasons": ["value_mismatch:number"],
  "scan_truncated": []
}
```

字段说明：

- `category_counts`：**只包含启用的类别**，每类给出 source/target 抽取数、匹配数、漏写数、多写数、错写数、无法解析数；
- `totals`：各字段总和；`totals.unparsed` 统计两侧 unparsed 条目总数；
- `conflicts`：按 `max_conflicts_per_record` 截断（真实总数见 `category_counts` 与 `conflicts_truncated`）；
  - `type` ∈ `missing_in_target`（源有目标无）、`extra_in_target`（目标多出）、`value_mismatch`（双边值/单位冲突）；
  - 数值类（number/percentage/currency/date/time/unit）输出**归一值** `source` / `target` 与 `source_span` / `target_span`；
  - `unit` 额外带 `dimension`，`currency` 额外带 `currency`；
  - `entity` 类**只输出哈希与长度** `source_hash` / `source_length` / `source_type` 与 `entity_type`，**绝不输出原文**；
- `unparsed`：畸形片段（如 `1.2.3`、`2024-13-45`），只带 `hash`（sha256 前 16 位）与 `length`，同样不输出原文；
- `scan_truncated`：被 `max_text_scan_chars` 截断的字段名列表（截断是安全上限，避免超长文本拖垮抽取）。

## 4. 拒绝 / 审计清单（`17_entity_rejects.jsonl`）

```json
{"line": 4, "id": "x-1", "language_pair": "zh-en", "reasons": ["value_mismatch"],
 "entity_consistency_status": "inconsistent", "entity_backend": "rule",
 "entity_conflict_reasons": ["value_mismatch:date"]}
```

- 硬拒绝（§1 表）只出现在此清单；
- `inconsistent_policy` / `waiting_manual_policy` 为 `mark_and_keep`（默认）时，对应记录既在主输出中出现，也在此清单留下审计条目；
  为 `move_to_rejects` 时该记录**只**出现在此清单，`reasons` 以 `policy:<status>` 开头；
- 清单条目**永不包含 `source` / `target` 原文**。

## 5. 阶段产物 zip（2026-10-19 无血缘改造后）

原 `lineage.json`（10 字段数组）已停用，任务目录不再产出任何 lineage 文件。
本算子在最终成功/部分成功/降级/待复核返回前生成阶段产物：

```text
OUTPUT_DIR/<uniqueId>/artifacts/stage_17_<uniqueId>.zip
├── 17_entity_checked.jsonl
├── 17_entity_rejects.jsonl（存在才打包）
└── run.log
```

- `storageUri`：配置 `OBJECT_STORE_DIR` 时为归档文件 `file://` 地址，否则为
  `local-mirror://<uniqueId>/<zip名>` 逻辑地址；
- 中间节点 `data.outputFile` 为输出文件**路径**；阶段末 `data.outputFile` 为文件**内容**。

## 6. statistics（全部来自真实处理结果）

| 字段 | 含义 |
| --- | --- |
| `input_records` | 实际读入的非空行数 |
| `passed_records` | 主输出实际落盘行数（由文件重新统计，不用内存计数） |
| `rejected` | 拒绝清单实际落盘行数 |
| `schema_error` | 硬拒绝条数（损坏 JSON、缺字段、类型错误、语言不匹配） |
| `audit_records` | 主输出内记录的审计条目数 |
| `moved_to_rejects` | 因策略被移出主输出的条数 |
| `consistent` / `inconsistent` / `waiting_manual` | 三类业务状态条数 |
| `model_scored_records` / `model_error_records` | 模型实际打分条数 / 模型错误条数 |
| `matched_total` / `missing_in_target_total` / `extra_in_target_total` / `value_mismatch_total` / `unparsed_tokens` | 全部记录的匹配汇总 |
| `category_counts` | 全部记录按类别的汇总（结构同 `entity_evidence.category_counts`） |
| `backend` / `model_version` / `degrade_reason` / `rule_version` / `encoding` | 后端与口径元信息 |

恒等关系（测试断言）：`passed_records + schema_error + moved_to_rejects == input_records`。

## 7. 归一化与单位口径

| 类别 | 归一方式 |
| --- | --- |
| `number` | Unicode 十进制数字（含全角）转 ASCII；千分位 `,`/`_` 去除；尾零归一（`3.50` = `3.5`） |
| `percentage` | 统一为比率（`50%` = `50 percent` = `0.5`） |
| `currency` | 归一为 `代码:金额`（`$100` = `100美元` = `USD:100`；`¥`/`￥` 默认按 CNY） |
| `date` | `YYYY-MM-DD`（支持 `2024-01-02`、`2024/1/2`、`2024.01.02`、`2024年1月2日`、`January 2, 2024`、`2 January 2024`），非法月日不识别 |
| `time` | `HH:MM`/`HH:MM:SS`（支持 `15:00`、`3:00 PM`、`下午3点`、`上午9点30分`） |
| `unit` | 白名单内换算到基准单位：`5 km` = `5000 m`、`1 t` = `1000 kg`、`212 °F` = `100 °C`；**不在白名单（furlong 等）不做任何猜测换算** |
| `entity` | 按配置正则抽取（编译时加 IGNORECASE），默认 `casefold` 比较；证据只输出哈希与长度 |

## 8. 与相邻算子的边界

- 上游算子 16 已提供 `language_pair` 与领域筛选结果，本算子不重复做领域判定；
- 本算子只做**数值/日期/时间/单位/实体**层面的机械一致性校验，不做语义等价判断（那属模型后端的可选项）；
- 后续算子可直接读取 `entity_consistency_status` 与 `entity_evidence` 做交付前复核。
