# 01 接入多模态批次（multimodal_batch_intake）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

表1 v4.1 **#01**：第 1 环节「输入核验」（`stageId=S1`），**阶段首算子**，类型=系统功能，
模型列 `--`，优先级 P0，总开关**默认开启**。

一句话：**按表1 冻结口径校验「来源、格式白名单、授权」，把通过的批次对象登记为可加工清单，
把不合格对象分流到「原始隔离区 / 样本隔离库 / 待补登记」三类台账——原文件只保存链接，不复制媒体。**

---

## 1. 功能边界

### 做什么

| 事项 | 说明 |
| --- | --- |
| 来源校验 | 只允许采集平台批次 / 预训练文本、图像、音频、视频产线交付（表1 五类，不能手填别的） |
| 格式白名单 | 文本 `txt/json/csv`；图像 `jpg/png/webp`；音频 `wav/mp3/flac`；视频 `mp4/mov/avi` |
| 授权校验 | 只有「可训练」可以进入加工 |
| 文件可读性 | `header`（默认）下若能定位本地文件，校验非空 + 文件头签名与扩展名一致 |
| 登记 | 通过项写工作集（只登记原文件链接），并生成批次台账 |
| 分流 | 原始隔离区 / 样本隔离库 / 待补登记三类台账 + 坏行拒绝子类 |
| 统计 | 四路守恒校验（`input = valid + rejected`、`objects = 四路之和` 等），不成立即失败 |

### 不做什么

- 不判定内容质量、不生成标签、不做去重抽样、不做场景编号核验（那是 #02/#03 及后续环节）；
- 不复制/转码媒体，不把原文件打包进算子间交付物；只保存链接；
- 不使用模型、不做打分（表1 模型列为 `--`）；不实现 `/downloadFile`；
- 不改变阶段边界：阶段末由工程侧通过 `isLastOperator=true` 决定。

---

## 2. 目录结构

```
01_接入多模态批次/
├── app.py                 # FastAPI：/uploadFile、/predict、/removeFile、/health
├── processor.py           # CLI 入口（--input/--unique-id/--output-root/--override/--is-last-operator）
├── inference.py           # 编排：输入解析、流式扫描、真统计、守恒、阶段形态
├── configs/config.yaml    # 部署配置（表1 冻结项 + 可覆盖业务项）
├── utils/
│   ├── intake.py          # 业务规则：来源/格式/授权/分流/台账/统计守恒
│   ├── schemas.py         # pydantic 配置与 /predict 请求模型（extra=forbid）
│   ├── io.py              # 流式 JSONL、原子写、任务隔离、路径安全、任务锁、安全解压
│   ├── transport.py       # crossmodal-files-v1 内容对象与运行清单
│   ├── lineage.py         # （v4.2 停用，存档）
│   └── logger.py          # 规范日志
├── tests/                 # 119 个单测 + 真实 uvicorn 冒烟 smoke_api.py
├── dataset/
│   ├── DATA_SCHEMA.md     # 输入/输出/统计/接口契约
│   └── sample/            # demo.jsonl + make_sample.py + 本地媒体样例
├── map.json               # 编号/端口/产物清单
├── requirements.txt / Dockerfile
└── output/                # 空目录（真实产物写入 OUTPUT_DIR/<uniqueId>/）
```

---

## 3. 输入输出字段（摘要，完整见 `dataset/DATA_SCHEMA.md`）

**输入**（JSONL 一行一条）：`sample_id`、`source`、`source_id`、`batch_id`、`upstream_version`、
`license`、`files[]`（每项 `modality` + 链接 `oss_uri`/`path`…），其余上游字段原样透传。

**输出**（`OUTPUT_DIR/<uniqueId>/multimodal_batch_intake/<runId>/`）：

