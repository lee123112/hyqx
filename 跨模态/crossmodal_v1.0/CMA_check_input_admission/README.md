# 算子 10：核对输入对象准入（`input_admission`）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

> 表1 **#10**，阶段 `S3`（表1 第 3 环节“数据加工”；demo 五阶段中属“跨模态数据对齐标注”）。
> 上游 `#9 准备候选描述`（并核对 `#1 接入多模态批次` 的来源/批次登记），下游 `#11 生成候选配对`。
> **纯规则实现：无模型、无权重、不训练、不联网**，不产生任何模型分数。

---

## 1. 功能与边界

### 1.1 做什么（表1 “必须核对”四项）

| # | 核对项 | 不通过时的处置 |
| --- | --- | --- |
| 1 | 已准备的描述非空、且不是文件名/分类标签（来自 #9 候选描述） | **不拦、退回 #9**（`流程-待补描述`）；退回超限才锁定样本隔离库 |
| 2 | 至少 2 种模态（`min_modalities` 可选 2–4） | 样本隔离库，`样本-语义不通` |
| 3 | 授权结论为“可训练”（表1 只读项，只有这一项） | 样本隔离库，`样本-敏感` |
| 4 | 来源 ID、批次号、文件都能打开 | 缺登记 → 退回 `#1`；文件打不开 → 原始隔离区 |

### 1.2 不做什么（表1 明确禁止越界）

- **不重做单模态画质评分**：只判断“文件能否打开”，不算清晰度/分辨率/信噪比；
- 不做图文/音视频配对，不打 CLIP 相似度分，不加载任何模型；
- 不修改上游结论：`alignment`、`qc`、其它 `processing` 键与未知扩展字段全部原样透传，
  只新增 `admission` 与 `processing["10"]`；
- 不改变阶段边界：阶段末由工程侧用 `isLastOperator=true` 标记；v4.2 起中间与阶段末响应同构。

### 1.3 判定优先级

```
结构(sample_id) → 文件可打开 → 来源ID/批次号 → 描述 → 授权 → 模态数 → 通过
```
原始文件问题优先：文件打不开的记录无法被任何下游消费。

---

## 2. 输入与输出

### 2.1 输入

- `/predict` 的 `inputFile`（阶段内串联），或阶段首上传后任务目录 `input/` 的入口 JSONL
  （`manifest.json` 的 `entrypoint` 优先；无 manifest 时只接受唯一候选）；
- 每行一条 #9 输出记录（严格 UTF-8）；坏行逐行进原始隔离台账，**不静默丢弃**；
- 需要的字段：`sample_id`、`text.content`（描述）、`metadata.source_id`/`batch_id`/
  `license`/`image_tags`/`retry_count`、`image|video|audio.path`。详见
  [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)。

### 2.2 输出（写入 `OUTPUT_DIR/<uniqueId>/input_admission/<runId>/`）

| 文件 | 去向 |
| --- | --- |
| `10_admitted.jsonl`（入口，合格待配对对象池） | #11 |
| `10_return.jsonl`（退回/待补） | #9、#1 |
| `10_origin_quarantine.jsonl`（原始隔离台账，含坏行） | #21 |
| `10_sample_quarantine.jsonl`（样本隔离库台账） | #21 |
| `manifest.json`、`run.log`、`media/` | 运行与审计 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py` 的血缘构建接口与 `utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 打包解包）；原血缘中的统计/配置快照等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

守恒式（不成立即结构化失败并清理产物）：

```
input         = valid_records + bad_lines
valid_records = admitted + returned + origin_quarantined + sample_quarantined
origin_entries = bad_lines + origin_quarantined
```

---

## 3. 配置（`configs/config.yaml`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `operator.name` / `.version` | `input_admission` / `v1.0.0` | 部署身份，请求不可改 |
| `stage_id` | `S3` | 阶段标识，请求不可改 |
| `min_modalities` | `2` | 至少几种模态，只能 2–4 |
| `min_description_chars` | `5` | 短于此视为没有有效描述（对齐 #9 默认 5 字） |
| `max_description_chars` | `500` | 超出说明 #9 未按 500 字截断，同样退回 #9 |
| `max_return_count` | `2` | 退回次数上限；达到上限的下一次锁定样本隔离库 |
| `require_source_and_batch` | `true` | 是否要求来源 ID/批次号齐备 |
| `file_check_mode` | `header` | `header`=纯 Python 头签名；`decode`=叠加真实解码 |
| `allow_degraded` | `false` | decode 缺解码器时是否允许降级为头签名 |
| `required_license` | `可训练` | 表1 只读项，**请求不可覆盖** |
| `trainable_license_values` | `[可训练, trainable]` | 授权字段归一化取值 |
| `allowed_extensions` | 见 DATA_SCHEMA | 表1∪共享 schema 白名单，只读 |

