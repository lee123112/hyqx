# 算子 #09：准备候选描述

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

> 表1 v4.1 正式编号 #09；环节「数据加工」；规则型；默认开启、不可跳过；P0；**无模型**。
> 目录：`G:\HYQX\算子\跨模态\demo_10.6\09_准备候选描述`；状态：已实现并本地验证（2026-10-06）。

## 1. 功能边界

只做一件事：为跨模态样本准备**候选描述**，供 #10 准入核对消费。

- 按表1固定优先级 `图注 → Alt → 标题 → 字幕 → 语音转写 → 人工补写` 取第一条长度在
  `[5, 500]` 的文本。
- 超长截断到 500 并记录；短于最短长度顺延下一来源。
- **文件名和分类标签不得作为描述**（等于媒体文件名/词干或等于分类标签即判无效）。
- 没有有效描述：生成**人工补写任务**，样本留在产线等待回写，**既不进入准入，也不进入任何隔离区**。
- 不做：配对、CLIP 评分、OCR、图像描述模型（不部署 BLIP / LLaVA）。
- 通用上游字段 `text` 不属于表1描述来源，不会被当作描述（见 §9 已知限制）。

## 2. 目录结构

```text
09_准备候选描述/
├── inference.py                  # run / load_config / CLI，业务与 HTTP 共用
├── app.py                        # POST /uploadFile、/predict、/removeFile
├── configs/config.yaml           # 表1冻结配置
├── utils/description.py          # 候选描述抽取与人工补写任务（业务核心）
├── utils/schemas.py              # 配置模型与严格范围校验
├── utils/io.py                   # 流式 JSONL、原子替换、任务目录隔离
├── utils/lineage.py  utils/logger.py  utils/transport.py   # lineage：血缘构建 v4.2 停用（now_iso 仍用）；transport：打包
├── dataset/DATA_SCHEMA.md        # 输入/输出字段契约
├── dataset/sample/               # 可复现样例（make_sample.py + demo.jsonl + 媒体）
├── tests/                        # 51 个用例：业务/接口/传输
├── packaging/                    # 打包脚本与 Dockerfile
├── output/                       # 运行输出根目录（保持为空）
├── map.json  requirements.txt  Dockerfile
└── README.md  OPERATOR_MANUAL.md  HANDOFF.md  DEVELOPMENT_STATUS.md
```

## 3. 输入 / 输出

### 输入（UTF-8 JSONL，每行一个对象）

必需 `sample_id`；描述来源字段 `caption` / `alt`(`alt_text`) / `title` / `subtitle` /
`transcript` / `manual_description`（也支持写在 `image.*`、`video.*`、`audio.*`、`metadata.*`）；
媒体字段 `image` / `video` / `audio` 为相对路径或 `{path: ...}`。上游字段全部原样保留。

### 输出

| 文件 | 角色 | 内容 |
|---|---|---|
| `09_descriptions.jsonl` | 主产物 → #10 | 上游记录 + `description`、`description_source`、`description_source_label`、`description_length`、`description_original_length`、`description_truncated`、`description_word_count`、`description_quality`（`ok`/`short`）、`description_lang`、`description_lang_source`、`description_alternate_index`（换用过候选时才有）、`processing["09"]` |
| `09_rewrite_tasks.jsonl` | 人工补写任务 | `task_id`、`sample_id`、`reason`、`label`、`action=waiting_manual`、`destination=pipeline_wait`、`is_bare_image`、`media`、`skipped_sources`、`model_used=null` |
| `09_rejects.jsonl` | 原始隔离台账 | `line`、`reason`、`label=原始文件-无法解析`、`destination=原始隔离区` |

统计守恒：`records = total_lines - bad_lines = described + pending_manual + missing_sample_id`；
`total_lines = described + pending_manual + rejected`，其中 `rejected = bad_lines + missing_sample_id`。

## 4. 配置（`configs/config.yaml`）

