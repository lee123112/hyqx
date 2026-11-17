# 算子 #16 标注整体对齐关系（`16_whole_alignment`）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

表1 v4.1 第 16 行：流程环节=数据加工，工具类型=小模型，优先级 P0，可跳过=否，默认开启不可关闭。
本算子负责**整体—整体**图文对齐结论：消费 #12 的真实 CLIP 分数、管理人工三按钮结论、
在人工改写描述后用同一 CLIP 实例重新评分，并输出结论台账、隔离台账与审计信息。
同时原生消费 #13 音文 CER、#14 视文 CLIP 与 #15 适用的时间轴结论。

## 1. 功能边界

做：

- 按阈值 `隔离线 / 0.32` 对 #12 真实分数分档（隔离线默认跟随上游标定值，缺省 0.20），产出 `alignment.whole` 结论与证据；
- 创建人工任务（中间分样本 + #24 抽检命中样本），接收人工「对齐 / 不对齐 / 存疑」三按钮回写；
- 人工「不对齐」后**锁定否决**，后续高分不得改回通过；
- 人工改写描述后用**同一 CLIP 实例**重新评分并按同一阈值重新分档（绝不沿用旧分数）；
- 不对齐样本进样本隔离台账；待人工留样本池；存疑交 #25；不适用样本留产线；
- 原始文件类问题（坏行、缺字段、重复编号、媒体缺失/损坏/路径非法）进独立异常台账；
- 完整统计守恒、规范日志、三接口。

不做（严格边界）：

- 不画框、不分词、不做局部对整体（#17）/局部对局部（#18）判断；
- 不替代 #12 的图文评分，也不重新实现 CLIP 训练/推理模型部署；
- 不实现 `/downloadFile`，不修改阶段边界，不由本算子决定 `isLastOperator`；
- 不产生任何固定分数、随机分数或规则伪造分数。

## 2. 目录结构

```
16_标注整体对齐关系/
├── app.py                     # FastAPI 三接口（/uploadFile、/predict、/removeFile）
├── inference.py               # 主编排、CLI、配置加载、后端构建、统计对外化
├── processor.py               # CLI 入口（转调 inference.main）
├── configs/config.yaml        # 阈值/模型/IO 配置（含定义域与禁改项）
├── utils/
│   ├── alignment.py           # 业务核心：分档决策、人工事件、重评分、守恒、台账
│   ├── backends.py            # CLIP-ViT/B-32 后端与显式降级表态
│   ├── io.py                  # 流式 JSONL、原子写、uniqueId 隔离、任务锁、媒体携带
│   ├── lineage.py             # v4.2 停用存档（血缘/阶段拆分不再调用，仅保留快照工具）
│   ├── transport.py           # crossmodal-files-v1 文件对象打包与恢复
│   ├── schemas.py             # /predict 请求字段严格校验
│   └── logger.py              # 规范日志
├── dataset/
│   ├── DATA_SCHEMA.md         # 输入输出字段契约
│   └── sample/                # 样例：demo.jsonl + 人工事件 + 真实 PNG（make_sample.py 幂等重建）
├── tests/                     # pytest 用例 + smoke_api.py 三接口冒烟
├── packaging/prepare_package.py
├── map.json  requirements.txt  requirements-model.txt  Dockerfile  README.md  OPERATOR_MANUAL.md
└── output/                    # 运行输出根目录（保持为空）
```

## 3. 输入输出

字段级契约见 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)。

输入：`#11` 候选对（含 `#12` 的 `image_text_score` 与 `#14` 的 `video_text_score` /
`processing.14`）、`#24` 抽检命中标记、人工回写事件（`16_human_events.jsonl`，随素材包上传）。

输出（`OUTPUT_DIR/<uniqueId>/16_whole_alignment/runNNN/`）：

