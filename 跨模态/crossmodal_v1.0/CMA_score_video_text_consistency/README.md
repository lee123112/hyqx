# 算子 14 评分视文语义一致性（表1 #14）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

S3 数据加工阶段的**中间节点**：复用图文评分的 **CLIP-ViT/B-32**，对视频**抽帧序列**
与文本描述逐帧计算余弦相似度，按语义段取均值并给出通过结论。

- 正式编号：**14**；算子标识：`video_text_scoring`；版本：`v1.0.0`；阶段：`S3`
- 服务端口：**8114**；入口：`app:app`
- 默认状态：**关闭**（`enabled: false`）

---

## 1. 功能边界

### 做什么

1. 解析「视频 + 文本」配对（兼容 #11 `11_candidate_pairs.jsonl` 的 `left`/`right` 形态
   与单记录扁平形态），取出**抽帧序列**与**文本描述**；
2. 按语义段组织帧：有 `video.segments[]` 时逐段处理，否则整段视频为一段；
3. **整段视频合计最多取 8 帧**（`max_frames_per_segment`，范围 1–8，均匀保序抽样以覆盖
   整段而非只取开头）。**2026-10-23 口径修正**：该上限是整段视频的帧预算，不再按段各 8 帧累计；
   多语义段时把预算分配到各段（总帧数 ≤ 上限）：
   段数 ≤ 上限时每段至少 1 帧、其余按各段候选帧数占比分配；
   段数 > 上限时在各段间均匀覆盖，未被覆盖的段 0 帧（段分记**无证据**、不因该段隔离，
   证据里显式注明）；
4. 复用 CLIP-ViT/B-32 逐帧计算余弦相似度：**段分 = 该段被选中帧的均值**；
   **整段视频分 = 所有被选中帧的均值**（不再是「段均值的均值」）；
5. 长视频**按段分别判定**：**任一段分低于阈值（配置默认 0.20）→ 整段不通过**；
   0 帧段不参与判定，但记录 `no_evidence_reason` 与 `segments_without_frames`；
6. 产出视文分数、段分、最低段分、通过结论与模型版本，以及帧预算证据
   `frames_used`（恒 ≤ `max_frames_per_segment`），交 **#19** 与 **#16**；
   不通过样本进**样本隔离库**并交 **#21**。

### 不做什么

- **不**解码视频、**不**新增视频模型：只消费 #6 抽取的**关键帧图像**（容器无需 ffmpeg）；
- **不**联网下载权重；**不**产出固定分、随机分或「模型不可用但仍标 model」的假降级；
- **不**提供 `/downloadFile`；**不**改变阶段边界（阶段末由工程侧用 `isLastOperator=true` 决定）；
- **不**在关闭路线时生成任何视文产物（不加载模型、不写评分文件、不搬运抽帧、不隔离）。

### 关闭路线（表1）

`enabled: false` 时：样本**原样透传**到 `14_passthrough.jsonl`，节点
`processing["14"].status = 1`、`process_status = "skipped"`、`video_text_score = null`，
**不拦、不隔离、留产线中**。

> 跨算子约束：图文评分关闭时 #13/#14 至少一条开启。该约束由**任务创建侧**拦截，
> 本算子只负责自己这一项开关，不在运行期读取兄弟算子配置。

---

## 2. 输入 / 输出字段

### 输入（每条记录，认两种形态）

**A. 配对形态（#11 产物）**

```json
{
  "pair_id": "pair-001",
  "left":  {"sample_id": "pair-001-video", "record": {"sample_id": "...", "video": {...}}},
  "right": {"sample_id": "pair-001-text",  "record": {"sample_id": "...", "text": {"content": "..."}}}
}
```

**B. 扁平形态**：同一记录内同时含 `video` 与 `text`，标识用 `sample_id`。

`video` 为对象时字段：`path`（视频引用，仅记录不解码）、`duration`、`fps`、
`frames[]`、`segments[]`；也接受纯字符串路径（此时无抽帧 → 待处理）。

