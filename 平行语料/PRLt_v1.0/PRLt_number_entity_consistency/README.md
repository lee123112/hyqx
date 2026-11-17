# 算子17 · 数字与实体一致性检测（entity_consistency）

平行语料流水线「双语质量检测过滤」阶段的中间节点。对读入的每条双语记录，抽取数字、百分比、
货币、日期、时间、白名单单位与可配置实体，做归一化后按类别一一比对，识别**漏写、错写、单位变化、
实体不一致**，为数据交付与质量报告提供可解释证据。

| 项 | 取值 |
| --- | --- |
| 算子编号 | 17 |
| 算子代码 | `entity_consistency` |
| 服务端口 | `8017` |
| 阶段位置 | 双语质量检测过滤（阶段内后续节点，**非阶段首算子**） |
| 默认输入 | `16_domain_checked.jsonl` |
| 主输出 | `17_entity_checked.jsonl` |
| 异常清单 | `17_entity_rejects.jsonl` |
| 阶段产物 / 日志 | `artifacts/stage_17_<uniqueId>.zip` / `run.log`（2026-10-19 起不再产出 `lineage.json`） |
| 默认后端 | `rule`（规则归一化，纯 CPU） |
| 规则版本 | `v1.0.0` |

## 1. 功能边界

**做**

- 抽取并归一：Unicode 十进制数字、千分位、小数尾零、百分比、货币、日期、时间、白名单单位、可配置实体；
- 类别化匹配：`missing_in_target`（漏写/漏译）、`extra_in_target`（多写/无据增译）、`value_mismatch`（错写、单位变化）；
- 单位白名单内换算比较：`5 km` = `5000 m`、`1 t` = `1000 kg`、`212 °F` = `100 °C`；
- 畸形片段（`1.2.3`、`2024-13-45`、`25:99`）记为 `unparsed`，不静默丢弃；
- 逐条产出可解释证据，聚合产出真实 `statistics`；输出严格 UTF-8、原子替换落盘。

**不做**

- 不做语义等价判断（“翻倍” 与 “200%” 不会被判为一致）；
- 不做真实语言检测（只校验记录声明的 `language_pair` 与配置方向是否一致）；
- 不做领域判定（属算子 16）；
- 不做拼写/流畅度修正（只检测，不改写原文）；
- 不提供 `/downloadFile`。

## 2. 输入 / 输出字段

输入、输出、拒绝清单与 `entity_evidence` 的完整字段说明见 **`dataset/DATA_SCHEMA.md`**（与本操作实际输出一致）。

输出记录在原记录基础上追加：

```
entity_consistency_status  consistent | inconsistent | waiting_manual
entity_evidence            证据对象（类别计数、totals、conflicts、unparsed、scan_truncated）
entity_backend             rule | model
entity_rule_version        v1.0.0
entity_model_version       真实模型版本或 null（规则后端为 null，绝不伪造）
entity_model_confidence    真实置信度或 null
entity_conflict_reasons    冲突原因码，如 missing_in_target:number
entity_checked_at          ISO 8601（UTC+8）
review_status              accepted | waiting_manual
metadata                   保留原字段并镜像上述摘要
```

状态判定：有冲突 → `inconsistent`；无冲突但有 unparsed / 扫描截断 → `waiting_manual`；否则 `consistent`。

## 3. 配置（`configs/config.yaml`）