| 文件 | 用途 |
|---|---|
| `16_whole_alignment.jsonl` | 主清单：`auto_aligned`/`manual_aligned`/`pending_manual_review`/`uncertain` |
| `16_review.jsonl` | 人工队列台账 |
| `16_isolation.jsonl` | 样本隔离台账（不对齐、人工否决） |
| `16_not_applicable.jsonl` | 不适用台账（无图文组合），留产线 |
| `16_rejects.jsonl` | 原始文件类异常台账 |
| `16_audit.jsonl` | 人工事件审计台账（生效/非法/覆盖/未匹配） |
| `manifest.json`、`config.snapshot.json`、`run.log`、`media/*` | 清单/配置快照/日志/携带媒体 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py`、`utils/mty_compliance.py` 的血缘/归档接口，代码保留存档；`utils/transport.py` 仅保留 `crossmodal-files-v1` 文件对象与 `manifest.json` 打包能力）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

结论 → 去向：`通过`→样本池(#19)；`待人工`→人工队列；`存疑`→#25；`不对齐`→#21；
不适用→留产线。质量标签/动作/去向均写在 `label`/`action`/`destination` 上。

### 3.1 视文路线（video + text）

`#16` 原生支持 `video` + `text` 整体对齐，测试台不再需要把 #6 关键帧塞进 `image` 代理：

- **路线判定**：一侧 `video`（`record.video` 有 `path`/`frames`，或 `side.modalities` 含
  `"video"`）+ 另一侧 `text` → 视文路线；`image+text` 仍**最高优先**
  （`video+image+text` 走图文）；还支持音频与文本，均不成立才进不适用台账。
- **取分顺序**：`pair.video_text_score`（#14 节点，dict 或裸标量）→ `pair.processing["14"]`
  → 视频侧 `record.video_text_score` → `record.processing["14"]`。节点内按
  `segment_mean` → `clip_score` → `score` → `video_text_score` 取值，并读取
  `model` / `model_version` / `tier` / `status`。**图像分永远最高优先**：只要存在 #12
  真实分数，行为与只支持图文时完全一致。
- **分档**：与图文完全相同（跟随 #14 的 `low_score_threshold` / 0.32）；`score_source` 写 `video_text_score` 或
  `processing.14`，`evidence.upstream.origin="14"`。**无分数时 `clip_score=null`、
  `score_source="none"`，走待人工，不伪造任何分数。**
- **媒体**：常规视文分档**不读**视频/帧文件、不需要本地 CLIP；只有人工改写描述需要
  rescore 时，取 `video.segments[].frames`（否则 `video.frames`）按顺序展开的**中间帧**
  作为 `image_target`，用同一 CLIP 实例重算。无帧/帧不可读 → `pending_manual_review`，
  `description_edit.process_status="no_representative_frame"`（或真实媒体错误码），
  `score_after=null`，任务 `degraded=true`；不因缺帧拒绝整条记录。
- **输出**：`alignment.whole` / `processing.16` / 统计/台账/守恒与图文**同构**（协议字段
  diff 0）；输入里的 `video_text_score`、`processing["14"]` 原样保留，供 #19 的
  `route.14` / `whole` 门读取。

### 3.2 音文、路线状态与媒体携带（2026-11-09 联调修复）

- 音文直接读取配对根级或左右 `record.processing["13"]`（兼容 `audio_text`）。要求
  `route_enabled=true`、`passed=true`、未降级、有限非负 CER 与实际
  `edit_distance_ratio_max`；满足该阈值才自动对齐，真实失败仍隔离，缺证据待人工。
- 优先读取显式 `final_cer` 等最终指标。当前 #13 的 `cer` / `edit_distance_ratio` 是
  同音容错前的字符指标，因此按其实际实现，在 `homophone_tolerance=true` 且拼音表可用时，
  消费它与 `edit_distance_ratio_homophone` 的较小值；字段来源记录在 `score_field`。
  CER 写 `score_metric="CER"`、`score_value`、`score_direction="lower_is_better"`、
  `score_threshold`，`clip_score` 保持 `null`，不把诊断余弦当生产指标。
