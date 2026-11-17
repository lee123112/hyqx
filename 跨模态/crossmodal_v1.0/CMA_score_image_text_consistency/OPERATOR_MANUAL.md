# 算子说明书 — #12 评分图文语义一致性

## 一、基本信息

| 项 | 内容 |
| --- | --- |
| 正式编号 | #12（表1 v4.1 第 12 行） |
| 算子名称 | 评分图文语义一致性 |
| 算子 ID | `image_text_semantic_scoring` |
| 版本 | v1.0.0 |
| 阶段 | S3 跨模态数据对齐标注（表1 数据加工） |
| 服务端口 | 8112 |
| 入口产物 | `12_image_text_scores.jsonl` |
| 代码入口 | `inference.py`（CLI：`processor.py`）、`app.py`（HTTP） |
| 部署单元 | 每个算子目录各自内置一份同一份 CLIP-ViT/B-32 权重（同 sha256）；可选复用共享 CLIP 实例 |

## 二、职责

对上游 #11 生成的图文候选对，用 CLIP-ViT/B-32 计算文本与图像的真实余弦相似度，
按阈值输出分数、质量标签与台账。**阈值以平台面板配置为唯一事实来源**：算子缺省
`isolation_threshold=0.20`、`auto_align_threshold=0.32`，可被 `/predict` 的 `config`
覆盖（取值范围分别为 `0.20`—`0.40` 与 `0.25`—`0.50`，且必须满足
`auto_align_threshold > isolation_threshold`）；低于隔离阈值进隔离库，达到自动对齐阈值进样本池，
两者之间为待人工队列。文档不固化其他分档数值。

可选的 YOLOv8 辅助仅对中间分补证据，**不改变分档**。

## 三、接口

仅三个正式接口，无 `/downloadFile`。

| 接口 | 入参 | 出参 |
| --- | --- | --- |
| `POST /uploadFile?uniqueId=` | multipart `file`（ZIP 或单 JSONL） | `data: {uniqueId, inputFile, entry, bytes}` |
| `POST /predict` | JSON：`uniqueId`(必)、`isLastOperator`(必, 严格布尔)、`inputFile`(阶段内后续算子传)、`config`(业务参数覆盖) | 见下 |
| `POST /removeFile?uniqueId=` | query | `data: {uniqueId, deleted}` |

外层信封：`{"code": 0|1, "msg": "...", "data": {...}}`。