未知字段一律拒绝（`extra="forbid"`），配置错误会让整次任务结构化失败并写明 `配置错误：ValidationError`。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `input_encodings` | `[utf-8]` | 候选编码，全部失败即 `failed` |
| `input_file_name` | `16_domain_checked.jsonl` | 上游默认产物名（阶段内实际路径由 `inputFile` 指定） |
| `text_fields` | `[source, target]` | 源在前、目标在后；可改字段名 |
| `source_lang` / `target_lang` | `zh` / `en` | 期望语言方向 |
| `language_check` | `true` | 是否校验 `language_pair` |
| `enabled_categories` | 全部 7 类 | 可裁剪以关闭某类检查 |
| `numeric_absolute_tolerance` / `numeric_relative_tolerance` | `0.0` / `0.0` | 容差：`|a-b| <= abs + rel*max(|a|,|b|)` |
| `unit_conversion` | `true` | 关闭后仍抽取单位但只按字面单位比较 |
| `disabled_dimensions` | `[]` | 指定量纲不参与换算 |
| `unit_aliases` | `{}` | 额外单位别名 → 白名单单位代码 |
| `currency_aliases` | `{}` | 货币符号/词 → ISO 代码（如需 `¥`=JPY 在此覆盖） |
| `entity_patterns` | 2 条演示模式 | 实体名 → 正则列表，编译时统一 `IGNORECASE` |
| `entity_case_sensitive` | `false` | 实体是否区分大小写 |
| `backend` | `rule` | `rule` / `auto` / `model` |
| `model_service_url` / `model_path` / `model_version` / `model_entrypoint` | `null`… | 模型后端适配器配置 |
| `fallback_policy` | `rule` | 模型不可用：`rule`=降级并标 degraded，`fail`=结构化失败 |
| `model_confidence_threshold` | `0.5` | 低于阈值不采纳模型「不一致」结论，改判 `waiting_manual` |
| `max_model_errors` | `20` | 单次任务允许的模型错误条数，超过即整次 `failed` |
| `inconsistent_policy` / `waiting_manual_policy` | `mark_and_keep` | `mark_and_keep`=主输出保留+审计；`move_to_rejects`=只进异常清单 |
| `max_conflicts_per_record` / `max_unparsed_per_record` | `20` | 证据列表上限（真实总数仍在计数中） |
| `max_text_scan_chars` | `20000` | 单字段扫描上限，超出即截断并标 `scan_truncated` |

## 4. 算法与模型后端

### 4.1 抽取优先级（跨度消费）

`entity → date → time → currency → percentage → unit → 畸形片段（保留跨度）→ number`

先匹配的长跨度类型会消费字符区间，因此 `AB-1234` 只算实体、`1,234.50 元` 只算货币、
`2024-01-02` 只算日期，不会被重复计数成普通数字。

### 4.2 归一化口径

| 类别 | 归一方式 |
| --- | --- |
| `number` | Unicode 十进制数字（含全角、阿拉伯-印度数字）转 ASCII；千分位 `,`/`_` 去除；英文序数后缀 `st/nd/rd/th` 去除（`348th` = `348`）；尾零归一（`3.50` = `3.5`、`1,234.5` = `1234.5`）；量级词换算（`2.639亿` = `263900000`、`16.5 million` = `16500000`） |
| `percentage` | 统一为比率：`50%` = `50 percent` = `0.5` |
| `currency` | 归一为 `代码:金额`：`$100` = `100美元` = `USD:100`；`100元` = `100 yuan` = `CNY:100`；带量级词时先乘后比（`$263.9 million` = `2.639亿美元`） |
| `date` | `YYYY-MM-DD`：`2024-01-02` / `2024/1/2` / `2024.01.02` / `2024年1月2日` / `2018 年 9月 29 日`（含空格排版）/ `January 2, 2024` / `2 January 2024`；年月 `YYYY-MM`（`2013年6月` = `June 2013`）；**无年份的月日**归一为 `--MM-DD`（`4月7日` = `7 April`，与完整日期互不相等）；区间拆成两个日期（`13日至16日` / `13 to 16 July` / `13 and 14 May 2015` / 跨月 `27 April to 8 May 2015`）；非法月日不识别（转 unparsed） |
| `time` | `HH:MM(:SS)`：`15:00` / `3:00 PM` / `下午3点` / `上午9点30分` |
| `unit` | 白名单内换算到基准单位：`5 km`→`length:5000`、`1000 kg`→`mass:1000`、`212 °F`→`temperature:100`；数量支持空格千分位与量级词（`1 650万吨` = `16.5 million tons`）；白名单外（furlong 等）**不换算**，只按普通数字处理 |
| `entity` | 按配置正则抽取（`IGNORECASE`），默认 `NFC + casefold` 比较；证据只输出哈希与长度 |