- 保留 #15 独立路线优先级：适用的 `passed` / `failed` / `pending_calibration` 仍先于新增音文
  分支；`not_applicable/no_timestamp_timeline` 不再遮住真实 #13 证据。
  #14 的时序 `not_applicable` 节点也不会遮住 #15。
  #15 若显式 `passed=false` 优先沿用失败，不能由 #13 救回；显式降级/待定转人工，
  禁用只作 `skipped`（可回退独立有效的 #13）。旧 #15 仅有 `process_status` 的合同仍支持。
- #12/#14 的显式禁用、待定、降级不会自动放行；显式失败沿用隔离。旧 #12 不要求新增
  `passed` 字段。#14 评分通过仍需达到 #16 自动线：例如 `0.303645 < 0.32` 保持待人工。
  跟随上游隔离线与默认 `0.20 / 0.32` 保留，上游较低自动线不能降低当前自动门槛。
- 音文改描述必须重新取得 #13 真实评分，旧 CER 不确认新描述。视频改描述保留原有中间帧真实
  CLIP 重分算法，代表帧仅写 `video_frame_reference`，不写成 `image_reference`。
- `image_reference.path` 可直接读取；保留左右嵌套与根级命名空间、真实人工否决锁定。
  输出携带实际音频、视频及所有关键帧；`frames` 可为整数计数，路径从数组 `frames` /
  `keyframes` 读取。常规分档仍不解码视频、不加载重模型。

## 4. 决策算法（表1 冻结顺序）

1. 人工否决锁定（`veto_locked` 或本次事件=不对齐）→ `not_aligned` + `human_veto_locked`；
2. 本次事件=「对齐」→ `manual_aligned`；=「存疑」→ `uncertain`（交 #25）；
3. 人工改描述但重评分不可用 → `pending_manual_review`（`degraded`，**不写分数**）；
4. 无分数但有 #15 结论（`processing["15"]`，`whole_alignment.temporal_route=true`）：按 #15 分档——
   `passed` → `auto_aligned`（`confirmation_method="temporal_15"`）、`failed` → `not_aligned`、
   `pending_calibration`/`skipped`/其余 → `pending_manual_review`；
5. 无分数且无 #15 结论：上游 `tier=isolated` → `not_aligned`；否则 → `pending_manual_review`（reason `no_score`）；
6. `score < 隔离线` → `not_aligned`（进隔离，不入人工队列）；
7. 抽检命中 → `pending_manual_review`（即使高分）；
8. `隔离线 ≤ score < 自动线` → `pending_manual_review`；
9. `score ≥ 自动线` → `auto_aligned`。

> **隔离线/自动线的来源**（`whole_alignment.follow_upstream_thresholds=true`，默认）：
> 上游节点自带标定线时跟随上游（#12 `thresholds.isolation/auto_align`、#14
> `low_score_threshold`），`threshold_source="upstream"`；上游没声明（人工重评分等）才用
> 本算子 `isolation_threshold`/`auto_align_threshold`，`threshold_source="config"`。

视文路线（`video+text`）复用同一顺序，只是 `score` 来自 #14 的 `video_text_score` /
`processing.14`（见 §3.1）；`image+text` 存在时仍先走图文分支。

人工改描述时：`perform_rescore` 调用注入的同一实例 `encode_texts`/`encode_images`，
用 `backends.cosine_similarity` 取真实余弦；`score_before` 记旧分、`score_after` 记新分，
`score_source` 变为 `rescore`。重评分失败则 `rescored=false`，并保留原 `score_source`（不冒充 rescore）。

## 5. 配置（`configs/config.yaml`）