| 字段 | 说明 |
| --- | --- |
| `video.frames[].path` | 抽帧图像路径，**相对素材包根**，禁绝对路径/`..`/反斜杠/URL |
| `video.frames[].timestamp` | 秒；缺省则整段共享全部候选帧 |
| `video.segments[].start` / `.end` | 语义段时间区间，取帧规则 `[start, end)` |
| `video.segments[].text` \| `.description` \| `.transcript` \| `.caption` | 段级文本（优先于记录级描述） |
| `video.segments[].frames[]` | 段自带帧（可选，优先于整视频抽帧） |
| `text.content` \| `.description` \| `.caption` | 记录级描述；回退 `metadata.*`、上游 `processing.<n>.description` |

### 输出（`OUTPUT_DIR/<uniqueId>/runs/<runId>/`）

| 文件 | 何时产生 | 角色 |
| --- | --- | --- |
| `14_passthrough.jsonl` | 路线**关闭**时的入口 | 原样透传 + `skipped` 节点 |
| `14_video_text_scores.jsonl` | 路线**开启**时的入口 | 通过 + 待处理 + 时序不适用透传记录（均带完整视文节点） |
| `14_rejects.jsonl` | 始终 | **原始隔离区**：坏行、缺字段、重复标识、编码异常、抽帧缺失/损坏/路径非法 |
| `14_sample_quarantine.jsonl` | 始终 | **样本隔离库**（交 #21）：任一段分低于阈值 |
| `14_not_applicable.jsonl` | 始终 | **时序路线不适用台账**：记录带 `timeline`/`temporal_bindings`/`temporal_anchors` 时#14 不评分、不隔离；完整记录同时保留在主入口给 #15 |
| `14_pending.jsonl` | 始终 | **待处理台账**：无视频/无抽帧/缺描述/模型不可用/评分异常 |
| `manifest.json` | 始终 | `crossmodal-manifest-v1` 运行清单 |
| `frames/*.png` | 仅**开启**路线 | 被本次评分引用的抽帧（同名不同内容追加摘要后缀） |
| `14_run.log` | 始终 | 逐行 `[时间] [动作] 真实数量` 日志 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py`、`utils/mty_compliance.py` 的血缘/归档接口，代码保留存档；`utils/transport.py` 仅保留 `crossmodal-files-v1` 文件对象与 `manifest.json` 打包能力）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 主输出的视文节点（`processing["14"]` 与顶层 `video_text_score`）

```json
{
  "status": 1,
  "process_status": "passed",
  "route_enabled": true,
  "passed": true,
  "reason": "passed",
  "label": "样本-评分：通过",
  "action": "pass",
  "destination": "#19",
  "video_text_score": 0.4123,
  "segment_mean": 0.4123,
  "min_segment_score": 0.3102,
  "low_score_threshold": 0.20,
  "segment_count": 2,
  "total_segments": 2,
  "scored_frames": 8,
  "frames_used": 8,
  "max_frames_per_segment": 8,
  "frames_within_budget": true,
  "segments_without_frames": [],
  "frame_budget_note": "整段视频合计最多 8 帧；未分配到帧的段记无证据、不参与隔离判定",
  "clip_model": "CLIP-ViT/B-32",
  "clip_model_version": "open_clip_model.safetensors",
  "model": {"name": "CLIP-ViT/B-32", "library": "open_clip", "device": "cpu",
            "weights_sha256": "...", "score_semantics": "cosine", "degraded": false},
  "segments": [
    {"index": 0, "start": 0.0, "end": 2.0, "frame_quota": 4, "frame_count": 4,
     "scored_frames": 4, "segment_mean": 0.5144, "passed": true,
     "has_evidence": true, "no_evidence_reason": null,
     "text_source": "video.segments[0].text",
     "frames": [{"path": "pair-002_0001.png", "score": 0.501, "timestamp": 0.0}]}
  ],
  "video_path": "video/pair-002.mp4",
  "description_source": "text.content",
  "degraded": false,
  "scored_at": "2026-10-06T17:34:18+08:00"
}
```

为便于下游直接取值，主输出另含标量字段 `video_text_score_value`。

**时序路线不适用节点**（记录带时间轴结构时）：

```json
{
  "status": 1, "process_status": "not_applicable", "route_enabled": true,
  "passed": null, "reason": "temporal_route_not_applicable",
  "label": "流程-时序路线不适用视文评分", "action": "passthrough",
  "destination": "产线待处理", "route": "temporal",
  "video_text_score": null, "segment_mean": null, "scored_at": null,
  "temporal_route_evidence": {
    "declared_by": ["timeline", "temporal_bindings"],
    "timeline_tracks": 3, "timeline_items": 5,
    "bindings": 4, "anchors": 2
  }
}
```

不写 `video_text_score`，避免下游把「未评分」当成真实分数；#16 读到该节点后按
`processing["15"]` 的时间轴结论分档。

---

## 3. 配置（`configs/config.yaml`）

| 键 | 默认 | 定义域 | 说明 |
| --- | --- | --- | --- |
| `operator.name` / `.version` | `video_text_scoring` / `v1.0.0` | 固定 | 与 `map.json` 一致 |
| `stage_id` | `S3` | 固定 | 表1 第 3 环节 |
| `enabled` | `false` | 严格布尔 | 表1 默认关闭 |
| `low_score_threshold` | `0.20` | `0.20–0.40` | **复用图文评分低分阈值**；任一段分低于即不通过 |
| `max_frames_per_segment` | `8` | `1–8` | **整段视频合计**最多帧数（多段共享该预算，不再按段累计） |
| `max_text_chars` | `500` | `1–5000` | 送模型文本截断上限（节点记录 `text_truncated`） |
| `temporal_route_exclusion` | `true` | 严格布尔 | 带时间轴结构的记录判 `not_applicable`（不评分不隔离，写 `14_not_applicable.jsonl`），结论交 #15 |
| `allow_degraded` | `false` | 严格布尔 | `false` 显式失败；`true` 显式降级（分数为 `null`） |
| `clip.library` | `auto` | `auto/open_clip/clip/transformers/none` | `none` = 显式禁用模型 |
| `clip.model_name` | `ViT-B-32` | 仅 CLIP-ViT/B-32 别名 | 表1 固定模型族 |
| `clip.weights` | `clip/ViT-B-32.pt` | 本地路径/目录 | 相对路径默认按 `<算子目录>/weight` 解析（内置权重）；**绝不联网下载**；未部署则按 `allow_degraded` 处理 |
| `clip.device` | `cpu` | `cpu/cuda/auto` | |
| `clip.batch_size` | `8` | `1–256` | 段内帧批量前向上限 |

`/predict` 的 `config` 仅允许覆盖上表中的**业务项**（`enabled`、`low_score_threshold`、
`max_frames_per_segment`、`allow_degraded`、`max_text_chars`、`temporal_route_exclusion`、`clip.*`）；
未知键、`"yes"` 之类非严格布尔、`max_frames_per_segment=9`、`low_score_threshold=0.9`
一律拒绝（`ConfigError`）。

2026-11-09 联调修复补齐 `temporal_route_exclusion` 的请求覆盖白名单与配置快照接线；
保持时序不适用路线和当前配置的 `0.20` 隔离线。TorchScript、torch<2.4 和 Unicode
权重路径加载沿用 11.2 版实现。

时序不适用记录同时写主入口和不适用台账，保留上游时间轴、转写与媒体引用，供 #15 直接继续
处理；`passed=null`、分数为空。`statistics.output_records` 来自主入口实际写入行数，并校验它
等于通过、待处理、关闭透传和不适用四桶之和，避免仅统计分桶守恒却在串行链路丢记录。

---

## 4. 算法 / 模型后端

- **模型**：CLIP-ViT/B-32（表1 固定），文本塔与图像塔输出均做 L2 归一化，
  分数语义统一为**余弦相似度**（`clip_score_semantics: "cosine"`）。
- **帧预算分配（`max_frames_per_segment` = 整段视频合计上限）**：设 `n` 段、
  `k = clamp(limit, 1, 8)`、`m_i` 为第 i 段候选帧数（按 `[start, end)` 过滤、空则回退全部）：
  - `n ≤ k`：每个有候选帧的段**保底 1 帧**，剩余 `k - n` 帧按各段 `m_i - 1` 的占比用
    最大余数法分配（配额不超过 `m_i`；候选帧总量不足时用完全部候选帧）；
  - `n > k`：在有候选帧的段之间按段序**均匀抽取 k 段、每段 1 帧**，未被抽中的段 0 帧；
  - 段内仍按「区间过滤 → 空回退 → 保序均匀采样」取帧，因此在单语义段（或 `n ≤ k`）时
    与旧口径（≤ 上限帧、保序均匀）完全等价；
  - 节点输出 `frames_used`（**恒 ≤ k**）、`frames_within_budget`、每段 `frame_quota` /
    `frame_count` / `has_evidence` / `no_evidence_reason`，并在 `frame_budget_note`
    中说明 0 帧段「记无证据、不参与隔离判定」的口径。
- **评分聚合**：段分 = 段内被选中帧余弦均值；`video_text_score` = **全部被选中帧**余弦均值；
  `segment_mean` 是该值的向后兼容别名；`min_segment_score` = 最低有证据段分。
- **后端顺序**：`auto` → 依次尝试 `open_clip` → `transformers` → `clip`（OpenAI）包；
  任一成功即用。
- **长字幕安全截断**：OpenAI `clip` 后端调用 `clip.tokenize(..., truncate=True)`，>77 token
  的长字幕截断到 context length（旧实现漏传 `truncate` 会抛 `RuntimeError` 并被记成
  `pending(score_error)`）；短文本 token 序列与旧行为完全一致。
- **权重解析**：`clip.weights` 指向文件或目录；目录时按
  `open_clip_model.safetensors` → `open_clip_pytorch_model.bin` → `model.safetensors`
  → `pytorch_model.bin` → `open_clip_model.pt` 顺序查找。
- **默认权重根**：未设 `MODEL_ROOT` 时取**本算子目录** `weight/`
  （`<算子目录>/weight/clip/ViT-B-32.pt`，随目录交付，见 `weight/README.md`）；
  `$MODEL_ROOT` 只作覆盖，指向的目录缺少权重时按 `weights_not_found` 显式失败/降级，
  **不会**回退到本地副本。单算子目录原样拷到包外也能独立加载。
- `shared_clip.runtime` 为**可选**依赖：单算子独立部署时包根没有 `shared_clip/`，导入失败
  静默走本地权重；仅当设置了 `MULTIMODAL_CLIP_SESSION` 且共享实例存在时复用远程实例
  （与改造前行为一致）。
- **实例复用**：
  - 进程内按签名（`library|arch|weights|device|batch_size`）缓存，**一次加载多次复用**；
  - 允许编排侧通过 `app.set_encoder_provider(...)` 或
    `utils.backends.register_encoder(signature, encoder)` **登记图文评分已加载的实例**，
    真正做到「复用图文 CLIP」；
  - 兄弟算子目录之间不得互相 import（各算子自带实现、独立打包），
    因此若既未登记且签名不同，则各进程各持一份实例——这是本算子的**已知限制**。

---

## 5. 降级与失败行为（绝不伪造分数）

| 情形 | `allow_degraded: false`（默认） | `allow_degraded: true` |
| --- | --- | --- |
| 未配置 `clip.weights` | 任务 **failed**（`ModelUnavailable`） | **degraded**，待处理台账，分数 `null`，不隔离 |
| 权重路径不存在 | 任务 **failed** | 同上 |
| `open_clip` 等库缺失 / 加载失败 | 任务 **failed** | 同上 |
| `clip.library: none` | 任务 **failed** | 同上 |
| 单条记录编码异常 | 编码失败行进原始隔离区；若有效记录为 0 则任务 **failed** | 同左 |

`video_text_score`、`segment_mean`、`min_segment_score`、`model` 在降级路径下**全部为 `null`**，
`statistics.degraded = true`、`model_unavailable > 0`，样本**留在产线**。

---

## 6. 启动与调用示例

### 启动服务

```bash
# 本地
uvicorn app:app --host 0.0.0.0 --port 8114

