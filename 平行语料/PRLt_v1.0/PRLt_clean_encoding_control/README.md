# 算子 3：编码与控制字符清理

正式编号 3（`通用粗清洗` 阶段第 1/6 个节点），算子标识 `encoding_cleanup`，端口 8003。

将平行语料 JSONL 严格解码为 UTF-8，清除句对与元数据中的 C0/C1/DEL 控制字符，输出异常清单、真实统计、日志与阶段产物。**纯规则算子：不加载任何模型，不产生任何分数。**

## 1. 功能边界

做的事：

- 按配置顺序（默认 `utf-8 → gb18030 → gbk`）**严格解码**输入，禁止 `replacement` 解码，不静默伪造文本；
- 按 `[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F\\x7F-\\x9F]` 递归清理**所有字符串键与值**（含 `metadata`），保留 `\t`、`\n`、`\r`；
- 每条通过记录在 `metadata` 追加真实计数与编码证据；
- 损坏 JSON、非对象 JSON、`metadata` 类型错误进入独立异常清单（带原始行号），不中断、不静默丢弃；
- JSONL 流式读写 + `.tmp`/`os.replace` 原子替换；`OUTPUT_DIR/<uniqueId>/` 任务隔离。

不做的事（避免与其他算子重叠判定）：

- 不校验 `source`/`target` 等字段是否齐全、是否为空、语言方向是否正确（算子 2 职责）；
- 不做 HTML/空白清理（算子 4）、页眉页脚（算子 5）、长度过滤（算子 6）、去重（算子 7）、脱敏（算子 8）；
- 不做真实语言检测（辅助_语言检测 / 算子 2 领域职责）；
- 不输出质量分、置信度或任何形式的评分。

## 2. 输入输出

| 项目 | 值 |
|---|---|
| 输入 | UTF-8 / GB18030 / GBK 的 JSONL，每行一个 JSON 对象；典型来自算子 2 的 `02_validated.jsonl` 或阶段首上传文件 |
| 主输出 | `03_encoding_cleaned.jsonl` |
| 异常输出 | `03_encoding_rejects.jsonl` |
| 其他产物 | `artifacts/stage_03_<uniqueId>.zip`（含主输出、异常清单、`run.log`）、`run.log`（**2026-10-19 起不再输出 `lineage.json`**） |

通过记录保留原字段，仅追加：

```json
{
  "id": "demo-0002",
  "source": "编码与控制字符会被清理。",
  "target": "Encoding and control characters are removed.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {
    "note": "制表符\t与换行\n保留",
    "encoding_original": "utf-8",
    "encoding_normalized": "utf-8",
    "control_chars_removed": 4
  }
}
```

异常清单每条：

```json
{"line": 7, "record": null, "reasons": ["invalid_json"]}
```

稳定原因码：`invalid_json`、`invalid_record`、`invalid_type:metadata`。
统计字段：`total`、`passed`、`rejected`、`records_changed`、`control_chars_removed`、`encoding`、`reason:*`。

## 3. 配置（`configs/config.yaml`）

```yaml
operator:
  name: encoding_cleanup
  version: v1.0.0

# 按顺序尝试，禁止 replacement 解码，避免静默伪造文本
input_encodings: [utf-8, gb18030, gbk]

# 清理 C0/C1/DEL 控制字符，保留制表符、换行和回车
remove_control_chars: true
```

`config` 为 `extra="forbid"`：未知字段、空编码列表、类型错误一律返回结构化 `failed`，`msg` 中带「配置错误」；
失败分支不生成阶段产物（`stageZip`/`storageUri` 为 `null`），也不再产出任何血缘文件。

## 4. 算法/模型后端与降级行为

本节点无模型、无外部服务，因此**不存在模型降级，也不存在伪造模型分数的代码路径**。

- 编码后端：Python `codecs` 增量解码（`errors="strict"`），按配置顺序探测；
- 清理后端：正则 `subn` 真实计数。

非正常路径只有两种，均不静默：

| 状态 | 触发条件 | 产物 |
|---|---|---|
| `failed` | 配置错误、全部候选编码解码失败、输入读取/输出写入失败、输入为空或无有效记录 | 不产出输出文件与异常清单，`outputFile`/`rejectFile`/`stageZip`/`storageUri` 为 `null` |
| `partial` | 至少一行进入异常清单 | 输出文件 + 异常清单均落盘 |

其余为 `success`。

## 5. 启动命令

```bash
pip install -r requirements.txt

# CLI
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 服务
python inference.py                          # 默认 8003
uvicorn inference:app --host 0.0.0.0 --port 8003
```

