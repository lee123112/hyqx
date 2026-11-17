# 算子2：语言与句对结构校验

检查源/目标语言声明、句对结构完整性及空值情况，输出通过记录与结构异常清单。
规则型算子，不加载语言检测模型。

- 算子编号：2（清单"必选"节点，输入核验环节）
- 算子标识：`structure_validation`（注册名 `PRLt_validate_lang_pair`）
- 类型：规则型
- 版本：v1.0.0
- 输出：`02_validated.jsonl`、`02_rejects.jsonl`、`artifacts/stage_02_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）

## 功能

- 必填字段完整性校验（默认 `id`/`source`/`target`，可配置）
- 空值/空白字段拦截
- 语言方向校验：记录声明语言与任务配置（默认 zh→en）不一致时拦截
- 违规策略可配置：`reject`（拦截）或 `warn`（保留并写入 `metadata.warnings`）
- 产出：通过 JSONL + 异常清单 + 阶段产物 zip + 规范日志

## 运行

```bash
pip install -r requirements.txt

# CLI 跑样例
python processor.py --input dataset/sample/demo.jsonl --unique-id demo_file1

# 启动服务（三接口）
python inference.py        # 或 uvicorn inference:app --port 8002
```

输出根目录默认为本算子目录下的 `output/`，部署时可通过环境变量 `OUTPUT_DIR` 指定容器内目录；不同 `uniqueId` 会在该目录下隔离。

## 接口（三接口）

正式接口仅有以下三个：`POST /uploadFile?uniqueId=...`、`POST /predict`、`POST /removeFile?uniqueId=...`。
上传和删除成功响应严格为 `{"code":0,"msg":"success"}`；不提供 `/downloadFile`，也不再产出任何血缘文件。

| 接口 | 说明 |
|---|---|
| `POST /uploadFile?uniqueId=` | 上传文件（form 字段 `file`），成功只返回 `{"code":0,"msg":"success"}` |
| `POST /predict` | body `{"uniqueId","operatorId?","isLastOperator","inputFile?","params?","config?"}`；`isLastOperator` 必传；`operatorId` 若传须为 `"02"` |
| `POST /removeFile?uniqueId=` | 删除该任务目录，成功只返回 `{"code":0,"msg":"success"}` |

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

- 中间调用（`isLastOperator=false`）：`data.outputFile` 是**输出文件路径**，下一算子 `inputFile` 直接用它。
- 阶段末调用（`isLastOperator=true`）：`data.outputFile` 是**输出文件内容字符串**。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_02_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- `/predict` 的 `params`（或兼容旧参数 `config`）可覆盖 `configs/config.yaml` 中的任意配置，`params` 优先；未知键返回结构化「配置错误」。

## 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.1 小节的 `param_` 行与 `configs/config.yaml` 字段对应如下
（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_源语言代码` | `source_lang` | 任务声明的源语言代码，默认 `zh` |
| `param_目标语言代码` | `target_lang` | 任务声明的目标语言代码，默认 `en` |
| `param_语言置信度阈值` | 无对应字段（未实现） | 规范注明基于 langdetect 置信度；本算子按「语言检测不加载模型」口径只校验**声明字段**，不计算置信度，故无对应配置项 |
| `param_空值拦截开关` | `empty_check_fields` | 空值检查字段集合，默认为 `[source, target]`（开启即拦截空白/空串） |

## 配置

```yaml
required_fields: [id, source, target]
empty_check_fields: [source, target]
check_declared_lang: true
source_lang: zh
target_lang: en
on_violation: reject   # reject | warn
```

未知配置字段直接报错（`extra="forbid"`）；非法取值返回结构化业务失败。

## 统计与阶段产物

统计：`total`、`passed`、`rejected`、`warned`、`reason:*`。所有计数来自实际读写结果。

阶段产物 `artifacts/stage_02_<uniqueId>.zip` 在成功/部分成功返回前由
`utils/artifacts.finalize_stage_artifacts` 生成，zip 内路径为相对任务目录的 POSIX 路径，
只包含本算子主输出、异常清单与运行日志；`storageUri` 未配置对象存储时为
`local-mirror://<uniqueId>/<zip名>` 逻辑地址。`status` 取值 `success | partial | failed`：
零记录或读取失败为 `failed`，存在拦截为 `partial`，全部通过为 `success`；失败分支不生成阶段产物。

> 【无血缘改造 2026-10-19】原 `lineage.json`（含 operator / operator_version / uniqueId / inputFile /
> outputFile / status / startedAt / finishedAt / config / statistics）已停用，任务目录不再产出任何 lineage 文件，
> 原实现保留在代码注释与备份中便于回溯。

## 测试

```bash
python -m pytest tests/ -q
# 12 passed
```

覆盖：正常样例、空文件、损坏行、缺字段、错误编码（GBK）、warn 策略、类型校验、非法配置、
三接口全流程与 4 字段契约、阶段产物 zip 与「无血缘文件」、`operatorId` 校验、`params` 覆盖生效、
路径穿越防护、多任务隔离。

## 验证结果（2026-10-19 无血缘改造后实跑）

- `D:\python\python.exe -m pytest tests -q` → `12 passed in 1.42s`
- `D:\python\python.exe -m compileall -q .` → exit 0
- `D:\python\python.exe -m json.tool map.json` → exit 0
- CLI：`python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke`
  → `status=partial`，`total=15`、`passed=11`、`rejected=4`；
  产出 `artifacts/stage_02_smoke_cli.zip`（内含 `02_validated.jsonl`、`02_rejects.jsonl`、`run.log`），
  `storageUri=local-mirror://smoke_cli/stage_02_smoke_cli.zip`，任务目录**无** `lineage.json`。
- API smoke（TestClient）：upload→`{"code":0,"msg":"success"}`；中间调用 `data` 为
  `{outputFile,rejectFile,stageZip,storageUri}` 且各路径文件存在；阶段末调用 `outputFile` 为文件内容；
  `operatorId` 不匹配、非法 `params`、缺失 `isLastOperator` 均返回结构化失败；
  `params={"check_declared_lang": false}` 使通过记录数由 11 增至 12（覆盖生效实测）。

## 已知限制与降级

- 语言校验基于记录中声明的 `source_lang`/`target_lang` 字段，不做实际语言识别（属算子6 职责）；
  规范 `param_语言置信度阈值` 因此无对应配置项。
- 输入非 UTF-8 时返回 `failed` 并提示检查编码，不做自动转码（转码属算子3 职责）。
- 正式契约不提供下载接口；阶段中间产物用 `data.outputFile` 路径传递，阶段末用 `data.outputFile` 内容交付，
  阶段归档用 `data.stageZip`/`data.storageUri`；不再使用血缘文件传递中间路径。