`/predict` 的 `config` 只允许覆盖
`min_modalities`、`min_description_chars`、`max_description_chars`、`max_return_count`、
`require_source_and_batch`、`file_check_mode`、`allow_degraded`；
携带 `operator`/`stage_id`/`required_license` 或任何未知键都会直接报错。

---

## 4. 算法/模型后端与降级行为

- **后端：无。** 本算子不加载模型、不读权重、不访问外部服务，因此不存在“模型不可用但仍标
  model”的伪造降级；表1 也未要求本节点产出任何模型分数。
- 文件可打开核对：
  - `header`（默认）：存在性 + 非空 + 扩展名与模态匹配 + 头签名（纯标准库，零第三方依赖）；
  - `decode`：追加真实解码（图片 Pillow、视频 OpenCV、`wav` 标准库 `wave`）；
    无内置解码器的格式（`mp3`/`flac`/`aiff`/`aif`/`m4a`）：
    - `allow_degraded=false`（默认）→ 结构化失败 `DependencyError`（HTTP 400 / CLI code 1），
      **不伪造“已解码”**；
    - `allow_degraded=true` → 该文件 `status=1`、`process_status=header_only`，运行 `status=degraded`，
      降级事实写入 `statistics.degraded_mode=true`（随 `data.result` 返回）。
- 单条失败不中断批次：坏行/隔离/退回分别进各自台账，只有“整文件非 UTF-8”“统计不守恒”
  “config 非法”“必需解码器缺失”这类运行级问题才整体失败。

---

## 5. 启动与调用

### 5.1 安装与启动

```powershell
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8110     # map.json 声明端口 8110
```

输出根由环境变量 `OUTPUT_DIR` 决定（默认本目录 `output/`）；配置路径由 `OPERATOR_CONFIG`
决定（默认本目录 `configs/config.yaml`）。

### 5.2 CLI

```powershell
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
# 阶段末标记预览（--is-last 为兼容参数，v4.2 起 data 形状与中间调用一致）：
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke --is-last
# 覆盖业务参数：
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke --override '{"min_modalities":3}'
```

### 5.3 三接口（只有这三个，**没有** `/downloadFile`）

```powershell
# 1) 上传素材包（zip，内含 demo.jsonl + media/ + manifest.json）
curl.exe -s -X POST "http://127.0.0.1:8110/uploadFile?uniqueId=task_a_f1" -F "file=@bundle.zip"

# 2) 预测（阶段首：不传 inputFile）
curl.exe -s -X POST "http://127.0.0.1:8110/predict" -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_a_f1","isLastOperator":false}'

# 2') 阶段内后续算子：inputFile 传上一中间响应里的输入路径
#     {"uniqueId":"task_a_f1","isLastOperator":false,"inputFile":"<上一次运行目录>/10_admitted.jsonl"}

# 3) 清理任务（幂等）
curl.exe -s -X POST "http://127.0.0.1:8110/removeFile?uniqueId=task_a_f1"
```

响应壳固定 `{"code":0|1,"msg":"...","data":{...}}`：

- [v4.2] `isLastOperator=false` 与 `=true` 同形：`data = {operator, status, outputFile, result}`，
  `outputFile` 为 `crossmodal-files-v1` 文件内容对象，`result` 含
  `uniqueId/runId/status/statistics/admission/eligible_object_id/reason_code`；
- 不再返回 `lineageFile`，也不再写 `lineage.json` / `stage_context.json`；
- 形态一（multipart `?detection=M10` + `file`）：`{status, uniqueId, admission, eligible_object_id,
  reason_code, process_status}`，判定型 `status` 为 int（1 准入/0 不准入）。

---

## 6. 真实测试结果（2026-10-06，本机 Python 3.9.7 / Windows）

```
python -m pytest tests/ -q
→ 68 passed
```

覆盖：正常样例、边界值（模态 2/3/4、描述 4/5/501 字、退回上限 1/2）、空文件、损坏
JSONL、缺字段、错误配置、错误编码、任务隔离、路径安全（uniqueId 穿越、inputFile 越界、
zip slip）、`isLastOperator` 两种取值的响应同构，以及本算子专项路径：缺描述退回 #9、损坏文件进
原始隔离区、授权/模态问题进样本隔离库、退回超限锁定、decode 依赖缺失失败与允许降级。

```
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
→ {"code": 0, "msg": "partial：原始隔离 3 条、样本隔离 4 条、坏行 2 条", ...}
   input=16 valid_records=14 bad_lines=2
   admitted=4 returned=3 origin_quarantined=3 sample_quarantined=4 locked=1
   origin_entries=5 files_checked=13 conservation_ok=true
```

```
python -m json.tool map.json      # 通过（0 退出码）
python -m compileall -q .         # 通过（0 退出码）
```

样例分类与 `dataset/DATA_SCHEMA.md §4`、`tests/test_operator.py::test_sample_dataset_classification_matches_spec`
三方一致。

---

## 7. 输出示例（实际产物，已截断）

`10_admitted.jsonl`（节选，[v4.2] `status` 为 int、原字符串迁入 `process_status`）：

