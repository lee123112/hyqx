# 算子 4：空白与 HTML 清理

对算子 3 产生的 UTF-8 平行语料 JSONL 做网页标签剥离、HTML 实体还原、不可见格式字符删除和空白规整。
默认只处理 `source`、`target`，输出清理统计、异常清单、日志和阶段产物。
规则型算子，不抓取网页、不调用模型。

- 算子编号：4（默认开启的通用粗清洗节点）
- 算子标识：`whitespace_html_cleanup`（注册名 `PRLt_clean_blank_html`）
- 类型：规则型
- 版本：v1.1.0
- 输出：`04_html_cleaned.jsonl`、`04_html_rejects.jsonl`、`artifacts/stage_04_<uniqueId>.zip`、`run.log`
  （**2026-10-19 起不再输出 `lineage.json`**）

## 运行

```bash
pip install -r requirements.txt
python processor.py --input dataset/sample/demo.jsonl --unique-id demo_file1
python inference.py
# 或 uvicorn inference:app --host 0.0.0.0 --port 8004
```

输出根目录默认为当前算子目录下的 `output/`，部署时可通过 `OUTPUT_DIR` 指定。任务目录为 `OUTPUT_DIR/<uniqueId>/`。

## 接口（三接口）

正式接口仅有以下三个：`POST /uploadFile?uniqueId=...`、`POST /predict`、`POST /removeFile?uniqueId=...`。
上传和删除成功响应严格为 `{"code":0,"msg":"success"}`；不提供 `/downloadFile`，也不再产出或返回任何血缘文件。

| 接口 | 说明 |
|---|---|
| `POST /uploadFile?uniqueId=` | multipart 字段 `file`，成功只返回 `{"code":0,"msg":"success"}` |
| `POST /predict` | JSON 必须包含 `uniqueId`、布尔 `isLastOperator`、`inputFile`（本节点非阶段首节点，必须传）；`operatorId` 选传（若传须为 `"04"`）；`params`/`config` 覆盖配置 |
| `POST /removeFile?uniqueId=` | 按 MTY 口径删除当前任务目录 |

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
- 阶段末调用（`isLastOperator=true`）：`data.outputFile` 是**文件内容字符串**。
- `stageZip`：`OUTPUT_DIR/<uniqueId>/artifacts/stage_04_<uniqueId>.zip`，内含主输出、异常清单与 `run.log`。
- `storageUri`：配置环境变量 `OBJECT_STORE_DIR` 时返回归档文件 `file://` 地址，否则返回 `local-mirror://<uniqueId>/<zip名>` 逻辑地址。
- `params`（或兼容旧参数 `config`）可覆盖 `configs/config.yaml` 中的任意配置，`params` 优先；未知键返回结构化「配置错误」。

## 规范参数映射

规范文档《【MTY规范】平行语料-算子开发规范文档-261016》4.3 小节的 `param_` 行与 `configs/config.yaml` 字段对应如下
（`/predict` 的 `params` 使用英文字段名）：

| 规范参数 | config 字段 | 说明 |
|---|---|---|
| `param_HTML标签黑名单` | `strip_html`（+ 内置 `DROP_TAGS`/`BLOCK_TAGS`） | 一期为固定规则：删除全部标签、注释及 `script/style/noscript/template` 内容；`strip_html=false` 可整体关闭，不提供逐标签黑名单配置 |
| `param_正文抽取开关` | 无对应可调字段（一期未实现正文抽取） | 规范默认关；本算子只做标签剥离与空白规整，不按版式规则抽取正文/去除广告导航块，该能力尚未实现 |

## 默认清理行为

- `HTMLParser` 提取可见文本，删除标签、注释和 `script/style/noscript/template` 内容，并还原实体。
- 删除零宽字符和 BOM；连续空白折叠为一个普通空格并裁剪首尾。
- `whitespace_mode` 支持 `linewise`（默认，逐行规整水平空白并保留算子 5 所需的换行）、`global`（全局折叠）、
  `trim`（只裁剪整个字段首尾）和 `preserve`（保留空白）；`normalize_whitespace=false` 可整体关闭。
