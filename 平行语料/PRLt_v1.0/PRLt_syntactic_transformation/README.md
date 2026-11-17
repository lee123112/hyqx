# PRLt_syntactic_transformation（算子23）

模型辅助主动/被动转换、句子拆分和合并。模型必须返回结构化 JSON，且变换类型必须在配置集合中；候选默认不可交付，必须回流 14–18。

> 【无血缘改造 2026-10-19】本算子不再生成/读取/返回任何血缘文件。接口由
> `common/augmentation_api.create_app` 统一提供；成功响应 `data` 固定为
> `outputFile` / `rejectFile` / `stageZip` / `storageUri` 四字段。

- 算子编号：23，包名 `PRLt_syntactic_transformation`，端口 **8023**
- 上游默认输入：`17_entity_checked.jsonl`（见 `map.json`），可选算子（`optional: true`）
- 回流：`rejoin: ["14", "15", "16", "17", "18"]`
- 入口：`inference:app`

## 1. 处理边界

**做什么**

1. **句式变换**：模型对双语对做一次具体句法变换（`active_passive` 主动/被动、`split` 拆分、
   `merge` 合并），要求返回结构化 JSON `{source, target, transform_type, evidence}`；
   变换类型必须属于配置集合 `transform_types`。
2. **候选记录**：新 `id`（`<parent_id>__aug23__NNNN`）、`parent_pair_id`、
   `augmentation_operator=PRLt_syntactic_transformation`、`augmentation_type=syntactic_transformation`、
   `augmentation_status=pending_qc`、`delivery_eligible=false`；`metadata.generation_evidence`
   记录 `transform_type`、`evidence`、模型证据。
3. **异常清单**：无效 JSON、缺 `source`/`target`、未配置的 `transform_type`、结果与原句对完全
   相同等进入 `23_rejects.jsonl`，不静默丢弃。

**不做什么**

- 每条输入最多生成 1 个候选（`max_candidates_per_input` 必须为 1）。
- 不改变语言方向、不覆盖原始数据；不生成/不返回任何血缘文件。
- 模型不可用或响应不是结构化 JSON 时**结构化失败**，不用规则改写替代。

## 2. 输入输出

输入：严格 UTF-8 JSONL，每行一个 dict（默认 `17_entity_checked.jsonl`）；阶段内实际路径由
`/predict` 的 `inputFile` 指定。必填 `id`、`source`、`target`。

输出（`OUTPUT_DIR/<uniqueId>/`）：

| 产物 | 说明 |
| --- | --- |
| `23_syntactic_transformed.jsonl` | 句式变换候选（新增句对，不覆盖原数据） |
| `23_rejects.jsonl` | 异常清单：`{line, id, reasons[], augmentation_operator, augmentation_type, delivery_eligible:false}` |
| `artifacts/stage_23_<uniqueId>.zip` | 阶段产物：主输出 + 异常清单（`run.log` 存在时一并收录；2026-10-19 起取代 `lineage.json`） |

输出记录主要字段：`id`、`parent_pair_id`、`source`、`target`、`source_lang`、`target_lang`、
`augmentation_operator`、`augmentation_type`、`augmentation_status`、`human_review_status`、
`delivery_eligible`、`metadata`（含 `model_name`、`model_version`、`config_hash`、`generation_evidence`）。

## 3. 接口

外层响应固定 `{code, msg, data}`；正式契约仅三个接口，**不提供** `/downloadFile`。接口由
[`common/augmentation_api.py`](../common/augmentation_api.py) 的 `create_app` 统一实现。

| 能力 | 接口 |
| --- | --- |
| 上传 | `POST /uploadFile?uniqueId=`（返回 `data.inputFile`） |
| 调用 | `POST /predict` |
| 删除 | `POST /removeFile?uniqueId=` |

`/predict` 请求体：`uniqueId` 必传；`isLastOperator` 默认 `false`；`inputFile` 选传（缺省取任务
`input/` 下最后一个 `.jsonl`）；`operatorId` 选传（传了必须为 `"23"`）；`params` 覆盖配置，
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

