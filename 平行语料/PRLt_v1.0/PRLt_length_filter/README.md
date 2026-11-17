# 算子 6：长度过滤

按源文、目标文字符长度和长度比例过滤过短、过长及比例异常的句对，保留可追踪的过滤原因。
本节点只做规则过滤：不改写文本、不做语言检测、不做去重、不评价语义等价，也不输出任何模型分数。

- 算子编号：6（默认开启的通用粗清洗节点）
- 类型：规则型
- 版本：v1.1.0
- 映射 id：`length_filtering`
- 上游输入：算子 5 的 `05_layout_cleaned.jsonl`
- 输出：`06_length_filtered.jsonl`、`06_length_rejects.jsonl`、`artifacts/stage_06_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）
- 服务端口：8006

## 运行

```bash
pip install -r requirements.txt
python processor.py --input dataset/sample/demo.jsonl --unique-id demo_file1
python inference.py
# 或 uvicorn inference:app --host 0.0.0.0 --port 8006
```

输出根目录默认为当前算子目录下的 `output/`，部署时通过 `OUTPUT_DIR` 指定；任务目录为 `OUTPUT_DIR/<uniqueId>/`。
`processor.py` 在找不到 `--output-root` 时同样读取 `OUTPUT_DIR`。

## 接口（三接口）

正式接口仅有以下三个：`POST /uploadFile?uniqueId=...`、`POST /predict`、`POST /removeFile?uniqueId=...`。上传和删除成功响应严格为 `{"code":0,"msg":"success"}`；不提供 `/downloadFile`。本算子位于通用粗清洗阶段内部（上游算子 5），`/predict` 必须传 `inputFile`。

| 接口 | 说明 |
|---|---|
| `POST /uploadFile?uniqueId=` | multipart 字段 `file`，成功只返回 `{"code":0,"msg":"success"}` |
| `POST /predict` | JSON 必传 `uniqueId`、布尔 `isLastOperator`；`operatorId` 选传（若传须为 `"06"`）；`inputFile` 必传；`params`/`config` 覆盖配置 |
| `POST /removeFile?uniqueId=` | 按 `uniqueId` 删除当前任务目录 |

外层响应固定为 `{code,msg,data}`（`code` 0=成功 1=失败）。`/predict` 成功响应 `data` 固定 4 个字段
（不再返回 `lineageFile`）：

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
- 阶段末调用（`isLastOperator=true`）：`data.outputFile` 是**输出文件内容**（UTF-8 文本）。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_06_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- `/predict` 的 `params`（或兼容旧参数 `config`）可覆盖 `configs/config.yaml` 中的任意配置项，`params` 优先；未知配置字段直接报配置错误。

## 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.5 小节的 `param_` 行与 `configs/config.yaml` 字段对应如下（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_最小长度` | `min_source_chars` / `min_target_chars` | 源文/目标文最小长度阈值（闭区间） |
| `param_最大长度` | `max_source_chars` / `max_target_chars` | 源文/目标文最大长度阈值（闭区间） |
| `param_异常字符占比上限` | 本算子未实现该维度（只做长度与长度比例过滤，不新增第二套字符占比阈值） | 当前版本不提供，接入方无需传 |

## 处理规则

- 长度按 Unicode **NFC** 归一后的 Python 字符数统计，不用字节数，不删除标点和空格。
- 阈值均为**闭区间**，等于边界视为通过。
- 长度比例 `length_ratio = target_chars / max(source_chars, 1)`，源文为空时用 1 兜底，不会除零。
- 空字符串触发 `empty_source`/`empty_target`；此时不再判定比例，避免同一记录被重复计数（例如空源文 + 非空目标文不会同时报 `length_ratio_above_max`）。
- 违规原因：`empty_source`、`empty_target`、`source_too_short`、`source_too_long`、`target_too_short`、`target_too_long`、`length_ratio_below_min`、`length_ratio_above_max`。
- `on_violation=reject`（默认）把违规句对写入 `06_length_rejects.jsonl`；`on_violation=warn` 保留记录并把 `metadata.length_warnings` 写入。
- 可为明确的语言方向配置软/硬比例策略。默认 `zh-en` 在基础比例 `0.2–5.0` 外、硬边界 `0.1–12.5` 内时保留并告警；超过硬边界仍拒绝。其他语言方向维持原有拒绝策略。
- 每条通过记录的 `metadata` 增加 `length_stats = {source_chars, target_chars, length_ratio}`，原有 metadata 字段（含算子 5 的语言检测结果）原样保留。
- 损坏 JSON、非对象行、缺字段、字段类型错误、`metadata` 非对象全部进入过滤清单，不静默丢弃。
- 输入严格按 UTF-8 解码；先写 `.tmp` 再原子替换，保证产物要么完整要么不存在，也不会截断同名输入。

配置项位于 `configs/config.yaml`，可通过 `/predict` 的 `params`/`config` 对象覆盖。负数、`min > max`、比例非正数、未知字段、非法枚举、
`text_fields` 不是 `[source, target]` 都会直接报配置错误并以 `failed` 结束。

## 测试

```bash
python -m pytest tests/ -q
```

本轮实测：`41 passed in 2.35s`。覆盖内容：

- NFC 与字符计数（组合字符、CJK、标点、空格）；
- 闭区间边界（等于阈值通过、越界一步被拦截）；
- 比例定义与源文为空时的除零保护；
- 每类违规原因与原因计数；
- 损坏 JSONL、非对象行、缺字段、类型错误、`metadata` 非对象；
- 空文件、错误编码、非法配置（负数 / min>max / 比例非正 / 未知字段 / 非法枚举 / text_fields）；
- `reject` 与 `warn` 两种违规策略，以及 `zh-en` 软告警/硬拒绝边界；
- 阶段产物 zip 与「无血缘文件」校验、配置覆盖生效（`on_violation=warn`）、输出行数与统计一致、UTF-8 输出；
- 三接口、`operatorId` 校验、`params` 覆盖、`isLastOperator` 必传、路径穿越、任务隔离、重复运行同一 `uniqueId`。

CLI smoke（本轮实跑）：

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
# status=partial，total=6、passed=4、rejected=2；
# stageZip=output_smoke/smoke_cli/artifacts/stage_06_smoke_cli.zip
# storageUri=local-mirror://smoke_cli/stage_06_smoke_cli.zip；任务目录无 lineage.json
```

## 已知口径与限制

- 本节点不做语言识别（算子 2/5 职责）、不做去重（算子 8 职责）、不把“比例异常”解释为翻译错误。
- 字符阈值和语言对软/硬比例策略随 `run.log` 与 `stageZip` 归档留痕；当前 `zh-en` 硬边界用于工程防误杀，仍需用独立 Gold 做业务校准。
- 规范 4.5 的 `param_异常字符占比上限` 在本节点没有对应实现（本节点只做长度与长度比例过滤），不虚构该阈值。
- `text_fields`、`input_encodings` 是沿用通用 IO 模板的适配项，本节点仍只按 `source`/`target` 计算长度。
- 正式接口不提供 `/downloadFile`；`/removeFile` 只按 `uniqueId` 删除当前任务目录。
- 本节点无模型、无外部服务依赖，因此没有“降级后端”概念，也不输出置信度或分数。
