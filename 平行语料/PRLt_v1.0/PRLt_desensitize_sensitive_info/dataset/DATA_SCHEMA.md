# 算子 8 数据 Schema

## 1. 输入

输入是算子 7 产生的 UTF-8 JSONL，每个非空行是一个 JSON 对象：

```json
{
  "id": "sample-0001",
  "source": "如需帮助请联系 zhang.san@example.com 或 13800138000。",
  "target": "Please contact zhang.san@example.com or 13800138000 for help.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {}
}
```

必填字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | string | 源文，必填，缺失进异常清单 |
| `target` | string | 目标文，必填，缺失进异常清单 |
| `id` | string/int | 可选，仅用于异常清单定位；疑似含敏感值时置空 |
| `metadata` | object | 可选，缺省视为 `{}`；非对象进异常清单 |

`source_lang` / `target_lang` 及其他业务字段原样透传，本算子不修改语言声明。
输入编码默认只接受严格 UTF-8（算子 3 已完成编码归一）。

## 2. 处理规则

| 类型 token | 覆盖内容 | 默认规则 |
|---|---|---|
| `email` | 邮箱地址（含多级域名） | `email_basic` |
| `phone` | 中国大陆手机号（可带 `+86`）、固话 | `phone_cn_mobile`、`phone_cn_landline` |
| `id_card` | 18 位 / 15 位身份证号 | `id_card_18`、`id_card_15` |
| `account` | 16-19 位银行卡号、带分隔符卡号、`账号/卡号` 等键值形式（英文 `account` 关键词后须有 `: ： # =` 分隔符，避免把 accountable 之类的普通词当账号值） | `bank_card_plain`、`bank_card_spaced`、`account_kv_cn` |
| `address` | 省/自治区 + 市 + 区县 链式地址、直辖市 + 区县 链式地址 | `address_cn_province`、`address_cn_municipality` |

替换口径：

- 占位符 `[REDACTED_<TYPE>_<N>]`，**不可逆**，只保留类型与序号，不含原始值；
- 重叠命中按 `priority` 升序 → 匹配更长者优先 → 起点靠前优先消解，同一字符只替换一次；
- 同一记录内「同类同值」共享序号，因此源文/目标文可确认的对应片段会得到同一 token；
- 未声明允许脱敏的 `metadata` 字段一律不改写。

同步校验：

- `sync_check_mode: value_multiset`（默认）要求源/目标「类型 + 规范化值多重集」完全一致；
  `type_counts` 只比较各类型数量；`off` 关闭校验；
- 不一致时记录 `redaction_alignment_warning`；`strict_sync: true`（默认）时该记录
  转入异常清单（`status: waiting_manual`），不做单边修改，也不猜测对应关系；
- `strict_sync: false` 时记录保留在主输出，`redaction_status` 为 `redacted_with_warning`。

## 3. 输出

| 文件 | 内容 |
|---|---|
| `08_deidentified.jsonl` | 脱敏后的 UTF-8 JSONL（原子替换落盘） |
| `08_deidentification_rejects.jsonl` | 异常/待人工清单，**不携带任何原文或敏感值** |
| `artifacts/stage_08_<uniqueId>.zip` | 阶段产物（主输出、异常清单与 `run.log`），2026-10-19 起取代 `lineage.json` |
| `run.log` | 带时间与动作标签的日志，只含类型与数量 |

异常清单条目结构：

```json
{
  "line": 5,
  "id": "deid-0005",
  "record_omitted": "sensitive_content",
  "status": "waiting_manual",
  "reasons": ["redaction_alignment_warning"],
  "redaction_rule_version": "rules-v1.0.1",
  "redaction_types": {"phone": 1}
}
```

`reasons` 取值范围：

| reason | 含义 |
|---|---|
| `invalid_json` | 该行不是合法 JSON |
| `invalid_record` | JSON 合法但不是对象 |
| `missing_field:source` / `missing_field:target` | 缺少必填文本字段 |
| `invalid_type:source` / `invalid_type:target` | 文本字段不是字符串 |
| `invalid_type:metadata` | `metadata` 不是对象 |
| `invalid_type:metadata.<field>` | 声明脱敏的 metadata 字段不是字符串 |
| `hit_limit_exceeded` | 单条命中数超过 `max_hits_per_record` |
| `redaction_alignment_warning` | 源/目标无法确认对应关系（待人工） |

主输出每条记录的 `metadata` 增加：

```json
{
  "redaction_status": "redacted",
  "redaction_types": {"email": 2, "phone": 2},
  "redaction_rule_version": "rules-v1.0.1",
  "redaction_total": 4,
  "redaction_alignment_warning": false
}
```

`redaction_status` 取值：`unchanged` | `redacted` | `redacted_with_warning`。

## 4. 统计

`statistics` 字段全部来自真实处理结果（保留在处理器统计结果与 `run.log` 中）：

| 字段 | 含义 |
|---|---|
| `input_records` | 输入非空行数 |
| `output_records` | 写入主输出的记录数 |
| `rejected_records` | 未写入主输出的记录数（含硬拒绝与待人工） |
| `hard_rejected_records` | 结构/类型/规模硬拒绝记录数 |
| `waiting_manual_records` | 因同步告警转入待人工的记录数 |
| `redacted_records` | 发生至少一处替换的输出记录数 |
| `unchanged_records` | 无命中的输出记录数 |
| `alignment_warnings` | 非严格模式下带告警仍保留的记录数 |
| `redaction_counts` | 各类型真实替换次数（如 `{"email": 5}`） |
| `total_redactions` | 替换总数 |
| `redaction_rule_version` | 规则版本 |
| `encoding` | 实际使用的输入编码 |
| `reason:<reason>` | 各异常原因计数 |

不变量：`input_records == output_records + rejected_records`。

## 5. 配置

完整配置见 `configs/config.yaml`：

```yaml
input_encodings: [utf-8]
text_fields: [source, target]
metadata_fields: [contact]
placeholder_template: "[REDACTED_{type}_{index}]"
rule_version: "rules-v1.0.1"
sync_check_mode: value_multiset   # value_multiset | type_counts | off
strict_sync: true
max_hits_per_record: 200
address_regions: [北京, 天津, 上海, 重庆, 广东, ...]
rules: [...]        # 默认 10 条规则，可整体覆盖
extra_rules: []     # 追加自定义规则
```
