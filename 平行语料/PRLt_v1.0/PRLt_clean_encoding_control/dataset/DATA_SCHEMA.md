# 算子 3 数据 Schema（编码与控制字符清理）

正式编号 3，算子标识 `encoding_cleanup`，端口 8003。

## 1. 输入

UTF-8 / GB18030 / GBK 编码的 JSONL，每行一个 JSON 对象；通常来自算子 2 的 `02_validated.jsonl`，
或「通用粗清洗」阶段首节点由工程侧上传的文件。

```json
{
  "id": "sample-0001",
  "source": "源语言文本",
  "target": "目标语言文本",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {}
}
```

本节点**不校验** `source`/`target` 是否存在、是否为空、语言方向是否一致（算子 2 职责），
缺字段记录原样通过，避免与其他算子重复判定。

损坏 JSON 行、非对象 JSON 值、`metadata` 非对象 → 异常清单（带原始行号），不静默丢弃。
文件无法按任一配置编码严格解码 → 整次 `failed`，不产出输出文件，不使用 replacement 字符替代原文。

## 2. 输出

| 文件 | 说明 |
|---|---|
| `03_encoding_cleaned.jsonl` | 清理后的 UTF-8 JSONL（`passed` 条） |
| `03_encoding_rejects.jsonl` | 异常行：`line`、`record`、`reasons`（`rejected` 条） |
| `artifacts/stage_03_<uniqueId>.zip` | 阶段产物（主输出、异常清单与 `run.log`），2026-10-19 起取代 `lineage.json` |
| `run.log` | 逐行 `[时间] [动作] 消息`，只含文件名与真实数量 |

通过记录保留全部原字段，并在 `metadata` 中追加：

```json
{
  "encoding_original": "gb18030",
  "encoding_normalized": "utf-8",
  "control_chars_removed": 2
}
```

异常清单记录形如：

```json
{"line": 7, "record": null, "reasons": ["invalid_json"]}
```

## 3. 控制字符口径

- 移除：匹配 `[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]` 的 C0 区（除 `\t`）、C1 区与 `DEL`；
- 保留：`\t`（0x09）、`\n`（0x0A）、`\r`（0x0D）；
- 递归作用于记录的**所有字符串键与值**（含 `metadata`），每处移除真实计数；
- `remove_control_chars: false` 时完全不做替换，`control_chars_removed` / `records_changed` 均为 0。

## 4. 统计字段（`statistics`）

| 字段 | 含义 |
|---|---|
| `total` | 非空输入行数 |
| `passed` | 写入 `03_encoding_cleaned.jsonl` 的行数 |
| `rejected` | 写入 `03_encoding_rejects.jsonl` 的行数 |
| `records_changed` | 至少移除过 1 个控制字符的记录数 |
| `control_chars_removed` | 实际移除的控制字符总数 |
| `encoding` | 实际命中的输入编码（小写）；解码失败为 `null` |
| `reason:<code>` | 按稳定原因码的行数计数 |

恒等式：`total == passed + rejected`；`03_encoding_cleaned.jsonl` 行数 == `passed`；
`03_encoding_rejects.jsonl` 行数 == `rejected`。

稳定原因码：`invalid_json`、`invalid_record`、`invalid_type:metadata`。

## 5. 状态

| status | 条件 | 产物 |
|---|---|---|
| `success` | 无异常行 | 输出 + 空异常清单 |
| `partial` | 存在异常行 | 输出 + 异常清单 |
| `failed` | 配置错误 / 编码失败 / 读写失败 / 空输入 | 不产出输出与异常清单，`outputFile`、`rejectFile` 为 `null` |

## 6. 配置

```yaml
input_encodings: [utf-8, gb18030, gbk]
remove_control_chars: true
```

`OperatorConfig` 为 `extra="forbid"`；未知字段、空 `input_encodings`、类型错误 → 结构化 `failed`。
【无血缘改造 2026-10-19】原写入血缘 `config.config_error` 的逻辑已停用，配置快照不再落盘为
血缘文件。

## 7. 样例

`dataset/sample/demo.jsonl`（10 行）：7 条通过、1 行损坏 JSON、1 行非对象 JSON、1 条 `metadata` 为非对象。
CLI 冒烟结果：`status=partial total=10 passed=7 rejected=3 control_chars_removed=6 records_changed=2`。

## 8. 阶段产物

【无血缘改造 2026-10-19】原 `lineage.json`（10 个必需字段：`operator`、`operator_version`、
`uniqueId`、`inputFile`、`outputFile`、`status`、`startedAt`、`finishedAt`、`config`、`statistics`）
已停用，任务目录不再产出任何 lineage 文件。阶段内中间路径通过 `/predict` 返回的
`data.outputFile` 传递（供下一节点读取）；阶段结束前把主输出、异常清单与 `run.log` 打包为
`artifacts/stage_03_<uniqueId>.zip`，响应返回 `stageZip` 与 `storageUri`；阶段末 `data.outputFile`
是文件内容而非路径。
