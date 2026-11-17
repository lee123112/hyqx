# 算子10：句子级对齐 数据结构说明

## 输入：上游段落句对（默认 `09_paragraph_pairs.jsonl`）

算子9 段落级对齐的通过集，逐行一条段落级中英句对。`dataset/sample/demo.jsonl` 按此结构提供 4 条样例。

```json
{
  "id": "para-p0001-t0001",
  "source": "欢迎使用平行语料系统。",
  "target": "Welcome to the parallel corpus system.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {
    "granularity": "paragraph",
    "source_ref": "para-001",
    "target_ref": "para-001",
    "confidence": 0.91,
    "scores": {"length": 0.88, "position": 1.0, "overlap": null},
    "backend": "rule_degraded",
    "low_confidence_reason": null
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 段落对标识，用于派生输出 `id` |
| `source` / `target` | string | 待切分的段落文本，必须存在且为字符串 |
| `source_lang` / `target_lang` | string | 声明语言，默认透传进输出；切句规则仍按配置的 `source_lang`/`target_lang` |
| `metadata` | object | 允许为对象或缺失；段落引用取 `metadata.source_ref`/`target_ref`，缺失时回退为 `para-{行号:03d}` |
| `metadata.*` | - | 原样保留在输出的 `metadata.paragraph_metadata` 中 |

非法输入不中断流程，按行写入异常清单：`invalid_json`、`invalid_record`（非对象）、`missing_field:<字段>`、`invalid_type:<字段>`、`empty_source`、`empty_target`。

## 输出：`10_sentence_pairs.jsonl`（通过集，逐行一条句子级句对）

```json
{
  "id": "para-p0001-t0001-pair0001",
  "source": "欢迎使用平行语料系统。",
  "target": "Welcome to the parallel corpus system.",
  "source_lang": "zh",
  "target_lang": "en",
  "metadata": {
    "granularity": "sentence",
    "confidence": 0.75,
    "scores": {"semantic": null, "length": 0.75, "evidence": null},
    "backend": "rule_degraded",
    "score_semantics": "rule_only",
    "model_version": null,
    "source_ref": "para-001-sent-001",
    "target_ref": "para-001-sent-001",
    "source_refs": ["para-001-sent-001"],
    "target_refs": ["para-001-sent-001"],
    "paragraph_id": "para-p0001-t0001",
    "paragraph_line": 1,
    "paragraph_source_ref": "para-001",
    "paragraph_target_ref": "para-001",
    "alignment_action": "1-1",
    "unit_counts": {"source": 10, "target": 6},
    "evidence_tokens": {"shared": []},
    "used_weights": {"length": 0.25},
    "model_name": null,
    "device": null,
    "window": 5,
    "min_confidence": 0.55,
    "low_confidence_reason": null,
    "paragraph_metadata": {"granularity": "paragraph"}
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | `{段落id}-pair{段落内序号:04d}` |
| `source` / `target` | string | 1-1 时为单句；1-2/2-1 时为合并后的原句文本（空格拼接，不改写内容） |
| `metadata.granularity` | string | 固定 `sentence` |
| `metadata.confidence` | float | 置信度，仅由实际可用分项加权归一得到 |
| `metadata.scores.semantic` | float \| null | Qwen3-Embedding-0.6B 真实余弦相似度；规则降级下恒为 `null`（不把长度分命名为语义分） |
| `metadata.scores.length` | float | 计量单元 Dice 相似度：`2*min/(a+b)`，单元 = 汉字数 + 拉丁词数 + 数字串数 |
| `metadata.scores.evidence` | float \| null | 硬 token（数字串、长度≥2 的拉丁词）重合 Dice；两端均缺少可比 token 时为 `null` |
| `metadata.backend` | string | `qwen3_embedding` 或 `rule_degraded` |
| `metadata.score_semantics` | string | `model`（有真实语义分）或 `rule_only`（仅规则分项） |
| `metadata.model_version` | string \| null | 真实模型版本（如 `Qwen3-Embedding-0.6B(dim=1024)`）；无模型为 `null` |
| `metadata.source_ref` / `target_ref` | string | 与根 `DATA_SCHEMA.md` 一致的句子引用 `para-003-sent-001` |
| `metadata.source_refs` / `target_refs` | array | 合并句对包含的全部句子引用 |
| `metadata.alignment_action` | string | `1-1` / `1-2` / `2-1` |
| `metadata.unit_counts` | object | 两端计量单元数，用于复核长度分 |
| `metadata.used_weights` | object | 本次置信度真正使用的权重（缺失分项会被剔除后重新归一） |
| `metadata.low_confidence_reason` | string \| null | 通过集恒为 `null`；取值口径见下 |

### `low_confidence_reason` 取值口径

| 取值 | 含义 |
| --- | --- |
| `null` | 未触发任何低置信/歧义判定 |
| `low_semantic` | 有模型语义分且语义分低于 `min_confidence` |
| `length_ratio_abnormal` | 无模型语义分，长度（及证据）分低于 `min_confidence` |
| `conflict_multi_candidate` | 最优配对与次优配对总分差 ≤ `ambiguity_margin`，无法唯一确定 |

## 异常输出：`10_sentence_rejects.jsonl`

未对齐句子、多候选、低置信和模型错误逐行列出，字段与通过集对齐，便于回溯。

```json
{
  "line": 4,
  "id": "para-p0004-t0004-rej0000",
  "paragraph_id": "para-p0004-t0004",
  "source_ref": "para-004-sent-001",
  "target_ref": null,
  "source": "对齐结果的可解释性很重要。",
  "target": null,
  "source_refs": ["para-004-sent-001"],
  "target_refs": [],
  "reasons": ["unaligned_source"],
  "action": null,
  "confidence": null,
  "scores": null,
  "low_confidence_reason": null,
  "ambiguity_margin": null,
  "second_action": null
}
```

`id` 为拒绝记录顶层标识：`<paragraph_id>-rej<序号>`（同段落多条拒绝按写出顺序编号）；
输入行本身无记录 id（`invalid_json`/`invalid_record`）时 `id=null`。

`reasons` 取值：`invalid_json`、`invalid_record`、`missing_field:<字段>`、`invalid_type:<字段>`、`empty_source`、`empty_target`、`unaligned_source`、`unaligned_target`、`low_confidence`、`conflict_multi_candidate`、`model_error`。

- 每个源/目标句子都可在通过集或异常清单中找到唯一去向。
- `low_confidence` / `conflict_multi_candidate` 条目额外带 `confidence`、`scores`、`action`、`low_confidence_reason`；多候选还带 `ambiguity_margin` 与 `second_action`。

## 阶段产物：`artifacts/stage_10_<uniqueId>.zip`

【无血缘改造 2026-10-19】原 `lineage.json`（JSON 数组，按执行顺序追加，含 operator /
operator_version / uniqueId / inputFile / outputFile / status / startedAt / finishedAt /
config / statistics）已停用，任务目录不再产出任何血缘文件。阶段内中间路径通过 `/predict`
返回的 `data.outputFile` 传递；阶段结束前把主输出、异常清单与 `run.log` 打包为
`artifacts/stage_10_<uniqueId>.zip`，响应返回 `stageZip` 与 `storageUri`。

### `statistics` 字段（保留在处理器统计结果与 `run.log` 中）

| 字段 | 说明 |
| --- | --- |
| `total` | 处理的非空输入行数（含非法行） |
| `pairs` | 通过集行数 |
| `rejected` | 异常清单行数 |
| `low_confidence` / `ambiguous` / `model_error` | 低置信 / 多候选 / 模型错误条目数 |
| `unaligned_source` / `unaligned_target` | 未对齐源句 / 目标句数量 |
| `source_sentences` / `target_sentences` | 切分得到的句子总数 |
| `action:1-1` / `action:1-2` / `action:2-1` | 各动作占比计数 |
| `paragraph_count_mismatch` | 源/目标句数不一致的段落数（提示性诊断，不自动拒绝） |
| `reason:<原因>` | 各异常原因计数 |
| `degraded` | 使用规则降级后端时为 1，否则 0 |
| `backend` / `model_version` / `device` | 实际后端、模型版本和运行设备 |
| `window` / `min_confidence` / `encoding` | 生效窗口、阈值和输入编码 |

状态口径：`degraded`（规则后端 + 产出句对）> `partial`（有异常条目）> `success`（无异常）；配置错误、编码错误、空输入、模型不可用且禁止降级、模型错误超上限均为 `failed`，`outputFile` 为 `null`。

## 编码约定

- 全链路严格 UTF-8：输入按 `input_encodings`（默认仅 `utf-8`）增量严格解码，失败即 `failed`。
- 输出（通过集、异常清单、阶段产物与日志）统一 UTF-8，逐行流式写入，不使用本地默认编码。


