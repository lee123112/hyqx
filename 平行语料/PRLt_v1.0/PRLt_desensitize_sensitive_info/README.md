# 算子 8：敏感信息脱敏

- 算子标识：`sensitive_info_deidentification`，版本 `v1.0.0`，端口 `8008`
- 所属阶段：通用粗清洗（`03 → 04 → 05 → 06 → 07 → 08`），上游算子 7，阶段末节点
- 接口口径：**仅三接口** `POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`；**不实现** `/downloadFile`
- 数据类型：平行语料双语句对（`source` / `target` + 声明允许的 metadata 字段）

## 1. 功能边界

做：

- 在 `source`、`target` 与声明允许的 metadata 字段上识别并**不可逆**替换
  邮箱、手机号（含固话）、身份证/证件号、银行卡/账号、地址；
- 重叠命中按规则优先级与匹配长度消解，同一字符只替换一次；
- 同一记录内「同类同值」共享占位符序号，保证双语句对替换 token 可对齐复核；
- 源/目标命中数量或规范化值不一致时记录 `redaction_alignment_warning`；
- 流式读、原子写、异常清单、真实统计、阶段产物与三接口。

不做：

- 不做单边修改，不猜测源/目标对应关系；无法确认时转入 `waiting_manual`；
- 不改写 `id`、语言声明和未声明的 metadata 字段；
- 不调用任何模型，不产生置信度或概率；
- 不做网页抓取、URL 删除、页眉页脚识别（属算子 4/5）、不做语言检测（属算子 2）与质量评分（属 14–19）。

## 2. 输入输出

输入：算子 7 的 `07_deduped.jsonl`（严格 UTF-8）。
输出（全部写入 `OUTPUT_DIR/<uniqueId>/`）：

| 文件 | 说明 |
|---|---|
| `08_deidentified.jsonl` | 脱敏后句对，`.tmp` + `os.replace` 原子替换 |
| `08_deidentification_rejects.jsonl` | 异常/待人工清单，**不含原文**，只有行号、id、原因、类型计数、规则版本 |
| `artifacts/stage_08_<uniqueId>.zip` | 阶段产物（主输出、异常清单、`run.log`），2026-10-19 起取代 `lineage.json` |
| `run.log` | `[时间] [动作] 消息`，只记录类型与数量 |

主输出每条记录 `metadata` 增加
`redaction_status`、`redaction_types`、`redaction_rule_version`、`redaction_total`、`redaction_alignment_warning`。
字段与 `reasons` 枚举详见 `dataset/DATA_SCHEMA.md`。

## 3. 配置

配置在 `configs/config.yaml`，可通过 `/predict` 的 `params`（或兼容旧参数 `config`）对象覆盖；未知字段、非法正则、
非法占位符一律拒绝（`failed`）。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `input_encodings` | `[utf-8]` | 只做严格解码，不做编码猜测 |
| `text_fields` | `[source, target]` | 必须包含 `source` 与 `target` |
| `metadata_fields` | `[contact]` | 声明允许脱敏的 metadata 字段，未声明一律不改写 |
| `placeholder_template` | `[REDACTED_{type}_{index}]` | 必须含 `{type}` 与 `{index}` |
| `rule_version` | `rules-v1.0.1` | 写入记录 metadata 与异常清单 |
| `sync_check_mode` | `value_multiset` | `value_multiset` / `type_counts` / `off` |
| `strict_sync` | `true` | 同步告警记录转 `waiting_manual`（不进主输出） |
| `max_hits_per_record` | `200` | 超限整条转异常清单，不做部分替换 |
| `address_regions` | 34 个省级锚点 | 地址规则锚点词表，可按任务替换 |
| `rules` | 10 条默认规则 | `priority` 越小越优先；`group=0` 替换整段，`group>0` 只替换捕获组 |
| `extra_rules` | `[]` | 追加自定义规则（如工单号、内部账号） |

默认类型 token：`EMAIL`、`PHONE`、`ID_CARD`、`ACCOUNT`、`ADDRESS`。

### 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.7 小节的 `param_` 行与 `configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_敏感类型` | `rules[].type`（`email`/`phone`/`id_card`/`account`/`address`） | 默认规则集覆盖的敏感类型；可通过 `rules` / `extra_rules` 增删 |
| `param_替换策略` | `placeholder_template` + `rules[].group` | 占位符不可逆替换；`group=0` 替换整段，`group>0` 只替换捕获组 |
| `param_双语同步开关` | `strict_sync` / `sync_check_mode` | 源/目标同步校验口径与告警记录是否转 `waiting_manual` |