# 容器：默认使用算子目录内 weight/clip/ViT-B-32.pt；
# 若镜像未打包权重，可把外部模型库只读挂载并用 MODEL_ROOT 覆盖（仅覆盖默认根）
docker build -t op14-video-text:v1.0.0 .
docker run --rm -p 8114:8114 \
  -v /data/weight:/models:ro -e MODEL_ROOT=/models \
  -v /data/output:/workdir/output \
  op14-video-text:v1.0.0
```

环境变量：`OUTPUT_DIR`（默认 `<算子目录>/output`）、
`OPERATOR_CONFIG`（默认 `<算子目录>/configs/config.yaml`）。

### API 示例（三接口，无 `/downloadFile`）

```bash
# 1) 上传任务输入（zip 素材包：demo.jsonl + frames/*.png）
curl -X POST "http://127.0.0.1:8114/uploadFile?uniqueId=task_001" \
     -F "file=@bundle.zip"

# 2) 形态一（单文件流）：直接上传一个 JSONL/JSON，?detection 可省略或写英文 id
curl -X POST "http://127.0.0.1:8114/predict?detection=score_video_text_consistency" \
     -F "file=@demo.jsonl"
# → {"status":1,"uniqueId":"upload_...","pair_id":"pair-001",
#    "segment_scores":[{"segment_index":0,"score":0.5144,"frame_count":4,
#                       "start_ms":0,"end_ms":2000}],
#    "decision":"pass","model_version":"CLIP-ViT/B-32","process_status":"passed"}
# 形态一可附带白名单表单标量：enabled / low_score_threshold / max_frames_per_segment /
# max_text_chars / allow_degraded / temporal_route_exclusion（未知字段忽略）

