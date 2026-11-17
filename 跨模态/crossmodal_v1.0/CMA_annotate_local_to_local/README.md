# 算子 18 标注局部对局部（`phrase_region_annotation`）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

跨模态数据加工产线 **S3 阶段**的可选细粒度标注节点，正式编号 **18**，默认 **关闭**。

用**已加载的 CLIP-ViT/B-32**（与 #12/#14 复用同一实例与配方）给「描述里的词」与
「#17 给出的局部候选框裁图」算余弦相似度，按分数降序做**一对一**匹配，
达到 `min_clip_score` 才预填，否则标为待人工。产出的是**待人工确认**的
词-区域关系；未确认关系不得交付。

---

## 1. 功能边界

**做**

- 复用 #17 候选框裁剪局部区域，逐框与逐词计算 CLIP 余弦分；
- 中文分词：优先复用预训练文本产线的 `tokens`，否则结巴分词（写进日志与 `processing["18"]`）；
- 阈值分档：`clip_score >= min_clip_score` → 预填；否则 → 待人工；
- 一对一匹配：一个候选框最多被一个词占用；
- 分诊台账：待人工确认 / 样本隔离库 / 原始隔离区 三类清单，逐条可追溯。

**不做**

- 不下载任何权重、不联网（默认加载本算子 `weight/`，也可用 `$MODEL_ROOT` 指向本地挂载，或复用环境内已加载的共享实例）；
- 不新建多语言模型；
- 不把文件名当词；
- 不产生「已确认」的关系（`human_verified`、`deliverable` 恒为 `false`）；
- 不使用固定/随机分数，不在模型不可用时伪造降级（只会 `degraded` 且分数为 `null`）。

---

## 2. 输入字段

输入是一份 JSONL。完整契约见 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)，摘要：

| 用途 | 取值优先级 |
| --- | --- |
| 标识 | `sample_id` → `pair_id` |
| 描述 | `text.content` → `text.description` → `text` → `description` → `metadata.description` |
| 图像 | `image`（str/obj） → `image_path` → `processing.17.paths.image` |
| 候选框 | `regions` → `boxes` → `image.regions` → `processing.17.regions` |
| 上游分词 | `text.tokens` → `tokens` → `processing.04.tokens` |

候选框支持 `xywh-pixel`（默认）、`xyxy-pixel`、`*-normalized`、扁平 `{x,y,w,h}`；
非法框会被丢弃并计入 `boxes_invalid`，不会静默忽略。

---

## 3. 输出字段

主输出（开启 `18_phrase_region_annotations.jsonl` / 关闭 `18_passthrough.jsonl`）
保留原字段并追加：

```jsonc
{
  "processing": { "18": { "status": 1, "process_status": "prefilled", "relations": [ /* … */ ], "statistics": null } },
  "phrase_box_relations": [
    {
      "phrase": "金毛犬",
      "box_id": "b1",
      "bbox": {"x": 2, "y": 2, "w": 30, "h": 24, "coord": "xywh-pixel"},
      "class_name": "dog",
      "clip_score": 0.842315,
      "best_score": 0.842315,
      "matched_text": "金毛犬",
      "variant": "word",
      "phrase_tokens": ["金毛犬"],
      "confirmation_method": "auto_prefill",
      "status": 1,
      "process_status": "prefilled",
      "reason": null,
      "human_verified": false,
      "review_required": true,
      "source_reference": "operator_18"
    }
  ],
  "operator_18": { "operator_number": 18, "status": 1, "process_status": "prefilled",
                   "decision": "prefill", "reason_code": "auto_prefill",
                   "human_verified": false, "deliverable": false }
}
```

> 记录级/关系级 `status` 为 v4.2 int（1=正常/预填/待人工/降级，0=隔离/失败），原字符串迁入
> `process_status`；`processing["18"]`、`phrase_box_relations[]`、`operator_18` 一致。
>
> `phrase` 恒为分词得到的**词**（一词一框）；`matched_text` 是该分数实际使用的文本，
> `variant` 为 `word` 或 `noun_phrase`，`phrase_tokens` 是短语覆盖的词。
> 节点另含 `phrases_total` / `phrases_used` / `token_variants` /
> `tokens_dropped_detail` / `class_profile` / `crop_padding_ratio` /
> `background_suppression`，均为新增字段，既有消费方可忽略。

台账（同目录）：

