# 算子 #12 评分图文语义一致性

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

| 项 | 值 |
| --- | --- |
| 正式编号 | **#12**（表1 v4.1 第 12 行；目录前缀 `12_`） |
| 算子 ID | `image_text_semantic_scoring` |
| 版本 | `v1.0.0` |
| 所属阶段 | `S3` 跨模态数据对齐标注（表1 数据加工） |
| 端口 | `8112` |
| 入口产物 | `12_image_text_scores.jsonl` |
| 上游 | #11 生成候选配对（`11_candidate_pairs.jsonl` + 媒体） |
| 下游 | #16 跨模态整体对齐、#19 跨模态反向验证；隔离清单交 #21 |

---

## 1. 功能边界

**做什么**

1. 消费上游 #11 的图文候选配对，按取向选出"一段文本 + 一张图像"；
2. 用 **CLIP-ViT/B-32** 计算文本与图像的**真实余弦相似度**；
3. 按阈值分档并生成统一质量台账（阈值以平台面板配置为唯一事实来源；下表按算子缺省值 0.20/0.32 示例，可由 `/predict` 的 `config` 覆盖）：

   | 条件 | label | action | destination |
   | --- | --- | --- | --- |
   | `score < 0.20` | `样本-语义不通` | `isolate` | `样本隔离库`（交 #21） |
   | `0.20 ≤ score < 0.32` | `样本-评分：待人工` | `queue_manual_review` | `人工队列`（留样本池，不拦） |
   | `score ≥ 0.32` | `样本-评分：通过` | `auto_align` | `样本池` |

4. 可选 YOLOv8 辅助（默认关闭，仅对 `0.20–0.32` 的中间分开）补证据，**不改变分档**；
5. 输出分数、分档、模型身份、真实统计与异常清单。

**不做什么**

- 不做音文/音视频语义判定（表1 #13/#14 职责）；
- 不修改上游候选对字段，不重建配对；
- 不实现 `/downloadFile`；
- 不用固定分数、随机分数或规则分冒充模型分数；**模型不可用时只输出"待模型"，不产出分数**；
- 不自动联网下载权重；不新训练模型。

---

## 2. 输入 / 输出

### 2.1 输入

- 主输入：JSONL（UTF-8），每条为 #11 的候选对（`pair_id`、`left`/`right`、`evidence` …）；
- 媒体：包内相对路径（如 `media/x.png`），相对输入文件所在目录解析；
- 上传：`/uploadFile` 接收 ZIP（含 `manifest.json` + 入口 JSONL + 媒体）或单个 JSONL。

完整字段契约见 `dataset/DATA_SCHEMA.md`。

### 2.2 输出（`OUTPUT_DIR/<uniqueId>/image_text_semantic_scoring/runNNN/`）

| 文件 | 说明 |
| --- | --- |
| `12_image_text_scores.jsonl` | 主清单：`score ≥ 阈值` 的已评分记录（交 #16/#19） |
| `12_isolation.jsonl` | 隔离台账：`score < 阈值`（交 #21） |
| `12_not_applicable.jsonl` | 不适用台账：无图文组合 / 路线关闭（留产线，不隔离） |
| `12_pending_model.jsonl` | 待模型台账：模型不可用时无法评分的记录（无分数） |
| `12_rejects.jsonl` | 异常台账：坏行 / 缺字段 / 媒体问题（原始隔离区） |
| `manifest.json` / `config.snapshot.json` / `run.log` | 清单、配置快照、日志 |
| `media/**` | 本次真实引用的媒体副本（相对路径保持不变） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`utils/lineage.py`、`utils/mty_compliance.py` 代码保留存档）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

主清单记录新增字段：`text_sample_id`、`image_sample_id`、`text_reference`、
`image_reference`、`image_text_score`、`scoring`、`processing["12"]`。

---

## 3. 配置（`configs/config.yaml`）

```yaml
image_text_scoring:
  enabled: true                     # 路线开关；表1 要求 #12/#13/#14 至少开一条
  isolation_threshold: 0.20         # 算子缺省；0.20–0.40，实际以平台面板下发值为准
  auto_align_threshold: 0.32        # 0.25–0.50，且必须大于隔离阈值；实际以平台面板下发值为准
  use_detector_for_borderline: false
model:
  backend: clip_vit_b32
  library: auto                     # auto | open_clip | clip | transformers | none
  model_name: ViT-B-32
  weights: clip/ViT-B-32.pt         # 相对路径默认按 <算子目录>/weight 解析（随目录交付）；null 表示后端不可用
  device: auto                      # auto | cpu | cuda
  batch_size: 8
  allow_degraded: true              # true -> degraded（待模型）；false -> failed
detector:
  weights: null
  device: auto
  confidence: 0.25
```

