# 算子 18「标注局部对局部」数据契约

对应表1 第 18 行（S3 数据加工阶段，可选节点，默认关闭）。
本文件描述的字段与实际输出**一致**；与共享 `demo/DATA_SCHEMA.md` 无冲突，只是在其基础上
补齐本算子的命名空间与台账结构。

---

## 1. 输入契约

输入是一份 JSONL，每行一条记录。算子按下列优先级从记录中取值，
**不读取自己写出的 `processing["18"]` / `operator_18`**（避免自引用）。

### 1.1 样本标识（必需）

| 取值顺序 | 字段 | 说明 |
| --- | --- | --- |
| 1 | `sample_id` | 单样本记录 |
| 2 | `pair_id` | #11 成对记录的标识 |

两者都没有 → 该行进**原始隔离区**（`reason = "missing_field: sample_id"`）。
同一输入文件内标识重复 → 第 2 条起进**原始隔离区**（`reason = "duplicate_identifier"`）。

### 1.2 描述文本（表1：「描述」）

| 优先级 | 路径 | `description_source` |
| --- | --- | --- |
| 1 | `text.content` | `text.content` |
| 2 | `text.description` | `text.description` |
| 3 | `text.caption` | `text.caption` |
| 4 | `text`（字符串） | `text` |
| 5 | `description` | `description` |
| 6 | `caption` | `caption` |
| 7 | `metadata.description` | `metadata.description` |
| 8 | `metadata.caption` | `metadata.caption` |
| 9 | `annotations.description` | `annotations.description` |

- 全部缺失 → **流程状态**（`missing_description`），留产线并进人工确认队列；
- 描述整体等于该记录图像的文件名/主干/扩展名 → 视为「文件名当词」，
  记为 `description_is_filename`，最终按第 5 节判为**样本-语义不通**。

### 1.3 图像引用（表1：「图像」）

| 优先级 | 路径 |
| --- | --- |
| 1 | `image`（字符串或 `{"path"｜"file"｜"name"｜"relative_path"}`） |
| 2 | `image_path` |
| 3 | `media.image` |
| 4 | `annotations.image` |
| 5 | `processing.17.paths.image` |
| 6 | `processing.17.path` |
| 7 | `processing.17.image` |

路径必须是**相对任务目录**的 posix 形式：禁止绝对路径、盘符、`~`、`\`、`http(s)://`、
`../` 穿越 → 违规进**原始隔离区**（`unsupported_media_path`）。
引用合法但文件不存在 → **原始隔离区**（`image_missing`）。
完全没有图像字段 → **流程状态**（`no_image_reference`），留产线。

### 1.4 局部候选框（表1：「#17 局部框」）

按优先级取第一个非空列表：

| 优先级 | 路径 |
| --- | --- |
| 1 | `regions` |
| 2 | `boxes` |
| 3 | `local_regions` |
| 4 | `candidate_boxes` |
| 5 | `image.regions` / `image.boxes` |
| 6 | `annotations.regions` |
| 7 | `processing.17.regions` / `.boxes` / `.local_regions` / `.candidate_boxes` |

**框的写法（均支持）**

```jsonc
// A. 规范写法（与共享 schema 一致，推荐）
{"box_id": "b1", "bbox": {"x": 2, "y": 2, "w": 30, "h": 24, "coord": "xywh-pixel"},
 "class_name": "dog", "confidence": 0.91}

// B. 像素 xyxy
{"box_id": "b1", "bbox": [4, 4, 36, 30]}

// C. 归一化
{"box_id": "b1", "bbox": [0.05, 0.10, 0.80, 0.90], "coord": "xyxy-normalized"}

// D. 扁平写法
{"id": "b9", "x": 0, "y": 0, "w": 5, "h": 6}
```

- 未声明 `coord`/`normalized` 时**按像素处理**（与共享 schema 的 `xywh-pixel` 一致），
  不做数值范围猜测，避免把小像素框误判为归一化坐标；