- 中间调用（`isLastOperator=false`）：`outputFile` 为 `OUTPUT_DIR/<uniqueId>/23_syntactic_transformed.jsonl` 路径；
- 阶段末调用（`isLastOperator=true`）：`outputFile` 为该文件的 UTF-8 文本内容；
- 失败返回 `{"code":1,"msg":"...","data":null}`，不生成阶段产物。

## 4. 阶段产物与无血缘

阶段结束前，`common/augmentation_runtime.run_records(..., operator_id="23")` 调用
[`common/artifacts.py`](../common/artifacts.py) 的 `finalize_stage_artifacts` 生成
`artifacts/stage_23_<uniqueId>.zip`，内含主输出与异常清单（任务目录存在 `run.log` 时一并收录）。配置环境变量
`OBJECT_STORE_DIR` 时 `storageUri` 为归档文件 `file://` 地址，否则为
`local-mirror://<uniqueId>/stage_23_<uniqueId>.zip`。

【无血缘改造 2026-10-19】原 `lineage.json`（`operator`/`operator_version`/`uniqueId`/`inputFile`/
`outputFile`/`rejectFile`/`status`/`startedAt`/`finishedAt`/`config`/`statistics`）输出已停用：
任务目录不再产出任何血缘文件，处理器返回 dict 不再含 `lineageFile`；阶段内路径由
`data.outputFile` 接力。

## 5. 配置

默认值以 [`processor.py`](processor.py) 的 `DEFAULT` 为准；`configs/config.yaml` 为同值静态说明
（处理器不自动加载该文件），运行时通过 `/predict` 的 `params`/`config` 覆盖。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `transform_types` | `[active_passive, split, merge]` | 允许的变换类型；非法集合直接配置错误 |
| `max_candidates_per_input` | `1` | 一期固定为 1，其它值配置错误 |
| `model_name` / `model_version` | `BITEXT-Qwen2.5-1.5B` / `v2.0` | 模型标识与版本 |
| `model_path` | `<MODEL_ROOT>/Qwen2.5-1.5B-Instruct` | 本地 Qwen 权重路径 |
| `model_service_url` | `null` | 显式配置才走远程 JSON 服务；默认本地模型 |
| `model_timeout_seconds` | `60` | 远程模型超时（秒） |
| `gen_batch_size` | `4` | 批处理快路径开关，`>1` 时批量生成（默认开启；`1` 会退回单条贪心路径） |
| `gen_num_beams` / `gen_max_new_tokens` | `1` / `384` | 批处理束宽与最大新 token |
| `gen_json_repair` | `true` | 批处理时修复模型输出的截断/杂质 JSON |
| `gen_retry_sample` | `true` | 批处理失败记录用采样模式重试一次（束搜索确定性，原样重试必失败） |
| `gen_retry_temperature` | `0.7` | 第一轮采样重试温度 |
| `gen_retry_escalate` | `true` | 第一轮采样重试仍失败后，再升温重试一次 |
| `gen_retry_temperature_escalated` | `0.9` | 升温重试温度 |

> 【2026-10-21 修复】`gen_batch_size` 此前未写入 `DEFAULT`（实际默认 1），`run_records` 因此
> 走 `augmentation_runtime._local_chat_json` 单条贪心路径（无 JSON 修复、无重试），1.5B 复述
> 原文或返回非 JSON 时零产出。现默认走 `augmentation_fastpath.chat_json_batch` 批路径（JSON
> 修复 + 两轮采样重试），`_prompt` 附合法 JSON 输出示例并明确"不得复制输入、只输出 JSON"。

## 6. 模型与资源

- 本地模型：Qwen2.5-1.5B-Instruct（`transformers` + `torch`），要求模型返回结构化 JSON。
- 权重根目录解析顺序：环境变量 `MODEL_ROOT` → 包内 `models/` → 开发验证机共享目录
  `G:\HYQX\算子\model`；默认 `model_path = <root>/Qwen2.5-1.5B-Instruct`。
- 开发验证包**仅本地模型**：未显式配置 `model_service_url` 时不会回退到远程 API，权重缺失即结构化失败。
- 资源：CPU 可跑但较慢，建议 GPU；模型首次加载慢，之后进程内缓存。

