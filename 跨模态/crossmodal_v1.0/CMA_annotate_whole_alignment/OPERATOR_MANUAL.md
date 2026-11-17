# 算子说明书 — #16 标注整体对齐关系

| 项 | 值 |
|---|---|
| 正式编号 | 16 |
| 算子名 / 版本 | `16_whole_alignment` / `v1.0.0` |
| 中文标签 | 标注整体对齐关系 |
| 表1 行 | v4.1 第 16 行 |
| 流程环节 / 阶段 | 数据加工 / `S3` |
| 工具类型 | 小模型（CLIP-ViT/B-32，复用 #12 实例） |
| 必选性 / 默认 | 可跳过=否（必须）；默认开启，不可关闭 |
| 优先级 | P0 |
| 端口 / 入口 | 8116 / `app:app`（CLI 为 `processor.py`） |
| 开发目录 | `G:/HYQX/算子/跨模态/demo_10.6/16_标注整体对齐关系` |

## 职责

判断两个完整模态（整体—整体）是否语义一致，输出对齐结论；管理人工三按钮结论与人工否决锁定；
人工改写描述后用同一 CLIP 实例重新评分。支持 `image+text` 与 `video+text` 两类整体配对：
图像分（#12）永远最高优先，只有没有图像分时才消费 #14 的视文分。不画框、不分词，
不做 #17/#18 的局部判定，不替代 #12/#14 评分。

## 输入 → 输出

- 输入：`#11` 候选对 + `#12` `image_text_score`（真实分数/模型版本/tier）；
  `#13` `processing["13"]`（实际 CER / 同音容错 / 生效阈值与路线状态）；
  `#14` `video_text_score` / `processing["14"]`（视文真实分/模型/tier，含视频侧记录级字段）；
  `#24` 抽检命中标记；人工回写事件 `16_human_events.jsonl`（可选，随素材包上传）。
- 输出：`16_whole_alignment.jsonl`（主清单）、`16_review.jsonl`（人工队列）、
  `16_isolation.jsonl`（样本隔离）、`16_not_applicable.jsonl`（不适用）、
  `16_rejects.jsonl`（原始文件类异常）、`16_audit.jsonl`（人工事件审计），
  以及 `manifest.json` / `config.snapshot.json` / `run.log` / `media/*`。
  输入中的 `video_text_score` 与 `processing["14"]` 原样保留，供 #19 的 route.14/whole 门读取。

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py`、`utils/mty_compliance.py` 的血缘/归档接口，代码保留存档；`utils/transport.py` 仅保留 `crossmodal-files-v1` 文件对象与 `manifest.json` 打包能力）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

## 阈值与结论

| 分数 | 结论 | 标签 | 去向 |
|---|---|---|---|
| `score ≥ 自动线`（未被抽检/否决） | `auto_aligned` | 样本-评分：通过 | 样本池 → #19 |
| `隔离线 ≤ score < 自动线` 或抽检命中 | `pending_manual_review` | 样本-评分：待人工 | 人工队列 |
| `score < 隔离线` | `not_aligned` | 样本-语义不通 | 样本隔离库 → #21 |
| 无 CLIP 分 + #15 判通过（时序窗口） | `auto_aligned`（`confirmation_method="temporal_15"`） | 样本-评分：通过 | 样本池 → #19 |
| 无 CLIP 分 + #15 判超限 | `not_aligned` | 样本-语义不通 | 样本隔离库 → #21 |
| 人工「对齐」 | `manual_aligned` | 样本-评分：通过 | 样本池 → #19 |
| 人工「存疑」 | `uncertain` | 样本-评分：待人工 | 人工复核队列 → #25 |
| 人工「不对齐」 | `not_aligned` + `human_veto_locked` | 样本-语义不通 | 样本隔离库 → #21 |
| 有效 #13 最终 CER ≤ 实际 edit_distance_ratio_max | `auto_aligned`（CER 单列、clip_score=null） | 样本-评分：通过 | 样本池 → #19 |
| #13 真实失败 / 最终 CER 超过实际阈值 | `not_aligned` | 样本-语义不通 | 样本隔离库 → #21 |
| 无 image/video/audio 与 text 组合 | — | 流程-不适用整体对齐 | 留产线（样本池） |
| 无分数且上游未隔离 | `pending_manual_review` | 样本-评分：待人工 | 人工队列（不伪造分数） |
| 人工改写描述但无代表帧/媒体不可用 | `pending_manual_review` + `degraded` | 样本-评分：待人工 | 人工队列（`score_after=null`） |

关键规则：人工「不对齐」一旦写入即锁定，后续高分（含重跑）不得改回通过。
音文必须明确启用、通过、未降级且指标/阈值有效；改描述后没有新 #13 评分则待重评分。
适用的 #15 仍优先于音文；`no_timestamp_timeline` 不适用节点不遮住 #13。
`frames` 数字只表示数量，`keyframes` 数组保留路径；输出携带原始音视频和关键帧。
视频人工重分代表帧写 `video_frame_reference`，不伪装为图像媒体。

**隔离线 / 自动线的取值**：默认 `whole_alignment.follow_upstream_thresholds=true`，
上游节点自带标定线时用上游的（#12 `thresholds.isolation/auto_align`、#14
`low_score_threshold`），`threshold_source="upstream"`；上游没声明时才用本算子的
`isolation_threshold`（0.20）/ `auto_align_threshold`（0.32），`threshold_source="config"`。
这样同一份 CLIP 分不会在 #12 判「通过」、在 #16 被判「不对齐」。

## 视文路线（video + text）

- **判定**：一侧 `video`（`record.video` 有 `path`/`frames`，或 `side.modalities` 含 `"video"`）
  + 另一侧 `text` → 视文路线；`image+text` 仍**最高优先**（`video+image+text` 走图文）。
- **取分**：`pair.video_text_score`（#14 节点，dict 或裸标量）→ `pair.processing["14"]` →
  视频侧 `record.video_text_score` → `record.processing["14"]`；节点内按
  `segment_mean` → `clip_score` → `score` → `video_text_score` 取值，并读
  `model`/`model_version`/`tier`/`status`。`score_source` 标 `video_text_score` 或
  `processing.14`，`evidence.upstream.origin="14"`。
- **分档**：跟随 #14 节点自带的 `low_score_threshold`（自动档仍为 0.32），不改响应结构。
- **媒体**：常规视文分档**不读**视频/帧文件、不需要本地 CLIP。只有人工改写描述时，
  取 `video.segments[].frames`（否则 `video.frames`）顺序展开的**中间帧**作为重评分目标；
  无帧/帧不可读 → `pending_manual_review`，
  `description_edit.process_status="no_representative_frame"`（或真实媒体错误码），
  `score_after=null`，任务 `degraded=true`；不拒绝整条记录、不伪造分数。

## 调用

```powershell
# 服务
uvicorn app:app --host 0.0.0.0 --port 8116
# CLI
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