## 4. 算法与模型后端

纯正则规则引擎，**无模型、无外部服务依赖、无 GPU 需求**。
规则一律使用带边界的模式（数字串与英文单词内部不命中），并在内存中按优先级消解重叠。
规范化值（小写邮箱、去空格手机号等）只用于同值判定与源/目标比对，不落盘、不写日志。

## 5. 降级行为

本算子没有模型依赖，因此**不存在**「模型不可用降级」，也**不设置任何伪造分数**。
非 `success` 路径只有：

| 状态 | 触发条件 | 行为 |
|---|---|---|
| `partial` | 存在结构/类型/规模硬拒绝记录 | 正常记录照常输出，异常记录进异常清单 |
| `waiting_manual` | 存在源/目标无法确认对应关系的记录（`strict_sync=true`） | 该记录进异常清单，不进主输出；绝不单边修改 |
| `failed` | 配置错误、规则编译失败、编码失败、空输入、读取失败、输入=输出路径 | 不产出主输出，只返回结构化失败信息（不生成阶段产物） |

`strict_sync=false` 时可把告警记录保留在主输出，`redaction_status` 置为
`redacted_with_warning`，`alignment_warnings` 统计该数量。

## 6. 启动

```bash
pip install -r requirements.txt
python -m uvicorn inference:app --host 0.0.0.0 --port 8008
```

输出根目录由环境变量 `OUTPUT_DIR` 指定，默认当前算子目录下的 `output/`；
任务目录为 `OUTPUT_DIR/<uniqueId>/`，`uniqueId = 任务id_文件id`。

## 7. CLI 示例

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

真实输出：

```text
status=partial statistics={'input_records': 8, 'output_records': 6, 'rejected_records': 2,
'hard_rejected_records': 1, 'waiting_manual_records': 1, 'redacted_records': 5,
'unchanged_records': 1, 'alignment_warnings': 0,
'redaction_counts': {'email': 5, 'phone': 2, 'id_card': 2, 'address': 2, 'account': 4},
'redaction_rule_version': 'rules-v1.0.1', 'total_redactions': 15, 'encoding': 'utf-8',
'reason:redaction_alignment_warning': 1, 'reason:invalid_json': 1}
stageZip=output_smoke\smoke_cli\artifacts\stage_08_smoke_cli.zip
storageUri=local-mirror://smoke_cli/stage_08_smoke_cli.zip
```

## 8. API 示例

上传：

```bash
curl -X POST "http://127.0.0.1:8008/uploadFile?uniqueId=task1_file1" -F "file=@07_deduped.jsonl"
# {"code":0,"msg":"success"}
```

中间节点（`isLastOperator=false`，`inputFile` 取上一算子返回的 `data.outputFile`）：

```bash
curl -X POST http://127.0.0.1:8008/predict -H "Content-Type: application/json" -d '{
  "uniqueId": "task1_file1",
  "operatorId": "08",
  "isLastOperator": false,
  "inputFile": "output/task1_file1/07_deduped.jsonl",
  "params": {}
}'
```

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "outputFile": "G:/.../output/task1_file1/08_deidentified.jsonl",
    "rejectFile": "G:/.../output/task1_file1/08_deidentification_rejects.jsonl",
    "stageZip": "G:/.../output/task1_file1/artifacts/stage_08_task1_file1.zip",
    "storageUri": "local-mirror://task1_file1/stage_08_task1_file1.zip"
  }
}
```

`data` 固定只有上述 4 个字段，不再返回 `lineageFile`。阶段末（`isLastOperator=true`）时
`data.outputFile` 是**文件内容**（UTF-8 文本），其余 3 个字段仍为路径/地址：

```json
{"code": 0, "msg": "success",
 "data": {"outputFile": "（08_deidentified.jsonl 的 UTF-8 文本内容）",
          "rejectFile": "G:/.../08_deidentification_rejects.jsonl",
          "stageZip": "G:/.../artifacts/stage_08_task1_file1.zip",
          "storageUri": "local-mirror://task1_file1/stage_08_task1_file1.zip"}}
