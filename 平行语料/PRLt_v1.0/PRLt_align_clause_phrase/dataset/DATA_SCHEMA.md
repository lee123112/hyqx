# 算子 11 数据 Schema（子句与短语级对齐）

## 输入：句子级句对（默认 `10_sentence_pairs.jsonl`）

上游算子 10 句子级对齐的通过集，逐行一条句子级句对。阶段内实际路径由 `/predict` 的
`inputFile` 指定（取上一中间算子返回的 `data.outputFile`）；默认文件名仅用于
CLI 与本地调试。

```json
{
  "pair_id": "pair-0001",
  "source": "数据清洗很重要。",
  "target": "Data cleaning is important.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {
    "granularity": "sentence",
    "source_ref": "para-001-sent-001",
    "target_ref": "para-001-sent-001",
    "confidence": 0.92
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `pair_id` | string | 父句对标识；兼容上游 `id`（`pair_id_fields: [pair_id, id]`） |
| `source` / `target` | string | 待切分的源/目标句子，必填且为非空字符串 |
| `source_lang` / `target_lang` | string | 声明语言；`language_check=true` 时必须与配置方向一致（默认 `zh → en`） |
| `metadata` | object | 允许缺失；父句引用取 `metadata.source_ref`/`target_ref`（回退顶层同名字段） |
| `metadata.confidence` | float | 父句置信度，作为 `parent` 分项；也可用 `alignment_confidence` 或顶层同名字段；缺失时为 `null` |

非法输入不中断流程，按行写入异常清单：`invalid_json`、`invalid_record`（非对象）、
`missing_field:<字段>`、`invalid_type:<字段>`、`schema_error:missing_source_ref`、
`schema_error:missing_target_ref`、`language_declaration_mismatch`、`empty_source`、
`empty_target`、`split_failed:empty_source`、`split_failed:empty_target`。

## 输出：`11_clause_pairs.jsonl`（通过集，逐行一条子句/短语对齐关系）

```json
{
  "id": "pair-0001-clause0001",
  "source": "数据清洗很重要。",
  "target": "Data cleaning is important.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {
    "granularity": "clause_or_phrase",
    "unit_type": "clause",
    "alignment": "1-1",
    "confidence": 0.911,
    "scores": {"semantic": null, "dictionary": 1.0, "position": 1.0,
               "length": 0.727273, "parent": 0.92},
    "evidence": {"source_terms": ["数据清洗"], "expected_targets": ["data cleaning"],
                 "target_hits": ["data cleaning"], "shared_tokens": []},
    "backend": "rule_degraded",
    "score_semantics": "rule_only",
    "model_name": null,
    "model_version": null,
    "device": null,
    "source_ref": "para-001-sent-001-clause-001",
    "target_ref": "para-001-sent-001-clause-001",
    "source_refs": ["para-001-sent-001-clause-001"],
    "target_refs": ["para-001-sent-001-clause-001"],
    "parent_pair_id": "pair-0001",
    "parent_source_ref": "para-001-sent-001",
    "parent_target_ref": "para-001-sent-001",
    "parent_confidence": 0.92,
    "unit_counts": {"source": 7, "target": 4},
    "used_weights": {"dictionary": 0.2, "position": 0.1, "length": 0.15, "parent": 0.1},
    "window": 3,
    "min_confidence": 0.5,
    "review_status": "auto_pass",
    "status": "degraded",
    "low_confidence_reason": null,
    "parent_metadata": {"granularity": "sentence", "source_ref": "para-001-sent-001"}
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | `{pair_id}-clause{父句内序号:04d}` |
| `source` / `target` | string | 1-1 时为单个子句；1-2/2-1 时为合并后的原文（空格拼接，不改写内容） |
| `metadata.granularity` | string | 固定 `clause_or_phrase` |
| `metadata.unit_type` | string | `clause`（主谓宾核心片段）或 `phrase`（整段命中词典词条） |
| `metadata.alignment` | string | `1-1` / `1-2` / `2-1` |
| `metadata.confidence` | float | 仅由实际可用分项加权归一得到 |
| `metadata.scores.semantic` | float \| null | 模型真实余弦相似度；`rule_degraded` 下恒为 `null`（不把长度分命名为语义分） |
| `metadata.scores.dictionary` | float \| null | 词典命中：源词条译文在目标片段中的覆盖率；源片段无词条命中时为 `null` |
| `metadata.scores.position` | float | 片段在父句内的归一化位置一致性：`1 - \|pos_src - pos_tgt\|` |
| `metadata.scores.length` | float | 计量单元 Dice：`2*min/(a+b)`，单元 = 汉字数 + 拉丁词数 + 数字串数 |
| `metadata.scores.parent` | float \| null | 父句置信度；上游缺失时为 `null` |
| `metadata.evidence` | object | 源词条、期望译文、目标命中译文、共有硬 token |
| `metadata.backend` | string | `model` 或 `rule_degraded` |
| `metadata.score_semantics` | string | `model`（有真实语义分）或 `rule_only`（仅规则分项） |
| `metadata.model_version` | string \| null | 真实模型版本（含权重 commit）；无模型为 `null` |
| `metadata.source_ref` / `target_ref` | string | 子句引用 `para-003-sent-001-clause-002` |
| `metadata.source_refs` / `target_refs` | array | 合并片段包含的全部子句引用 |
| `metadata.parent_pair_id` | string | 父句对 `pair_id`，用于回溯上游 |
| `metadata.parent_source_ref` / `parent_target_ref` | string | 父句引用 |
| `metadata.used_weights` | object | 本次置信度真正使用的权重（缺失分项剔除后重新归一） |
| `metadata.review_status` | string | `auto_pass`（≥ `review_threshold`）或 `needs_review` |
| `metadata.status` | string | 通过集为 `success`（模型后端）或 `degraded`（规则降级） |
| `metadata.low_confidence_reason` | string \| null | 通过集恒为 `null` |
| `metadata.parent_metadata` | object | 上游原始 `metadata`，原样保留便于追溯 |

## 异常输出：`11_clause_rejects.jsonl`

切分失败、词典无证据、冲突、低置信和模型不可用记录逐行列出。

```json
{
  "line": 4,
  "id": "pair-0004-rej0000",
  "pair_id": "pair-0004",
  "source_ref": "para-004-sent-001-clause-001",
  "target_ref": null,
  "source": "对齐结果的可解释性很重要。",
  "target": null,
  "source_refs": ["para-004-sent-001-clause-001"],
  "target_refs": [],
  "reasons": ["unaligned_source"],
  "alignment": null,
  "confidence": null,
  "scores": null,
  "low_confidence_reason": null,
  "ambiguity_margin": null,
  "second_alignment": null
}
```

`id` 为拒绝记录顶层标识：`<pair_id>-rej<序号>`（同句对多条拒绝按写出顺序编号）；
输入行本身无记录 id（`invalid_json`/`invalid_record`）时 `id=null`。

`reasons` 取值：`invalid_json`、`invalid_record`、`missing_field:<字段>`、`invalid_type:<字段>`、
`schema_error:missing_source_ref`、`schema_error:missing_target_ref`、
`language_declaration_mismatch`、`empty_source`、`empty_target`、`split_failed:empty_source`、
`split_failed:empty_target`、`no_evidence`、`conflict_multi_candidate`、`low_confidence`、
`unaligned_source`、`unaligned_target`、`model_error`。

- 每个源/目标子句都能在通过集或异常清单找到唯一去向。
- `low_confidence` / `conflict_multi_candidate` 条目额外带 `confidence`、`scores`、`alignment`；
  多候选还带 `ambiguity_margin` 与 `second_alignment`。
- 上游句子级句对的输入层异常（缺字段等）会整条记录写入异常清单，不产生子句对。

## 阶段产物：`artifacts/stage_11_<uniqueId>.zip`

【无血缘改造 2026-10-19】原 `lineage.json`（JSON 数组，按执行顺序追加，含 operator /
operator_version / uniqueId / inputFile / outputFile / status / startedAt / finishedAt /
config / statistics）已停用，任务目录不再产出任何血缘文件。阶段内中间路径通过 `/predict`
返回的 `data.outputFile` 传递；阶段结束前把主输出、异常清单与 `run.log` 打包为
`artifacts/stage_11_<uniqueId>.zip`，响应返回 `stageZip` 与 `storageUri`。

### `statistics` 字段（保留在处理器统计结果与 `run.log` 中）

| 字段 | 说明 |
| --- | --- |
| `input_pairs` | 处理的非空输入行数（含非法行） |
| `input_clauses` | 切分得到的源+目标子句片段总数 |
| `aligned_clauses` | 通过集行数 |
| `rejected_clauses` | 异常清单行数 |
| `conflicts` | 多候选冲突条目数 |
| `low_confidence` / `no_evidence` / `model_error` | 低置信 / 词典无证据 / 模型错误条目数 |
| `unaligned_source` / `unaligned_target` | 未对齐源片段 / 目标片段数 |
| `action:1-1` / `action:1-2` / `action:2-1` | 各动作计数 |
| `reason:<原因>` | 各异常原因计数 |
| `degraded` | 使用规则降级后端时为 1，否则 0 |
| `backend` / `model_version` / `device` | 实际后端、模型版本与运行设备 |
| `window` / `min_confidence` / `encoding` | 生效窗口、阈值与输入编码 |

状态口径：`degraded`（规则后端且有产出）> `partial`（有异常条目）> `success`（无异常）；
配置错误、编码错误、空输入、模型不可用且 `fallback_policy=fail`、模型错误超上限均为
`failed`，`outputFile` 为 `null`。

## 编码约定

- 全链路严格 UTF-8：输入按 `input_encodings`（默认仅 `utf-8`）增量严格解码，失败即 `failed`。
- 输出（通过集、异常清单、阶段产物与日志）统一 UTF-8，逐行流式写入，不使用本地默认编码。