| 项 | 默认 | 说明 |
|---|---|---|
| `whole_alignment.enabled` | `true` | 必选工具，置 `false` 直接 `ConfigError` |
| `whole_alignment.isolation_threshold` | `0.20` | 定义域 `[0.20, 0.40]`，含等号进人工；仅在上游未声明标定线时生效 |
| `whole_alignment.auto_align_threshold` | `0.32` | 定义域 `[0.25, 0.50]`，必须 > 不对齐阈值 |
| `whole_alignment.rescore_modified_description` | `true` | 置 `false` 直接 `ConfigError` |
| `whole_alignment.follow_upstream_thresholds` | `true` | 跟随 #12/#14 节点自带标定线；`false` 时固定用本算子 0.20/0.32（回归对比用） |
| `whole_alignment.temporal_route` | `true` | 无 CLIP 分时按 #15（`processing["15"]`）分档；`false` 时一律 `no_score/pending`（复现旧行为） |
| `model.library` | `auto` | `auto/open_clip/clip/transformers/none` |
| `model.weights` | `clip/ViT-B-32.pt` | 本地权重路径（默认加载本算子 `weight/`）；`null` 时仅影响「人工改描述重评分」 |
| `model.device` | `auto` | `auto/cpu/cuda`（`cuda` 不可用回退 `cpu`） |
| `model.allow_degraded` | `true` | `true`=重评分延后为待人工；`false`=任务失败 |

请求级 `config` 只能覆盖 `isolation_threshold` / `auto_align_threshold`；
`weights`、`device`、`library`、`enabled`、输出根目录等**拒绝被请求改写**（未知键直接报错）。

## 6. 模型后端与降级行为

- 正式模型：**CLIP-ViT/B-32**（`open_clip` / `clip` / `transformers` 均支持），只加载**本地**权重；
- 同签名（library+model_name+weights+device+batch_size）复用同一实例；编排侧可用
  `backends.register_encoder(signature, encoder)` 注入 #12 已加载实例；
- **默认加载本算子 `weight/clip/ViT-B-32.pt`**（`$MODEL_ROOT` 覆盖优先，见 `weight/README.md`）；
  权重缺失（文件不存在或 `weights` 置空）时 `build_clip_backend(strict=False)` 返回
  `(None, reason, None)`，`reason` 例如 `model_weights_not_configured` /
  `backend_disabled_by_config` / `weights_not_found: *`；
- 此时常规分档仍按 #12 真实分数完成，只有「人工改描述」的记录被显式延后为
  `pending_manual_review`，任务状态 `degraded`、`degradation_reason` 如实上报；
- `allow_degraded: false` 时改为任务失败（`reason = rescore_requires_model: ...`）；
- 任何情况下**不产生分数字段**：无上游分数时 `clip_score = null`，重评分失败时 `score_after = null`。

## 7. 启动与调用

### 7.1 安装与启动

```powershell
pip install -r requirements.txt
# 可选（仅"人工改描述重评分"需要）：pip install -r requirements-model.txt
python dataset/sample/make_sample.py          # 幂等重建样例与真实 PNG
uvicorn app:app --host 0.0.0.0 --port 8116
# 或 python app.py（读环境变量 PORT，默认 8116）
```

环境变量：`OUTPUT_DIR`（输出根，默认 `<算子目录>/output`）、`PORT`（默认 8116）。

### 7.2 CLI

```powershell
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke --is-last-operator
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke --auto-align-threshold 0.40 --isolation-threshold 0.30
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke --human-events dataset/sample/16_human_events.jsonl
```

### 7.3 HTTP 三接口

只提供三个接口，**没有** `/downloadFile`。外层统一 `{"code": 0|1, "msg": "...", "data": {...}}`。