单位白名单覆盖 6 个量纲：长度、质量、面积、体积、时长、温度（含 `°C/℃/摄氏度`、`°F/℉/华氏度`、
`K/开尔文`，温度使用显式公式而非线性因子）。数据量单位（KB/MB）故意不纳入，因 1000/1024 存在歧义。

时长量纲（2026-11-04 补齐）：`s/ms/min/h/d/wk/mo/qtr/yr`，中英文别名
`秒/分钟/小时/天/周/个月/月/个季度/季度/季/年` ↔ `second(s)/minute(s)/hour(s)/day(s)/week(s)/month(s)/quarter(s)/year(s)`。
月与季度按「365 天 ÷ 12 / ÷ 4」换算，保证 `1 year = 12 months = 4 quarters` 精确相等；
因此 **`3 年 vs 3 months` 判为 `inconsistent`（unit 类 `value_mismatch`），而
`3 年 = 36 months = 12 quarters` 判为 `consistent` 不误拦**；
`2024 年`、`3 月` 这类日历表达仍由日期抽取器优先接管，不当作时长单位。

区间与符号（2026-11-04 实跑修正）：`2015-2017年`、`15-19 years` 这类**区间后半截**不抽单位、
不消费跨度（两侧数字按裸数字对齐），避免被当成负数时长；负号只在前面不是数字时才算正负号，
`-5 °C` 仍按温度单位抽取。对应 `_is_range_tail` 与单位正则的 `(?<!\d)`。

### 4.3 模型后端（可选）

默认 `backend=rule`，规则的证据标记 `entity_backend=rule`、`entity_model_version=null`。

若显式配置 `backend=auto/model`：

- 本地适配器：`model_path` 指向 `.py`（或包含 `model.py` 的目录），调用 `model_entrypoint(source, target)`；
- HTTP 适配器：向 `model_service_url` POST `{source, target, entrypoint}`；
- 返回严格解析：`consistent` 必须为布尔；`confidence` 必须为 0~1 数值或 `null`；
  `model_version` 必须为非空字符串或 `null`；`evidence` 必须为数组或 `null`。
  任何不合规 → 逐条 `model_error`，该条判 `waiting_manual`，`entity_model_confidence=null`；
- 结论合并：规则已判 `inconsistent` 时保持 `inconsistent`；模型判 `inconsistent` 且置信度 ≥ 阈值 → `inconsistent`；
  模型判一致且规则因 unparsed 而 `waiting_manual` 且置信度达标 → `consistent`（标 `model_resolved`）；
  否则保守 `waiting_manual`。

### 4.4 降级行为（门禁表）

| 场景 | 行为 |
| --- | --- |
| `backend=rule` | 纯规则，不加载模型，`statistics.model_version=null` |
| `backend=auto` + 模型不可用 + `fallback_policy=rule` | 降级为规则，运行状态 `degraded`，`statistics.degrade_reason="model_unavailable: ..."`，记录仍标 `backend=rule` |
| `backend=auto` + 模型不可用 + `fallback_policy=fail` | 整次 `failed` |
| `backend=model` + 模型不可用 | 整次 `failed`，`statistics.backend="unavailable"` |
| 模型逐条报错超过 `max_model_errors` | 整次 `failed`（不静默截断） |

任何情况下都不会出现固定分数、随机分数或“模型不可用但仍标 model”的伪造结果。

## 5. 启动与调用

### 5.1 安装

```bash
pip install -r requirements.txt
```

### 5.2 启动服务

```bash
python inference.py            # 等价：uvicorn inference:app --host 0.0.0.0 --port 8017
```

### 5.3 Docker

```bash
docker build -t parallel-op17:1.0.0 .
docker run --rm -p 8017:8017 -v /host/output:/app/output parallel-op17:1.0.0
```

> 本次交付环境**未安装 Docker**，镜像构建未实际执行（见 `DEVELOPMENT_STATUS.md`）。

### 5.4 CLI

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

### 5.5 API（三接口）

