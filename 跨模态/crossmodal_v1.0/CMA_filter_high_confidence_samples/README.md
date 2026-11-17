# 算子 19：筛选高置信样本入池（high_confidence_pool）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

> 表1 v4.1 正式编号 **19**｜阶段 **S3 数据加工**｜优先级 **P0**｜开关 **默认开启，必须**
> 类型 **规则型**｜模型列 **--**（无模型、无外部服务、不联网）
> 版本 `v1.0.0`｜HTTP 端口 **8119**

## 1. 功能边界

### 1.1 做什么

按表1 #19 的四道必过项筛出可入候选池的样本：

| 门 | 表1 原文 | 判定依据（读取位置） |
|---|---|---|
| `route` | 评分开启且通过 | 已开启且在生效的评分路线（#12 图文 / #13 音文 / #14 视文）**全部**通过；三条都未生效时，若 #15 时序结论为「通过」，则以 `processing["15"]` 作为路线兜底证据（`enabled_routes=["15"]`，2026-11-05 起默认开启，见 §3 `temporal_route_fallback`） |
| `whole` | 整体自动/人工对齐 | 整体对齐结论 ∈ `{auto_aligned, manual_aligned}`（由 `required_alignment` 限定） |
| `license` | 授权可训练 | 授权结论为「可训练」（`可训练` / `trainable` / `yes` / `true`，大小写不敏感） |
| `temporal` | 有时间戳样本时序通过 | **存在时间轴**的样本，时序结论必须通过；不存在时间轴视为「不适用」，不拦截 |

**四项同时满足**才写入候选池。附加约束（表1 研发测边界）：

- **不得用加权分把已隔离样本加回**：本算子不计算任何合成分数（`score_fields_produced=false`），
  且显式尊重上游隔离标记（`processing["21"].status=="isolated"`、`#17 incomplete`、`#18 isolated`）。
- **局部标注不作为入池硬条件**：`#17`/`#18` 仅作为 `evidence.local_annotation` 记录，
  `local_annotation_hard_requirement` 恒为 `false`；`#17 pending_manual_review` / `#18 pending_manual`
  不影响入池。

### 1.2 不做什么

- 不修改、不覆盖任何上游结论字段（#10–#18 的命名空间原样保留）。
- 不产出任何分数、分档或加权结果；不做模型推理。
- 不把 `data.outputFile`（`crossmodal-files-v1` 文件内容对象）当作下一算子路径。
- 不实现 `/downloadFile`。
- 不把「必过项结论缺失」误判为低质量隔离（属流程状态，进 `pipeline_wait`）。

## 2. 输入与输出

### 2.1 输入

- **入口**：UTF-8 JSONL，一行一条 JSON 对象；支持扁平记录（`sample_id`）与 #11 成对记录（`pair_id` + `left`/`right`）。
- **读取的上游字段**：见 `dataset/DATA_SCHEMA.md` §1.2（逐字段列出优先级与来源）。
- **媒体引用**：只读取记录中声明的相对路径，用于①路径合法性扫描 ②尽力搬运。
  绝对路径 / 目录穿越 / `C:\` / `http://` / 反斜杠一律判为**路径非法** → 原始隔离区。

### 2.2 输出（`OUTPUT_DIR/<uniqueId>/high_confidence_pool/<runNNN>/`）

| 文件 | 说明 | 下游 |
|---|---|---|
| `19_candidate_pool.jsonl` | 主输出：**仅入池样本**，保留原字段 + 追加 `processing["19"]` 与 `operator_19` | #20、#22、#24 |
| `19_sample_quarantine.jsonl` | 必过项明确失败（`样本-语义不通`） | #21 样本隔离库 |
| `19_pending.jsonl` | 结论未定/待人工（`流程-*`），流程状态，不隔离 | 留产线 |
| `19_rejects.jsonl` | 坏行、非对象、编码异常、缺标识、重复标识、路径非法 | 原始文件排查 |
| `19_run.log` | 逐行「时间 \| 动作 \| 数量」 | 运维 |
| `manifest.json` | 运行清单（`crossmodal-manifest-v1`） | 工程侧 |
| `media/<相对路径>` | 仅在任务内确实存在时搬运的媒体副本（缺失不报错） | 工程侧 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py` 的血缘节点/阶段上下文函数与 `utils/mty_compliance.py`，代码保留存档；`lineage.py` 的 `now_iso`/`config_snapshot`/`artifact_entry` 等通用工具仍在用，`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计、模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