| 文件 | 内容 |
| --- | --- |
| `01_workitems.jsonl` | 可加工对象（上游字段 + `operator_01` + `processing["01"]`），交 #02（开启时）或 #03 |
| `01_origin_quarantine.jsonl` | 【原始文件-无法解析】原始隔离区（含坏行/非对象/编码错误/超长行） |
| `01_sample_quarantine.jsonl` | 【样本-敏感】样本隔离库（授权非可训练/待确认/缺失/无法识别） |
| `01_return.jsonl` | 【流程-待补登记】退回清单（缺来源/来源ID/批次号/上游版本、重复登记、模态不一致） |
| `01_batch_registry.json` | 批次台账（按 来源+批次号 汇总） |
| `manifest.json` / `01_run.log` | 运行清单、规范日志 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py`、`utils/mty_compliance.py` 代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

判定优先级：**结构 → 链接可搬运 → 格式白名单 → 本地文件 → 授权 → 来源/模态 → 登记要素 → 通过**，
一条登记项只归一个台账（原始文件问题优先），保证统计唯一归口。

---

## 4. 算法 / 模型后端

无模型、无 GPU、无外部服务依赖：纯规则判定（扩展名白名单 + 文件头签名 + 授权枚举 + 登记要素）。
表1 模型列为 `--`，因此**不存在模型分数**，也不存在「模型不可用仍标 model」的伪造降级。

---

## 5. 降级与失败行为

| 场景 | 行为 |
| --- | --- |
| 远端链接无法本地打开（默认 `header` 模式） | 正常登记，`file_check=link_only`（明确标注「只登记链接」，不假装校验过文件） |
| `strict` 模式下找不到/打不开本地文件 | 【原始文件-无法解析】原始隔离区，`reason_code=file_not_accessible` |
| 损坏 JSON / 非对象 / 非 UTF-8 行 / 超长行 | 写入原始隔离台账拒绝子类，`stats.rejected` 计数，**不静默丢弃** |
| 统计守恒不成立 | 结构化失败：写 `failure.json`，`/predict` 返回 `code=1`，CLI 退出码非 0，不伪造成功 |
| 配置或 `config` 非法（含覆盖表1 冻结项） | `code=1`（HTTP 层为 422/200+code=1），不静默生效 |
| 总开关 `enabled=false` | 本次不跑：产出空台账，`status=disabled`、`code=0` |
| 同一 `uniqueId` 并发写 | 任务锁拒绝第二次写（`TaskBusyError` → `code=1`） |
| 响应（`outputFile` 内容对象）超 `max_response_bytes` | 失败，**不截断**后仍报成功 |

---

## 6. 启动

```powershell
# 依赖
pip install -r requirements.txt

# HTTP 服务（端口 8101）
$env:OUTPUT_DIR = "G:\data\op01_output"
python -m uvicorn app:app --host 0.0.0.0 --port 8101

# CLI（处理器入口）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke --is-last-operator
```

---

## 7. CLI / API 示例

### 7.1 CLI

```powershell
# [v4.2] 阶段内/阶段末返回形态统一：均返回 crossmodal-files-v1 的 outputFile + result
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke --is-last-operator

# 业务覆盖（仅 enabled / file_check_mode / dedupe / check_source_modality）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --override file_check_mode=strict
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --override enabled=false
```

CLI 真实输出（节选自本次验证，`status=partial`）：

```json
{"code": 0, "msg": "接入多模态批次完成",
 "data": {"runId": "run002", "status": "partial",
          "dataShape": {"outputFile": "file-object"},
          "outputFile": {"format": "crossmodal-files-v1", "entrypoint": "01_workitems.jsonl",
                         "fileCount": 8, "totalBytes": 0, "files": []}}}
```

### 7.2 API（三接口 + 形态一单文件流，外层信封 `{code,msg,data}`）

```powershell
# 1) 上传（zip 或多文件）
curl.exe -X POST "http://127.0.0.1:8101/uploadFile?uniqueId=task_001" -F "file=@bundle.zip"

# 2) 阶段首（省略 inputFile）：统一返回 outputFile + result（[v4.2] 无 lineageFile）
curl.exe -X POST http://127.0.0.1:8101/predict -H "Content-Type: application/json" `
  -d '{\"uniqueId\":\"task_001\",\"isLastOperator\":false}'

# 3) 阶段末：返回同样的 outputFile 内容对象 + result
curl.exe -X POST http://127.0.0.1:8101/predict -H "Content-Type: application/json" `
  -d '{\"uniqueId\":\"task_001\",\"isLastOperator\":true}'

# 4) 阶段内续跑：inputFile 用上一步 outputFile 恢复出的任务内路径
curl.exe -X POST http://127.0.0.1:8101/predict -H "Content-Type: application/json" `
  -d '{\"uniqueId\":\"task_001\",\"isLastOperator\":false,\"inputFile\":\"<任务内入口>\"}'

# 5) 形态一：单文件流（multipart/form-data），处理型 status（1 正常/0 失败）
curl.exe -X POST "http://127.0.0.1:8101/predict?detection=M01" -F "file=@batch.jsonl"
# → {"status":1,"uniqueId":"...","batch_id":"B1","accepted":2,"isolation_records":[],"process_status":"success"}

# 6) 删除（幂等）
curl.exe -X POST "http://127.0.0.1:8101/removeFile?uniqueId=task_001"
```

形态二响应示例：

```json
// isLastOperator=false 与 =true 同形
{"code":0,"msg":"阶段内完成",
 "data":{"operator":"multimodal_batch_intake","status":"partial",
         "outputFile":{"format":"crossmodal-files-v1","entrypoint":"01_workitems.jsonl",
                       "fileCount":8,"totalBytes":0,"files":[]},
         "result":{"uniqueId":"task_001","runId":"run001","status":"partial",
                   "statistics":{"registered":7,"origin_quarantined":2,
                                 "sample_quarantined":1,"returned":2,"conservation_ok":true},
                   "batch_id":"BATCH-20261001-01","accepted":7}}}