```bash
# 1) 上传（阶段由工程侧调度时使用；本节点为阶段内节点，通常直接传上一算子的 outputFile）
curl -X POST "http://127.0.0.1:8017/uploadFile?uniqueId=task_001" -F "file=@demo.jsonl"

# 2) 阶段中间节点（isLastOperator=false）
curl -X POST http://127.0.0.1:8017/predict -H "Content-Type: application/json" -d '{
  "uniqueId": "task_001",
  "isLastOperator": false,
  "inputFile": "input/demo.jsonl"
}'

# 3) 阶段末节点（isLastOperator=true）
curl -X POST http://127.0.0.1:8017/predict -H "Content-Type: application/json" -d '{
  "uniqueId": "task_001",
  "isLastOperator": true,
  "inputFile": "input/demo.jsonl"
}'

# 4) 清理任务目录
curl -X POST "http://127.0.0.1:8017/removeFile?uniqueId=task_001"
```

响应外层固定 `{code, msg, data}`。`/predict` 请求体：`uniqueId`（必传）、`isLastOperator`（必传 strict bool）、
`inputFile`（本算子要求传）、`operatorId`（选传，若传须为 `"17"`）、`params`/`config`（选传对象，
`params` 优先，键名为 `configs/config.yaml` 英文字段名）。

成功响应 `data` 固定 4 个字段（不再返回 `lineageFile`）：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "outputFile": "<中间算子=输出文件路径；阶段末=输出文件内容>",
    "rejectFile": "<异常清单路径>",
    "stageZip": "<阶段产物 zip 路径>",
    "storageUri": "<对象存储地址：file:// 或 local-mirror://>"
  }
}
```

- 中间节点（`isLastOperator=false`）：`data.outputFile` 是输出文件**路径**，下一算子 `inputFile` 直接用它；
- 阶段末（`isLastOperator=true`）：`data.outputFile` 是输出文件 UTF-8 **内容**（不是路径）；
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_17_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`；
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回
  `local-mirror://<uniqueId>/<zip名>` 逻辑地址。

## 6. 输出示例（真实运行结果，节选）

`output_smoke/smoke_cli/17_entity_checked.jsonl` 第 4 行（漏写日期检测）：

```json
{
  "id": "entity-0004",
  "source": "合同签署日期为 2024-01-02。",
  "target": "The contract was signed on 2024-01-03.",
  "language_pair": "zh-en",
  "entity_consistency_status": "inconsistent",
  "entity_backend": "rule",
  "entity_rule_version": "v1.0.0",
  "entity_model_version": null,
  "entity_model_confidence": null,
  "entity_conflict_reasons": ["value_mismatch:date"],
  "review_status": "waiting_manual",
  "entity_evidence": {
    "rule_version": "v1.0.0",
    "backend": "rule",
    "category_counts": {
      "date": {"source": 1, "target": 1, "matched": 0, "missing_in_target": 0,
               "extra_in_target": 0, "value_mismatch": 1, "unparsed": 0}
    },
    "totals": {"source": 1, "target": 1, "matched": 0, "missing_in_target": 0,
               "extra_in_target": 0, "value_mismatch": 1, "unparsed": 0},
    "conflicts": [
      {"category": "date", "type": "value_mismatch",
       "source_span": [8, 18], "source": "2024-01-02",
       "target_span": [27, 37], "target": "2024-01-03"}
    ],
    "conflicts_truncated": 0,
    "unparsed": [],
    "unparsed_truncated": 0,
    "conflict_reasons": ["value_mismatch:date"],
    "scan_truncated": []
  }
}
```

说明：`category_counts` 只列出**实际启用的类别**，上例为节选（完整记录含全部 7 类）。
实体类冲突只输出 `source_hash` / `source_length` / `entity_type`，真实样例：

```json
{"category": "entity", "type": "missing_in_target",
 "source_span": [10, 17], "source_hash": "9f8525f66be7d9f2",
 "source_length": 7, "source_type": "product_code", "entity_type": "product_code"}
```

无法解析片段（`1.2.3`）真实样例（两侧各 1 条，`waiting_manual`）：

```json
{"side": "source", "field": "source", "category": "number",
 "reason": "unparsed_number", "hash": "c47f5b18b8a430e6", "length": 5, "span": [4, 9]}
```

## 7. 阶段产物与对象存储（无血缘改造 2026-10-19）