```

中间节点的 `data.outputFile` 路径可直接作为下一算子的 `inputFile`。删除任务目录：

```bash
curl -X POST "http://127.0.0.1:8008/removeFile?uniqueId=task1_file1"
# {"code":0,"msg":"success"}，只删除当前 uniqueId 目录
```

注意：算子 8 位于通用粗清洗阶段内部（上游为算子 7），**不接受省略 `inputFile`**
的调用；缺 `inputFile` 返回 `code=1`。`operatorId` 若传必须为 `"08"`，`params` 覆盖
`configs/config.yaml` 配置项且优先级高于旧参数 `config`。

## 9. 输出与阶段产物示例

输出记录（真实运行结果）：

```json
{"id": "deid-0001",
 "source": "如需帮助请联系 [REDACTED_EMAIL_1] 或 [REDACTED_PHONE_1]。",
 "target": "Please contact [REDACTED_EMAIL_1] or [REDACTED_PHONE_1] for help.",
 "source_lang": "zh", "target_lang": "en",
 "metadata": {"redaction_status": "redacted", "redaction_types": {"email": 2, "phone": 2},
              "redaction_rule_version": "rules-v1.0.1", "redaction_total": 4,
              "redaction_alignment_warning": false}}
```

异常清单条目（真实运行结果，**无原文**）：

```json
{"line": 5, "id": "deid-0005", "record_omitted": "sensitive_content",
 "status": "waiting_manual", "reasons": ["redaction_alignment_warning"],
 "redaction_rule_version": "rules-v1.0.1", "redaction_types": {"phone": 1}}
```

## 10. 资源需求

| 项目 | 需求 |
|---|---|
| CPU | 单核即可，纯正则流式处理，2 vCPU 足够 |
| 内存 | 与单条记录长度相关，常驻 < 300 MB（无模型加载） |
| GPU | 不需要 |
| 存储 | 输入 + 输出 + 异常清单 + 阶段产物 zip，约为输入体积的 1.1–1.5 倍 |
| 并发 | 不同 `uniqueId` 可并发；同一 `uniqueId` 必须串行 |

## 11. 已知限制与误报边界

1. **地址规则**依赖省级锚点词表 + 「省/市/区县」链式结构，正文中提到完整行政区划
   （如「广东省深圳市南山区」）会被识别为地址，属已知误报；可用 `address_regions`
   收窄，或把 `address_cn_*` 规则的 `enabled` 置 false 关闭。
2. **银行卡/账号**规则把 16–19 位连续数字视为账号，会把同长度的其他编号
   （如长订单号、设备号）一并脱敏；需要精确口径时按业务调整规则或优先级。
3. **身份证 15 位规则**可能把形如 `^\d{6}\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}$`
   的 15 位编号误判为证件号。
4. **同步口径严格**：`value_multiset` + `strict_sync=true` 会把「源文有手机号、译文没译出」
   这类记录转入 `waiting_manual`（不进主输出），这是有意为之的安全默认值；
   允许带告警保留或放宽到 `type_counts` 需业务确认后改配置。
5. **异常清单不含原文**（安全约束）：人工复核需按 `id` 回上游 `07_deduped.jsonl` 定位原文；
   若上游没有稳定 `id`，清单只能定位行号。
6. **不支持可逆脱敏**：占位符不可逆，不保存值→token 映射表，无法还原原文。
7. 未与真实上游算子 7 产物联调，未做亿级规模压测。

## 12. 测试

```bash
python -m pytest tests/ -q
```

真实结果：

```text
python -m pytest tests -q        → 36 passed in 2.40s
python -m compileall -q .        → exit 0
python -m json.tool map.json     → exit 0（port 8008）
```

覆盖：正常样例（邮箱/手机/证件/账号/地址）、重叠命中、边界防误匹配、同值与不同值序号分配、
自定义规则与捕获组、规则开关、占位符覆盖、配置错误（未知字段/非法正则/非法类型/非法模板）、
空文件、纯空白文件、错误编码、`../` 与分隔符路径安全、输入=输出拒绝、两个 `uniqueId` 隔离、
`isLastOperator` 两种返回形态、`waiting_manual` 与 `redacted_with_warning` 两条分支、
异常清单与日志不泄露敏感值、UTF-8 编码、阶段产物 zip 与「无血缘文件」校验。

3 个 warning 现象已消失：本轮 `36 passed`，无 warning。