# 3) 形态二（阶段文件传递）：阶段首 / 阶段内中间节点（不传 inputFile）
curl -X POST http://127.0.0.1:8114/predict \
     -H "Content-Type: application/json" \
     -d '{"uniqueId":"task_001","isLastOperator":false}'
# → data = {operator, status, outputFile(crossmodal-files-v1), result(业务摘要)}
#   isLastOperator 只标记阶段末，false/true 响应形态完全同构

# 阶段内后续算子：把上一运行目录内的入口文件作为 inputFile
curl -X POST http://127.0.0.1:8114/predict \
     -H "Content-Type: application/json" \
     -d '{"uniqueId":"task_001","isLastOperator":false,
          "inputFile":"runs/run001/14_video_text_scores.jsonl"}'

# 阶段末
curl -X POST http://127.0.0.1:8114/predict \
     -H "Content-Type: application/json" \
     -d '{"uniqueId":"task_001","isLastOperator":true}'
# → data 与中间调用同构：{operator, status, outputFile, result}

# 4) 清理
curl -X POST "http://127.0.0.1:8114/removeFile?uniqueId=task_001"
```

开启路线（示例：`MODEL_ROOT` 指向空目录时的显式降级；有内置权重时同一请求会真实评分）：

```bash
curl -X POST http://127.0.0.1:8114/predict \
  -H "Content-Type: application/json" \
  -d '{"uniqueId":"task_001","isLastOperator":false,
       "config":{"enabled":true,"allow_degraded":true}}'
