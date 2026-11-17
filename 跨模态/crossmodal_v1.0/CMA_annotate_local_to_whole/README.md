# 算子 #17 标注局部对整体（`local_to_whole_annotation`）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

跨模态 demo_10.6 · 阶段 **S3 数据加工** · 表1 #17 · 默认关闭 · 严格布尔 `isLastOperator` · 仅三个 HTTP 接口

按表1 #17 与 `DEVELOPMENT_PLAN.md` / `TECHNICAL_DESIGN.md` 实现：
**图像只预填 YOLOv8n 官方通用类别中「与描述可对上」的框，专用设备不预填（由标注员手画）；
音视频沿用转写起止时间预填；只有人工确认的 bbox / 时间段可交付。**

---

## 1. 能力与边界（诚实口径）

| 项目 | 表1 #17 口径 | 本实现 |
|---|---|---|
| 开关 | 默认关闭 | `enabled: false`；关闭时不加载模型、不产生任何框或时间戳、不隔离，原样透传 |
| 模型/后端 | YOLOv8n（仅通用类别预填） | `detector_backend: auto\|yolov8n\|none`；只从**本地**权重加载，绝不联网下载 |
| 禁止项 | 不部署 Grounding DINO、不训练专用检测模型、不做词-区域一一对应 | 代码中不存在相关模型与训练路径；过滤只做「类别名/别名是否出现在描述里」 |
| 音视频 | 沿用转写起止时间预填 | 从内联 `transcript*` 或随包 sidecar 读取，最多 `max_time_ranges` 段 |
| 交付门禁 | 只有人工确认可交付 | `human_verified=true` 且标注完整 → `confirmed`（可交付）；否则 `pending_manual_review` |
| 不完整标注 | 样本隔离库（标签 `样本-语义不通`） | `incomplete` → `17_sample_quarantine.jsonl`，**不进主输出** |
| 资源不可用 | 允许显式降级/显式失败 | `allow_degraded=true` → `status=degraded`、**零框零时间戳**；`false` → 显式失败（`code=1`） |

**绝不伪造**：没有真实权重时不返回固定框、随机框或假置信度；预填框一律 `human_verified=false`。

## 2. 目录结构

```text
17_标注局部对整体/
  DEVELOPMENT_PLAN.md  TECHNICAL_DESIGN.md  DEVELOPMENT_STATUS.md
  inference.py          # run() + CLI + 真实统计（v4.2 血缘/阶段上下文输出已停用）
  app.py                # 三个正式接口（无 /downloadFile）
  utils/
    localization.py     # 业务核心：逐条判定、路线/标签/去向、下游 #18 字段
    detectors.py        # YOLOv8n 适配器、COCO-80 通用类别与描述匹配、显式降级
    io.py               # 路径安全、任务锁、原子写、流式 JSONL、安全解压
    lineage.py          # （v4.2 血缘输出停用）时间/快照等通用工具仍在用
    transport.py        # crossmodal-files-v1 文件对象、manifest、入口解析
    logger.py           # 逐行结构化日志（tee stderr + run.log）
    schemas.py          # 配置模型与冻结口径校验
  configs/config.yaml
  dataset/DATA_SCHEMA.md  dataset/sample/{demo.jsonl,manifest.json,make_sample.py,media/}
  tests/{test_operator.py,test_api.py,test_transport.py,test_localization.py,
         test_detectors.py,conftest.py,fixtures.py,smoke_api.py}
  packaging/{prepare_package.py,Dockerfile}
  output/               # 空目录（运行期写入 output/<uniqueId>/<operator_id>/<runId>/）
  requirements.txt  map.json  README.md  OPERATOR_MANUAL.md  HANDOFF.md
  Dockerfile  .dockerignore
```

## 3. 快速开始

```powershell
# 依赖（已按本机实测版本锁定；镜像内请按目标平台重锁）
python -m pip install -r requirements.txt

# 生成样例（真实可解码 PNG/WAV，不依赖网络）
python dataset/sample/make_sample.py

# 测试与端到端 smoke
python -m pytest -q
python tests/smoke_api.py

# CLI：关闭路线（默认；8 条记录全部原样透传）
python inference.py --input dataset/sample/demo.jsonl --unique-id cli_demo --output-root output_smoke

# CLI：开启路线（本机无本地权重 → 显式 degraded、零伪造框）
python inference.py --input dataset/sample/demo.jsonl --unique-id cli_demo_enabled `
    --output-root output_smoke --enable