三接口：`POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`（无 `/downloadFile`）。
`/predict` 双协议：`multipart/form-data` 为形态一（单文件流，`?detection=`），返回 int `status`（处理型 1 成功/0 失败）+ `process_status`/`alignment_label`/`decision_source`/`score`/`review_id`；`application/json` 为形态二，必传 `uniqueId` + 严格布尔 `isLastOperator`，返回 `{code,msg,data{operator,status,outputFile,result}}`。`isLastOperator` 仅标记阶段末调用，v4.2 起中间与阶段末响应同构（均含 `outputFile` + `result`），不再用 `false` 只回血缘对象。

## 资源与降级

- 常规分档：1 核 / < 300 MB，无需 GPU、无需本地权重（消费 #12 图文分或 #14 视文分）。
- 人工改描述重评分：2–4 核 / 2–3 GB + ≈600 MB 权重；`device: cuda` 可选。
  视文路线只在此时读取代表帧；无帧时显式延后为待人工。
- 权重缺失（`model.weights: null`、`$MODEL_ROOT` 覆盖目录无文件等）时：常规分档照常，重评分记录延后为
  `pending_manual_review`，任务 `status=degraded` 且 `degradation_reason` 如实上报；
  `allow_degraded: false` 时任务失败。**任何情况下不产生分数字段。**

## 验证状态

- 单测：`python -m pytest tests/ -q` → **231 passed / 17 skipped**（2026-11-05）（含阈值边界、人工三按钮、
  否决锁定、重评分四条路径、守恒式、坏行/编码/路径安全、三接口统一响应形态、
  以及视文路线 `tests/test_video_route.py` 17 条）。
- CLI/接口冒烟：`processor.py` 与 `tests/smoke_api.py` 均通过（详见 `README.md` §11）；
  视文路线已用构造的 `video+video_text_score` 输入跑通 CLI（`score_source=video_text_score`，
  不再进 `16_not_applicable`，无伪分）。
- 未验证：真实 CLIP 权重下的重评分数值（本机无权重）；真实视频文件解码（视文分档不使用）；
  Docker 镜像未构建。
