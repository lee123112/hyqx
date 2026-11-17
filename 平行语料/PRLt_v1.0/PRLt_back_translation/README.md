# PRLt_back_translation（算子21）

可选、手动触发的单轮回译增强。目标语言反向翻译为源语言，新增候选句对，不覆盖原始数据。
所有候选默认 `pending_qc`、`delivery_eligible=false`，必须回流 14–18。

模型服务不可用或响应不是结构化 JSON 时，任务结构化失败，不使用规则结果替代。

> 【无血缘改造 2026-10-19】本算子不再生成/读取/返回任何血缘文件。接口由
> `common/augmentation_api.create_app` 统一提供；成功响应 `data` 固定为
> `outputFile` / `rejectFile` / `stageZip` / `storageUri` 四字段。

- 算子编号：21，包名 `PRLt_back_translation`，端口 **8021**
- 上游默认输入：`17_entity_checked.jsonl`（见 `map.json`），可选算子（`optional: true`）
- 回流：`rejoin: ["14", "15", "16", "17", "18"]`
- 入口：`inference:app`

## 1. 处理边界

**做什么**

1. **单轮回译**：把 `target` 回译为源语言，生成新的 `source` 候选；`target` 保持原值。
2. **候选记录**：新 `id`（`<parent_id>__aug21__NNNN`）、`parent_pair_id`、
   `augmentation_operator=PRLt_back_translation`、`augmentation_type=back_translation`、
   `augmentation_status=pending_qc`、`delivery_eligible=false`；模型证据写入
   `metadata.generation_evidence`，并记录 `model_name`/`model_version`/`config_hash`。
3. **异常清单**：缺 `source`/`target`、模型无返回、增强结果与原句对完全相等等进入
   `21_rejects.jsonl`，不静默丢弃。

**不做什么**

- 不做多轮回译（`rounds` 必须为 1），每条输入最多 1 个候选（`max_candidates_per_input` 必须为 1）。
- 不覆盖原始数据、不修改上游记录；不生成/不返回任何血缘文件。
- 模型不可用或响应不是结构化 JSON 时**结构化失败**，不用规则结果替代、不编造译文。

## 2. 输入输出

输入：严格 UTF-8 JSONL，每行一个 dict（默认 `17_entity_checked.jsonl`）；阶段内实际路径由
`/predict` 的 `inputFile` 指定。必填 `id`、`source`、`target`；`source_lang`/`target_lang`
缺省取配置（默认 `en`/`zh`）。

输出（`OUTPUT_DIR/<uniqueId>/`）：

| 产物 | 说明 |
| --- | --- |
| `21_back_translated.jsonl` | 回译候选（新增句对，不覆盖原数据） |
| `21_rejects.jsonl` | 异常清单：`{line, id, reasons[], augmentation_operator, augmentation_type, delivery_eligible:false}` |
| `artifacts/stage_21_<uniqueId>.zip` | 阶段产物：主输出 + 异常清单（`run.log` 存在时一并收录；2026-10-19 起取代 `lineage.json`） |

输出记录主要字段：`id`、`parent_pair_id`、`source`（回译文本）、`target`、`source_lang`、
`target_lang`、`augmentation_operator`、`augmentation_type`、`augmentation_status`、
`human_review_status`、`delivery_eligible`、`metadata`（含 `model_name`、`model_version`、
`config_hash`、`generation_evidence`）。

## 3. 接口

外层响应固定 `{code, msg, data}`；正式契约仅三个接口，**不提供** `/downloadFile`。接口由
[`common/augmentation_api.py`](../common/augmentation_api.py) 的 `create_app` 统一实现。

| 能力 | 接口 |
| --- | --- |
| 上传 | `POST /uploadFile?uniqueId=`（返回 `data.inputFile`） |
| 调用 | `POST /predict` |
| 删除 | `POST /removeFile?uniqueId=` |

`/predict` 请求体：`uniqueId` 必传；`isLastOperator` 默认 `false`；`inputFile` 选传（缺省取任务
`input/` 下最后一个 `.jsonl`）；`operatorId` 选传（传了必须为 `"21"`）；`params` 覆盖配置，
兼容字段 `config` 同义（`params` 优先）；未知配置键报配置错误。