- 配置定义域非法（阈值越界、`auto ≤ isolation`、枚举非法、类型错误）→ 运行 `failed`，原因 `config_error: ...`；
- 请求级 `config` 只允许 `enabled` / `isolation_threshold` / `auto_align_threshold` /
  `use_detector_for_borderline`；**模型、设备、路径、身份不可被请求改写**（越权键直接拒绝）。

---

## 4. 算法 / 模型后端

- 正式模型：**CLIP-ViT/B-32**，`utils/backends.py` 支持三种本地加载方式：
  `open_clip`（`ViT-B-32` + 本地 checkpoint）、`clip`（OpenAI 包 + 本地 `.pt`）、
  `transformers`（本地目录，`local_files_only=True`）；`library: auto` 依次尝试。
- 只为本地权重：`weights` 必须存在，否则后端显式不可用；不自动联网。
- 默认权重根是**本算子目录** `weight/`：`<算子目录>/weight/clip/ViT-B-32.pt`（随目录交付，
  见 `weight/README.md`）。未设 `MODEL_ROOT` 时按该目录解析，因此单算子目录原样拷到包外
  也能独立加载；`$MODEL_ROOT` 只作覆盖。若 `$MODEL_ROOT` 指向的目录里没有权重，按
  `weights_not_found` 显式降级/失败，**不会**回退到本地副本。
- `shared_clip.runtime` 为**可选**依赖：单算子独立部署时包根没有 `shared_clip/`，导入失败
  静默走本地权重；仅在设置了 `MULTIMODAL_CLIP_SESSION` 且共享实例存在时复用远程实例
  （行为与改造前一致）。
- 全产线复用：同进程内按 `(library, model_name, weights, device, batch_size)` 签名缓存实例
  （`backends.cached_encoder`），避免重复加载权重。
- 相似度：文本/图像编码后 L2 归一化，`cosine_similarity` 为纯 Python 实算（`math.fsum`）。
- 媒体不可解码 → 该记录进 `12_rejects.jsonl`（`原始文件-损坏`），不参与评分。

### 4.1 降级行为（严格区分"没分数"和"分数低"）

| 情形 | 退出状态 | 行为 |
| --- | --- | --- |
| 权重/依赖/设备不可用且 `allow_degraded: true` | `degraded` | 全部图文对写入 `12_pending_model.jsonl`，`clip_score: null`、`tier: null`、`model: null`、`reason: model_unavailable`；统计 `degradation_reason` 记录技术原因（如 `model_weights_not_configured`） |
| 同上但 `allow_degraded: false` | `failed` | 直接失败，原因 `model_unavailable: ...`，不写任何业务产物 |
| 运行期后端整体异常 | `degraded` | `runtime_degraded: true`，剩余记录转待模型，`degradation_reason` 保留异常类型与短信息 |
| YOLOv8 开启但不可用 | 不影响评分 | `detector.status: unavailable`，`tier` 不变 |

---

## 5. 启动

```powershell
# 1) 依赖（服务/测试）
pip install -r requirements.txt
# 2) 需要真实 CLIP 评分时再加模型依赖；权重已随算子目录内置在 weight/clip/ViT-B-32.pt，
#    不设 MODEL_ROOT 即默认从本算子目录加载；如需改用外部模型库，用 MODEL_ROOT 覆盖：
pip install -r requirements-model.txt
#    $env:MODEL_ROOT = "D:\models"   # 仅覆盖默认根，需存在 <MODEL_ROOT>/clip/ViT-B-32.pt

# 3) 启动服务（默认 8112）
uvicorn app:app --host 0.0.0.0 --port 8112
```

环境变量：`OUTPUT_DIR`（输出根目录，默认 `<算子目录>/output`）、`PORT`（直启 `app.py` 时使用）。

## 6. CLI 示例

```powershell
python processor.py --input dataset/sample/pairs.jsonl --unique-id smoke_cli --output-root output_smoke

# 调阈值 / 开辅助检测 / 关路线
python processor.py --input dataset/sample/pairs.jsonl --unique-id smoke_t \
  --isolation-threshold 0.30 --auto-align-threshold 0.40 --enable-detector
python processor.py --input dataset/sample/pairs.jsonl --unique-id smoke_off --disable-route
```

## 7. API 示例

```powershell
# 上传素材包
curl.exe -X POST "http://127.0.0.1:8112/uploadFile?uniqueId=task_001" -F "file=@pairs.zip"

# 阶段内第一个算子：不传 inputFile
curl.exe -X POST http://127.0.0.1:8112/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_001","isLastOperator":false}'

# 阶段内后续算子：传上一响应 result.files.outputFile（任务内路径）
curl.exe -X POST http://127.0.0.1:8112/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_001","isLastOperator":false,"inputFile":"<上一响应的 result.files.outputFile>"}'

# 阶段末
curl.exe -X POST http://127.0.0.1:8112/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_001","isLastOperator":true,"inputFile":"<上一响应的 result.files.outputFile>"}'

# 释放任务
curl.exe -X POST "http://127.0.0.1:8112/removeFile?uniqueId=task_001"
```