- [v4.2] `isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；**中间与阶段末响应同构**：
  `data = {operator, status, process_status, outputFile, result}`，`outputFile` 为
  `crossmodal-files-v1` 文件内容对象（入口 `12_image_text_scores.jsonl`），`result` 为业务摘要
  `{runId, runDir, files, statistics, config, stageId}`；
- 不再用 `false` 只回血缘对象，也不再返回 `lineageFile`。

阶段首（本阶段第一个算子）不传 `inputFile`；阶段内后续算子传上一响应 `result.files.outputFile`（任务内路径）。
若 `inputFile` 指向本算子上次运行的入口产物，则直接复用该次运行（不重复推理）。

## 四、输入

- #11 的 `11_candidate_pairs.jsonl`（UTF-8 JSONL）+ 同包媒体（相对路径）。
- 必需字段：`pair_id`、`left`、`right`；文本取 `text.content` 或 `metadata.description`；
  图像取 `image.path`（或 `image` 字符串）。
- 详细字段契约见 `dataset/DATA_SCHEMA.md`。

## 五、输出

| 产物 | 用途 |
| --- | --- |
| `12_image_text_scores.jsonl` | 主清单（人工队列 + 自动对齐），交 #16/#19 |
| `12_isolation.jsonl` | 隔离台账（`样本-语义不通`），交 #21 |
| `12_not_applicable.jsonl` | 无图文组合 / 路线关闭，留产线不隔离 |
| `12_pending_model.jsonl` | 模型不可用时的待模型记录（无分数） |
| `12_rejects.jsonl` | 坏行 / 缺字段 / 媒体问题，进原始隔离区 |
| `manifest.json`、`config.snapshot.json`、`run.log`、`media/**` | 清单、快照、日志、媒体副本 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`lineage.py`/`mty_compliance.py` 代码保留存档）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

## 六、依赖

- 服务：`fastapi`、`uvicorn`、`pydantic`、`pyyaml`、`python-multipart`、`pillow`；
- 真实模型（可选安装）：`torch`、`open_clip_torch`（或 OpenAI `clip` 包、`transformers`）；
- 可选辅助：`ultralytics` + YOLOv8 本地权重（默认关闭）；
- **权重必须本地提供**：默认 `<算子目录>/weight/clip/ViT-B-32.pt`（随目录交付，见
  `weight/README.md`）；`$MODEL_ROOT` 只作覆盖，不设时按本算子目录解析；服务不联网下载；
- YOLOv8n 可选权重 `weight/YOLOv8n/yolov8n.pt`（同目录内置，默认关闭，`detector.weights` 仍为 `null`）；
- NFS 只读媒体、无 ffmpeg 等环境不影响本算子（仅需图像解码）。

## 七、异常与质量口径

| 情形 | 标签 | 去向 |
| --- | --- | --- |
| 非法 UTF-8 / 非法 JSON / 非对象行 | `原始文件-编码异常` / `原始文件-无法解析` | 原始隔离区（`12_rejects.jsonl`） |
| 缺 `pair_id` / `pair_id` 重复 | `原始文件-缺字段` | 原始隔离区 |
| 媒体路径非法（绝对/URL/`..`） | `原始文件-无法解析` | 原始隔离区 |
| 媒体缺失 / 图片不可解码 | `原始文件-损坏` | 原始隔离区 |
| 无图文组合（如仅有音文/音图） | `流程-不适用图文评分` | 留产线（不隔离） |
| `enabled=false`（路线关闭） | `流程-路线关闭` | 留产线（不隔离） |
| 模型不可用 | `流程-待模型评分` | 留产线（`12_pending_model.jsonl`，无分数） |

统计守恒：`input == output + isolated + pending_model + not_applicable + rejected`，
`conservation_ok` 为真实计算结果。

## 八、部署与运维

```powershell
pip install -r requirements.txt          # 服务与测试
pip install -r requirements-model.txt    # 需要真实评分时
uvicorn app:app --host 0.0.0.0 --port 8112
```

- `OUTPUT_DIR`：输出根目录（默认算子目录下 `output/`）；
- 任务隔离：所有产物写入 `OUTPUT_DIR/<uniqueId>/`，任务级排他锁（`OUTPUT_DIR/.locks/`）；
- 重复运行自动新建 `runNNN` 目录，互不覆盖；输入文件路径与产物路径冲突时拒绝写入并报错；
- 日志一行一动作，只记数量与标识，不记录完整原文与堆栈。

## 九、当前验证状态（详见 DEVELOPMENT_STATUS.md）

- `python -m pytest tests/ -q` → 127 passed, 30 skipped，无失败（29 项 v4.2 血缘/运输输出停用
  既有 skip + 1 项未设 `OP12_CLIP_WEIGHTS` 的可选真实权重集成检查）；设 `OP12_CLIP_WEIGHTS`
  指向本算子 `weight/clip/ViT-B-32.pt` 后，`tests/test_clip_plumbing.py` 4 passed（真实权重加载与前向）；
- 包外 standalone（只复制本算子目录，无包根 `weight/`、无 `shared_clip/`、未设 `MODEL_ROOT`）：
  `python inference.py --input dataset/sample/pairs.jsonl --unique-id smoke_standalone --output-root output_standalone`
  → 退出码 0，`status=success`，`scored=4 / output=4 / isolated=0 / pending_manual=4 / pending_model=0`，`clip_score=0.22073`（0.20 线不隔离该分，转人工复核）；
  模型身份 `weights=ViT-B-32.pt`，实际加载副本目录 `weight/clip/ViT-B-32.pt`（`library=open_clip`）；
- `MODEL_ROOT` 指向空目录：`status=degraded`，`degradation_reason=weights_not_found: ViT-B-32.pt`，
  `pending_model=4`，主清单 0 条，**未**回退本地副本；
- `python tests/smoke_api.py` → 15/15 PASS（历史实测，本次未重跑）；
- `python -m json.tool map.json`、`python -m compileall -q .` 历史通过；Docker 未运行。
