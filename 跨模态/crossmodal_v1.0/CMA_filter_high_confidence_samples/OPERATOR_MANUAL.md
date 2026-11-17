# 算子说明书 · 19 筛选高置信样本入池

## 一句话

按表1 #19 的四道必过项（评分路线通过、整体对齐、授权可训练、时序通过）筛出可交付候选样本，
只把「四项同时满足」的样本放进候选池，其余样本按原因分别进「样本隔离库 / 待人工 / 原始隔离区」。

## 基本信息

| 项 | 值 |
|---|---|
| 正式编号 | **19** |
| 中文名称 | 筛选高置信样本入池 |
| 工程标识 | `high_confidence_pool` |
| 版本 | `v1.0.0` |
| 流程环节 | S3 数据加工 |
| 开关 / 是否必须 | 默认开启 / **必须**（不可关闭） |
| 优先级 | P0 |
| 类型 / 模型 | 规则型 / `--`（无模型、无网络、无 GPU） |
| 端口 | 8119 |
| 输出目录 | `OUTPUT_DIR/<uniqueId>/high_confidence_pool/<runNNN>/` |

## 四道必过项

| 门 | 通过条件 | 不适用 | 结论未定（不隔离） |
|---|---|---|---|
| `route` | 已开启且在生效的路线（#12/#13/#14）**全部**通过；三条都未生效时，`temporal_route_fallback=true`（默认）且 #15 判「通过」→ 以 `processing["15"]` 作为路线证据（`enabled_routes=["15"]`） | 路线 `skipped`/`route_closed`/`route_enabled=false` → 不参与 | 路线 `pending`/`pending_model`；#15 `not_applicable`/`pending_calibration` 不补路线 |
| `whole` | 整体结论 ∈ 允许集合（默认 `auto_aligned`、`manual_aligned`） | — | `uncertain`、`pending_manual_review`、缺失 |
| `license` | 授权结论为「可训练」 | — | 授权结论缺失 |
| `temporal` | 存在时间轴时时序结论通过 | 不存在时间轴 → 不拦截 | 存在时间轴但 #15 结论缺失或 `pending_calibration` |

判定顺序：结构检查 → 门失败（route→whole→license→temporal 取首个）→ 上游隔离 → 门待定 → 入池。

> **时序窗口的路线兜底（2026-11-05 起默认开启）**：时序窗口没有 CLIP/CER 评分
> （#14 判 `not_applicable`，#13 可能因转写不可靠判 `not_applicable`），此时若 #15 已判
> 「时序通过」，route 门以 `enabled_routes=["15"]` 通过，样本可正常入池。兜底只补 route 门：
> 整体对齐/授权/时序三门照常判定；#15 失败时 route 不补，仍以 `temporal_failed` 隔离
> （主因不改写为 `route_failed`）；`not_applicable`/`pending_calibration` 不构成证据，
> 保持「无评分路线」待人工。置 `temporal_route_fallback=false` 复现旧行为。

## 输入 → 输出

```text
#10 授权(#admission.license) ─┐
#12/#13/#14 评分结论 ─────────┤
#16 整体对齐(#alignment.whole)─┼─→ [19 规则判定] ─→ 19_candidate_pool.jsonl      → #20 / #22 / #24
#15 时序(temporal_alignment) ─┤                  ├→ 19_sample_quarantine.jsonl  → #21 样本隔离库
路线开关 route_enabled ───────┘                  ├→ 19_pending.jsonl            → 留产线（不隔离）
#17/#18/#21 隔离标记 ─────────┘                  └→ 19_rejects.jsonl            → 原始文件排查
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/uploadFile?uniqueId=<id>` | 上传任务 zip（含 `manifest.json` 或唯一 `.jsonl`） |
| POST | `/predict` | `{"uniqueId","isLastOperator",["inputFile"],["overrides"]}` |
| POST | `/removeFile?uniqueId=<id>` | 清理任务目录（幂等）；返回 `removed` 布尔 |
| GET | `/health` | 探活；声明 `downloadFile: not_registered` |

- **不提供 `/downloadFile`**。
- `isLastOperator` 必须是严格布尔，仅标记阶段末调用；v4.2 起**中间与阶段末响应同构**，`data` 均含
  `operator`/`status`/`outputFile`（`crossmodal-files-v1` 文件内容对象）/`result`
  （`uniqueId`、`runId`、`entrypoint`、任务内相对 `outputFile`、`statistics`、`model`），
  不再用 `false` 只回血缘对象。
- 形态一（`POST /predict?detection=filter_high_confidence_samples`，`multipart/form-data`，`file` 必传）：
  判定型 int `status`（1 入池/待人工，0 不入池）+ `process_status`、`uniqueId`、`sample_id`、
  `pool_status`、`pool`、`route`、`reason_code`。
- 外层信封统一 `{"code":0|1,"msg":"...","data":{...}}`。

## 配置（固定口径 + 可覆盖键）

**固定口径（不可通过请求削弱）**：`required_alignment` 固定为 `auto|human`
（自动对齐与人工对齐两类整体结论都接受，不允许收窄）、
`require_temporal_if_present` 固定为 `true`（带时间轴的样本必须过时序门）。
这两个键**不在** `/predict` 覆盖白名单内：`overrides` 或形态一表单携带它们时
一律返回 HTTP **422**（`errorType=FrozenOverride`，中文错误给出固定取值），
既不静默忽略也不允许收窄/关闭——表1 #19 四项必过项不得去掉任何一项。

**可覆盖键（`/predict.overrides` 与形态一表单）**：`enabled`（必须 `true`）、
`training_authorization`（必须 `required`）、`min_enabled_routes`（1–3）、
`missing_upstream_policy`（`waiting_manual`/`failed`）。

**仅部署配置可改（不在请求白名单内）**：`temporal_route_fallback`（默认 `true`）——
三条评分路线都未生效时，是否把 #15「时序通过」作为 route 门的兜底证据。请求携带该键按
未允许键处理（HTTP 400），避免调用方在不知情时放开/收紧入池口径。

## 质量处置对照（表1 原文）

| 情形 | 标签 | 去向 |
|---|---|---|
| 必过项失败 | `样本-语义不通`（授权原因时 `label_detail=样本-敏感`） | 样本隔离库（#21） |
| 通过 | `样本-评分：入池` | 样本池（留 #24 抽检） |
| 结论未定 | `流程-待人工对齐/待补授权/待时序判定/无评分路线/待人工确认` | 留产线 |

## 关键约束（不得违反）

1. 不产出任何分数、不用加权分把已隔离样本加回（`score_fields_produced=false`）。
2. 局部标注（#17/#18）不作为入池硬条件，仅作证据。
3. 尊重上游隔离标记，不得把已隔离样本重新入池。
4. 坏行/缺字段/编码错误进隔离清单，绝不静默丢弃。
5. 统计逐项守恒，守恒失败即运行状态 `failed`。

## 一键验证

```bash
python -m pytest tests/ -q                     # 176 passed / 30 skipped（2026-11-05）
python tests/smoke_api.py 8129                 # 真实服务 16 项
python processor.py --input dataset/sample/demo.jsonl \
       --unique-id smoke_cli --output-root output_smoke
```