| 文件 | 内容 |
| --- | --- |
| `18_pending_manual.jsonl` | 待人工确认台账（预填 + 待人工） |
| `18_sample_quarantine.jsonl` | 样本隔离库（`样本-语义不通`，附原始记录） |
| `18_rejects.jsonl` | 原始隔离区（坏行、缺标识、标识重复、图像缺失/损坏/路径非法） |
| `18_run.log` | 逐行日志（时间 / 动作 / 真实数量） |
| `manifest.json` | 运行清单（`crossmodal-manifest-v1`） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py` 的血缘节点/阶段上下文函数与 `utils/mty_compliance.py`，代码保留存档；`lineage.py` 的 `now_iso`/`config_snapshot`/`artifact_entry` 等通用工具仍在用，`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计、模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

---

## 4. 配置（`configs/config.yaml`）

| 键 | 默认 | 范围/取值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `false` | bool | 工具总开关，默认关闭 |
| `min_clip_score` | `0.25` | **0.20–0.40** | 达到才预填；越界直接报错 |
| `tokenizer` | `pretrained` | `pretrained` / `jieba` | 优先预训练产线分词，否则结巴 |
| `max_tokens` | `64` | 1–512 | 每条记录最多参与匹配的词数 |
| `max_text_chars` | `500` | 1–10000 | 描述截断长度 |
| `merge_phrases` | `true` | bool | 把「修饰语 + 名词」合并为短语，作为词的上下文变体参与打分（词仍是匹配单元） |
| `class_calibration` | `prior` | `none` / `znorm` / `prior` | 按框的检测类别校准，**只用于词→框的指派**：`znorm` 做类内标准化，`prior` 加「匹配文本 vs 框类别名」的语义先验；落盘分数与 `min_clip_score` 判定仍是原始余弦 |
| `class_prior_weight` | `0.05` | **0–0.5** | `prior` 的权重：越大越依赖类别名，越小越依赖原始 CLIP 分（`none`/`znorm` 下无效） |
| `crop.padding_ratio` | `0.0` | **0.00–0.25** | 框外扩边比例；0 = 紧贴检测框（实测扩边不增益，默认关） |
| `crop.background_suppression` | `none` | `none` / `mean` / `gray` | 扩边后把框外环形区域填成纯色；需 `padding_ratio > 0` |
| `allow_degraded` | `false` | bool | 模型不可用时是否允许显式降级 |
| `clip.library` | `auto` | `auto` / `open_clip` | 后端 |
| `clip.model_name` | `ViT-B-32` | `CLIP-ViT/B-32` 等价写法 | 架构 |
| `clip.weights` | `clip/ViT-B-32.pt` | 本地路径 | 默认加载本算子 `weight/`；为空表示复用环境共享实例/无权重 |
| `clip.device` | `cpu` | `cpu` / `cuda` | 设备 |
| `clip.batch_size` | `8` | 1–64 | 裁图批大小 |

请求级可用 `overrides`（仅白名单键）临时覆盖，例如
`{"enabled": true, "allow_degraded": true}`。

---

## 5. 算法与模型后端

1. `tokenize`：`text.tokens` → 结巴 → 确定性规则兜底（标记 `rule_fallback`）；
   过滤文件名/主干/扩展名/主干片段、标点、功能词/数词/量词（有 `jieba.posseg` 时
   另按词性剔除代词、介词、连词、助词等），保序去重并截断；剔除原因分类计入
   `tokens_dropped_detail`。
2. `phrases`：相邻「修饰语 + 名词」合并为名词短语（中文 ≤6 字、英文 2 词且
   ≤40 字符；动词头、跨标点、名词+动名词不合并），每个词的匹配文本 =
   词本身 + 覆盖它的短语（`variants`）。
3. `crop`：按 `bbox` 换算像素矩形并夹紧到图像边界；可选外扩
   （`crop.padding_ratio`）与框外背景抑制；空矩形报错（不产空图）。
4. `encode`：同一 CLIP 实例分别编码匹配文本与裁图，输出 L2 归一化向量（512 维）。
5. `score`：`cos(θ)`，裁剪到 `[-1, 1]`，保留 6 位小数；零向量记 `0.0`；
   每个词对每个框取「词 vs 所在短语」最高分，`matched_text`/`variant` 记录胜出文本。