- 规范化输出固定为 `bbox = {x, y, w, h, coord}`，`coord ∈ {xywh-pixel, xyxy-pixel,
  xywh-normalized, xyxy-normalized}`；
- `box_id` 缺失时按顺序生成 `box-001`、`box-002`…（稳定、可追溯）；
- 非法框（宽高非正、坐标非数值、NaN/Inf、原点为负）会被丢弃并计入 `boxes_invalid`，
  **不静默忽略**（计数进统计，记录仍可继续）；
- 无可用框（原本为空或全部非法）→ **流程状态**（`no_boxes`），按研发测边界
  「没有框时给人工空白列表」处理，`relations = []`。

### 1.5 上游分词（可选，表1：「复用预训练文本产线」）

| 优先级 | 路径 |
| --- | --- |
| 1 | `text.tokens` / `text.words` |
| 2 | `tokens` |
| 3 | `processing.04.tokens` / `processing.4.tokens` |
| 4 | `processing.03.tokens` / `processing.3.tokens` |

`tokenizer: pretrained` 时优先复用这些词；缺失或过滤后为空 → 回退结巴分词。
上游词与结巴词都要再过同一套过滤：文件名/主干/标点、停用词、数词/量词，
有 `jieba.posseg` 时按词性剔除代词/介词/连词/助词等；剔除原因分类计入
`tokens_dropped_detail`。`merge_phrases: true` 时，相邻「修饰语 + 名词」会合并成
名词短语，作为该词的上下文变体参与打分（`phrase` 仍是词，一词一框不变）。

### 1.6 成对结构（#11 输出）

```jsonc
{"pair_id": "pair-0008",
 "left":  {"sample_id": "s-text",  "record": {"text": {"content": "小狗在草地上奔跑"}}},
 "right": {"sample_id": "s-image", "record": {"image": "media/shot_001.png", "regions": [...]}}}
```

两侧都会被拍平后按 1.2–1.5 的优先级取值。

---

## 2. 输出契约

### 2.1 主输出

| 文件 | 何时写出 |
| --- | --- |
| `18_phrase_region_annotations.jsonl` | `enabled: true` |
| `18_passthrough.jsonl` | `enabled: false`（默认） |

每条记录**保留全部原字段**，并追加：

```jsonc
{
  "sample_id": "enabled-0001",
  "text": {"content": "金毛犬在草地上接飞盘"},
  "image": "media/shot_001.png",
  "regions": [ /* 原样保留 */ ],

  "processing": {
    "18": { /* 权威节点，见 2.2 */ }
  },

  "phrase_box_relations": [ /* 与 processing["18"].relations 相同，便于下游直接消费 */ ],

  "operator_18": {           // 扁平摘要：状态/证据/版本/原因，便于快速筛选
    "operator_number": 18,
    "operator_version": "v1.0.0",
    "stage_id": "S3",
    "status": 1,
    "process_status": "prefilled",
    "decision": "prefill",
    "reason_code": "auto_prefill",
    "evidence": {
      "relations_total": 3, "relations_prefilled": 3, "boxes_used": 3,
      "best_score": 0.842315, "min_clip_score": 0.25,
      "clip_model": "CLIP-ViT/B-32", "tokenizer_source": "pretrained"
    },
    "human_verified": false,
    "review_required": true,
    "deliverable": false,
    "source_reference": "operator_18"
  }
}
```

> v4.2 记录级 `status` 为 int（1=正常/预填/待人工/降级/跳过，0=隔离/失败），原字符串在
> `process_status`；`operator_18`、`processing["18"]`、关系与台账条目一致。

> `human_verified` 与 `deliverable` **恒为 `false`**：本算子未接入人工复核平台，
> 因此没有「已确认」的关系，未确认关系不得交付。