字段级结构、原因码表、statistics 守恒等式见 `dataset/DATA_SCHEMA.md` §2–§4。

## 3. 配置

`configs/config.yaml`（未知键一律失败，`extra=forbid`）：

```yaml
operator: {number: 19, id: high_confidence_pool, name: high_confidence_pool,
           label: 筛选高置信样本入池, version: v1.0.0, enabled: true}
transport: {format: crossmodal-files-v1, max_response_bytes: 67108864}
stage_id: S3
enabled: true                      # 表1 必须工具，置 false 直接失败
required_alignment: auto|human     # 固定口径（请求不可覆盖）；auto / human / auto|human
require_temporal_if_present: true  # 固定口径（请求不可覆盖）
temporal_route_fallback: true      # 无评分路线时用 #15「时序通过」补 route 门（仅部署配置）
training_authorization: required
min_enabled_routes: 1              # 1..3
missing_upstream_policy: waiting_manual   # waiting_manual | failed
```

`/predict` 的 `overrides` 与形态一表单只允许 4 个业务键（`enabled`、
`training_authorization`、`min_enabled_routes`、`missing_upstream_policy`），
亦**不允许**把 `enabled` 置 false（HTTP 400）。

**固定口径（不可通过请求削弱）**：`required_alignment` 固定为 `auto|human`、
`require_temporal_if_present` 固定为 `true`——表1 #19 的四项必过项不能去掉任何一项，
所以这两个键**不在**请求覆盖白名单内：`overrides` 或形态一表单携带它们时一律返回
HTTP **422**（`errorType=FrozenOverride`，中文错误明确给出固定取值），既不静默忽略
也不允许收窄/关闭；取值只由部署配置决定（默认值见上）。

**`temporal_route_fallback`（2026-11-05 起默认 `true`，仅部署配置可改，不在请求白名单内）**：
时序窗口的 #14 判 `not_applicable`（不评分），#13 可能因转写不可靠也判 `not_applicable`，
三条评分路线都不生效；此时若 #15 已判「时序通过」，本算子把 `processing["15"]` 当作路线证据，
避免「#15 通过 + #16 已对齐」的样本仍以 `no_scoring_route_enabled` 留在产线。

- 只认「通过」：`passed=true` 或 `status=passed`；#15 失败**不**补路线，仍由 `temporal` 门以
  `temporal_failed` 隔离（主因不会被改写成 `route_failed`）；
- `not_applicable` / `skipped` / `pending_calibration` 不构成证据，保持「无评分路线」待人工；
- 只在**没有任何评分路线生效**时生效；已有 #12/#13/#14 生效时，route 门仍只按评分路线判定；
- 置 `false` 复现旧行为（一律待人工），用于口径对照验收。

> 与计划第 3 节 YAML 草稿的对应：`settings` 字符串列表落地为强类型扁平键；
> `operator.enabled` 与顶层 `enabled` 同时支持且必须一致；`transport` 段已落地并真实生效。

## 4. 算法 / 模型后端

**无模型后端。** 表1 模型列为 `--`，本算子是纯规则判定：

- 不加载 CLIP / ASR / 任何权重，不联网，不需要 GPU；
- 不产生任何分数；`statistics.model` 恒为 `null`，`models_available` 恒为 `false`；
- 因此不存在「模型不可用但仍标 model」的伪造降级路径。

## 5. 降级行为