原 `lineage.json`（含 operator / operator_version / uniqueId / inputFile / outputFile / status /
startedAt / finishedAt / config / statistics）已停用：任务目录不再产出任何 lineage 文件，原实现保留在
`utils/lineage.py` 与 `processor.py` 注释中便于回溯。阶段内路径传递改用 `data.outputFile`，
阶段归档改用 `data.stageZip` / `data.storageUri`。

阶段产物 zip（真实运行结果，成员清单）：

```text
artifacts/stage_17_smoke_cli.zip
├── 17_entity_checked.jsonl     # 主输出
├── 17_entity_rejects.jsonl     # 异常清单（含审计条目）
└── run.log                     # 运行日志（无原文泄漏）
```

`storageUri`：未配置 `OBJECT_STORE_DIR` 时为 `local-mirror://smoke_cli/stage_17_smoke_cli.zip`；
配置后为归档文件的 `file://` 地址。

## 7.1 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.16 小节（算子 17）的 `param_` 行与
`configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_实体校验模式` | `enabled_categories` / `entity_patterns` | 默认仅数字/日期/时间/货币/百分比/单位校验（轻量无图谱）；启用 `entity` 类别并配置 `entity_patterns` 可做实体字面比对 |
| `param_匹配模式` | `numeric_absolute_tolerance` / `numeric_relative_tolerance` + `entity_case_sensitive` | 默认严格匹配（容差 0、实体区分大小写关闭即 NFC+casefold）；调大容差或开启大小写敏感即为宽松/严格口径 |

## 8. 资源需求

- **CPU 模式，无 GPU、无模型权重**；默认规则后端的单条耗时在微秒级；
- 内存与磁盘均为流式：逐行读取、逐行写出，峰值内存约为「单条记录 + 一行缓冲区」；
  单条文本长度由 `max_text_scan_chars` 兜底（默认 2 万字符）；
- 单任务产物与纯文本同量级（主输出 + 少量审计条目），无中间大文件；
- 模型后端为可选扩展（HTTP 或本地 `.py` 适配器），启用后资源需求由该模型决定。

## 9. 已知限制与未确认口径

**已知限制**

1. `¥` / `￥` 默认按 CNY 处理（JPY 需在 `currency_aliases` 显式覆盖）；
2. 前导点小数（`.5`）不识别；**裸数字**的空格千分位（`3 747 800`）不合并，只在货币/单位锚定处启用
   （`3 747 800美元`、`1 650万吨`）——避免把 `100 200 300` 这类并列列举并成一个数；
3. 中文 `3点` 无「上午/下午」前缀时按 `03:00` 处理（“点”本身存在上下午歧义，已记录口径）;
4. 单位白名单仅覆盖 6 个量纲，且**不包含**数据量单位（KB/MB 的 1000/1024 歧义）；
5. 单字母单位别名（`m`/`s`/`h`/`g`/`t`/`l`/`d`）在极端文本下存在误判风险，
   已用词边界断言与“最长优先”缓解；`in` 等常见英文词刻意不作为英寸别名；
6. 日期不识别美式 `M/D/YYYY` 顺序（歧义），仅识别 4 位年在前的形态与英文月名写法；
7. 判定是 token 级机械一致性，不做语义等价；
8. 数字写成文字（`4次` ↔ `four regional workshops`、`4年` ↔ `four years`）仍判不一致——
   没有数字就没有 token 可比，这是有意保留的已知残余；
9. 月份写成词/带修饰（`6月中旬` ↔ `mid-June`、`2月底` ↔ `the end of February`）仍判不一致；
10. 模型后端已实现适配器与严格解析，但本次环境**未接真实 XLM-R 权重**，
    仅通过注入替身验证了门禁、降级与解析路径。

**未确认口径（已在交付记录中标注，等待主 agent/工程侧确认）**

1. 冲突记录默认 `mark_and_keep`（保留在主输出并写审计条目），而非直接过滤删除；
   `waiting_manual` 亦保留。如需在本节点完成过滤，请把两个 policy 配为 `move_to_rejects`；