### 2.2 `processing["18"]` 节点

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | int | 记录级结论：1=正常/预填/待人工/降级/跳过，0=隔离/失败 |
| `process_status` | string | 原字符串流程态：`prefilled` / `pending_manual` / `degraded` / `skipped` / `isolated` |
| `route_enabled` | bool | 本次运行是否开启（配置生效值） |
| `action` | string | `skip` / `prefill` / `pending` / `isolate` / `origin` |
| `reason` | string | 见 2.6 原因码 |
| `label` | string | 表1 中文标签 |
| `destination` | string | 去向（见 2.5） |
| `correspondence_found` | bool | 是否建立了词-区域对应 |
| `prefilled` | bool | 是否产出 ≥1 条达阈值的预填关系 |
| `degraded` | bool | 是否处于显式降级 |
| `clip_model` / `clip_model_version` | string | 固定 `CLIP-ViT/B-32` / 后端版本 |
| `min_clip_score` | float | 本次生效阈值 |
| `tokenizer_mode` | string | 配置项：`pretrained` / `jieba` |
| `tokenizer_source` | string\|null | 实际来源：`pretrained` / `jieba` / `rule_fallback` |
| `tokenizer_backend` | string\|null | `pretrained-text-pipeline` / `jieba` / `rule` |
| `token_count` / `tokens_dropped` / `tokens_truncated` | int/int/bool | 分词真实计数 |
| `tokens_dropped_detail` | object | 剔除原因分类计数：`filename` / `function_word` / `stopword` / `numeric` / `symbol` / `empty` / `duplicate` |
| `phrases_total` / `phrases_used` / `token_variants` | int | 名词短语数 / 实际以短语得分的关系数 / 不同匹配文本数 |
| `crop_padding_ratio` / `background_suppression` | float/string | 本次生效的裁图扩边比例与背景抑制方式（默认 `0.0` / `none`） |
| `class_profile` | object | 按检测类别聚合：`{class: {relations, prefilled, best_score}}`，仅作校准证据，不改阈值 |
| `boxes_received` / `boxes_used` / `boxes_invalid` | int | 候选框计数（含被丢弃的非法框） |
| `crops_scored` / `crops_failed` | int | 裁图编码成功/失败数 |
| `relations` | array | 词-区域关系（见 2.3） |
| `relations_total` / `relations_prefilled` / `relations_pending` | int | 关系计数 |
| `best_score` | float\|null | 本次最高余弦分 |
| `image_path` / `image_source` / `description_source` / `boxes_source` | string\|null | 取值溯源 |
| `model` | object\|null | 后端身份；降级时为 `null` |
| `human_verified` / `review_required` / `deliverable` | bool | 恒 `false`/`true`/`false` |
| `scored_at` / `detail` | string\|null | 打分时间 / 人类可读说明 |

### 2.3 `phrase_box_relations[]` 条目

```jsonc
{
  "phrase": "金毛犬",                 // 词片段（1.5/分词产出，绝不含文件名）
  "box_id": "b1",                     // 候选框 ID
  "bbox": {"x": 2, "y": 2, "w": 30, "h": 24, "coord": "xywh-pixel"},
  "class_name": "dog",                // #17 的类别（若有）
  "clip_score": 0.842315,             // 词-区域 CLIP 余弦（6 位小数）
  "best_score": 0.842315,             // 该词对可用框的最高分（未配对时仅作参考）
  "matched_text": "金毛犬",            // 该分数实际使用的文本：词本身或其所在名词短语
  "variant": "word" | "noun_phrase",  // 匹配文本类型
  "phrase_tokens": ["金毛犬"],         // 名词短语覆盖的词；word 变体即 [phrase]
  "confirmation_method": "auto_prefill" | "pending_manual_review",
  "status": 1,                        // 1=预填/待人工；隔离/失败为 0
  "process_status": "prefilled" | "pending_manual",
  "reason": null | "below_threshold" | "no_available_region",
  "human_verified": false,
  "review_required": true,
  "source_reference": "operator_18"
}
```

`phrase` 恒为词，一词一框；`matched_text` 是实际参与余弦的文本（`variant=word` 时
等于 `phrase`，`variant=noun_phrase` 时是覆盖该词的名词短语），便于人工核对分数来源。