| 场景 | 行为 |
|---|---|
| 必过项结论缺失（缺 #16 / 缺授权 / 缺 #15 / 路线 `pending_model`） | `waiting_manual`：写 `19_pending.jsonl`，**不隔离**；`missing_upstream_policy=failed` 时整体 `failed` |
| 无生效评分路线，但 #15 时序通过（时序窗口） | `temporal_route_fallback=true`（默认）：route 门以 `enabled_routes=["15"]` 通过，继续判整体对齐/授权/时序；置 `false` 时以 `no_scoring_route_enabled` 待人工 |
| 候选池为空且全部记录待人工 | 运行状态 `degraded`（不伪造任何结论） |
| 全部行都坏（`valid_records==0` 且 `bad_lines>0`） | 运行状态 `failed`，仍产出完整的 `19_rejects.jsonl` 与 `manifest.json`、`run.log` |
| 统计守恒失败 | 运行状态 `failed`，`conservation_failures` 列出具体等式 |
| 输入为空 | `empty_input=true`、`status=success`（不是崩溃） |
| 非 UTF-8 编码 | 记录 `encoding_error=true`，该行进 `19_rejects.jsonl`，解析在异常处停止并记 `fileReadIncomplete` |

## 6. 启动

```bash
# 安装依赖（Python 3.9+）
pip install -r requirements.txt

# 方式一：HTTP 服务（正式接口）
uvicorn app:app --host 0.0.0.0 --port 8119
# 或
python app.py

# 环境变量
OUTPUT_DIR=/data/output          # 输出根目录（默认 <算子目录>/output）
OPERATOR_CONFIG=/app/configs/config.yaml
OPERATOR_LOG_LEVEL=INFO
```

```bash
# Docker
docker build -t operator-19-high-confidence-pool:v1.0.0 .
docker run --rm -p 8119:8119 -v /host/output:/data/output \
  operator-19-high-confidence-pool:v1.0.0
```

## 7. CLI 示例

```bash
python processor.py --input dataset/sample/demo.jsonl \
                    --unique-id smoke_cli --output-root output_smoke

# 严格模式（结论缺失即失败）
python processor.py --input dataset/sample/demo_strict.jsonl \
                    --unique-id strict_demo --output-root output_smoke \
                    --override '{"missing_upstream_policy":"failed"}'

# 只允许任务目录内输入（与 API 行为一致）
python processor.py --input <任务目录>/input/demo.jsonl \
                    --unique-id task_x --restrict-to-task
```

退出码：`0` 成功/待人工，`1` 运行状态 `failed`，`2` 输入或配置错误。
stdout 为统一信封 `{"code": 0|1, "msg": "...", "data": {...}}`。

## 8. API 示例（只有三个接口）

### 8.1 `POST /uploadFile?uniqueId=<id>`

```bash
curl -X POST "http://127.0.0.1:8119/uploadFile?uniqueId=task_a" \
     -F "file=@bundle.zip"
# {"code":0,"msg":"上传成功","data":{"uniqueId":"task_a","entrypoint":"demo.jsonl","files":[...]}}
```

`bundle.zip` 需含 `manifest.json`（`entrypoint` 指向主 JSONL）或**唯一**一个 `.jsonl`。

### 8.2 `POST /predict`

```bash
# 形态一：单文件流（multipart/form-data，?detection=filter_high_confidence_samples，file 必传）
curl -X POST "http://127.0.0.1:8119/predict?detection=filter_high_confidence_samples" \
     -F "file=@demo.jsonl"
# {"status":1,"process_status":"success","uniqueId":"task_a","sample_id":"hc-0003",
#  "pool_status":"accepted","pool":"high_confidence","route":"14",
#  "reason_code":"all_gates_passed"}      # 判定型：1 入池/待人工，0 不入池（失败为 {"status":0,"msg":"..."}）

# 形态二：中间（isLastOperator=false）与阶段末（true）响应同构，均返回 outputFile + result
curl -X POST http://127.0.0.1:8119/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task_a","isLastOperator":false}'
# {"code":0,"msg":"候选池 6 条；...","data":{"operator":"high_confidence_pool","status":"success",
#   "outputFile":{...crossmodal-files-v1...},
#   "result":{"uniqueId":"task_a","runId":"run001","entrypoint":"19_candidate_pool.jsonl",
#             "outputFile":"high_confidence_pool/run001/19_candidate_pool.jsonl",
#             "statistics":{...},"model":null}}}

# 阶段内后续算子：传任务内相对路径（上一阶段产物路径由编排侧记录）
curl -X POST http://127.0.0.1:8119/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task_b","isLastOperator":false,
       "inputFile":"task_b/input/demo.jsonl"}'
```