- 只处理 `source` 和 `target`，不抓取网页、不删除 URL、不识别页眉页脚或时间戳。
- 输入严格按 UTF-8 解码；损坏 JSON、非对象行、文本字段类型错误和非字典 `metadata` 进入异常清单。

配置项位于 `configs/config.yaml`，可通过 `/predict` 的 `params`/`config` 覆盖。

## 统计与阶段产物

统计：`total`、`passed`、`rejected`、`records_changed`、`html_tags_removed`、`whitespace_normalized`、
`invisible_chars_removed`、`encoding`、`reason:*`。

阶段产物 `artifacts/stage_04_<uniqueId>.zip` 在成功/部分成功返回前由
`utils/artifacts.finalize_stage_artifacts` 生成，zip 内路径为相对任务目录的 POSIX 路径，
只包含本算子主输出、异常清单与运行日志；`storageUri` 未配置对象存储时为
`local-mirror://<uniqueId>/<zip名>` 逻辑地址。`status` 取值 `success | partial | failed`：
零记录或读取/编码失败为 `failed`，存在拦截为 `partial`，全部通过为 `success`；失败分支不生成阶段产物。

> 【无血缘改造 2026-10-19】原 `lineage.json`（含 operator / operator_version / uniqueId / inputFile /
> outputFile / status / startedAt / finishedAt / config / statistics）已停用，任务目录不再产出任何 lineage 文件，
> 原实现保留在代码注释与 `utils/lineage.py` 中便于回溯（处理器不再 import）。

## 测试

```bash
python -m pytest tests/ -q
# 16 passed
```

覆盖：HTML 标签/实体、脚本样式噪声、空白和不可见字符、配置开关、空白模式、损坏行、错误编码、空文件、
非法配置、三接口 4 字段契约、阶段产物 zip 与「无血缘文件」、`operatorId` 校验、`params` 覆盖生效、
路径安全和任务隔离。

## 验证结果（2026-10-19 无血缘改造后实跑）

- `D:\python\python.exe -m pytest tests -q` → `16 passed in 1.21s`
- `D:\python\python.exe -m compileall -q .` → exit 0
- `D:\python\python.exe -m json.tool map.json` → exit 0
- CLI：`python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke`
  → `status=success`，`total=4`、`passed=4`、`rejected=0`、`records_changed=4`、`html_tags_removed=24`；
  产出 `artifacts/stage_04_smoke_cli.zip`（内含 `04_html_cleaned.jsonl`、`04_html_rejects.jsonl`、`run.log`），
  `storageUri=local-mirror://smoke_cli/stage_04_smoke_cli.zip`，任务目录**无** `lineage.json`。
- API smoke（TestClient）：upload→`{"code":0,"msg":"success"}`；中间调用 `data` 为
  `{outputFile,rejectFile,stageZip,storageUri}` 且各路径文件存在；阶段末调用 `outputFile` 为文件内容；
  `operatorId` 不匹配、非法 `params`、缺失 `isLastOperator`/`inputFile` 均返回结构化失败；
  `params={"strip_html": false}` 覆盖生效（输出保留 `<p>`）；`/removeFile` 删除后任务目录不存在。

## 已知口径与降级行为

- 只做标签剥离、实体还原与空白规整，不做网页正文抽取（不按版式规则去除广告/导航块），不调用模型。
- `strip_html=false` 时保留原始标签文本；`whitespace_mode=preserve` 时完全保留空白。
- 非 UTF-8 输入直接返回结构化 `failed`（上游算子 3 已归一编码），不做自动转码。
- 正式契约不提供下载接口；阶段中间产物用 `data.outputFile` 路径传递，阶段末用 `data.outputFile` 内容交付，
  阶段归档用 `data.stageZip`/`data.storageUri`；不再使用血缘文件传递中间路径。