输出根目录由环境变量 `OUTPUT_DIR` 指定（默认本算子目录下 `output/`）；任务目录为 `OUTPUT_DIR/<uniqueId>/`。

## 6. 接口（三接口）

正式接口只有 `POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`；
**不提供 `/downloadFile`**，也不再产出或返回任何血缘文件。外层响应固定 `{code, msg, data}`，
`code=0` 为接口成功，`code=1` 为请求或执行失败。

`/predict` 必须传 `uniqueId` 与**布尔** `isLastOperator`；`operatorId` 选传（若传须为 `"03"`）；
本节点是阶段首节点，因此 `inputFile` 可省略；阶段内后续调用可把上一中间响应的 `data.outputFile`
原样作为 `inputFile`。`params`（或兼容旧参数 `config`）可覆盖 `configs/config.yaml` 中的配置，`params` 优先。

`/predict` 成功响应 `data` 固定 4 个字段（不再返回 `lineageFile`）：

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

- 中间调用（`isLastOperator=false`）：`outputFile` 是**输出文件路径**；阶段末（`isLastOperator=true`）是**文件内容字符串**。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_03_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。

```bash
curl -X POST "http://127.0.0.1:8003/uploadFile?uniqueId=task01_f1" -F "file=@dataset/sample/demo.jsonl"

# 中间算子：返回输出文件路径
curl -X POST "http://127.0.0.1:8003/predict" -H "Content-Type: application/json" \
  -d '{"uniqueId":"task01_f1","operatorId":"03","isLastOperator":false}'

# 阶段末：返回最终文件内容
curl -X POST "http://127.0.0.1:8003/predict" -H "Content-Type: application/json" \
  -d '{"uniqueId":"task01_f1","operatorId":"03","isLastOperator":true,"inputFile":"<上一中间响应的 data.outputFile>"}'

curl -X POST "http://127.0.0.1:8003/removeFile?uniqueId=task01_f1"
```

中间响应示例：

```json
{"code": 0, "msg": "success",
 "data": {"outputFile": ".../03_encoding_cleaned.jsonl",
          "rejectFile": ".../03_encoding_rejects.jsonl",
          "stageZip": ".../artifacts/stage_03_task01_f1.zip",
          "storageUri": "local-mirror://task01_f1/stage_03_task01_f1.zip"}}
```

`inputFile` 只允许落在 `OUTPUT_DIR/<uniqueId>/` 内；非法 `uniqueId`（含 `/`、`\`、`:`、`*`、`?`、`"`、`<`、`>`、`|`、`.`、`..`）、绝对路径越界和 `../` 穿越一律拒绝。

## 7. 阶段产物与对象存储

成功/部分成功返回前，`processor.run()` 调用 `utils/artifacts.finalize_stage_artifacts`
生成 `artifacts/stage_03_<uniqueId>.zip`，zip 内路径为相对任务目录的 POSIX 路径，只包含：

- `03_encoding_cleaned.jsonl`（主输出）
- `03_encoding_rejects.jsonl`（异常清单）
- `run.log`（存在才打包）

`storageUri` 未配置 `OBJECT_STORE_DIR` 时为 `local-mirror://<uniqueId>/<zip名>` 逻辑地址；
配置后复制 zip 到 `OBJECT_STORE_DIR/<uniqueId>/<zip名>` 并返回 `file://` 地址。

> 【无血缘改造 2026-10-19】原 `lineage.json`（含 operator / operator_version / uniqueId / inputFile /
> outputFile / status / startedAt / finishedAt / config / statistics）已停用，任务目录不再产出任何 lineage 文件，
> 原实现保留在代码注释与 `utils/lineage.py` 中便于回溯（处理器不再 import）。

日志逐行 `[时间] [动作] 消息`（输入、配置、读取、处理、输出、归档、完成、失败），只记录文件名与真实数量，
不含完整原文、密钥与堆栈。

## 7.1 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.2 小节的 `param_` 行与 `configs/config.yaml` 字段对应如下
（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_目标编码` | 无对应可调字段（输出固定 UTF-8） | 规范默认 UTF-8；本算子输出恒为 UTF-8，`input_encodings` 只用于输入解码候选 |
| `param_乱码处理策略` | 无对应字段（未实现丢弃/占位替换） | 一期口径：严格解码、禁止 replacement；无法解码的文件整体返回 `failed`，不静默伪造文本 |

## 8. 资源需求

- 纯 CPU、纯规则：单核即可，无 GPU、无显存要求；
- 内存与单行最大长度同量级（流式逐行处理，不整文件驻留）：建议 ≥ 512 MB；
- 磁盘：输出体积与输入同量级，另有同体积的临时 `.tmp` 文件（原子替换期间短暂存在）；
- 镜像：`python:3.10-slim`（本机以 Python 3.9.7 验证兼容）。

## 9. 测试数据与真实测试结果（2026-10-19 无血缘改造后实跑）

样例 `dataset/sample/demo.jsonl`（10 行）：7 条通过（含控制字符行、`\t`/`\n` 保留行、C1 区行）、
1 行损坏 JSON、1 行非对象 JSON、1 条 `metadata` 为字符串。

```
D:\python\python.exe -m pytest tests -q   → 31 passed in 2.16s（Windows / Python 3.9.7）