一对一约束：每个 `box_id` 最多被一个词占用（按分数降序贪心）；
词多于框时，未配对的词保留 `box_id = null`、`reason = "no_available_region"`，
并用 `best_score` 记录其最高候选分，交由人工决定。

### 2.4 台账文件

| 文件 | 内容 | 角色 |
| --- | --- | --- |
| `18_pending_manual.jsonl` | 待人工确认台账（`prefill` 与 `pending` 两类记录各一条） | `pending` |
| `18_sample_quarantine.jsonl` | **样本隔离库**：`样本-语义不通`，条目内附 `record`（超 32KB 截断） | `quarantine` |
| `18_rejects.jsonl` | **原始隔离区**：坏行、非对象、缺标识、标识重复、图像缺失/损坏/路径非法 | `rejects` |
| `18_run.log` | 逐行日志（时间 / 动作 / 真实数量） | `log` |
| `manifest.json` | 运行清单（`crossmodal-manifest-v1`） | `sidecar` |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py`、`utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

台账条目结构：

```jsonc
{
  "line": 12, "identifier": "sample-0001", "sample_id": "sample-0001", "pair_id": null,
  "operator": "phrase_region_annotation", "operator_number": 18,
  "operator_version": "v1.0.0", "stage_id": "S3",
  "status": 0, "process_status": "isolated", "action": "isolate", "reason": "no_valid_tokens",
  "label": "样本-语义不通", "destination": "样本隔离库",
  "human_verified": false, "review_required": true, "deliverable": false,
  "detail": "工具开启后描述无法产生任何有效词（或描述即文件名），不存在词-区域对应关系",
  "evidence": { "token_count": 0, "boxes_used": 0, "best_score": null, /* … */ },
  "detected_at": "2026-10-07T10:18:14+08:00",
  "source_reference": "operator_18"
}
```

### 2.5 分诊口径（表1 质量处置）

| 判定 | `label` | `destination` | `action` |
| --- | --- | --- | --- |
| 工具关闭 | `流程-局部对局部未启用` | 产线待处理 | `skip` |
| 有 ≥1 条达阈值关系 | `样本-评分：待人工` | 人工确认队列 | `prefill` |
| 有框有词但全部低于阈值 | `流程-待人工确认` | 人工确认队列 | `pending` |
| 无可用候选框（空白人工列表） | `流程-无候选框` | 人工确认队列 | `pending` |
| 无描述 | `流程-待补描述` | 人工确认队列 | `pending` |
| 无图像引用 | `流程-无图像` | 人工确认队列 | `pending` |
| 模型不可用（显式降级） | `流程-待人工确认` | 人工确认队列 | `pending` |
| 打分异常 | `流程-待人工确认` | 人工确认队列 | `pending` |
| 开启后没有对应关系（无有效词） | `样本-语义不通` | **样本隔离库** | `isolate` |
| 图像缺失 / 引用非法 | `原始文件-无法解析` | **原始隔离区** | `origin` |
| 图像损坏（全部裁图不可读） | `原始文件-损坏` | **原始隔离区** | `origin` |
| 缺标识 / 标识重复 | `原始文件-缺字段` / `原始文件-重复标识` | **原始隔离区** | `origin` |
| 坏行 / 非对象 / 编码错误 | `原始文件-无法解析` / `原始文件-编码异常` | **原始隔离区** | `origin` |

### 2.6 原因码

`route_disabled`、`missing_description`、`description_is_filename`、`no_image_reference`、
`unsupported_media_path`、`image_missing`、`image_unreadable`、`no_boxes`、
`no_valid_tokens`、`below_threshold`、`no_available_region`、`model_unavailable`、
`score_error`、`duplicate_identifier`、`json_parse_error`、`record_not_object`、
`encoding_error`、`missing_field: sample_id`。

---

## 3. statistics 字段与守恒