6. `match`：按分数降序贪心，词与框一对一（**词仍是匹配单元**）。
7. `decide`：`score >= min_clip_score`（含等于）→ 预填；否则待人工。

模型不可用时：

| `allow_degraded` | 行为 |
| --- | --- |
| `false`（默认） | **显式失败**：整次运行 `status=failed`，CLI 退出码 3 / HTTP 400，不写任何分数 |
| `true` | **显式降级**：`status=degraded`，记录进待人工台账，`clip_score` 为 `null`，`model` 为 `null` |

---

## 6. 启动与调用

### 6.1 HTTP 服务

```bash
export OUTPUT_DIR=/data/output          # Windows: set OUTPUT_DIR=...
export OPERATOR_CONFIG=$PWD/configs/config.yaml
python -m uvicorn app:app --host 0.0.0.0 --port 8118
```

三个正式接口（**没有 `/downloadFile`**）：

```bash
# 上传任务包（zip：一个 .jsonl + 可选 media/）
curl -X POST "http://127.0.0.1:8118/uploadFile?uniqueId=task_001" -F "file=@bundle.zip"

# 形态一：单文件流（multipart/form-data，?detection=annotate_local_to_local，file 必传）
curl -X POST "http://127.0.0.1:8118/predict?detection=annotate_local_to_local" -F "file=@demo.jsonl"

# 形态二：中间（isLastOperator=false）与阶段末（true）响应同构，均返回 outputFile + result
curl -X POST http://127.0.0.1:8118/predict \
  -H 'Content-Type: application/json' \
  -d '{"uniqueId":"task_001","isLastOperator":false}'
curl -X POST http://127.0.0.1:8118/predict \
  -H 'Content-Type: application/json' \
  -d '{"uniqueId":"task_001","isLastOperator":true}'

# 阶段内后续算子：传任务内相对路径（上一阶段产物路径由编排侧记录）
curl -X POST http://127.0.0.1:8118/predict \
  -H 'Content-Type: application/json' \
  -d '{"uniqueId":"task_001","isLastOperator":false,"inputFile":"input/demo.jsonl"}'

# 清理任务（幂等）
curl -X POST "http://127.0.0.1:8118/removeFile?uniqueId=task_001"
```

响应统一为 `{"code":0|1,"msg":"...","data":{...}}`；`isLastOperator` 仍必传（严格布尔），只标记阶段末调用，v4.2 起不再改变响应形态。

### 6.2 命令行

```bash
# 默认（工具关闭）：原样放行，不产生细粒度对应
python processor.py --input dataset/sample/demo.jsonl \
    --unique-id smoke_cli --output-root output_smoke

# 开启 + 显式降级（无本地权重时可用）
python processor.py --input dataset/sample/demo_enabled.jsonl \
    --unique-id smoke_enabled --output-root output_smoke --enable --allow-degraded
```

### 6.3 响应示例（v4.2）

形态二（`POST /predict`，JSON body；中间/阶段末同构）：

```json
{"code":0,"msg":"模型不可用：按配置显式降级为待人工，未产生任何分数","data":{
  "operator":"annotate_local_to_local",
  "status":"degraded",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"18_phrase_region_annotations.jsonl",
    "fileCount":5,"totalBytes":0,
    "files":[{"path":"18_phrase_region_annotations.jsonl","encoding":"utf-8","size":0,"content":"..."}]},
  "result":{"uniqueId":"smoke_enabled","runId":"run001",
            "statistics":{"input_records":9,"valid_records":9,"bad_lines":0,
                          "prefilled_records":0,"pending_manual_records":6,
                          "sample_isolated":1,"origin_quarantined":2,
                          "skipped_records":0,"deliverable_records":0,
                          "degraded":true,"conservation_ok":true}}}}
```

形态一（`POST /predict?detection=annotate_local_to_local`，multipart/form-data，处理型 `status`）：

```json
{"status":1,"uniqueId":"task_001","sample_id":"enabled-0001",
 "phrase_box_relations":[],"unconfirmed_count":0,"process_status":"pending_review"}
```

失败：`{"status":0,"msg":"..."}`（形态一）或 `{"code":1,"msg":"...","data":{}}`（形态二，参数不合法为 HTTP 422 + `code=1`）。

---

## 7. 资源需求

