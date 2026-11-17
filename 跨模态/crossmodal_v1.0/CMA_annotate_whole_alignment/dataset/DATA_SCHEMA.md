# 算子 #16 标注整体对齐关系 — 数据契约

> 本文件描述本算子**实际读写**的字段，与 `utils/alignment.py`、`inference.py` 实现一致。
> 编号 16、阶段 S3（数据加工）、算子名 `16_whole_alignment`。

## 1. 输入

### 1.1 主输入（阶段首上传 / 阶段内由上一算子传入）

标准输入是 #11 `11_candidate_pairs.jsonl` 的候选对，并由 #12 追加 `image_text_score`；
视文配对由 #14 追加 `video_text_score` 与 `processing["14"]`（节点同构）：

```json
{
  "pair_id": "pair-t01--i01",
  "group_id": "grp-src-src-16",
  "modality_pair": ["text", "image"],
  "evidence": {"type": "A", "criterion": "same_source_id", "evidence_source": "rule",
               "value": {"source_id": "src-16"}},
  "evidence_priority": 1,
  "left": {"sample_id": "t01", "modalities": ["text"], "record": { "...": "..." }},
  "right": {"sample_id": "i01", "modalities": ["image"], "record": { "...": "..." }},
  "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0",
               "uniqueId": "sample_task"},
  "image_text_score": {
    "clip_score": 0.41,
    "score_semantics": "cosine",
    "tier": "auto_aligned",
    "label": "样本-评分：通过",
    "action": "auto_align",
    "destination": "样本池",
    "status": "scored",
    "thresholds": {"isolation": 0.20, "auto_align": 0.32},
    "model": {"name": "CLIP-ViT/B-32", "version": "ViT-B-32", "library": "open_clip",
              "device": "cpu", "degraded": false, "real_backend": true,
              "score_semantics": "cosine"}
  }
}
```

本算子只读不写上游字段，允许的分数来源按优先级：

| 来源 | 字段 | `alignment.whole.score_source` |
|---|---|---|
| 上一轮 #16 | `alignment.whole.clip_score` | `alignment.whole` |
| #12 主写 | `image_text_score.clip_score` | `image_text_score` |
| #12 命名空间 | `processing.12.clip_score` | `processing.12` |
| 兼容形态 | `alignment.image_text.clip_score` / `score` | `alignment.image_text` |
| 兜底 | 顶层 `image_text_score` / `clip_score` 标量 | `top_level.<key>` |
| #14 主写（视文） | 顶层 `video_text_score` 节点（`segment_mean`/`clip_score`/`score`/`video_text_score`） | `video_text_score` |
| #14 命名空间（视文） | `processing.14` 同结构节点 | `processing.14` |
| #14 记录级（视文） | 视频侧 `record.video_text_score` / `record.processing.14` | `video_text_score` / `processing.14` |
| #15 时序结论（无 CLIP 分时） | `processing.15` / 记录侧 `processing.15`（时序窗口，#14 已判 `not_applicable`） | `processing.15` |
| #13 音文（无适用 #15 时） | 根级/左右 `record.processing.13`，兼容 `audio_text` | 实际来源，如 `right.record.processing.13` |
| 无 | 无任何分数 | `none`（结论为待人工，**不编造分数**） |

图像分（#12）永远最高优先：只要存在图像真实分数，就不会用视文分覆盖。
音文不借用图文 CLIP 分。#13 `route_enabled=true`、`passed=true`、未降级且有限非负最终 CER
与实际阈值有效时，按 `CER <= edit_distance_ratio_max` 判定；#15 适用结论仍优先，
`not_applicable/no_timestamp_timeline` 不遮住 #13。

音文新增字段同时位于 `alignment.whole` / `processing.16`：`score_metric="CER"`、
`score_value`、`score_direction="lower_is_better"`、`score_threshold`、`score_field`；
`clip_score=null`，`bucketed_by="audio_13"`。当前 #13 的 `cer` 是字符口径，存在有效同音容错时
按原实现取字符与 `edit_distance_ratio_homophone` 的较小值；显式最终指标优先。
视频重分代表帧写 `video_frame_reference.path`，不写 `image_reference`；后者仅是图像引用。
媒体携带读取 image/audio/video 与其 reference/path 别名及帧数组，忽略整数 `frames` 计数。