### 8.3 `POST /removeFile?uniqueId=<id>`

```bash
curl -X POST "http://127.0.0.1:8119/removeFile?uniqueId=task_a"
# {"code":0,"msg":"已清理","data":{"uniqueId":"task_a","removed":true}}   # 二次调用 removed=false
```

`GET /health` 返回 `downloadFile: "not_registered"`；`/downloadFile` 返回 404。

## 9. 输出与响应示例（v4.2）

形态二响应（`POST /predict`，JSON body；`isLastOperator` 必传，中间/阶段末同构）：

```json
{"code":0,"msg":"候选池 6 条；样本隔离 6 条；待人工 4 条；原始隔离 3 条","data":{
  "operator":"high_confidence_pool",
  "status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"19_candidate_pool.jsonl",
    "fileCount":6,"totalBytes":0,
    "files":[{"path":"19_candidate_pool.jsonl","encoding":"utf-8","size":0,"content":"..."}]},
  "result":{"uniqueId":"smoke_cli","runId":"run001","entrypoint":"19_candidate_pool.jsonl",
            "outputFile":"high_confidence_pool/run001/19_candidate_pool.jsonl",
            "statistics":{"input_records":21,"valid_records":19,"admitted_records":6,
                          "sample_isolated":6,"pending_records":4,"origin_quarantined":3,
                          "conservation_ok":true,"model":null},
            "model":null}}}
```

`isLastOperator` 仍必传（严格布尔），只标记阶段末调用；v4.2 起不再用 `false` 只回血缘对象，也**不再返回 `lineageFile`**。`data.result.outputFile` 是任务内相对路径，`data.outputFile` 是 `crossmodal-files-v1` 文件内容对象。

入池记录示例（节选）：

```json
{"sample_id": "hc-0003", "processing": {"12": {...}, "19": {
  "status": 1, "process_status": "admitted", "decision": "admit", "action": "keep",
  "destination": "candidate_pool", "reason_code": "all_gates_passed",
  "label": "样本-评分：入池",
  "gates": {"route": {"passed": true, "enabled_routes": ["14"]},
            "whole": {"passed": true, "decision": "auto_aligned"},
            "license": {"passed": true, "value": "可训练"},
            "temporal": {"passed": true, "applicable": true, "status": "passed"}},
  "local_annotation_hard_requirement": false, "score_fields_produced": false,
  "model": null, "human_verified": false, "review_required": true}},
 "operator_19": {"decision": "admit", "reason_code": "all_gates_passed", ...}}
```

记录级 `processing["19"].status` 为 v4.2 int（1=入池/待人工，0=不入池/原始隔离），原字符串在
`process_status`；`operator_19` 扁平摘要同构。

## 10. 资源需求

| 项 | 需求 |
|---|---|
| CPU / 内存 | 1 核 / 256 MiB 起（纯流式读写，逐行处理，不把整文件读入内存） |
| GPU | 不需要 |
| 网络 | 不需要（运行期零出网） |
| 磁盘 | 输出体积与输入同量级；媒体仅在存在时按需复制 |
| 并发 | 同一 `uniqueId` 有任务锁（`<task>/<uniqueId>/.lock`），并发调用返回 409 |

## 11. 已知限制

1. **判定依赖上游结论的字段存在性**：若上游算子未在其输出中写入 `processing["15"]`/`processing["16"]`
   等节点，本算子只能判为「结论未定（待人工）」，不会猜测。
2. **时间轴存在性判定基于启发式字段**（`time_index`/`timeline`/`video.frames[].timestamp` 等）：
   若上游用其它私有字段表达时间轴，会被判为「不存在时间轴」从而不拦时序。
   该情形**不能靠请求参数归零**：`require_temporal_if_present` 固定为 `true`
   （不可通过请求关闭，携带该键直接 422），部署侧如需评估只能在 `configs/config.yaml`
   调整后重新验收，不得由调用方削弱四项必过项。