| 配置项 | 默认 | 允许范围 | 说明 |
|---|---|---|---|
| `description.priority` | 图注→Alt→标题→字幕→转写→人工补写 | 只读固定 | 重排即报配置错误 |
| `description.min_length` | 5 | 5–20 | 短于该长度视为没有描述 |
| `description.max_length` | 500 | 100–500 | 超过截断并记录 |
| `description.forbid_filename` | true | true/false | 文件名不得作为描述 |
| `description.forbid_tags` | true | true/false | 分类标签不得作为描述 |
| `description.min_words` | 6 | 0–50 | 描述词数下限：不足标 `description_quality=short`（拉丁按单词、中文按汉字计） |
| `description.prefer_longer_alternate` | true | true/false | 描述过短时优先换用记录自带的同源候选图注（`metadata.alt_candidates`）中最长的一条有效候选 |
| `io.output_root` | output | 目录名 | CLI 默认输出根目录 |

`/predict` 的 `config` 只允许覆盖 `min_length` / `max_length` / `forbid_filename` / `forbid_tags` /
`min_words` / `prefer_longer_alternate`，超出范围、未知字段或试图改动来源顺序都会返回 `code=1`。

### 4.1 描述语言标记（v1.0.1）

写描述时同时输出 `description_lang`（`zh`/`en`/`ja`/`ko`/`und`，按字符脚本判定）与
`description_lang_source`（`detected` 或 `declared`）。若记录里的 `text` 是对象且
`text.lang` 与内容明显矛盾（典型：上游 #4 按部署声明把英文图注写成 `zh`），本算子会
把 `text.lang` 纠正为实际语言，原值保留在 `text.lang_declared`，并写
`text.lang_source=detected`。判不出（`und`）时不动原值。

## 5. 算法 / 模型后端

**无模型**。纯规则抽取，无权重、无网络、无 GPU；`statistics` 与 `manifest.json` 均不含模型分数，
人工补写任务的 `model_used` 恒为 `null`，不存在固定分数或伪造降级。

## 6. 降级与失败行为

| 情况 | 行为 | 状态 |
|---|---|---|
| 无有效描述 | 生成人工补写任务，样本留在产线 | 响应 `status=1`、`process_status=waiting_description`；任务 `action=waiting_manual`，不隔离 |
| 输入文件不存在 | 不写业务结果，`run()` 返回 `status=failed` | `status=failed`，接口 `code=1` |
| 损坏 JSON / 非对象 | 记入 `09_rejects.jsonl`，带物理行号 | 原始隔离区 |
| 缺 `sample_id` | 记入 `09_rejects.jsonl` | 原始隔离区 |
| 配置越界/未知字段 | 拒绝执行 | `code=1` |

## 7. 启动与调用

```powershell
Set-Location -LiteralPath 'G:\HYQX\算子\跨模态\demo_10.6\09_准备候选描述'
python -m pip install -r requirements.txt
python dataset/sample/make_sample.py
python -m uvicorn app:app --host 0.0.0.0 --port 8109    # OUTPUT_DIR 控制输出根
```

### CLI

```powershell
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

### 三接口（正式接口仅这三个，不提供 `/downloadFile`）

| 接口 | 请求 | 说明 |
|---|---|---|
| `POST /uploadFile?uniqueId=` | multipart `file`（ZIP 或单个 JSONL） | ZIP 安全解压后原子提交到 `input/` |
| `POST /predict` | `{uniqueId, isLastOperator(严格 bool), inputFile?, config?}` | 阶段首省略 `inputFile`；阶段内传上一次响应的 `data.result.outputFile`（任务内路径） |
| `POST /removeFile?uniqueId=` | query | 幂等，只清理本 `uniqueId` 任务目录 |

[v4.2] `isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；**中间与阶段末响应同构**，
均返回 `outputFile` + `result`，不再用 `false` 只回血缘对象。响应示例：