成功响应 `data` 固定 4 个字段：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "outputFile": "<中间算子=输出文件路径；阶段末=输出文件内容>",
    "rejectFile": "<异常清单路径>",
    "stageZip": "<阶段产物 zip 路径>",
    "storageUri": "<file:// 或 local-mirror:// 地址>"
  }
}
```

- 中间调用（`isLastOperator=false`）：`outputFile` 为 `OUTPUT_DIR/<uniqueId>/21_back_translated.jsonl` 路径；
- 阶段末调用（`isLastOperator=true`）：`outputFile` 为该文件的 UTF-8 文本内容；
- 失败返回 `{"code":1,"msg":"...","data":null}`，不生成阶段产物。

## 4. 阶段产物与无血缘

阶段结束前，`common/augmentation_runtime.run_records(..., operator_id="21")` 调用
[`common/artifacts.py`](../common/artifacts.py) 的 `finalize_stage_artifacts` 生成
`artifacts/stage_21_<uniqueId>.zip`，内含主输出与异常清单（任务目录存在 `run.log` 时一并收录）。配置环境变量
`OBJECT_STORE_DIR` 时 `storageUri` 为归档文件 `file://` 地址，否则为
`local-mirror://<uniqueId>/stage_21_<uniqueId>.zip`。

【无血缘改造 2026-10-19】原 `lineage.json`（`operator`/`operator_version`/`uniqueId`/`inputFile`/
`outputFile`/`rejectFile`/`status`/`startedAt`/`finishedAt`/`config`/`statistics`）输出已停用：
任务目录不再产出任何血缘文件，处理器返回 dict 不再含 `lineageFile`；阶段内路径由
`data.outputFile` 接力。

## 5. 配置

默认值以 [`processor.py`](processor.py) 的 `DEFAULT` 为准；`configs/config.yaml` 为同值静态说明
（处理器不自动加载该文件），运行时通过 `/predict` 的 `params`/`config` 覆盖。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `rounds` | `1` | 固定单轮，非 1 直接配置错误 |
| `source_lang` / `target_lang` | `en` / `zh` | 记录缺字段时的语言方向 |
| `max_candidates_per_input` | `1` | 一期固定为 1，其它值配置错误 |
| `model_name` / `model_version` | `BITEXT-NLLB-200-distilled-600M` / `v1.0` | 模型标识与版本 |
| `model_path` | `<MODEL_ROOT>/nllb-200-distilled-600M` | 本地 NLLB 权重路径 |
| `model_service_url` | `null` | 显式配置才走远程 JSON 服务；默认本地模型 |
| `model_timeout_seconds` | `60` | 远程模型超时（秒） |
| `gen_batch_size` | `1` | 批处理快路径开关，`>1` 时批量翻译；默认关闭 |
| `gen_num_beams` / `gen_max_new_tokens` | `4` / `256` | 批处理时的束宽与最大新 token |

## 6. 模型与资源

- 本地模型：NLLB（`transformers` + `torch`），CPU 可跑、GPU 更快；模型首次加载较慢，之后进程内缓存。
- 权重根目录解析顺序：环境变量 `MODEL_ROOT` → 包内 `models/` → 开发验证机共享目录
  `G:\HYQX\算子\model`；默认 `model_path = <root>/nllb-200-distilled-600M`。
- 开发验证包**仅本地模型**：未显式配置 `model_service_url` 时不会回退到远程 API，权重缺失即结构化失败。
- 资源：规则与打包部分仅需 CPU/磁盘；NLLB 推理建议 GPU（或较大内存 CPU）。

## 7. 启动与本地验证

```bash
uvicorn inference:app --host 0.0.0.0 --port 8021
# 或
python inference.py
```

`OUTPUT_DIR` 控制任务目录根：API 默认 `PRLt/work/augmentation_outputs`。本包 `processor.py`
只提供 `run()`，无独立 CLI；本地直调示例：

```bash
python -c "import processor; r=processor.run('<seed.jsonl>','smoke21','output_smoke'); print(r['status'], r['stageZip'], r['storageUri'])"
```

## 8. 真实测试与验证

- 契约：根 [`tests/test_augmentation_21_24.py`](../tests/test_augmentation_21_24.py) 覆盖（根 pytest
  合计 **11 passed**），断言四字段、`stageZip`/`storageUri` 可用、任务目录无 `lineage*`。
- 真实模型验证（2026-10-19，`MODEL_ROOT=G:\HYQX\算子\model`，`D:\python\python.exe`）：
  2 条种子样本回译约 **22s**，`status=partial`；响应为四字段，`stageZip` 位于 `artifacts/`，
  `storageUri=local-mirror://...zip`，任务目录**无** lineage 文件。

## 9. 已知限制

1. 依赖真实 NLLB 权重与 `transformers`/`torch` 环境；缺失时结构化失败，不降级为规则结果。
2. `status=partial` 表示部分样本生成失败并进入异常清单（真实 2 条验证时出现），不是链路错误。
3. 未做真实 Docker 构建；未与下游 14–18 做工程侧联调。
4. 每条只生成 1 个候选、单轮；语义等价性/流畅度等判定不在本节点（回流 14–18）。
5. 运行统计只记数量与文件名，不落原文全文。