## 7. 启动与本地验证

```bash
uvicorn inference:app --host 0.0.0.0 --port 8023
# 或
python inference.py
```

`OUTPUT_DIR` 控制任务目录根：API 默认 `PRLt/work/augmentation_outputs`。本包 `processor.py`
只提供 `run()`，无独立 CLI；本地直调示例：

```bash
python -c "import processor; r=processor.run('<seed.jsonl>','smoke23','output_smoke'); print(r['status'], r['stageZip'], r['storageUri'])"
```

## 8. 真实测试与验证

- 包内单测（2026-10-21 新增 [`tests/test_processor_batch.py`](tests/test_processor_batch.py)，6 条，
  不加载真实模型）：`D:\python\python.exe -m pytest PRLt\PRLt_syntactic_transformation\tests -q`
  → **6 passed**。覆盖 DEFAULT 可 JSON 序列化/默认开启批路径、提示词 JSON 示例与边界、
  重试温度阶梯 0.7→0.9、拒绝原因保留、`config.yaml` 与 `DEFAULT` 同步。
- 契约：根 [`tests/test_augmentation_21_24.py`](../tests/test_augmentation_21_24.py) 覆盖（根 pytest
  合计 **11 passed**）：非法 `transform_types` 结构化失败、任务目录无 `lineage*`、`stageZip`/
  `storageUri` 可用。2026-10-21 复跑该文件 **6 passed**。
- 真实模型验证（2026-10-19，`MODEL_ROOT=G:\HYQX\算子\model`，`D:\python\python.exe`）：
  2 条种子样本句式变换约 **25s**，`status=partial`；响应为四字段，`stageZip` 位于 `artifacts/`，
  `storageUri=local-mirror://...zip`，任务目录**无** lineage 文件。
- 【2026-10-21 修复验证】24 条样本（`prlt_test/runs/run200_models6_20261021` 的 4 条被拒记录
  + `18_dataset` 随机 20 条，seed=20261021），默认配置直调 `processor.run`：
  **产出 24/24、拒绝 0、`generation_path=batched`、耗时 180s、峰值显存 4938MB**；
  生成路径 20 条贪心 + 4 条采样重试（0.7）成功，无「不是有效 JSON」类拒绝。
  诚实质量口径：仅 **6/24** 两侧都实质改写、**1/24** 同时保持双侧语言方向（其余仅改 target 侧
  或把某侧换成另一语言），属 Qwen2.5-1.5B 指令遵循上限；完整数据与样例见
  `prlt_test/runs/aug23_fix_20261021/summary.json`。

## 9. 已知限制

1. 依赖真实 Qwen 权重与 `transformers`/`torch` 环境；缺失时结构化失败，不降级为规则改写。
2. 模型可能返回未配置的 `transform_type`、无效 JSON 或与原句相同的结果，均进异常清单；
   `status=partial` 即部分样本未生成有效候选（真实 2 条验证时出现），不是链路错误。
3. 未做真实 Docker 构建；未与下游 14–18 做工程侧联调。
4. 每条只生成 1 个候选；语义等价性/流畅度等判定不在本节点（回流 14–18）。
5. 运行统计只记数量与文件名，不落原文全文。
6. **1.5B 指令遵循上限（2026-10-21 实测）**：批量快路径 + JSON 修复 + 两轮采样重试能把
   「复述原文/坏 JSON」的 0 产出修到机械口径 24/24，但真正两侧同时句式变换且语言方向正确的
   比例很低（24 条中 1 条）；多数输出只改 target 侧或把某侧换成另一语言。生产建议换 7B/远端
   JSON 模型，或把「两侧均实际变换/语言方向正确」作为 14–18 回流 QC 必检项；若在算子内强校验，
   需新增语言方向/相似度拒绝口径（本次按要求未改原有拒绝口径）。
7. 默认 `gen_batch_size=4` 在本机 4GB 级 GPU 上峰值约 4.9GB，接近上限；与其它模型任务并发时
   建议降到 2 或串行执行，避免 OOM/耗时波动。
