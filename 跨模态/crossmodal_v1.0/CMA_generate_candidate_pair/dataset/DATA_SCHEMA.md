# 算子 #11 生成候选配对 数据 Schema（v1.0）

> 全局契约见 `跨模态/demo/DATA_SCHEMA.md` 与 `跨模态/demo/DEVELOPMENT_CONTRACT.md`；
> 本文件固定本算子（表1 #11）的输入输出字段，编号为 11，不使用旧编号或旧四接口。

## 1. 输入：准入通过对象 JSONL（来自 #10）

每行一个 dict。必填 `sample_id`；`text`/`image`/`video`/`audio` 至少一个有效。

```json
{
  "sample_id": "sample-0001",
  "source_id": "src-A",
  "batch_id": "batch-01",
  "scene_id": "outdoor",
  "text": {"content": "一只金毛犬在草地上接飞盘", "original": "一只金毛犬在草地上接飞盘",
           "tokens": ["金毛犬", "草地", "飞盘"], "lang": "zh", "time_index": null},
  "image": {"path": "golden_retriever_grass.png", "width": 32, "height": 24, "format": "PNG"},
  "video": null,
  "audio": null,
  "processing": {},
  "alignment": {"whole": null, "local": []},
  "qc": {},
  "metadata": {"image_tags": ["金毛犬", "飞盘"]}
}
```

字段归一化规则：

- `source_id` 取值顺序：`source_id` → `sourceId` → `metadata.source_id` → `metadata.sourceId` → `metadata.source`；
- `scene_id` 取值顺序：`scene_id` → `sceneId` → `group_id` → `groupId` → `metadata` 中同名键 → `metadata.scene`；
- 模态有效性：`null`、空串、空白、空对象/空数组均视为该模态不存在；
- 时间范围来源：`time_range`、`text.time_index`、`text/audio/video.segments[].start/end`、`audio/video.duration`、`video.frames[].timestamp`；
  汇总为 `(min_start, max_end)`；没有任何有效区间记为 `null`（时间不适用），不使用零值填充；
- 关键词来源：`text.tokens` → `text.keywords` → `text.content` → `description` → `metadata.image_tags/keywords/tags/labels` → `audio.transcript`；
  **不使用文件名**。单条字符串按「中文 2–8 字整体词 + 全部二元字组 + 拉丁/数字词长 ≥3」抽取，全小写。
- 未知扩展字段原样保留并随配对透传。

输入引用媒体的相对路径以输入 JSONL 所在目录为根解析，禁止绝对路径与 `..` 逃逸。

## 2. 输出 1：`11_candidate_pairs.jsonl`（主产物）

每个候选对一行；一对多会拆成多条一对一，并共享同一 `group_id`。

```json
{
  "pair_id": "pair-sample-0001--sample-0002",
  "group_id": "grp-src-src-A",
  "modality_pair": ["text", "image"],
  "evidence": {"type": "A", "criterion": "same_source_id", "evidence_source": "rule",
               "value": {"source_id": "src-A"}},
  "evidence_priority": 1,
  "left": {"sample_id": "sample-0001", "modalities": ["text", "image"], "record": {}},
  "right": {"sample_id": "sample-0002", "modalities": ["text", "image"], "record": {}},
  "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0",
               "uniqueId": "task_file"}
}
```

| 字段 | 说明 |
|---|---|
| `pair_id` | `pair-<左sample_id>--<右sample_id>`，确定性且唯一 |
| `group_id` | 同来源 `grp-src-<source_id>` / 同场景 `grp-scene-<scene_id>` / 时间 `grp-time-<播种sample_id>` / 关键词 `grp-kw-<播种sample_id>` |
| `modality_pair` | 两侧模态并集，按 text→image→video→audio 排序 |
| `evidence.type` | 只能是 `A`、`B`、`C` |
| `evidence.criterion` | `same_source_id` / `same_scene_id` / `time_overlap` / `shared_keywords` |
| `evidence.evidence_source` | 固定 `rule`（与兄弟算子 08 同名字段；本算子不含模型证据） |
| `evidence.value` | A：`{"source_id"}` 或 `{"scene_id"}`；B：`{"overlap_seconds","left_range","right_range"}`；C：`{"min_overlap","matched","matched_count","tokenizer"}` |
| `evidence_priority` | A=1、B=2、C=3 |
| `left` / `right` | 两侧 `sample_id`、模态列表和完整原始记录，下游无需回查输入 |

证据 C 的 `tokenizer` 固定为 `upstream_tokens+cjk_bigram_rule`，说明该次判定用的是上游 `tokens` 还是内置规则分词，便于复核阈值口径。

`modality_pair` 两侧模态并集不足 2 种时不会形成候选对（计入 `same_modality_pairs_skipped`），
避免把两个同模态对象冒充跨模态配对。

## 3. 输出 2：`11_unpaired.jsonl`（未配对清单，不隔离）

