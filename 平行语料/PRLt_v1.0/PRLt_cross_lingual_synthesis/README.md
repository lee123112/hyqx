# PRLt_cross_lingual_synthesis（算子24）

使用高资源语言到指定低资源语言的翻译模型，保留中文锚点，生成低资源语言-中文候选。目标低资源语种必填；每条样本必须 `pending_manual`，人工接受后才可回流 14–18。

> 【无血缘改造 2026-10-19】本算子不再生成/读取/返回任何血缘文件。接口由
> `common/augmentation_api.create_app` 统一提供；成功响应 `data` 固定为
> `outputFile` / `rejectFile` / `stageZip` / `storageUri` 四字段。

- 算子编号：24，包名 `PRLt_cross_lingual_synthesis`，端口 **8024**
- 上游默认输入：`17_entity_checked.jsonl`（见 `map.json`），可选算子（`optional: true`）
- 回流：`rejoin: ["14", "15", "16", "17", "18"]`
- 入口：`inference:app`

## 1. 处理边界

**做什么**

1. **跨语言合成**：把高资源语言源文本（默认 `en`）翻译为配置的低资源语言，生成
   「低资源语言-中文」候选；中文锚点（`target`）保持不变。
2. **候选记录**：新 `id`（`<parent_id>__aug24__NNNN`）、`parent_pair_id`、
   `augmentation_operator=PRLt_cross_lingual_synthesis`、
   `augmentation_type=cross_lingual_synthesis`、`augmentation_status=pending_qc`、
   `human_review_status=pending_manual`、`delivery_eligible=false`；输出记录
   `source_lang=<低资源语言>`、`target_lang=zh`（锚点语言），并把
   `source_high_resource_text`、`target_low_resource_lang`、证据写入 `metadata.generation_evidence`。
3. **异常清单**：缺 `source`/`target`、模型无返回等进入 `24_rejects.jsonl`，不静默丢弃。

**不做什么**

- 目标低资源语种必填（`target_low_resource_lang` 为空直接配置错误）。
- 人工校验不可关闭（`human_review_required` 必须为 `true`，否则配置错误）。
- 不修改中文锚点、不覆盖原始数据；不生成/不返回任何血缘文件。
- 模型不可用或响应不是结构化 JSON 时**结构化失败**，不用规则结果替代。

## 2. 输入输出

输入：严格 UTF-8 JSONL，每行一个 dict（默认 `17_entity_checked.jsonl`）；阶段内实际路径由
`/predict` 的 `inputFile` 指定。必填 `id`、`source`、`target`（`target` 为中文锚点）。

输出（`OUTPUT_DIR/<uniqueId>/`）：

| 产物 | 说明 |
| --- | --- |
| `24_cross_lingual_synthesized.jsonl` | 跨语言合成候选（新增句对，不覆盖原数据） |
| `24_rejects.jsonl` | 异常清单：`{line, id, reasons[], augmentation_operator, augmentation_type, delivery_eligible:false}` |
| `artifacts/stage_24_<uniqueId>.zip` | 阶段产物：主输出 + 异常清单（`run.log` 存在时一并收录；2026-10-19 起取代 `lineage.json`） |

输出记录主要字段：`id`、`parent_pair_id`、`source`（低资源语言文本）、`target`（中文锚点）、
`source_lang`、`target_lang`、`augmentation_operator`、`augmentation_type`、
`augmentation_status`、`human_review_status`、`delivery_eligible`、`metadata`（含
`model_name`、`model_version`、`config_hash`、`generation_evidence`）。

## 3. 接口

外层响应固定 `{code, msg, data}`；正式契约仅三个接口，**不提供** `/downloadFile`。接口由
[`common/augmentation_api.py`](../common/augmentation_api.py) 的 `create_app` 统一实现。

| 能力 | 接口 |
| --- | --- |
| 上传 | `POST /uploadFile?uniqueId=`（返回 `data.inputFile`） |
| 调用 | `POST /predict` |
| 删除 | `POST /removeFile?uniqueId=` |

`/predict` 请求体：`uniqueId` 必传；`isLastOperator` 默认 `false`；`inputFile` 选传（缺省取任务
`input/` 下最后一个 `.jsonl`）；`operatorId` 选传（传了必须为 `"24"`）；`params` 覆盖配置，
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

- 中间调用（`isLastOperator=false`）：`outputFile` 为 `OUTPUT_DIR/<uniqueId>/24_cross_lingual_synthesized.jsonl` 路径；
- 阶段末调用（`isLastOperator=true`）：`outputFile` 为该文件的 UTF-8 文本内容；
- 失败返回 `{"code":1,"msg":"...","data":null}`，不生成阶段产物。

## 4. 阶段产物与无血缘