3. **`temporal_alignment` 的 `pending_calibration` 一律按「待时序判定」处理**，不区分校准原因。
4. **授权不可训练的 label**：表1 #19 质量处置只给了一个标签（`样本-语义不通`），
   因此本算子以 `label=样本-语义不通` 为准，同时用 `label_detail=样本-敏感` 保留规则文档 §2.3 的语义。
5. **媒体仅尽力搬运**：找不到的媒体只计数（`media_missing`）不阻断判定；文件内容不在本算子职责内校验。
6. **单次响应上限 64 MiB**（`transport.max_response_bytes`）：超限时返回 400（`code=1`），需工程侧分段。
7. 未接入 `#20/#22/#24`，因此 `human_verified` 恒为 `false`、`review_required` 恒为 `true`。

## 12. 真实测试结果

环境：Windows / Python 3.9.7 / pytest 8.4.2（本机实测，命令与输出如下）。

```text
$ python -m pytest tests/ -q -p no:cacheprovider
176 passed, 30 skipped          # 2026-11-05 时序路线兜底改动后实测（本机 4.74s）

$ python tests/smoke_api.py 8129
合计 16 项，通过 16 项，失败 0 项        # 真实 uvicorn 监听 8129 + httpx 走 HTTP

$ python processor.py --input dataset/sample/demo.jsonl \
      --unique-id smoke_cli --output-root output_smoke
CLI_EXIT 0
msg: 候选池 6 条；样本隔离 6 条；待人工 4 条；原始隔离 3 条
input_records=21 valid_records=19 blank_lines=1 bad_lines=2
admitted_records=6 sample_isolated=6 pending_records=4 origin_quarantined=3
rejects_entries=5 (= bad_lines 2 + origin_quarantined 3)
conservation_ok=true, conservation_failures=[]
model=null  models_available=false  rule_based=true  score_fields_produced=false
```

覆盖内容：正常样例、边界（空文件、空行、坏行、非对象、缺字段、重复标识、非 UTF-8）、
错误配置（未知键 / 非法取值 / 试图关闭工具 / 错误携带模型键 / 固定口径键被请求覆盖 → 422）、
两个 `uniqueId` 隔离、路径安全（穿越 / 绝对 / 保留名 / 跨任务 inputFile）、中间/阶段末统一响应、
文件内容对象往返、`/downloadFile` 不存在、`/removeFile` 幂等、降级与严格失败路径、
样例数据守恒与 UTF-8 内容一致性、时序路线兜底（无评分路线 + #15 通过 → 入池；
#15 失败仍按 `temporal_failed` 隔离；`not_applicable`/`pending_calibration` 与
`temporal_route_fallback=false` 仍待人工）。

## 13. 目录结构

```text
19_筛选高置信样本入池/
├── app.py                    # 三接口（/uploadFile、/predict、/removeFile）
├── inference.py              # run / CLI / 统计守恒
├── processor.py              # CLI 入口
├── configs/config.yaml       # 正式配置
├── utils/
│   ├── io.py                 # 路径安全、流式 UTF-8、原子写入、任务锁、安全解压
│   ├── logger.py             # 逐行运行日志
│   ├── lineage.py            # （v4.2 停用，存档）血缘 / 阶段上下文 / 配置快照
│   ├── transport.py          # crossmodal-files-v1 与运行清单
│   ├── schemas.py            # 严格配置与请求模型
│   ├── upstream.py           # 上游 #10–#18 结论取值（只读）
│   └── pool_filter.py        # ★业务规则：四道必过项与台账
├── dataset/
│   ├── DATA_SCHEMA.md        # 数据契约
│   └── sample/               # 可复现样例 + make_sample.py + manifest.json
├── tests/                    # 单元 + 集成 + smoke_api.py
├── packaging/prepare_package.py
├── Dockerfile / .dockerignore / requirements.txt / map.json
├── README.md / OPERATOR_MANUAL.md / HANDOFF.md / DEVELOPMENT_STATUS.md
└── output/                   # 空目录（运行期写入）
```