```json
{"code":0,"msg":"success","data":{
  "operator":"candidate_description",
  "status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"09_descriptions.jsonl",
    "files":[{"path":"09_descriptions.jsonl","encoding":"utf-8","content":"..."},
             {"path":"09_rewrite_tasks.jsonl","encoding":"utf-8","content":"..."},
             {"path":"09_rejects.jsonl","encoding":"utf-8","content":"..."},
             {"path":"manifest.json","encoding":"utf-8","content":"..."},
             {"path":"media/img_0001.png","encoding":"base64","content":"..."}]},
  "result":{"uniqueId":"task_1_file_1",
            "outputFile":"candidate_description/run001/09_descriptions.jsonl",
            "statistics":{"total_lines":13,"records":12,"bad_lines":1,"described":8,
              "pending_manual":3,"missing_sample_id":1,"rejected":2,"truncated":1,
              "source_counts":{"caption":2,"alt":2,"title":1,"subtitle":1,
                               "transcript":1,"manual":1}}}}}
```

`outputFile` 为 `crossmodal-files-v1` 文件内容对象：入口 `09_descriptions.jsonl`，`files` 含入口、
补写任务、拒绝清单、`manifest.json` 与全部引用媒体（文本 utf-8、媒体 base64）。
`outputFile` 是内容不是路径，下一阶段由工程侧解码后重新上传。失败返回
`{"code":1,"msg":"<原因>","data":{}}`（参数不合法为 HTTP 400 + `code=1`）。

## 8. 资源需求

纯 CPU 规则处理；内存随单行记录大小线性增长（流式逐行读写，不整体载入）。
本地实测（Python 3.9.7 / Windows，样例 13 行）：CLI 单次约 0.6 秒，`pytest tests/ -q` 约 4.4 秒。
Docker 镜像未构建（见 HANDOFF）。

## 9. 已知限制

1. 表1未把通用 `text` 列入描述来源，因此只有 `text` 的样本会进入人工补写队列；若上游需要直接
   使用清洗后文本进准入，需主 agent 明确该字段与六来源的映射关系。
2. 没有检测"描述仅为无意义符号/仅数字/纯空白"的语义质量，只做长度与文件名/标签校验。
3. 媒体只做路径存在性检查与随包携带，不解码像素（本算子不需要）。
4. `map.json` 端口 8109 与历史 `demo\09_图像文本局部标注` 相同（不同目录、不同算子），同机同时
   启动会冲突，最终端口由集成侧决定。
5. 未实现跨进程任务锁（同一 `uniqueId` 并发写由工程侧保证串行）。

## 10. 真实测试结果（2026-10-06，本目录实测）

```text
$ python -m pytest tests/ -q
51 passed in 4.41s

$ python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
result: success described=8 pending_manual=3 rejected=2

$ python -m json.tool map.json     # 正常输出
$ python -m compileall -q .        # 退出码 0，无语法错误
```

真实 HTTP smoke（uvicorn 127.0.0.1:8109 + httpx；predict 三行按 v4.2 统一响应契约标注，
v4.1 的 `lineageFile` 形态已停用、不再返回）：

```text
upload  smoke_api_file_01 200 code=0 ; smoke_api_file_02 200 code=0
predict_mid   code=0 data=['operator','status','outputFile','result'] described=8
predict_inner code=0 data=['operator','status','outputFile','result']
predict_last  code=0 data=['operator','status','outputFile','result'] entrypoint=09_descriptions.jsonl files=12
isolated      smoke_api_file_02 code=0 described=8        # 两个 uniqueId 互不污染
illegal_uid   '../escape' 400 code=1
cross_task_path 'app.py'  400 code=1                       # 任务外路径被拒
downloadFile  404                                          # 未注册
removeFile    smoke_api_file_01 200 code=0 deleted=True
removeFile    smoke_api_file_02 200 code=0 deleted=True
```

上述 upload/isolated/illegal_uid/cross_task_path/downloadFile/removeFile 为实际执行输出；
predict 三行为 v4.2 中间/阶段末同构契约；Docker 未运行，未声称镜像验证通过。
