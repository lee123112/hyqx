# 算子14 语义等价性检测 数据格式说明

> 字段名一旦冻结不得随意变更；适配变更须同步更新本文件与 README.md。

## 1. 输入

默认输入文件名：`13_graded.jsonl`（阶段内实际路径由 `/predict` 的 `inputFile` 指定）。
每行一个 dict（UTF-8 JSONL，严格 UTF-8）：

```json
{
  "id": "sample-0001",
  "source": "机器学习模型可以识别文本中的语义关系。",
  "target": "Machine learning models can identify semantic relations in text.",
  "source_lang": "zh",
  "target_lang": "en",
  "grade": "S",
  "metadata": {
    "confidence": 0.95,
    "manual_confirmed": false
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 记录唯一标识（非空字符串） |
| `source` | 是 | 源语言文本（非空字符串） |
| `target` | 是 | 目标语言文本（非空字符串） |
| `source_lang` / `target_lang` | 否 | 声明语言；缺省按配置 `zh`/`en`，声明与配置方向不一致判 `language_declaration_mismatch` |
| `grade` / `quality_grade` | 条件必填 | 对齐质量等级；顶层或 `metadata` 均可。默认只放行 `S`/`A` |
| `metadata.manual_confirmed` / `metadata.human_confirmed` | 否 | 人工确认标记；为 `true` 时允许放行非 S/A（如人工确认合格的 C） |
| `metadata` | 否 | 其他元数据，原样保留 |

缺字段、类型错误、损坏 JSON、准入失败一律进入异常清单，不静默丢弃。

## 2. 输出

产物位于 `OUTPUT_DIR/<uniqueId>/`：

| 文件 | 说明 |
| --- | --- |
| `14_semantic_checked.jsonl` | 语义等价通过记录（`quality_status=accepted`）与待复核记录（`quality_status=waiting_quality`） |
| `14_rejects.jsonl` | 未通过记录（低分、准入失败、结构异常、降级拒收） |
| `artifacts/stage_14_<uniqueId>.zip` | 阶段产物（主输出、异常清单与 `run.log`），2026-10-19 起取代 `lineage.json` |
| `run.log` | 逐行日志 `[时间] [动作] 消息` |

### 2.1 通过/待复核记录

在原记录基础上写入 `metadata`：

```json
{
  "metadata": {
    "grade": "S",
    "backend": "model",
    "model_name": "Qwen3-Embedding-0.6B",
    "model_version": "Qwen3-Embedding-0.6B(dim=1024)",
    "threshold": 0.70,
    "quality_status": "accepted",
    "semantic_score": 0.91,
    "semantic_evidence": {
      "length_ratio": 0.5,
      "length_ratio_ok": true,
      "source_units": 6,
      "target_units": 3,
      "number_check": {"type": "number_mismatch", "missing_numbers": [], "extra_numbers": [], "consistent": true},
      "language_direction": {
        "declared_source_lang": "zh",
        "declared_target_lang": "en",
        "detected_source_lang": "zh",
        "detected_target_lang": "en",
        "language_detection_backend": "langdetect",
        "language_detection_confidence": 0.99,
        "language_detection_reason": "ok/ok",
        "direction_ok": true
      }
    },
    "problem_labels": [],
    "degraded": false,
    "detected_source_lang": "zh",
    "detected_target_lang": "en",
    "language_detection_backend": "langdetect",
    "language_detection_confidence": 0.99,
    "language_detection_reason": "ok/ok"
  }
}
```

- `backend`：`model`（真实模型分）或 `rule_degraded`（规则降级）。**降级时 `semantic_score` 恒为 `null`，不伪造模型概率。**
- `quality_status`：`accepted`（真实分 ≥ `threshold`）或 `waiting_quality`（降级，无真实分，`degraded_policy=waiting_quality`）。交付节点在 `semantic_score` 缺失时默认拒收。
- `semantic_evidence`：只含真实可复算观测（长度比例、数字一致性、语言方向），不含任何拼凑分数。
- `problem_labels`：问题标签数组，每项为 `{"label": ..., "evidence": {...}}`，标签与证据一一对应。

### 2.2 标签与证据

| label | 证据 type | 说明 |
| --- | --- | --- |
| `omission` | `number_mismatch` / `length_ratio` | 源有数字目标缺 / 目标明显过短 |
| `addition` | `number_mismatch` / `length_ratio` | 目标有源无数字 / 目标明显过长 |
| `mistranslation` | `model_margin` | 真实模型分低于阈值，证据含 `semantic_score`、`threshold`、`margin` |
| `semantic_drift` | `language_direction` | 两端语言方向与声明不符（仅在 langdetect 可用时判定） |
| `low_evidence` | `no_model_score` | 规则降级且无其他证据 |

### 2.3 异常清单

```json
{
  "line": 3,
  "id": "sample-0005",
  "source": "该句对仅通过机器评分。",
  "target": "This pair only passed machine scoring.",
  "reasons": ["grade_not_eligible"],
  "quality_status": "rejected",
  "backend": "rule_degraded",
  "model_version": null,
  "semantic_score": null,
  "semantic_evidence": null,
  "labels": [],
  "threshold": 0.70
}
```

稳定原因码：`invalid_json`、`invalid_record`、`missing_field:id`、`invalid_type:*`、`missing_grade`、
`grade_not_eligible`、`language_declaration_mismatch`、`empty_source`、`empty_target`、
`semantic_not_equivalent`、`semantic_score_missing`、`model_error`。

## 3. 阶段产物

【无血缘改造 2026-10-19】原 `lineage.json`（JSON 数组，按执行顺序追加，含 operator /
operator_version / uniqueId / inputFile / outputFile / status / startedAt / finishedAt /
config / statistics）已停用，任务目录不再产出任何血缘文件。阶段内中间路径通过 `/predict`
返回的 `data.outputFile` 传递；阶段结束前把主输出、异常清单与 `run.log` 打包为
`artifacts/stage_14_<uniqueId>.zip`，响应返回 `stageZip` 与 `storageUri`。

`status`：`success | partial | failed | degraded | waiting_manual`。

`statistics` 字段（technical_design §4）：

| 字段 | 说明 |
| --- | --- |
| `input_records` | 输入有效记录数 |
| `model_scored` | 取得真实模型分的记录数 |
| `degraded` | 走规则降级后端的记录数 |
| `accepted` | 真实分 ≥ 阈值，写入通过文件 |
| `waiting_quality` | 降级待复核，写入通过文件 |
| `rejected` | 写入异常清单的记录数 |
| `output_records` | `accepted + waiting_quality`，等于通过文件行数 |
| `threshold` | 阈值快照 |
| `model_version` | 模型版本（规则降级为 `null`） |
| `backend` | `model` / `rule_degraded` |
| `degraded_policy` | 降级去向 |
| `reason:*` | 各原因计数 |

## 4. 接口

| 能力 | 接口 | 说明 |
| --- | --- | --- |
| 上传 | `POST /uploadFile?uniqueId=` | `multipart/form-data` |
| 调用 | `POST /predict` | 必传 `uniqueId`、布尔 `isLastOperator`；阶段内后续算子传 `inputFile` |
| 删除 | `POST /removeFile?uniqueId=` | 删除当前任务目录 |

外层响应固定 `{code, msg, data}`（`code` 0=成功，1=失败）。
成功时 `data` 固定为 `outputFile`、`rejectFile`、`stageZip`、`storageUri` 四字段：
中间算子 `isLastOperator=false` 时 `outputFile` 是输出文件路径；
阶段末 `isLastOperator=true` 时 `outputFile` 是最终输出内容字符串；不再返回 `lineageFile`。