其他被兼容的输入形态：

- **上游台账包裹**：顶层是 #12 的隔离/异常台账条目，内含 `record`（候选对）。此时读取顶层
  `label`/`tier`/`reason`/`destination`，`label == "样本-语义不通"` 或 `tier == "isolated"`
  直接沿用为不对齐。
- **扁平单记录**：本身含 `sample_id` + `text.content` + `image.path`（无 `pair_id` 时内部编号
  `single-<sample_id>`）。
- **抽检命中**：`metadata.sampling_hit = true` 或 `sampling.hit = true` 或
  `processing.24.sampling_hit`。命中即强制进入人工队列（即使分数 ≥ 0.32）。
- **人工否决锁定**：`alignment.whole.human_veto_locked = true`，或
  `confirmation_method == "human"` 且 `decision == "not_aligned"`。
- **不适用**：候选对不含整体配对（无 image/video 与 text 的内部/交叉组合，如音文）→
  不适用台账，留产线不隔离。`video+text` 走视文路线，**不再**进不适用台账。

### 1.2 人工回写事件（可选旁路输入）

随素材包一起上传，文件名按候选顺序探测：`16_human_events.jsonl`、`human_events.jsonl`、`16_review.jsonl`。
记录内联形态 `metadata.human_review` / `alignment.whole.human_review` / `processing.16.human_review` 也支持。

```json
{
  "pair_id": "pair-t14--i14",
  "decision": "aligned | not_aligned | uncertain",
  "reviewer_id": "ann-04",
  "reviewed_at": "2026-10-07T09:30:00+08:00",
  "review_task_id": "16-sample_task-pair-t14--i14",
  "previous_decision": "pending_manual_review",
  "comment": "人工确认",
  "new_description": "人工改写后的描述（可选）"
}
```

校验规则（任一不满足即入审计台账，不静默丢弃）：

| reason | 含义 |
|---|---|
| `human_event_missing_target` | 既无 `pair_id` 也无 `sample_id` |
| `human_event_missing_reviewer` | 缺 `reviewer_id`（人工权限校验） |
| `human_event_missing_reviewed_at` | 缺 `reviewed_at` |
| `human_event_unknown_decision` | `decision` 不是 对齐/不对齐/存疑 |
| `human_event_invalid_description` | `new_description` 非字符串或空白 |
| `human_event_no_action` | 既无结论也无改写描述 |
| `human_event_target_not_found` | 匹配不到任何输入记录 |
| `human_event_encoding_error` / `_json_parse_error` / `_record_not_object` | 行本身坏 |

同键多条事件按 `reviewed_at` 取最新，被覆盖的写入审计台账。

## 2. 输出

运行目录：`OUTPUT_DIR/<uniqueId>/16_whole_alignment/runNNN/`