```powershell
# 1) 上传素材包（ZIP 内含 demo.jsonl + 16_human_events.jsonl + media/，或直接上传单个 JSONL）
curl -X POST "http://127.0.0.1:8116/uploadFile?uniqueId=task_01-file_01" -F "file=@bundle.zip"
# → data.inputFile = OUTPUT_DIR/task_01-file_01/input/demo.jsonl

# 2) 形态一（单文件流）：直接上传一个 JSONL/JSON，?detection 可省略或写英文 id
curl -X POST "http://127.0.0.1:8116/predict?detection=annotate_whole_alignment" -F "file=@demo.jsonl"
# → {"status":1,"uniqueId":"upload_...","process_status":"auto_aligned",
#    "pair_id":"pair-t01--i01","alignment_label":"aligned",
#    "decision_source":"automatic","score":0.41,"review_id":null}

# 3) 形态二（阶段文件传递）：阶段首算子，不传 inputFile；false/true 响应同构
curl -X POST http://127.0.0.1:8116/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_01-file_01","isLastOperator":false}'
# → data = {operator, status, outputFile(crossmodal-files-v1), result(业务摘要)}

# 阶段内后续算子：把上一运行目录内的入口文件作为 inputFile
curl -X POST http://127.0.0.1:8116/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_01-file_01","isLastOperator":false,"inputFile":".../16_whole_alignment/runNNN/16_whole_alignment.jsonl"}'

# 阶段末：isLastOperator=true（仅标记阶段末）
curl -X POST http://127.0.0.1:8116/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_01-file_01","isLastOperator":true,"config":{"auto_align_threshold":0.35}}'

# 4) 清理本任务临时目录
curl -X POST "http://127.0.0.1:8116/removeFile?uniqueId=task_01-file_01"
```

返回形态约束：

- **形态一**：`status`（int，处理型 1 成功/0 失败）+ `process_status`（整体结论 `auto_aligned`/`manual_aligned`/`not_aligned`/`uncertain`/`pending_manual_review`）+ `alignment_label`（`aligned`/`not_aligned`/`uncertain`）+ `decision_source`/`score`/`review_id`；
- **形态二**：`data = {operator, status, outputFile(crossmodal-files-v1 文件内容对象), result{uniqueId, runId, statistics}}`；`isLastOperator` 仅作阶段末标记，中间与阶段末响应同构，不再返回 `lineageFile`。

形态一可附带白名单表单标量：`isolation_threshold`、`auto_align_threshold`（未知字段忽略）。

## 8. 输出示例（v4.2 统一响应）

```json
{"code":0,"msg":"success","data":{
  "operator":"16_whole_alignment",
  "status":"degraded",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"16_whole_alignment.jsonl",
    "files":[{"path":"16_whole_alignment.jsonl","encoding":"utf-8","content":"…"},
             {"path":"16_review.jsonl","encoding":"utf-8","content":"…"},
             {"path":"manifest.json","encoding":"utf-8","content":"…"}]},
  "result":{"uniqueId":"smoke_cli","runId":"run001",
    "statistics":{"input":19,"output":10,"review":7,"isolated":5,
                  "rejected":3,"audit":7,"degraded":true,
                  "conservation_ok":true}}}}
```

`outputFile` 为阶段交付的 `crossmodal-files-v1` 文件内容对象（仅包内相对路径，文本 UTF-8、媒体 base64）；`result.statistics` 为真实计数摘要。`config.snapshot.json` 保存生效配置；`manifest.json` 记录入口、产物计数、`media.carried/missing` 与真实统计。

> 注：原 `lineage.json` / `stage_lineage.json` 示例已随 v4.2 停用，相关文件不再产出；统计与配置快照改由 `data.result`、`manifest.json`、`run.log` 承载。

## 9. 资源需求

| 场景 | CPU | 内存 | 磁盘 | GPU |
|---|---|---|---|---|
| 常规分档（消费 #12 图文 / #14 视文分数，含人工回写） | 1 核即可 | < 300 MB | 与输入同量级（额外 ≈ 隔离+队列+审计副本） | 不需要 |
| 人工改描述重评分 | 2–4 核 | ≈ 2–3 GB（ViT-B/32 + torch） | 权重 ≈ 600 MB | 可选（`device: cuda`） |

容器无 GPU 时 `device` 回退 CPU；权重缺失时功能完整，只是「人工改描述重评分」延后为待人工。
视文常规分档同样**不读视频/帧文件**、不加载本地 CLIP；只有视文路线的人工改描述重评分
才会读取代表帧。

## 10. 已知限制

1. **权重缺失**（`model.weights: null`、`$MODEL_ROOT` 覆盖目录无文件等）→ 样例中的「人工改描述」
   记录被延后为 `pending_manual_review`，任务状态 `degraded`；真实权重下的重评分数值已在
   正式版内置 `weight/clip/ViT-B-32.pt` 下复测；不含权重场景用确定性测试替身验证调用链与
   分档（`tests/test_alignment.py` 中 `StubEncoder`）。