2. `entity_patterns` 默认的两条正则（`product_code`、`model_version`）为**演示默认值**，实际任务应替换；
3. `attempt` 级别的 tolerance 默认全为 0（严格相等），是否需要给数值类一个默认相对容差尚未确认。

## 10. 真实测试结果

**（1）2026-10-19 无血缘改造后实跑（本机 Python 3.9.7 / Windows）**

```
$ D:\python\python.exe -m pytest tests/ -q
64 passed in 3.29s
```

**（3）2026-11-04 时长单位补齐后（`PRLt_handoff/.venv_prlt`）**

```
$ .venv_prlt/Scripts/python.exe -m pytest tests -q
83 passed in 62.65s
```

> 2026-11-04 新增：时长单位 year/month/quarter（含中英文别名，`1 year = 12 months = 4 quarters` 精确相等）、
> 年份/年龄区间后半截不当时长（`2015-2017年`、`15-19 years`）、负号前置才按正负号（`-5 °C`）。

**（2）2026-11-03 抽取口径修订后（`PRLt_handoff/.venv_prlt`，Python 3.9）**

```
$ .venv_prlt/Scripts/python.exe -m pytest tests/ -q
78 passed in 4.40s
```

新增 14 项全部在 `tests/test_operator.py` 第 7 节（抽取口径修订）：空格千分位、英文序数、
段落编号排除、年月/月日/日期区间、量级词、脚注号、带空格中文日期，以及两条"仍是已知残余"的回归护栏。
全链路复跑结果见 `_测试报告_B1B2改造_20261102.md` 第 13 节。

- `tests/test_operator.py`：58 项，覆盖正常样例、边界值、空文件、损坏 JSONL、缺字段、
  错误配置（11 组参数化）、错误编码、任务隔离、路径安全、扫描截断、容差、
  单位白名单内/外换算、实体哈希脱敏、阶段产物 zip 与「无血缘文件」校验，
  以及模型后端的降级 / 失败 / 逐条错误 / 错误超限 / 伪造防护路径；
- `tests/test_api.py`：6 项，覆盖三接口四字段契约、阶段中间与阶段末返回形态、`operatorId` 校验、
  `params` 覆盖、`/downloadFile` 不存在（404）、两个 `uniqueId` 隔离、`inputFile` 目录穿越拒绝、
  业务配置错误返回 `code=1`。

```
$ D:\python\python.exe processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
status=partial  input_records=11  passed_records=8  rejected=8
schema_error=3  consistent=3  inconsistent=4  waiting_manual=1
matched_total=6  missing_in_target_total=2  extra_in_target_total=1
value_mismatch_total=1  unparsed_tokens=2  backend=rule  encoding=utf-8
stageZip=output_smoke/smoke_cli/artifacts/stage_17_smoke_cli.zip
storageUri=local-mirror://smoke_cli/stage_17_smoke_cli.zip

$ D:\python\python.exe -m json.tool map.json          # exit 0
$ D:\python\python.exe -m compileall -q .             # exit 0
```

阶段产物核对（实测）：`stage_17_smoke_cli.zip` 成员为 `17_entity_checked.jsonl`、
`17_entity_rejects.jsonl`、`run.log`；任务目录**无** `lineage.json`（`map.json` 中的 `lineageFile`
声明也已按无血缘契约删除）。

API 冒烟（pytest TestClient 覆盖）：

```
upload a/b -> code 0
middle response keys -> ['outputFile','rejectFile','stageZip','storageUri']（outputFile 为存在的路径）
final  response keys -> ['outputFile','rejectFile','stageZip','storageUri']（outputFile 为内容，8 行 = passed_records）
operatorId=99 -> code 1 ; 非法 params -> code 1 ; 合法 params -> code 0
isolation -> True ; traversal reject code -> 1 ; missing input code -> 1 ; missing flag code -> 1
GET /downloadFile -> 404
removeFile -> code 0（任务目录已删除）
```

**Docker 构建未执行**：本次交付环境没有安装 Docker（`docker --version` 不可用），
因此不对镜像构建做任何“已验证”的声明。`Dockerfile` 已提供，可在具备 Docker 的环境验证。
