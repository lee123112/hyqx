# 算子 25「管理人工复核任务」（表1 #25）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

> 表1 v4.1 第 7 环节「人工抽检复核」｜`stageId=S7`｜正式编号 **25**｜优先级 **P1**｜
> 类型=**系统功能**｜模型列 **`--`**｜**手动触发**（默认关闭）｜`operator_version=v1.0.0`

## 1. 功能边界

**做什么**（表1 #25 冻结口径）：

1. 只消费三类样本，并为其生成人工复核任务与派单状态：
   - **存疑**：`alignment` 为 `uncertain`/`doubt`，或 `processing["16"]` 为 `uncertain`，
     或 `processing["12"/"13"/"14"/"15"]` 的 `score ∈ [0.25, 0.32)` / 标签 `样本-评分：待人工`；
   - **#24 抽检异议**：`processing["24"].role=dispute`，或 `decision=rejected`，
     或 `decision=uncertain` 且 `sampled=true`；
   - **加工退回**：`processing["21"].action=return` / `decision=returned` / `status=returned`
     / `label=流程-退回重跑`，或 `record.returned=true` / `return_reason` 非空。
2. **手动触发**：`enabled=false`（默认）时不生成任何任务，只做扫描与台账；
   必须由 `/predict` 的 `config.enabled=true` 或 CLI `--override enabled=true` 显式开启。
3. **派单**：为每条入队样本生成任务（`task_id`、轮次、优先级 P1、派单状态）；
   人工平台未接入（`manual_platform_available=false`，默认）时派单状态只能是
   `waiting_manual`、`assignee=null`。
4. **结论回写**：只读输入证据，通过 → 回候选池（#19/#20）；不通过 → 标签
   `样本-语义不通`、样本隔离库（#21）；无结论 / 结论非法 / 结论存疑 → 留人工队列
   （`waiting_manual`，不拦其余样本）。
5. **四条研发测边界**全部实现：
   - 无三类样本 → 不生成任务（`no_eligible_category`）；
   - 不重复 #24 抽样（`repeat_spot_check=false`，只消费 #24 结论，不重新抽样）；
   - 自动对齐且未被 #24 抽中的存疑样本 → 不进队列（`auto_aligned_not_sampled`）；
   - 同一条已完成 2 次复核 → 不再派单并锁定样本隔离库（`max_review_reached`）。
6. 统计守恒、逐行日志、安全解压、任务隔离与路径安全。

**不做什么**（边界，避免吸收相邻算子职责）：

- 不做任何模型打分 / 相似度计算（表1 模型列 `--`），不写固定分、随机分或「规则分」冒充模型分；
- 不重复 #24 的抽检抽样，不自行改变 #24/#21/#20 的结论，只在本节点命名空间写证据；
- 不把「预填」当成「人工确认」：无复核人身份或结论取值非法一律判 `待人工`；
- 不实现 `/downloadFile`，不按旧版四接口实现；
- 不打包隔离样本进入可交付链（隔离样本只在隔离清单中登记，交 #21）。

## 2. 输入与输出

### 2.1 输入

- **入口**：`OUTPUT_DIR/<uniqueId>/input/` 下的唯一 JSONL（按 `manifest.json.entrypoint`；
  无清单时要求恰好一个根级 `.jsonl`）；
- **阶段内续跑**：`inputFile` 传上一节点 `outputFile` 内容对象恢复出的任务内入口路径，必须位于**同一 `uniqueId`** 任务目录内；
- **编码**：严格 UTF-8；非 UTF-8 结构化失败（`code=1`）；
- **记录字段**：`sample_id`（必需）、模态引用、`metadata.batch_id`、`alignment`、
  `processing["12"/"13"/"14"/"15"/"16"/"21"/"24"]`、人工结论对象（`review` 等）、
  历史复核轮数（`review_count`/`review_history`）以及未知扩展字段（原样保留）。
  完整字段表见 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)。

### 2.2 输出（写入 `OUTPUT_DIR/<uniqueId>/manual_review_task_management/<runId>/`）