D:\python\python.exe processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
→ status=partial  total=10 passed=7 rejected=3 records_changed=2 control_chars_removed=6 encoding=utf-8
  reason:invalid_json=1  reason:invalid_record=1  reason:invalid_type:metadata=1
  stageZip=output_smoke\smoke_cli\artifacts\stage_03_smoke_cli.zip
  storageUri=local-mirror://smoke_cli/stage_03_smoke_cli.zip

D:\python\python.exe -m json.tool map.json   → exit 0（port 8003）
D:\python\python.exe -m compileall -q .      → exit 0
```

CLI 产物独立核对：输出 7 行 = `passed`；异常清单 3 行 = `rejected`；
`control_chars_removed` 合计 6 与逐条 `metadata.control_chars_removed` 之和一致；输出文件中不再含
`\x00\x08\x0b\x0c\x1f\x7f\x9f`；`artifacts/stage_03_smoke_cli.zip` 含主输出/异常清单/`run.log`；
任务目录**无** `lineage.json`；无 `.tmp` 残留；两个 `uniqueId` 目录互不污染。

API 冒烟（pytest `fastapi.testclient` 覆盖）：`uploadFile` code=0；中间 `data` 为
`{outputFile,rejectFile,stageZip,storageUri}` 且各路径文件存在；阶段末 `data.outputFile` 为文件内容；
缺/非布尔 `isLastOperator`、无输入、`inputFile` 目录穿越与绝对路径越界、非法 `uniqueId`、
`operatorId` 不匹配、非法 `params` 均 code=1；`GET /downloadFile` → 404；
`params={"remove_control_chars": false}` 覆盖生效（输出中保留 `\u0000`）；`removeFile` code=0 且目录已删除。

> 历史验证记录（2026-10-09，血缘版）：`30 passed, 5 warnings`、API 23/23 PASS；
> 该记录中的 `lineage.json` 已随 2026-10-19 无血缘改造停用，保留仅供回溯。


## 10. 已知限制与未确认口径

- 编码探测会按配置顺序**重新读取**文件一次，正式处理再增量解码一次（两遍 IO）；超大文件建议由工程侧分片后提交；
- GB18030 是 GBK 的超集，因此默认顺序下 GBK 文件会判为 `gb18030`（`encoding` 统计值）；如需严格区分请把 `input_encodings` 收窄为单值；
- 控制字符清理是**递归**的：会同时清理 `metadata` 的键与值；若某些键本身含控制字符，清理后可能出现键名合并（当前样例与上游产物不存在该情况）；
- 输入路径与输出路径相同时（同一 `uniqueId` 用自己上一轮产物重跑），依靠 `.tmp` + `os.replace` 保证源文件不被截断，**不**像算子 1/13 那样直接拒绝；这是本节点落实「原子替换避免自截断」的实现口径；
- `03_encoding_cleaned.jsonl` / `03_encoding_rejects.jsonl` 与 `demo/DATA_SCHEMA.md` §3、`算子数据流说明.md` §5.1 一致；根目录 `operator_records.json`（登记表派生）写作 `03_cleaned.jsonl`/`03_rejects.jsonl`，属记录口径差异，未修改共享清单；
- 本次提交清单 `算子名称` 为 `PRLt_clean_encoding_control`，根 `map.json` 与目录名保持一致；
- 未执行 Docker 构建（本机无可用 Docker 守护进程），镜像与启动未验证；
- 未做真实工程侧联调（算子 4 消费本节点产物）与并发/大文件压测。

## 11. 目录

```text
03_编码与控制字符清理(无需)/
├── processor.py            # 处理逻辑 + CLI
├── inference.py            # FastAPI 三接口（端口 8003）
├── configs/config.yaml
├── utils/{io,lineage,logger,schemas,artifacts,timeutil}.py   # lineage.py 已停用，仅保留回溯
├── tests/{test_operator.py,test_api.py}
├── dataset/DATA_SCHEMA.md
├── dataset/sample/demo.jsonl
├── README.md
├── requirements.txt
├── Dockerfile
├── map.json
├── output/                 # 空目录（交付态）
└── DEVELOPMENT_STATUS.md
```