# HTTP 服务（只暴露三个正式接口）
python app.py            # 或 uvicorn app:app --host 0.0.0.0 --port 8117
```

真实预填：本算子已随包内置 `weight/YOLOv8n/yolov8n.pt`，默认配置
`detector.weights: YOLOv8n/yolov8n.pt` 相对 `<本算子目录>/weight` 解析即可直接加载（见
`weight/README.md`）；也可用 `MODEL_ROOT` 或绝对路径 `--config` 指向其它本地副本，
**任何情况都不会自动下载**。

## 4. HTTP 契约

| 接口 | 请求 | 成功返回 |
|---|---|---|
| `POST /uploadFile?uniqueId=` | multipart ZIP | `{"code":0,"msg":"success","data":{...}}` |
| `POST /predict` | 形态二：JSON（`uniqueId`、严格布尔 `isLastOperator`、可选 `inputFile`、受限 `config`）；形态一：multipart（`detection` + `file`） | 见下 |
| `POST /removeFile?uniqueId=` | query | `{"code":0,"msg":"success","data":{"removed":bool}}` |

- 阶段首省略 `inputFile`（按上传 manifest 定位入口）；阶段内可用 `inputFile` 传任务内相对路径（上一阶段产物路径由编排侧记录）。
- **中间与阶段末响应同构**（v4.2）：`data = {"operator":"annotate_local_to_whole","status":<运行状态>,"outputFile":<crossmodal-files-v1 文件内容对象>,"result":{"uniqueId","runId","statistics"}}`。
- `isLastOperator` 仍必传（严格布尔），只标记阶段末调用；v4.2 起不再用 `false` 只回血缘对象，也**不再返回 `lineageFile`**。
- 形态一（`?detection=annotate_local_to_whole`，`file` 必传）：处理型 `status`，成功为 `{"status":1,"uniqueId","sample_id","local_annotations":[...],"process_status":"success|pending_review|not_applicable|route_disabled|failed"}`，失败为 `{"status":0,"msg":"..."}`。
- 失败统一 `{"code":1,"msg":"...","data":{}}`；参数错误 422，业务失败 400/409/413/500 但仍是同一信封。
- 同一 `uniqueId` 并发写 → `409` + `msg` 含 `task_busy`；不注册 `/downloadFile`（GET 返回 404 `code=1`）。

## 5. 输出布局（`OUTPUT_DIR/<uniqueId>/<operator_id>/<runId>/`）

```text
17_local_annotations.jsonl      # 开启路线主输出（关闭时为 17_passthrough.jsonl）
17_pending.jsonl                # 待人工确认旁路副本（与主记录不重复计数）
17_sample_quarantine.jsonl      # 样本隔离台账（样本-语义不通 → 交 #21）
17_rejects.jsonl                # 原始隔离 / 坏行台账
media/…                         # 被引用的媒体与 sidecar（相对路径保持一致）
manifest.json                   # crossmodal-manifest-v1
run.log                         # 本地控制文件（不进文件内容对象）
```

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py` 的血缘节点/阶段上下文函数与 `utils/mty_compliance.py`，代码保留存档；`lineage.py` 的 `now_iso`/`config_snapshot`/`artifact_entry` 等通用工具仍在用，`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计、模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

## 6. 结果去向

| 路线 | process_status | 标签 | 去向 | 主输出 |
|---|---|---|---|---|
| 人工确认完成 | `confirmed` | 样本-局部标注：已人工确认 | 样本池 | 是（可交付） |
| 未确认 / 未完成确认 | `pending_manual_review` | 流程-局部标注：待人工确认 | 人工队列 | 是（不可交付） |
| 已确认但标注不完整 | `incomplete` | 样本-语义不通 | 样本隔离库 | 否 |
| 模态不适用 / 整体未对齐 | `not_applicable` | 流程-局部标注不适用 | 样本池 | 是 |
| 工具关闭 | `skipped` | 流程-工具关闭：原样透传 | 样本池 | 是 |
| 原始文件/字段问题 | — | 原始隔离-* | 原始隔离区 | 否 |

记录级 `processing["17"].status` 为 v4.2 int（1=正常流程态/待人工，0=隔离/失败），上表
`process_status` 列为其原字符串值；`local_annotations`/`operator_17` 同构。

下游：`processing["17"]` 提供 `regions`/`boxes`/`candidate_boxes`/`confirmed_boxes`/
`paths.image`（归一化 xywh），供 **#18 标注局部对局部** 直接消费；结果链再交 #19。

## 7. 已实测（本机 CPython 3.9.7 / Windows）

| 命令 | 结果 |
|---|---|
| `python -m pytest -q` | **133 passed, 51 skipped**（T3 权重内置整改后本机实测；skip 均为代码内既有 `@pytest.mark.skip`：v4.2 血缘/运输输出停用等） |
| `python tests/smoke_api.py` | **SMOKE PASSED: 61 checks** |
| CLI 关闭路线（`demo.jsonl` 8 条） | `status=success`、`skipped=8`、`main=8`、`conservation_ok=true` |
| CLI 开启路线（`MODEL_ROOT` 指向空目录模拟无权重） | `status=degraded`（`model_weights_not_found`）、`confirmed=2 / pending=2 / incomplete=1 / not_applicable=1 / origin_quarantined=2`、`main=5`、`boxes_prefilled=0`（**零伪造框**） |
| CLI 开启路线（内置 `weight/YOLOv8n/yolov8n.pt`，默认配置） | `detector_ready source=local_weights`、`real_backend=true`、`network_access=false`；真实图片预填 `bus/person` 5 框（`source=yolov8n`、`human_verified=false`）；详见 `weight/README.md` |
| `python -m json.tool map.json` | exit 0 |
| `python -m compileall -q .` | exit 0 |

未实测项（见 `HANDOFF.md`）：Docker 构建、人工复核平台对接。
（真实 `yolov8n.pt` 端到端预填已在 T3 权重内置整改中实测，见 `weight/README.md`。）

## 8. demo 约定说明

- 阶段间承载格式 `crossmodal-files-v1` 与 `max_response_bytes=67108864` 是
  **demo 约定**（非甲方最终二进制协议），由 `utils/transport.py` 统一封装，后续只替换适配层。
- `run.log` 为本地控制文件，按契约 §4 不放入文件内容对象；v4.2 起不再产出 `lineage.json` / `stage_context.json`。
- 本目录为 demo_10.6 独立的 #17 单算子工程；组合服务与父级 `map.json`/`Dockerfile` 由集成处理。