响应信封恒为 `{"code": 0|1, "msg": "...", "data": {...}}`：

- [v4.2] `isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；**中间与阶段末响应同构**：
  `data = {operator, status, process_status, outputFile, result}`，`outputFile` 为
  `crossmodal-files-v1` 文件内容对象，`result` 为
  `{runId, runDir, files{outputFile,isolationFile,notApplicableFile,pendingModelFile,rejectsFile},
  statistics, config, stageId}`；
- 不再用 `false` 只回血缘对象，也不再返回 `lineageFile`；
- 没有 `/downloadFile`，访问返回 404。

### 7.1 输出示例

主清单记录（节选）：

```json
{
  "pair_id": "pair-0001",
  "text_sample_id": "s-0001",
  "image_sample_id": "s-0002",
  "image_reference": {"side": "right", "sample_id": "s-0002",
                      "path": "media/golden_retriever.png"},
  "image_text_score": {
    "clip_score": 0.312451,
    "score_semantics": "cosine",
    "tier": "pending_manual",
    "label": "样本-评分：待人工",
    "action": "queue_manual_review",
    "destination": "人工队列",
    "status": 1,
    "process_status": "scored",
    "orientation": "left_text_right_image",
    "thresholds": {"isolation": 0.20, "auto_align": 0.32},
    "model": {"name": "CLIP-ViT/B-32", "library": "open_clip", "degraded": false},
    "detector": {"status": "not_requested", "tier_unchanged": true}
  }
}
```

形态二响应（中间/阶段末同构，字段按 `app.py` 实际构造）：

```json
{"code":0,"msg":"success","data":{
  "operator":"image_text_semantic_scoring",
  "status":"success",
  "process_status":"degraded",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"12_image_text_scores.jsonl",
    "files":[{"path":"12_image_text_scores.jsonl","encoding":"utf-8","content":"..."},
             {"path":"manifest.json","encoding":"utf-8","content":"..."},
             {"path":"media/golden_retriever.png","encoding":"base64","content":"..."}]},
  "result":{"runId":"run001","runDir":"image_text_semantic_scoring/run001",
            "files":{"outputFile":"image_text_semantic_scoring/run001/12_image_text_scores.jsonl",
                     "isolationFile":"image_text_semantic_scoring/run001/12_isolation.jsonl",
                     "notApplicableFile":"image_text_semantic_scoring/run001/12_not_applicable.jsonl",
                     "pendingModelFile":"image_text_semantic_scoring/run001/12_pending_model.jsonl",
                     "rejectsFile":"image_text_semantic_scoring/run001/12_rejects.jsonl"},
            "statistics":{"input":11,"output":0,"isolated":0,"pending_model":4,
                          "not_applicable":2,"rejected":5,"conservation_ok":true},
            "config":{"image_text_scoring":{"enabled":true,"isolation_threshold":0.20,
                                           "auto_align_threshold":0.32}},
            "stageId":"S3"}}}