```json
{"sample_id": "sample-0008", "modalities": ["text", "image"], "source_id": "src-F",
 "scene_id": "lab", "reason": "no_candidate", "tried_evidence": ["A", "B", "C"],
 "record": {}}
```

表1 边界：配不出候选不拦、不进入评分、不写入样本隔离库，留在产线未配对清单。

## 4. 输出 3：`11_rejects.jsonl`（异常/拒收清单）

```json
{"line": 9, "sample_id": null, "reason": "json_parse_error",
 "label": "原始文件-无法解析", "stage": "11", "operator": "generate_candidate_pairs",
 "action": "记录并隔离", "destination": "原始隔离区",
 "event_time": "2026-10-06T15:09:14+08:00", "batch_id": null, "retry_count": 0}
```

`reason` 枚举与台账：

| reason | label | action | destination |
|---|---|---|---|
| `json_parse_error` / `record_not_object` | 原始文件-无法解析 | 记录并隔离 | 原始隔离区 |
| `encoding_error` | 原始文件-编码异常 | 记录并隔离 | 原始隔离区 |
| `missing_field: sample_id` | 原始文件-缺字段 | 记录并隔离 | 原始隔离区 |
| `no_modality` | 流程-无有效模态 | 不配对并记录 | 算子异常清单（不隔离） |
| `duplicate_sample_id` | 流程-重复样本ID | 跳过重复记录 | 算子异常清单（不隔离） |
| `not_admitted` | 流程-未准入 | 不配对并记录 | 算子异常清单（不隔离） |
| `group_pair_limit_exceeded` | 流程-配对上限 | 记录并保留未配对 | 算子异常清单（不隔离） |

坏行只保存物理行号与原因，不保存整行原文。

## 5. 统计字段（`statistics`，全部来自真实结果）

| 字段 | 含义 |
|---|---|
| `input_records` | 非空物理行总数 |
| `valid_records` | 参与配对的有效记录数 |
| `paired_records` / `unpaired_records` | 已配对 / 未配对记录数，二者之和等于 `valid_records` |
| `pairs_total` / `pairs_by_evidence` | 候选对总数与 A/B/C 分布，二者之和相等 |
| `groups_total` | 主产物中不同 `group_id` 数量 |
| `same_modality_pairs_skipped` | 因两侧模态并集不足 2 种而未成对的数量 |
| `group_pair_limit_hits` | 触发 `max_pairs_per_group` 上限的候选数 |
| `rejected_records` / `bad_lines` / `rejects_total` | 业务拒收 / 坏行 / 异常清单总条数 |
| `media_carried` / `media_missing` | 已复制 / 缺失的被引用媒体数 |
| `empty_input` | 是否空输入 |
| `conservation_ok` | 统计守恒校验是否通过 |
| `input` / `output` / `rejected` | 记录级共用口径（与 08/14/21 同名）：`input = output + rejected` |
| `applicable` / `not_applicable` | 记录级共用口径：`output = applicable + not_applicable`（未配对记为 not_applicable） |
| `degraded` / `degradation_reason` | 本算子恒为 `false` / `null`（无模型后端，不存在降级态） |
| `active_backend` | 固定 `rule` |

守恒关系：`input_records = valid_records + rejected_records + bad_lines`；
`rejects_total = rejected_records + bad_lines + group_pair_limit_hits`；
`valid_records = paired_records + unpaired_records`；
`pairs_total = sum(pairs_by_evidence)`；`input = output + rejected`；`output = applicable + not_applicable`。
任一条不成立时 `conservation_ok=false` 并写日志告警，不静默通过。

## 6. 血缘输出（v4.2 停用）

血缘/阶段上下文输出已于 2026-10-19 停用：`/predict` 不再返回 `lineageFile`，运行目录不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`。原血缘中的业务信息（`statistics`、配置快照、统计守恒结果）改由 `data.result`（业务摘要）与 `manifest.json`/`run.log` 承载。

`isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；v4.2 起中间与阶段末响应同构，均返回
`{operator, status, outputFile, result}`，`result` 为 `{runId, runDir, files, statistics, config, stageId}`。

`stageId`：demo 约定 `S3`（跨模态数据对齐标注覆盖 #8–#15），表1 正式环节为「数据加工」。

## 7. 输出文件内容对象（`crossmodal-files-v1`，中间/阶段末同构）

```json
{"format": "crossmodal-files-v1", "entrypoint": "11_candidate_pairs.jsonl",
 "files": [{"path": "11_candidate_pairs.jsonl", "encoding": "utf-8", "content": "..."},
           {"path": "golden_retriever_grass.png", "encoding": "base64", "content": "..."}]}
```

包含：主产物、未配对清单、异常清单、`manifest.json` 与全部被引用媒体（按原相对路径）。
这是 demo 内部约定（`DEVELOPMENT_CONTRACT.md` §4），不是甲方正式二进制协议；正式协议到位时只替换 `utils/transport.py`。