```

HTTP 的相对 `inputFile` 从当前任务根解析：上传入口可用
`input/candidate_records.jsonl`，后续产物可用 `runs/run001/…`。
省略时自动解析上传入口；裸文件名不会自动补 `input/`。任务内绝对路径仍可用，
任务外路径和 `../` 逃逸仍拒绝。CLI/库入口允许外部文件时，相对路径继续按进程工作目录解析。

### CLI 示例（与 API 同一套实现）

```bash
# 默认关闭：原样透传，不加载模型
python processor.py --input dataset/sample/demo.jsonl \
    --unique-id smoke_cli --output-root output_smoke

# 阶段末调用（响应与中间调用同构）
python processor.py --input dataset/sample/demo.jsonl \
    --unique-id smoke_cli --output-root output_smoke --is-last-operator

# 启用评分（权重已随算子目录内置在 weight/clip/ViT-B-32.pt，默认自动加载）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_enabled \
    --output-root output_smoke --enabled
# 可选：指定其它本地权重（绝对路径原样使用；--allow-degraded 可在无权重时显式降级）
#   --clip-weights /models/clip/ViT-B-32.pt
```

> CLI 的 stdout 固定为 **UTF-8 JSON**；Windows GBK 控制台如需正常显示中文请先 `chcp 65001`。
> 日志走 stderr，且已放宽编码错误为替换，日志编码问题不会打断业务处理。

---

## 7. 输出示例（v4.2 统一响应）

形态二（`application/json`）成功响应：

```json
{"code":0,"msg":"视文评分完成：通过 3 条，隔离 1 条","data":{
  "operator":"video_text_scoring",
  "status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"14_video_text_scores.jsonl",
    "files":[{"path":"14_video_text_scores.jsonl","encoding":"utf-8","content":"…"},
             {"path":"14_rejects.jsonl","encoding":"utf-8","content":"…"}]},
  "result":{"uniqueId":"smoke_cli","runId":"run001",
    "entrypoint":"14_video_text_scores.jsonl","status":"success",
    "message":"视文评分完成：通过 3 条，隔离 1 条",
    "statistics":{"route_enabled":true,"input_records":7,"valid_records":7,
      "passed_records":3,"sample_isolated":1,"pending_records":3,
      "conservation_ok":true}}}}