| 场景 | 内存 | 说明 |
| --- | --- | --- |
| 关闭路线 | ~120 MB | 只有 FastAPI/分词，无 torch 前向 |
| 开启路线（CPU，ViT-B-32） | ~1.5–2.5 GB | torch + CLIP 权重；建议 ≥4 vCPU |
| 开启路线（GPU） | 显存 ~1.5 GB | `clip.device: cuda`，单卡可并发多任务 |

磁盘：每条记录媒体按引用去重复制一次（`<run_dir>/media/`）；
单次响应默认上限 256 MB（`utils.transport.MAX_RESPONSE_BYTES`），超限返回 413。

---

## 8. 已知限制

1. **未接入人工复核平台**：所有关系都是候选，`deliverable_records` 恒为 0；
   人工确认需要下游（#25）回写。
2. **权重缺失时开启路线会显式失败**（默认已内置 `weight/clip/ViT-B-32.pt`；`$MODEL_ROOT`
   覆盖到空目录或 `clip.weights` 置空即复现）；业务链路验证使用注入编码器
   （`tests/conftest.py` 的 `StubEncoder`，明确标注为测试替身），
   真实前向另验证管线形状与单位化（`tests/test_clip_plumbing.py`，随机初始化、无语义）。
3. 分词兜底 `rule_fallback` 只是确定性切分，不具备分词模型的词边界能力。
4. 台账按行写入，未做分片；超大批次（>10 万行）建议按批次切分输入。
5. 逐块 UTF-8 解码失败时按「1 条输入记录」计入 `bad_lines`（无法得知该块内的真实行数），
   因此该场景下 `input_records` 是保守下界。

---

## 9. 真实测试结果

环境：Windows / CPython 3.9.7 / fastapi 0.128.8 / pydantic 2.13.5 / torch 2.3.1+cu121 /
open_clip_torch 2.26.1 / jieba 0.42.1。

```
$ python -m pytest tests/ -q
174 passed in 27.31s

$ python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
{"code": 0, "msg": "局部对局部处理完成（全部关系待人工确认）", ...}
  input_records=10  valid_records=10  skipped_records=10  rejects_entries=2  conservation_ok=true

$ python processor.py --input dataset/sample/demo_enabled.jsonl --unique-id smoke_enabled \
      --output-root output_smoke --enable --allow-degraded
{"code": 0, "msg": "模型不可用：按配置显式降级为待人工，未产生任何分数", ...}
  input_records=9  pending_manual_records=6  sample_isolated=1  origin_quarantined=2
  model_unavailable=4  no_boxes=1  missing_description=1  relations_total=0
  degraded=true  conservation_ok=true

$ python processor.py --input dataset/sample/demo_enabled.jsonl --unique-id smoke_strict \
      --output-root output_smoke --enable
{"code": 1, "msg": "模型不可用：clip_weights_missing", "data": {}}     # 退出码 3

$ python tests/smoke_api.py
共 27 项检查，通过 27 项，失败 0 项

$ python -m compileall -q .        # 无输出（通过）
$ python -m json.tool map.json     # 通过
```

`tests/smoke_api.py` 覆盖：上传/预测（中间与阶段末统一响应）、两个 `uniqueId` 隔离、
非法 `uniqueId`、zip 穿越、跨任务 `inputFile`、非法 `isLastOperator`、
`/downloadFile` 不存在、`removeFile` 幂等、开启路线的显式降级（零分数）与严格失败、
阈值越界拒绝。

---

## 10. 目录结构

```
18_标注局部对局部/
├── app.py                 # FastAPI 三接口
├── processor.py           # CLI 入口（业务模块）
├── inference.py           # 核心流程：隔离/锁/流式处理/统计/清单
├── configs/config.yaml
├── utils/                 # 共享契约层与业务规则
│   ├── schemas.py io.py transport.py logger.py lineage.py  # lineage.py（v4.2 血缘输出停用，通用工具仍在用）
│   ├── scoring.py tokenization.py phrase_region.py backends.py
├── dataset/DATA_SCHEMA.md
├── dataset/sample/        # 可复现样例（真实 PNG）+ make_sample.py + manifest.json
├── tests/                 # 174 个单测 + smoke_api.py
├── packaging/prepare_package.py
├── requirements.txt / requirements-model.txt
├── Dockerfile / .dockerignore
├── map.json
├── output/                # 保持为空
└── README.md / OPERATOR_MANUAL.md / HANDOFF.md / DEVELOPMENT_STATUS.md
```