2. `weights=None` 只构建架构（`untrained_architecture_only`），仅用于适配器连通性测试；
   公共入口默认配置不会走这条路，但代码保留了该能力。
3. 人工队列的**下游派单**（具体标注平台任务创建）由工程侧/#25 负责，本算子只产出队列台账
   与 `review_task_id` 约定（`16-<uniqueId>-<pair_id>`）。
4. `manifest.json` 在运行结束时写两次（先写计数、后补产物清单与统计），最终内容为完整版本；
   不依赖中间态。
5. 显式 `runId` 允许覆盖同名运行目录（幂等重跑）；同名覆盖时只保留最后一次运行目录内的产物
   （`manifest.json`、`config.snapshot.json` 等）；v4.2 起血缘输出停用，任务级不再维护
   `lineage.jsonl` 历史（不再产出该文件）。
6. 媒体携带按「本次记录真实引用」收集；输入引用但不参与本次结论的媒体不会被打包。
7. 视文路线只消费 #14 的**真实上游分**，不会在 #16 内用 CLIP 重算视频分；也不校验视频
   是否可解码。代表帧只在「人工改写描述」时使用，且只对单帧 + 改写文本算一次余弦
   （与图文重评分同一条链路）。

## 11. 真实测试结果

运行环境：Windows + Python 3.9.7（本机已装 `open_clip`/`torch`，但**未配置权重**）。

```powershell
python -m pytest tests/ -q
# 231 passed, 17 skipped（2026-11-05，阈值联动/时序路线改动后）

python -m pytest tests/ -q      # 变异验证：把中间分档改坏后
# 8 failed, 166 passed
# 被击穿用例：test_threshold_buckets[0.25|0.28|0.319999]、
# test_review_queue_only_holds_middle_band_and_sampling、
# test_human_event_requires_reviewer_identity、
# test_rescore_overrides_low_old_score_but_keeps_old_score_in_evidence、
# test_missing_media_without_rescore_is_only_counted、
# test_sample_run_matches_expected_true_counts
# （还原实现后重新全绿，证明用例非空转；用例数随后增至 231）

python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
# status=degraded，输入=19 自动对齐=2 人工对齐=1 待人工=7 存疑=1 不对齐=5 不适用=0，
# 主清单=11 人工队列=8 隔离=5 不适用=0 异常=3 审计=7，conservation_ok=true

# 视文路线 CLI 冒烟（构造 video+video_text_score，无本地 CLIP）：
# status=success，输入=6 自动对齐=2 待人工=2 不对齐=1 不适用=1；
# 主清单 score_source=video_text_score / processing.14，16_not_applicable 只剩音文

python tests/smoke_api.py      # 三接口：health/upload×2/predict(中间)/predict(复用)/predict(阶段末)/越界/removeFile
python -m json.tool map.json
python -m compileall -q .
```

测试覆盖：阈值边界（0.24/0.25/0.319999/0.32/0.41/无分数）、三种人工结论、人工否决锁定、
改描述重评分（成功/延后/推理失败/媒体不可读）、抽检命中、存疑回流、隔离守恒、坏行/缺字段/
重复编号/编码错误、不适用模态、空输入、错误配置、非法 uniqueId 与路径穿越、两种 `isLastOperator`
调用（响应同构）、`/removeFile` 只清本任务、`/downloadFile` 不存在；
视文路线单独覆盖（`tests/test_video_route.py`）：video 高/低/中间/无分档、`processing.14` 与
记录级取分（视频侧优先）、image 优先回归、图文/视文 `alignment.whole` 键集合一致、
无帧重评分显式 `pending` 且不调用模型、代表帧=中间帧重评分。

> 测试通过 ≠ 交付验收通过。验收需在交付环境配置真实权重后复测重评分数值，并由工程侧完成
> 阶段联调（`isLastOperator=true` 由工程侧决定）。