```

统计守恒：`input == output + isolated + pending_model + not_applicable + rejected`。

---

## 8. 资源需求

| 项 | 说明 |
| --- | --- |
| CPU | 服务与规则逻辑毫核级；真实 CLIP 推理建议 ≥4 核 |
| 内存 | 服务 < 1 GB；CLIP-ViT/B-32 fp32 约 2–3 GB（含框架） |
| GPU | 可选；表1 按全产线一份 CLIP，`device: auto|cuda|cpu` |
| 磁盘 | 每次运行携带被引用媒体副本；`batch_size` 默认 8 |

---

## 9. 测试与真实验证结果

以下均为本机实际执行结果（命令见括号）。

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| 单元/集成测试 | `python -m pytest tests/ -q` | **127 passed, 30 skipped**（30 skip = 1 项未设 `OP12_CLIP_WEIGHTS` 的可选真实权重集成检查 + 29 项 v4.2 血缘/运输输出停用既有 skip；无失败） |
| 真实权重集成 | `$env:OP12_CLIP_WEIGHTS="<本算子目录>/weight/clip/ViT-B-32.pt"; python -m pytest tests/test_clip_plumbing.py -q` | **4 passed**（真实 ViT-B-32 权重加载 + 前向） |
| 包外 standalone CLI（权重内置） | 把本算子目录单复制到包外临时目录（无包根 `weight/`、无 `shared_clip/`、不设 `MODEL_ROOT`），运行 `python inference.py --input dataset/sample/pairs.jsonl --unique-id smoke_standalone --output-root output_standalone` | 退出码 0；`status=success`，`scored=4 / output=4 / isolated=0 / pending_manual=4 / pending_model=0`，`clip_score=0.22073`（0.20 线不隔离该分，转人工复核）；模型身份 `library=open_clip`、`weights=ViT-B-32.pt`，实际加载副本目录 `weight/clip/ViT-B-32.pt`（`encoder.weights` 为副本内绝对路径） |
| `MODEL_ROOT` 覆盖 | `$env:MODEL_ROOT=<空目录>` 后跑同一 CLI | 退出码 0（`allow_degraded: true`）；`status=degraded`，`degradation_reason=weights_not_found: ViT-B-32.pt`，`pending_model=4`，主清单 0 条（**未**回退本地副本） |
| 本地权重负控 | 临时改名副本 `weight/clip/ViT-B-32.pt` 后再跑同一 CLI | `status=degraded`、`degradation_reason=weights_not_found: ViT-B-32.pt`、`pending_model=4`，证明评分实际读取副本内权重；改名后已恢复，sha256 不变 |
| HTTP 冒烟 | `python tests/smoke_api.py` | **15/15 PASS**（历史实测，本次未重跑） |
| `map.json` | `python -m json.tool map.json` | 通过（`id=image_text_semantic_scoring`、`port=8112`、`formal_operator_numbers=[12]`） |
| 语法编译 | `python -m compileall -q .` | 通过（退出码 0） |
| CLIP 适配器连通性 | `pytest tests/test_clip_plumbing.py` | 通过：真实 `open_clip` ViT-B-32 架构前向（512 维、单位化、余弦区间、不可解码图片返回 `media_unreadable`）；**该测试只验证管线，不断言分数** |
| Docker | 未运行 | 本机未执行 `docker build`，未声称镜像验证通过 |

真实权重已随算子目录内置（`weight/clip/ViT-B-32.pt`），可按需复核真实权重集成检查：

```powershell
$env:OP12_CLIP_WEIGHTS="$PWD\weight\clip\ViT-B-32.pt"
python -m pytest tests/test_clip_plumbing.py -q
```

---

## 10. 已知限制

1. **权重已随算子目录内置**（`weight/clip/ViT-B-32.pt`），默认配置走真实 CLIP 前向并产出真实
   余弦分数；但 `dataset/sample` 只有 32×24 的合成小图，分数仅用于验证管线与模型身份，
   **不代表真实数据精度**。业务分档逻辑另有测试替身（`tests/conftest.py::StubEncoder`，明确标注
   test double）覆盖，同样不代表模型精度。
2. 真实模型吞吐/显存占用未实测；`model.batch_size` 需按部署机调优。
3. YOLOv8 辅助未实测（权重已内置 `weight/YOLOv8n/yolov8n.pt`，默认关闭、`detector.weights` 仍为
   `null`）；开启时若不可用只记录 `unavailable`，不改变分档。
4. `crossmodal-files-v1` 为 demo 约定格式，非甲方正式二进制协议；正式协议只需替换
   `utils/transport.py` 适配层，不改三接口字段。
5. 单次响应上限 64 MB（`transport.MAX_RESPONSE_BYTES`）；媒体较大的阶段需依赖产物目录传递。
6. 编码仅支持 UTF-8（带 BOM 可读）；其他编码按 `原始文件-编码异常` 进异常清单。
7. `processing["12"]` 与 `image_text_score` 为新增命名空间，不改动上游 `processing` 其他节点。

## 11. 目录结构

```
12_评分图文语义一致性/
  processor.py            CLI 入口（DEVELOPMENT_PLAN 约定）
  inference.py            运行编排、降级判定、产物与统计
  app.py                  三接口 /uploadFile /predict /removeFile
  configs/config.yaml     配置
  utils/                  backends(CLIP/YOLO)、scoring(业务核心)、io、lineage(snapshot 工具仍用，血缘构建 v4.2 停用存档)、transport、schemas、logger
  dataset/DATA_SCHEMA.md  输入输出字段契约
  dataset/sample/         make_sample.py + pairs.jsonl + media/*.png + manifest.json
  weight/                 内置权重（clip/ViT-B-32.pt、YOLOv8n/yolov8n.pt）+ README.md
  tests/                  conftest/test_scoring/test_operator/test_api/test_transport/
                          test_backends/test_clip_plumbing/smoke_api
  packaging/              prepare_package.py + 多算子镜像 Dockerfile
  map.json requirements.txt requirements-model.txt Dockerfile
  DEVELOPMENT_STATUS.md   开发状态与真实验证记录
  output/                 运行产物根目录（空）
```