| 文件 | 角色 | 说明 |
|---|---|---|
| `25_review_candidates.jsonl` | 主输出（入口） | 透传上游字段 + 追加 `operator_25` / `processing["25"]`（无时间戳，逐字节可复现） |
| `25_review_tasks.jsonl` | 任务 | 派单任务与派单状态 |
| `25_review_conclusions.jsonl` | 结论 | 通过 / 不通过 / 存疑 / 待人工结论登记 |
| `25_sample_pool_return.jsonl` | 回池 | 复核通过样本 → `operator_19`、`operator_20` |
| `25_sample_quarantine.jsonl` | 隔离 | 复核不通过（`isolate`）与复核超限锁定（`lock`）→ `operator_21` |
| `25_pending_manual.jsonl` | 待人工 | 无结论 / 结论非法 / 结论存疑 → 人工复核平台 |
| `25_not_dispatched.jsonl` | 未派单 | 非三类 / 未触发 / 自动对齐未抽中 / 复核超限 / 上游已隔离 |
| `25_original_quarantine.jsonl` | 原始隔离 | 缺 / 重复 `sample_id`（保留原记录） |
| `25_rejects.jsonl` | 坏行 | JSON 解析失败、非对象（≤200 字符预览） |
| `25_review_report.json` | 报告 | 任务/结论/轮次/交接/统计汇总 |
| `manifest.json` / `run.log` | 清单与日志 | 运行清单（`entrypoint=25_review_candidates.jsonl`）、规范日志 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py` 仅保留通用工具函数、代码存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

## 3. 配置

`configs/config.yaml`（部署身份与业务参数分层）：

| 键 | 默认 | 可否覆盖 | 说明 |
|---|---|---|---|
| `operator.name/version/label/number`、`stage_id` | `manual_review_task_management` / `v1.0.0` / `管理人工复核任务` / `25` / `S7` | 否 | 部署身份，`/predict` 覆盖一律拒绝 |
| `trigger` | `manual` | 否（冻结） | 表1「手动触发」 |
| `max_review_count` | `2` | 否（冻结） | 同一条最多复核 2 次 |
| `eligible` | `[uncertain, sampling_dispute, returned]` | 否（冻结） | 三类样本范围 |
| `repeat_spot_check` | `false` | 否（冻结） | 不重复 #24 抽样 |
| `transport.format` / `max_response_bytes` | `crossmodal-files-v1` / `67108864` | 否 | 阶段末内容对象口径 |
| `enabled` | `false` | 是 | 手动触发开关 |
| `auto_dispatch` | `true` | 是 | 关闭后只登记队列，不派单 |
| `manual_platform_available` | `false` | 是 | 平台是否接入（决定派单状态） |
| `require_reviewer_identity` | `true` | 是 | 结论必须带复核人身份 |
| `allow_degraded` | `false` | 是 | 允许显式降级（人工平台未接入时继续并标记 `waiting_manual`） |

未声明字段、类型不符（如 `enabled="yes"`）或越界值一律 `ConfigError`（`code=1`），不静默生效。

`/predict` 形态二请求体只接受 `uniqueId`、`isLastOperator`（严格布尔）、`inputFile`、`config`；
`config` 仅接受上表 5 个「可否覆盖=是」的业务开关，`review_task_id`、`retry_limit` 等未声明字段
一律拒绝（HTTP 422 + `code=1`）；`max_review_count=2` 属部署身份冻结值，只校验不可覆盖。

## 4. 算法 / 模型后端与降级行为

- **无模型、无权重、无 GPU、无网络、无外部服务**（表1 模型列 `--`）：全部为确定性规则与
  证据解析，不使用随机数，不产生任何分数。
- **人工复核平台未接入**（默认）：派单状态 `waiting_manual`、`assignee=null`；队列记录保持
  `pending_manual`，运行状态 `status=waiting_manual`，报告 `status=waiting_manual`。
  这是**显式降级**，不是伪造「人工已确认」。
- **算子未触发**：`status=disabled`，`tasks_generated=0`，所有记录回到 `not_triggered` 台账。
- 只有输入证据中出现合法人工结论（`pass`/`fail`/`uncertain` + 复核人）才会写
  `human_verified=true`。

## 5. 启动与调用

### 5.1 CLI

```powershell
# 模板要求的基础命令（默认关闭 → status=disabled，退出码 0）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 手动触发（真实派单 + 结论回写）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli2 `
    --output-root output_smoke --override enabled=true

# 平台已接入
python processor.py --input dataset/sample/demo_pending.jsonl --unique-id smoke_cli3 `
    --output-root output_smoke --override enabled=true --override manual_platform_available=true

# 阶段末形态（outputFile 打包为 crossmodal-files-v1 内容对象，[v4.2] 无 lineageFile）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli4 `
    --output-root output_smoke --is-last-operator