阶段结束前，`common/augmentation_runtime.run_records(..., operator_id="24")` 调用
[`common/artifacts.py`](../common/artifacts.py) 的 `finalize_stage_artifacts` 生成
`artifacts/stage_24_<uniqueId>.zip`，内含主输出与异常清单（任务目录存在 `run.log` 时一并收录）。配置环境变量
`OBJECT_STORE_DIR` 时 `storageUri` 为归档文件 `file://` 地址，否则为
`local-mirror://<uniqueId>/stage_24_<uniqueId>.zip`。

【无血缘改造 2026-10-19】原 `lineage.json`（`operator`/`operator_version`/`uniqueId`/`inputFile`/
`outputFile`/`rejectFile`/`status`/`startedAt`/`finishedAt`/`config`/`statistics`）输出已停用：
任务目录不再产出任何血缘文件，处理器返回 dict 不再含 `lineageFile`；阶段内路径由
`data.outputFile` 接力。

## 5. 配置

默认值以 [`processor.py`](processor.py) 的 `DEFAULT` 为准；`configs/config.yaml` 为同值静态说明
（处理器不自动加载该文件），运行时通过 `/predict` 的 `params`/`config` 覆盖。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `source_high_resource_lang` | `en` | 高资源语言 |
| `target_low_resource_lang` | `null` | 目标低资源语言，**必填**（如 `lo`） |
| `anchor_lang` | `zh` | 中文锚点语言 |
| `human_review_required` | `true` | 必须为 `true`，关闭直接配置错误 |
| `model_name` / `model_version` | `BITEXT-NLLB-200-distilled-600M` / `v1.0` | 模型标识与版本 |
| `model_path` | `<MODEL_ROOT>/nllb-200-distilled-600M` | 本地 NLLB 权重路径 |
| `model_service_url` | `null` | 显式配置才走远程 JSON 服务；默认本地模型 |
| `model_timeout_seconds` | `60` | 远程模型超时（秒） |
| `gen_batch_size` | `1` | 批处理快路径开关，`>1` 时批量翻译；默认关闭 |
| `gen_num_beams` / `gen_max_new_tokens` | `4` / `256` | 批处理时的束宽与最大新 token |

## 6. 模型与资源

- 本地模型：NLLB（`transformers` + `torch`），语言代码映射见
  [`common/augmentation_runtime.py`](../common/augmentation_runtime.py) 的 `_NLLB_LANGS`
  （含 `lo` 老挝语等低资源语种）。
- 权重根目录解析顺序：环境变量 `MODEL_ROOT` → 包内 `models/` → 开发验证机共享目录
  `G:\HYQX\算子\model`；默认 `model_path = <root>/nllb-200-distilled-600M`。
- 开发验证包**仅本地模型**：未显式配置 `model_service_url` 时不会回退到远程 API，权重缺失即结构化失败。
- 资源：CPU 可跑、GPU 更快；模型首次加载较慢，之后进程内缓存。

## 7. 启动与本地验证

```bash
uvicorn inference:app --host 0.0.0.0 --port 8024
# 或
python inference.py
```

`OUTPUT_DIR` 控制任务目录根：API 默认 `PRLt/work/augmentation_outputs`。本包 `processor.py`
只提供 `run()`，无独立 CLI；本地直调示例（必须显式指定目标低资源语言）：

```bash
python -c "import processor; r=processor.run('<seed.jsonl>','smoke24','output_smoke', overrides={'target_low_resource_lang':'lo'}); print(r['status'], r['stageZip'], r['storageUri'])"
```

## 8. 真实测试与验证

- 契约：根 [`tests/test_augmentation_21_24.py`](../tests/test_augmentation_21_24.py) 覆盖（根 pytest
  合计 **11 passed**）：未配 `target_low_resource_lang`、`human_review_required=false` 均结构化失败；
  任务目录无 `lineage*`；`stageZip`/`storageUri` 可用。
- 真实模型验证（2026-10-19，`MODEL_ROOT=G:\HYQX\算子\model`，`D:\python\python.exe`，
  `target_low_resource_lang=lo`）：约 **2s**，`status=success`；响应为四字段，`stageZip` 位于
  `artifacts/`，`storageUri=local-mirror://...zip`，任务目录**无** lineage 文件。

## 9. 已知限制

1. 依赖真实 NLLB 权重与 `transformers`/`torch` 环境；缺失时结构化失败，不降级为规则结果。
2. 低资源语种翻译质量受模型能力限制，必须经人工校验（`pending_manual`）后才可回流。
3. 未做真实 Docker 构建；未与下游 14–18 做工程侧联调。
4. 中文锚点原样保留，不做锚点改写；语义/流畅度等判定不在本节点（回流 14–18）。
5. 运行统计只记数量与文件名，不落原文全文。
6. 清单参数 `param_reviewFile`（人工校验结果回流文件）当前由 `tools/run_augmentation_branch.py`
   的 `--review-file` 承载，不是 `/predict` 的 `params` 键；增强候选一律 `pending_manual`，
   未经人工确认不得回流 14–18。