| 文件 | 内容 | 记录数 |
|---|---|---|
| `16_whole_alignment.jsonl` | 主清单（自动对齐/人工对齐/待人工/存疑） | statistics.output |
| `16_review.jsonl` | 人工队列台账（中间分、抽检命中、存疑） | statistics.review |
| `16_isolation.jsonl` | 样本隔离台账（不对齐、人工否决） | statistics.isolated |
| `16_not_applicable.jsonl` | 不适用台账（无 image/video 与 text 组合） | statistics.not_applicable |
| `16_rejects.jsonl` | 原始文件类异常台账（坏行/缺字段/重复/媒体问题） | statistics.rejected |
| `16_audit.jsonl` | 人工事件审计台账（生效/非法/覆盖/未匹配） | statistics.audit |
| `manifest.json` | 入口、产物清单、计数、媒体携带、统计 | — |
| `config.snapshot.json` | 生效配置快照 | — |
| `run.log` | 规范日志（只记动作与真实数量） | — |
| `media/*` | 本次真实引用并携带的媒体（相对路径） | statistics.media_carried |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py`、`utils/mty_compliance.py` 的血缘/归档接口，代码保留存档；`utils/transport.py` 仅保留 `crossmodal-files-v1` 文件对象与 `manifest.json` 打包能力）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 主清单记录追加字段

保留上游全部字段，仅追加本算子命名空间：

```json
{
  "alignment": {
    "whole": {
      "decision": "auto_aligned|manual_aligned|not_aligned|uncertain|pending_manual_review",
      "clip_score": 0.41,
      "score_source": "image_text_score|video_text_score|processing.14|rescore|alignment.whole|processing.12|alignment.image_text|top_level.*|none",
      "threshold_review": 0.20,
      "threshold_auto": 0.32,
      "threshold_source": "upstream|config",
      "confirmation_method": "auto_threshold|human|rescore|temporal_15",
      "bucketed_by": "upstream_score|rescore|temporal_15|none",
      "human_verified": false,
      "rescored": false,
      "rescore_status": null,
      "model": "CLIP-ViT/B-32",
      "model_version": "ViT-B-32",
      "review_task_id": null,
      "reviewer_id": null,
      "reviewed_at": null,
      "previous_decision": null,
      "comment": null,
      "human_veto_locked": false,
      "sampling_hit": false,
      "evidence": {
        "score_source": "image_text_score",
        "upstream": {"origin": "12|14", "source": "image_text_score|video_text_score|processing.14",
                     "tier": "auto_aligned", "isolated": false, "reason": "no_upstream_score"},
        "sampling": {"hit": false, "source": null},
        "human_event": null,
        "temporal": null,
        "rescoring": null,
        "label": "样本-评分：通过",
        "action": "keep_in_pool",
        "destination": "样本池",
        "reason": "score_above_auto_threshold",
        "notes": []
      }
    },
    "local": []
  },
  "processing": {
    "16": {
      "status": 1,
      "process_status": "auto_aligned",
      "reason": "score_above_auto_threshold",
      "operator": "16_whole_alignment",
      "operator_version": "v1.0.0",
      "label": "样本-评分：通过",
      "action": "keep_in_pool",
      "destination": "样本池",
      "event_time": "2026-10-07T10:00:00+08:00",
      "batch_id": "batch-16",
      "retry_count": 0,
      "clip_score": 0.41,
      "score_source": "image_text_score",
      "human_verified": false,
      "rescored": false,
      "line": 1
    }
  },
  "image_reference": {"side": "right", "sample_id": "i01", "path": "media/cat.png"}
}
```

人工改描述时追加 `alignment.whole.description_edit` 与 `metadata.human_description`：

```json
{
  "description_edit": {
    "old_description_summary": "原描述（≤30 字）",
    "new_description_ref": {"sha256": "...", "length": 21, "preview": "前 40 字"},
    "score_before": 0.30,
    "score_after": 0.42,
    "model_version": "ViT-B-32",
    "status": 1,
    "process_status": "ok|model_unavailable|inference_failed: *|empty_embedding|no_representative_frame"
  },
  "metadata": {
    "human_description": {"content": "人工改写后的完整描述", "reviewer_id": "ann-04",
                          "reviewed_at": "...", "sha256": "..."}
  }
}
```

> `manifest.json` 与日志只保存描述的哈希/长度/短预览；完整新描述只写入主清单的记录字段
> （`metadata.human_description.content`），不写日志。
> 视文路线的 `image_reference` 只在「人工改写描述重评分」时出现，`path` 是本次使用的
> 代表帧（`video.segments[].frames` 或 `video.frames` 的中间帧）；常规视文分档不读媒体，
> 不会产生 `image_reference`。无帧时 `process_status="no_representative_frame"`，
> `score_after=null`。

### 2.2 台账条目字段（review / isolation / not_applicable / rejects / audit）

```json
{
  "line": 12, "pair_id": "pair-t12--i12", "sample_id": "t12",
  "reason": "human_not_aligned",
  "label": "样本-语义不通",
  "stage": "数据加工", "operator": "16_whole_alignment", "operator_label": "#16",
  "action": "isolate", "destination": "样本隔离库",
  "event_time": "2026-10-07T10:00:00+08:00",
  "batch_id": "batch-16", "retry_count": 0,
  "evidence": {"decision": "...", "clip_score": 0.41, "score_source": "...",
               "confirmation_method": "human", "human_verified": true,
               "rescored": false, "thresholds": {"review": 0.20, "auto": 0.32},
               "stage": "样本业务状态"},
  "record": { "...": "主清单同结构记录（review/isolation 才有）" },
  "entry_kind": "human_queue | sample_isolation | not_applicable"
}
```

`16_rejects.jsonl` 额外带 `source_reference`，标签来自 `原始文件-*` 系列，去向 `原始隔离区`：

| reason | label | destination |
|---|---|---|
| `encoding_error` | 原始文件-编码异常 | 原始隔离区 |
| `json_parse_error` / `record_not_object` / `media_path_invalid` | 原始文件-无法解析 | 原始隔离区 |
| `missing_field: pair_id` / `duplicate_pair_id` / `missing_side` | 原始文件-缺字段 | 原始隔离区 |
| `media_missing` / `media_unreadable` | 原始文件-损坏 | 原始隔离区 |

`16_audit.jsonl` 三种 `kind`：`human_event`（生效）、`human_event_rejected`（非法/未匹配）、
`human_event_superseded`（被更晚事件覆盖）。

## 3. 结论与去向（表1 冻结）

| decision | label | action | destination | 去向算子 |
|---|---|---|---|---|
| `auto_aligned` | 样本-评分：通过 | keep_in_pool | 样本池 | #19 |
| `manual_aligned` | 样本-评分：通过 | keep_in_pool | 样本池 | #19 |
| `pending_manual_review` | 样本-评分：待人工 | queue_manual_review | 人工队列 | 人工标注/#24 |
| `uncertain` | 样本-评分：待人工 | queue_manual_review | 人工复核队列(#25) | #25 |
| `not_aligned` | 样本-语义不通 | isolate | 样本隔离库 | #21 |
| 不适用 | 流程-不适用整体对齐 | pass_through | 留产线（样本池） | 留产线 |

阈值：`< 隔离线`（默认 0.20）不对齐并隔离；`隔离线 ≤ score < 0.32` 待人工；`≥ 0.32` 自动对齐。

## 4. 统计口径（`statistics`）

`input`、`blank_lines`、`valid`、`applicable`、`not_applicable`、`bad_lines`、
`structural_rejects`、`auto_aligned`、`manual_aligned`、`pending_manual_review`、`uncertain`、
`not_aligned`、`output`、`review`、`isolated`、`rejected`、`audit`、`review_entries`、
`audit_entries`、`reject_entries`、`human_events_total/applied/rejected/superseded/unmatched`、
`rescored`、`rescore_requested`、`rescore_deferred`、`sampling_hits`、`veto_locked`、
`media_carried`、`media_missing`、`models_available`、`degraded`、`degradation_reason`、
`thresholds`、`route_enabled`、`model`、`conservation_ok`、`conservation_failures`、`empty_input`。

守恒式（全部成立时 `conservation_ok = true`，否则逐条列在 `conservation_failures`）：

```
input         == 主清单 + 隔离 + 不适用 + 异常
input         == valid + bad_lines + structural_rejects
valid         == applicable + not_applicable
applicable    == auto + manual + pending + uncertain + not_aligned
主清单        == auto + manual + pending + uncertain
隔离          == not_aligned
人工队列      == pending + uncertain
reject_entries== bad_lines + structural_rejects
```

> 说明：`structural_rejects` 中的记录只进 `16_rejects.jsonl`，不重复计入主清单；
> 不对齐样本同时出现在主清单**之外**的隔离台账，因此计数不会互相污染。