```

### 5.2 服务

```powershell
$env:OUTPUT_DIR = "output"; python -m uvicorn app:app --host 0.0.0.0 --port 8125
# 真实 HTTP 冒烟（uvicorn 子进程 + 真实 multipart）
python tests/smoke_api.py 8135
```

### 5.3 HTTP（仅三个正式接口）

| 接口 | 说明 |
|---|---|
| `POST /uploadFile?uniqueId=` | 上传待复核 JSONL / ZIP（安全解压，失败零落盘） |
| `POST /predict` | `{"uniqueId","isLastOperator","inputFile?","config?}` |
| `POST /removeFile?uniqueId=` | 幂等删除当前任务目录 |

形态一（`POST /predict?detection=manage_manual_review_task`，`multipart/form-data`，处理型 `status`）：

```json
{"status":1,"uniqueId":"task_1","process_status":"waiting_manual",
 "review_tasks":[{"review_task_id":"task-prev_task-unc-01-r1","sample_id":"unc-01",
                  "task_status":"waiting_manual","trigger":"uncertain","round":1,
                  "max_review_count":2}],
 "review_results":[]}
```

- `status`：处理型 int，1 = 本次运行完成（含队列为空 / 未触发），0 = 参数/输入/执行失败；
- `process_status`：运行状态字符串（`success`/`disabled`/`waiting_manual`/`partial`），
  无任务时同样是 `status=1` 的空操作成功，不伪造任务或结论。
- 形态一可带表单标量 `enabled`、`auto_dispatch`、`manual_platform_available`、
  `require_reviewer_identity`、`allow_degraded`（字符串布尔），未知字段忽略；
  参数/输入失败时返回 `{"status":0,"msg":"..."}`。

形态二（`POST /predict`，JSON body，`isLastOperator` 必传）：

```json
{"code":0,"msg":"人工复核任务处理完成","data":{
  "operator":"manual_review_task_management","status":"waiting_manual",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"25_review_candidates.jsonl",
    "fileCount":11,"totalBytes":0,
    "files":[{"path":"25_review_candidates.jsonl","encoding":"utf-8","size":0,"content":"..."}]},
  "result":{"operator":"manual_review_task_management","operatorVersion":"v1.0.0",
            "stageId":"S7","uniqueId":"task_1","runId":"run001","status":"waiting_manual",
            "reviewStatus":"waiting_manual","entrypoint":"25_review_candidates.jsonl",
            "inputFile":"demo.jsonl",
            "statistics":{"input_lines":33,"input":32,"valid":30,"output":28,
                          "tasks_generated":19,"dispatched":0,"waiting_dispatch":19,
                          "concluded_passed":3,"concluded_rejected":3,
                          "concluded_pending":11,"concluded_uncertain":2,
                          "conservation_ok":true},
            "artifacts":[{"path":"25_review_candidates.jsonl","role":"main","records":28}],
            "report":{"status":"waiting_manual",
                      "review":{"trigger":"manual","enabled":true,"max_review_count":2},
                      "handover":{"passed_to":["operator_19","operator_20"],
                                  "failed_to":["operator_21"],
                                  "pending_to":["operator_25_platform"]}}}}}
```

`isLastOperator` 仍必传，用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile` + `result`，不再用 `false` 只回血缘对象。`data.outputFile` 是 `crossmodal-files-v1` 内容对象（`entrypoint=25_review_candidates.jsonl`，含 10 个产物 + `manifest.json`）；记录级/任务级 `status` 为 int（1/0）+ `process_status`，`data.result.status`/`reviewStatus` 为运行状态字符串。

失败：`{"code":1,"msg":"...","data":{}}`（参数不合法为 HTTP 422 + `code=1`）。

> 阶段末 `data.outputFile` 是**本阶段最终交付物**，不得当作下一算子的 `inputFile`；
> 下一阶段必须由工程侧重新上传。

## 6. 资源需求

| 项目 | 需求 |
|---|---|
| CPU / 内存 | 单进程纯规则处理，1 vCPU + 512MB 可跑（样例 33 行 < 1 秒） |
| GPU | 不需要 |
| 网络 | 不需要（不下载权重/数据） |
| 磁盘 | 与输入同量级（台账含记录副本，约为输入的 3–4 倍） |
| 端口 | 8125（`Dockerfile` `EXPOSE 8125`，`HEALTHCHECK` 探测 `/health`） |

## 7. 已知限制

1. **人工复核闭环缺失**：`waiting_manual` 记录需要人工平台回写结论（写回
   `processing["25"].review` 或 `review`）后重跑本算子才会闭环；平台未接入前
   `human_verified` 始终为 `false`，绝不伪造人工确认。
2. **证据解析依赖上游口径**：类别判定读取 `alignment` / `processing["NN"]` 的
   `status/decision/action/role/label/score`；上游若改用其它字段名，需要同步扩展
   `utils/dispatch.py` 的证据解析（已在测试中逐条固定口径）。
3. **「超过 2 次不再派单」的锁定去向**：本算子按全局规则（同一条最多 2 次、
   第 3 次锁定样本隔离库）把超限记录同时登记为 `未派单原因` 与 `样本隔离库`，
   统计上只计一次（`max_review_reached`/`locked`）。
4. **未触发时的类别统计**：`enabled=false` 时不区分三类，统一记为 `not_triggered`
   （不做派单判定）；开启后四条边界规则才逐条生效。
5. **主产物不含处理时刻**（为逐字节可复现），时间见台账 `event_time`、报告
   `generated_at`、`run.log`（`run()` 返回值另含 `startedAt`/`finishedAt`）。
6. `crossmodal-files-v1` / `crossmodal-manifest-v1` 为 demo 承载约定，**非甲方正式协议**；
   正式对象存储/分片协议到位时只需替换 `utils/transport.py`。
7. 样例中媒体路径为**引用不落地**（本算子不读媒体内容），如需带媒体验证请由上游提供真实文件。

## 8. 真实测试结果（本次实测）

```powershell
# 1) 单测：TDD 红灯 → 绿灯
python -m pytest tests/ -q
# 红灯（实现前）→ 4 errors during collection（No module named 'utils' / 'inference'）
# 绿灯（实现后）→ 136 passed, 1 skipped in 4.43s（两次复跑 4.43s / 5.62s）
#   skipped 1：当前 Windows 环境无创建符号链接权限（symlink 逃逸用例）

# 2) CLI 冒烟（模板要求命令，真实落盘）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
# exit 0, code=0, status=disabled（默认未触发）
# input_lines=33 blank_lines=1 input=32 rejected=2 valid=30 output=28
# origin_quarantined=2 duplicates=1 not_triggered=23 tasks_generated=0 conservation_ok=true

# 3) 开启路线（真实派单与结论回写）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli2 `
    --output-root output_smoke --override enabled=true
# exit 0, code=0, status=waiting_manual
# tasks_generated=19 dispatched=0 waiting_dispatch=19
# concluded_passed=3 concluded_rejected=3 concluded_pending=11 concluded_uncertain=2
# returned_to_pool=3 sample_quarantined=5（3 不通过 + 2 复核超限）
# auto_aligned_not_sampled=2 max_review_reached=2 locked=2 not_eligible=3 not_applicable=2
# conservation_ok=true

# 4) 平台接入 + 无结论样例（全部待人工）
python processor.py --input dataset/sample/demo_pending.jsonl --unique-id smoke_cli3 `
    --output-root output_smoke --override enabled=true --override manual_platform_available=true
# exit 0, tasks_generated=19 dispatched=19 concluded_pending=19 status=waiting_manual
# conservation_ok=true

# 5) 真实 HTTP 冒烟（uvicorn 子进程 + 真实 multipart，非 TestClient）
python tests/smoke_api.py 8135
# openapi paths=['/health','/predict','/removeFile','/uploadFile']（无 /downloadFile）
# upload 200 code=0 entrypoint=25_review_candidates.jsonl
# predict 统一 data=[operator,status,outputFile,result]（[v4.2] 无 lineageFile）tasks=4 conservation=True
# predict(mid/final) 均为 crossmodal-files-v1 fileCount=11（10 产物 + manifest.json）
# 非法 uniqueId → code=1；跨任务 inputFile → code=1；/downloadFile → 404 code=1
# removeFile 幂等 removed=True/False 且其他任务目录未被删除

# 6) 清单与语法
python -m json.tool map.json    # exit 0
python -m compileall -q .       # exit 0
```

**Docker**：开发机未安装 docker CLI，**未执行** `docker build`，不声称镜像已验证通过；
`Dockerfile` 按 CPU 纯服务口径静态提供（`python:3.11-slim`、`EXPOSE 8125`、`HEALTHCHECK /health`）。