```jsonc
{
  "route_enabled": true,
  "input_records": 9, "valid_records": 9, "blank_lines": 0, "bad_lines": 0,
  "prefilled_records": 0, "pending_manual_records": 6, "skipped_records": 0,
  "sample_isolated": 1, "origin_quarantined": 2,
  "isolated_no_correspondence": 1, "scored_records": 0,
  "no_boxes": 1, "below_threshold": 0, "missing_description": 1,
  "no_image_reference": 0, "model_unavailable": 4, "score_error_records": 0,
  "tokens_total": 3, "tokens_dropped": 0,
  "relations_total": 0, "relations_prefilled": 0, "relations_pending": 0,
  "boxes_received": 4, "boxes_used": 0, "boxes_invalid": 1,
  "crops_scored": 0, "crops_failed": 0,
  "media_carried": 4, "images_carried": 4,
  "pending_entries": 6, "rejects_entries": 2, "quarantine_entries": 1,
  "deliverable_records": 0, "human_verified_records": 0,
  "tokenizer_source": "pretrained", "tokenizer_backend": "pretrained-text-pipeline",
  "models_available": false, "model_source": "unavailable",
  "clip_model": "CLIP-ViT/B-32", "min_clip_score": 0.25,
  "tokenizer_mode": "pretrained", "max_tokens": 64,
  "degraded": true, "encoding_error": false, "file_read_incomplete": false,
  "empty_input": false, "conservation_ok": true, "conservation_failures": []
}
```

**守恒等式**（`conservation_ok = not failures`，`failures` 里是未满足的等式名）

1. `input_records == valid_records + bad_lines`
2. `valid_records == origin_quarantined + sample_isolated + pending_manual_records
   + prefilled_records + skipped_records`
3. `sample_isolated == isolated_no_correspondence`
4. `pending_manual_records == no_boxes + below_threshold + missing_description
   + no_image_reference + model_unavailable + score_error_records`
5. `scored_records == prefilled_records + below_threshold`
6. `relations_total == relations_prefilled + relations_pending`

> `boxes_received / boxes_used / boxes_invalid / crops_scored / crops_failed` 是**诊断计数**，
> 不含在守恒等式内（例如「无有效词被隔离」的记录也会统计 `boxes_invalid`，
> 但不会进入打分阶段，因此不存在 `received == invalid + failed + used` 的全局等式）。

`bad_lines` 只统计**行级读取失败**（非法 JSON、非对象）；缺字段、标识重复、
媒体问题等属**记录级**问题，计入 `origin_quarantined` 与 `rejects_entries`。

---

## 4. 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、模型身份、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

`isLastOperator` 仍必传，用于标记阶段末调用；v4.2 起中间与阶段末响应同构，均返回 `outputFile` + `result`，不再用 `false` 只回血缘对象。

---

## 5. 与表1 的对应关系（可核查）

| 表1 #18 条目 | 实现位置 |
| --- | --- |
| 默认关闭 | `configs/config.yaml` `enabled: false`；关闭时不加载模型、不搬运媒体、不产细粒度对应 |
| 模型 CLIP-ViT/B-32（复用） | `utils/backends.py`（与 #12/#14 同配方，按签名缓存复用实例） |
| 复用 #17 局部框 | `utils/phrase_region.resolve_candidate_boxes` |
| 分词复用预训练产线，否则结巴 | `utils/tokenization.tokenize_description` |
| 阈值才预填，否则待人工 | `utils/scoring.meets_threshold` + `utils/phrase_region.evaluate_record` |
| 词片段 / 候选框ID / CLIP分数 / confirmation_method | `phrase_box_relations[]` |
| 一对一关系 | `utils/scoring.assign_one_to_one` |
| 文件名不能当词 | `utils/tokenization.forbidden_tokens` |
| 没有人工确认不能交付 | `deliverable_records = 0`、`human_verified = false` 恒成立 |
| 不新建多语言模型 | 仅复用 CLIP 与中文分词（结巴/预训练产线），无新增模型 |
| 开启后没有对应关系 → 样本隔离库 | `no_valid_tokens` → `18_sample_quarantine.jsonl` |
| 关闭：不拦、留样本池、不产细粒度对应 | 关闭分支只写透传文件，`relations = []` |