```

`data.outputFile` 为 `crossmodal-files-v1` 内容对象：
`{format, entrypoint, fileCount, totalBytes, files:[{path, encoding, size, content}]}`，
其中 `files` 含 `01_workitems.jsonl`、三类台账、`01_batch_registry.json`、`01_run.log`、`manifest.json`；
文本用 UTF-8 字符串、二进制用 base64；[v4.2] 不再返回 `lineageFile`。

---

## 8. 输出示例

```json
// 01_workitems.jsonl（主输出一行）
{"sample_id":"demo-img-local","source":"采集平台批次","source_id":"SRC-DEMO-2026",
 "batch_id":"BATCH-20261001-01","upstream_version":"pipeline-1.4.2","license":"可训练",
 "files":[{"modality":"image","path":"media/demo-img-local.png","size_bytes":16}],
 "processing":{"00":{"status":"uploaded"},
   "01":{"status":1,"process_status":"registered","label":"流程-可加工","action":"register","destination":"工作集",
         "modality":"image","reg_format":"png","file_check":"local_verified"}},
 "operator_01":{"operator":"multimodal_batch_intake","operator_number":1,"operator_version":"v1.0.0",
   "stage":"S1","sample_id":"demo-img-local","lineno":1,"source":"collection_platform",
   "source_label":"采集平台批次","batch_id":"BATCH-20261001-01","license":"可训练",
   "modality":"image","format_ext":"png","file_link":"media/demo-img-local.png",
   "file_check":"local_verified","media_policy":"link_only_no_copy","status":1,
   "process_status":"registered",
   "label":"流程-可加工","action":"register","destination":"工作集","registered":true,
   "uniqueId":"smoke_cli","runId":"run001"}}

// 01_sample_quarantine.jsonl（样本隔离一行，节选）
{"lineno":9,"role":"sample_quarantine","sample_id":"demo-untrainable","label":"样本-敏感",
 "action":"isolate","destination":"样本隔离库","reason_code":"license_untrainable",
 "reason":"授权结论为不可训练","license_state":"untrainable",
 "file_link":"oss://demo/img/demo-untrainable.png","original_record":{}}


```

---

## 9. 资源需求

- CPU 纯服务，**无 GPU、无模型权重**；内存随单行长度与批次台账规模线性增长（单行上限 8 MiB）；
- 默认限额：上传 256 MiB / 解压 512 MiB / 文件数 1000 / 响应 64 MiB（可用环境变量调整）；
- 落地建议：2C4G 起；`OUTPUT_DIR` 需与其它算子共享同一挂载卷，便于工程侧按 `uniqueId` 取产物。

---

## 10. 已知限制

1. **同格式别名按表1 字面拒绝**：`jpeg`（同 `jpg`）、`tif`、`mkv` 等不在表1 白名单的写法一律进原始隔离区；
   是否放宽需主 agent 决策（表1 措辞是「就这些，页面上不能手填别的格式」）。
2. **远端链接不做内容下载**：`oss://`/`http(s)://` 只登记链接（`file_check=link_only`），
   不做「文件打不开」的远端验证；如需远端可读性校验，需接入对象存储 SDK（本次不允许新增外部依赖）。
3. **`check_source_modality` 默认关闭**：来源产线模态与文件模态一致性校验在表1 未明确，
   默认不拦，开启后按「文本/图像/音频/视频产线只收对应模态，采集平台批次不限」执行。
4. **文本格式只校验扩展名**：`txt/json/csv` 不校验内容是否可解析（格式白名单口径 = 扩展名）。
5. **`file_check_mode=whitelist` 不打开文件**，因此该模式下无法发现「文件打不开」。
6. **重复登记判定键**为 `sample_id + 归一化链接 + 模态`，默认只保留首次、其余退回补登记。
7. **未在真实 Docker 环境验证**（开发机无 docker CLI），`Dockerfile` 仅按 CPU 纯服务口径提供。
8. 端口 `8101` 按 demo_10.6 「8100+编号」约定取值，若正式端口分配表另有规定需以工程侧为准。

---

## 11. 真实测试结果（本次验证，Windows / CPython 3.9.7）

```powershell
python -m pytest tests/ -q          # 119 passed
python tests/smoke_api.py           # SMOKE 30/30 passed（真实 uvicorn 子进程 + httpx）
python -m compileall -q .           # 通过
python -m json.tool map.json        # 通过
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

覆盖范围：正常样例 / 边界值（空文件、空包、单文件、多文件、超长行、BOM）/ 空输入 /
损坏 JSON / 非对象行 / 非 UTF-8 行 / 缺字段（`sample_id`、`batch_id`、`source`、`source_id`、
`upstream_version`、文件链接）/ 错误配置（非法 `file_check_mode`、覆盖冻结项、未知键、非布尔覆盖）/
错误编码 / 任务隔离（两个 `uniqueId`、跨任务 `inputFile`、并发锁）/ 路径安全（`uniqueId` 逃逸、
zip slip、绝对路径连接、目录穿越）/ 两种 `isLastOperator` 返回形态 / 本算子专项降级失败路径
（`strict` 模式不可访问、文件头不符、空文件、`whitelist` 跳过、守恒不成立即失败）。

CLI 冒烟实测：`13` 行输入（含 2 条坏行）→ `registered=7`、`origin_quarantined=2`、
`sample_quarantined=1`、`returned=2`（其中 1 条重复登记）、`local_verified=1`、`batches=6`，守恒通过。