```

`outputFile` 为阶段交付的文件内容对象（`format` / `entrypoint` / `files`；文本 UTF-8、媒体 base64），仅含包内相对路径；`result` 为业务摘要（`uniqueId`、`runId`、`entrypoint`、`status`、`message`、`statistics` 全量计数）。中间调用与阶段末调用返回同构，`isLastOperator` 只作阶段末标记。

> 注：原 `lineage.json` / 阶段血缘示例已随 v4.2 停用，相关文件不再产出；统计与配置快照改由 `data.result`、`manifest.json`、`run.log` 承载。

---

## 8. 资源需求

| 场景 | 需求 |
| --- | --- |
| 路线关闭（默认） | 无模型、无 GPU；内存 < 200 MB；纯 JSONL 流式处理 |
| 路线开启（CPU） | CLIP-ViT/B-32 权重约 350–600 MB（fp32），进程常驻内存约 1.5–3 GB |
| 路线开启（GPU） | 建议 4 GB+ 显存；`clip.device: cuda` |
| 吞吐 | **整段视频合计最多 8 帧**、按 `clip.batch_size` 批量前向；读为流式，**不把全文读入内存** |
| 容器 | `python:3.10-slim` + CPU 版 torch；**不需要 ffmpeg**（不解码视频） |

---

## 9. 测试与验证（真实结果）

```powershell
cd "G:\HYQX\算子\跨模态\demo_10.6\14_评分视文语义一致性"
python -m pytest tests/ -q
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
python -m json.tool map.json
python -m compileall -q .
python tests/smoke_api.py     # 真实 uvicorn + httpx 黑盒冒烟
```

实测结果（2026-10-06 基线；2026-10-31 权重内置后复核 standalone 与 pytest，Windows / CPython 3.9.7）：

| 命令 | 结果 |
| --- | --- |
| `python -m pytest tests/ -q` | **182 passed, 48 skipped**（48 skip = 1 项未设 `OP14_CLIP_WEIGHTS` 的可选真实权重集成检查 + 47 项 v4.2 血缘/运输输出停用既有 skip；无失败） |
| 长字幕回归测试 | `python -m pytest tests/test_clip_plumbing.py -q` | **12 passed, 1 skipped**（新增 2 项：`truncate=True` 只截长文本、短文本 token 不变；OpenAI clip 后端 >77 token 真实前向返回 512 维向量） |
| 长字幕 CLI 修复前后对比 | 同一条 `temporal_nasa-science-live-ep1(__pair)` 长字幕样本 | 修复前 `reason=score_error`、`video_text_score/segment_mean=null`；修复后 `reason=passed`、`video_text_score=segment_mean=0.338148`；对照短字幕 `msvd-0001` 前后均为 `0.233172`（完全一致） |
| 真实权重集成 | `$env:OP14_CLIP_WEIGHTS="<本算子目录>/weight/clip/ViT-B-32.pt"; python -m pytest tests/test_clip_plumbing.py -q` | **11 passed**（真实 ViT-B-32 权重加载 + 前向） |
| `python processor.py … --unique-id smoke_cli` | `rc=0`，`status=success`，入口 `14_passthrough.jsonl`，`skipped_records=7`，`conservation_ok=true` |
| 同上 `--is-last-operator` | `data = {outputFile(crossmodal-files-v1), result(业务摘要)}`（v4.2 统一响应，不再返回 `lineageFile`） |
| `--enabled`（权重内置，包外 standalone 副本，无包根 `weight/`/`shared_clip/`、未设 `MODEL_ROOT`） | `rc=0`，`status=success`，`scored_records=3`，`sample_isolated=3`，`segment_mean=0.202508 / 0.209372 / 0.197514`（非空真实余弦）；模型身份 `weights=<副本>/weight/clip/ViT-B-32.pt`（`library=clip`），`models_available=true` |
| `--enabled` + `MODEL_ROOT` 指向空目录 | `rc=2`，`status=failed`，`ModelUnavailable`，`weights_not_found：本地权重不存在（weights_not_found:ViT-B-32.pt）`，**未产出任何分数** |
| `--enabled --allow-degraded` + `MODEL_ROOT` 空目录 | `rc=0`，`status=degraded`，`pending_records=6`（其中 `model_unavailable=3`），`origin_quarantined=1`，`frames_carried=16`，全部分数为 `null`（**未**回退本地副本） |
| `--max-frames 9`（非法配置） | `rc=2`，`ConfigError` |
| `python -m json.tool map.json` | 退出码 0 |
| `python -m compileall -q .` | 退出码 0（无错误输出） |
| `python tests/smoke_api.py` | **40 通过 / 0 失败**（真实 HTTP：三接口信封、形态一单文件流、形态二阶段首/阶段内/阶段末调用（统一响应）、`crossmodal-files-v1` 恢复、两个 uniqueId 隔离、非法 uniqueId、非严格布尔、任务外 `inputFile`、zip 逃逸、`/downloadFile` 404、`removeFile` 幂等、显式失败与显式降级） |

Docker：**未运行**（开发机未确认可用的 docker daemon），因此本 README 不声明镜像验证通过；
`Dockerfile` 与 `.dockerignore` 由交付包根统一维护；本算子默认从算子目录 `weight/` 加载，
容器化时可用 `MODEL_ROOT` 指向只读挂载的外部模型库（仅覆盖默认根）。

模型/外部依赖：本机装有 `open_clip_torch 2.26.1` / `transformers 4.41.2` / `torch 2.3.1` /
`Pillow 9.4.0`；CLIP 权重已随算子目录内置（`weight/clip/ViT-B-32.pt`），包外 standalone 副本
已跑通真实前向并产出非空 `segment_mean`。样例为 32×24 合成帧，故真实视频的精度与吞吐仍未验证
（架构级管线另由 `tests/test_clip_plumbing.py` 验证：tokenize → encode_text →
encode_image → L2 → 余弦，输出 512 维单位向量，坏图返回 `media_unreadable`）。

---

## 10. 已知限制

1. **样例口径**：真实权重已随算子目录内置，并已在包外 standalone 副本跑通（`segment_mean` 非空）；
   但样例是 32×24 合成帧，端到端语义分只证明管线与模型身份，**不代表真实视频精度**，吞吐亦未实测。
   设置 `OP14_CLIP_WEIGHTS=<本算子>/weight/clip/ViT-B-32.pt` 可复核真实权重集成检查。
2. **跨算子实例复用**为进程内 / 编排登记式：兄弟算子目录不得互相 import，
   同进程部署时需由编排侧调用 `set_encoder_provider` 或 `register_encoder` 才能真正共享；
   否则各进程各持一份实例（各算子目录各自内置同一份权重，sha256 相同；`shared_clip` 共享
   实例存在时仍按原逻辑复用）。
3. **不解码视频**：只消费 #6 抽取的关键帧；若上游未抽帧，样本进入**待处理台账**
   （不隔离），需上游补齐后重跑。
4. **段级文本来源**：段自带 `text/description/transcript/caption` 优先，
   否则回退记录级描述；若长视频各段实际描述不同而只有一条记录级描述，
   各段将用同一描述评分（口径已记录在此，待主 agent 确认）。
5. **零向量兜底**：CLIP 文本/图像向量为零向量时余弦返回 `0.0`（可解释而非抛错），
   该样本必然低于阈值 → 进样本隔离库。
6. 抽帧格式限定为 PNG/JPG 等 PIL 可解码图像；损坏文件按**原始文件问题**隔离，不计入语义不通过。
7. **多段视频帧预算**：`max_frames_per_segment` 是整段视频合计上限（1–8），段数多于上限时
   按段均匀覆盖、部分段可能 0 帧（`segments_without_frames`）；0 帧段段分为 `null`、
   **不参与隔离判定**，下游需结合 `has_evidence`/`no_evidence_reason` 理解该段无证据。

---

## 11. 目录结构

```
14_评分视文语义一致性/
├── app.py                      # 三接口（/uploadFile、/predict、/removeFile）+ /health
├── inference.py                # 业务入口：配置、流式处理、统计守恒、统一响应
├── processor.py                # CLI 薄壳（与 HTTP 同一套实现）
├── configs/config.yaml         # 表1 冻结项 + 可覆盖业务项
├── utils/
│   ├── schemas.py              # Pydantic v2 严格校验
│   ├── frames.py               # 抽帧/分段纯逻辑（整段视频合计上限 + 段间预算分配）
│   ├── scoring.py              # 余弦、均值、帧分/段分 → 视频分 → 通过结论
│   ├── backends.py             # CLIP-ViT/B-32 适配器 + 实例复用
│   ├── video_text.py           # 表1 #14 业务模块（解析/判定/分流）
│   ├── io.py / logger.py / transport.py
│   └── lineage.py              # v4.2 停用存档（仅保留 now_iso 时间工具）
├── dataset/
│   ├── DATA_SCHEMA.md          # 与实际输出一致的字段契约
│   └── sample/                 # 可复现样例（make_sample.py + demo.jsonl + frames/）
├── weight/                     # 内置 CLIP 权重 clip/ViT-B-32.pt + README.md
├── tests/                      # 190 passed / 49 skipped（2026-11-05）
├── packaging/prepare_package.py
├── map.json / requirements.txt / requirements-model.txt / Dockerfile
├── output/                     # 运行时输出根（交付时为空）
└── DEVELOPMENT_STATUS.md       # 开发过程与真实验证记录
```