```json
{"sample_id": "s-ok-1", "text": {"content": "红色方块在白色桌面上", "description_source": "图注"},
 "image": {"path": "media/img_ok.png"}, "processing": {"09": {"status": 1, "process_status": "success"},
 "10": {"status": 1, "process_status": "success", "action": "admit", "modalities": ["text", "image"]}},
 "admission": {"operator": "input_admission", "formalOperatorNumber": 10, "passed": true,
 "modalities": ["text", "image"], "modality_count": 2,
 "description": {"source": "图注", "chars": 10, "valid": true}, "license": "可训练",
 "source_id": "SRC-A", "batch_id": "BATCH-2026-10",
 "file_checks": {"image": {"status": 1, "process_status": "checked", "mode": "header",
                           "checker": "signature"}}}}
```

`10_return.jsonl`（节选，**不隔离**）：

```json
{"sample_id": "s-ret-1", "line": 5, "label": "流程-待补描述", "action": "return",
 "destination": "09_准备候选描述", "reason": "description_missing", "retry_count": 0,
 "locked": false}
```

`10_origin_quarantine.jsonl`（节选）：

```json
{"sample_id": "s-orq-1", "line": 8, "label": "原始文件-损坏", "action": "isolate",
 "destination": "原始隔离区", "reason": "file_missing", "batch_id": "BATCH-2026-10"}
```

各清单记录的 `status` 均为 int，原字符串迁入 `process_status`（详见 `dataset/DATA_SCHEMA.md`）。
v4.2 起不再产出 `lineage.json` / `stage_context.json`，业务摘要见响应的 `data.result`。

---

## 8. 资源需求

| 项 | 需求 |
| --- | --- |
| CPU/内存 | 单核即可；处理 1 万行 + 千级媒体约 < 100MB 峰值内存（流式逐行） |
| GPU | **不需要** |
| 磁盘 | ≈ 输入规模 + 通过样本的媒体副本（被隔离记录的媒体不复制） |
| 网络 | **不需要**（无外部服务、无模型下载） |
| Python | 3.9+（本机实测 3.9.7；镜像基线 3.11-slim） |

---

## 9. 已知限制与未确认口径

1. **描述承载位置**：#9 尚未落地，本算子同时接受 `text.content`、`metadata.description`，
   来源取 `text.description_source` → `metadata.description_source` → `processing["09"]`。
   待 #9 定稿后应固定为单一位置（见 `HANDOFF.md` 待决策项 1）。
2. **来源 ID/批次号缺失**的表1 处置未明确：本算子按“退回批次登记（#1）**不隔离**”处理，
   属于流程状态而非质量隔离；如需改为样本隔离库需主 agent 定口径。
3. **重复 `sample_id`**：只计数并在 `admission.duplicate` 标记，不丢弃、不隔离（表1 #10 未
   要求去重）；下游若需去重需另行确认。
4. **`required_license` 固定为“可训练”**：`config`/YAML 均不可改（表1 只读），但 YAML 中
   的键仍可被部署侧改错值，改动会直接改变准入结果，部署前需核对 `configs/config.yaml`。
5. **decode 模式覆盖不全**：`mp3`/`flac`/`aiff`/`m4a` 无内置解码器，只能头签名；默认
   `header` 模式下所有格式都只做头签名校验（这是“能否打开”的演示口径，不是画质评分）。
6. **阶段边界**：本算子按 S3 中间节点实现（阶段内后续为 #11–#19）。若工程侧把 S3 阶段末
   定在 #10，则 `isLastOperator=true` 仅表示阶段末调用；v4.2 起中间与阶段末响应同构，无需分支回归。
7. **Docker 镜像未实际构建**：开发环境未安装 Docker，`Dockerfile` 只做静态提供。

---

## 10. 目录结构

```
10_核对输入对象准入/
  app.py                    # 三接口（FastAPI）
  inference.py              # 配置 + run() + CLI + 业务统计（v4.2 不再写血缘/阶段上下文）
  configs/config.yaml       # 部署默认配置
  utils/
    admission.py            # 业务模块：准入核对（纯规则）
    io.py                   # 流式 JSONL、路径安全、任务锁、安全解压
    schemas.py              # 配置/请求模型与格式白名单
    lineage.py              # 血缘/阶段上下文构建（v4.2 停用，存档）
    transport.py            # crossmodal-files-v1 打包/恢复
    logger.py               # 规范日志
  dataset/DATA_SCHEMA.md    # 读写字段口径
  dataset/sample/           # demo.jsonl + media/ + manifest.json + make_sample.py
  tests/                    # 68 个用例（业务/接口/传输）
  packaging/prepare_package.py  # 可选装配脚本（产出 dist/Dockerfile + dist/operator/）
  Dockerfile                # 本目录可直接 docker build
  map.json  requirements.txt  output/  README.md  OPERATOR_MANUAL.md  HANDOFF.md
```
