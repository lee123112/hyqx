# 管理人工复核任务 算子说明书

> 编制依据：甲方《光源一致性校验算子说明手册》格式 + 本地规则文档增补章节
> （部署方法、响应示例、时间戳说明、已知限制与降级行为）。
> 对应表1 v4.1 #25，正式编号 **25**，`stageId=S7`，`operator_version=v1.0.0`。

## 一、基础信息

| 项 | 内容 |
|---|---|
| 算子名称 | 管理人工复核任务 |
| 算子编号 | 25（表1 v4.1；旧 `demo/15_标注员确认与标注指派` 保持不动，非本编号） |
| 所属环节 | 第 7 环节「人工抽检复核」（`stageId=S7`，同环节还有 #24） |
| 工具类型 | 系统功能（模型列 `--`） |
| 是否必须 | 可选（手动触发） |
| 优先级 | P1 |
| 触发方式 | 手动（`enabled=false` 为默认，需显式开启） |
| 服务标识 | `manual_review_task_management`，端口 8125，入口 `app:app` |
| 交付形态 | FastAPI 三接口服务 + CLI（`processor.py` / `inference.py`）+ `map.json` + Dockerfile |

## 二、功能定义

按表1 #25 冻结口径，对上游产生的**存疑样本、#24 抽检异议样本、加工退回样本**执行：

1. **入队判定**：只认三类显式证据（见 README §1），非三类样本不生成任务；
2. **手动触发派单**：`enabled=true` 时按样本生成复核任务（含轮次、优先级 P1、派单状态）；
3. **结论登记与回写**：读取输入中的人工结论（不生成结论），通过 → 回候选池；
   不通过 → 标签`样本-语义不通`、样本隔离库；无结论/非法/存疑 → 留人工队列；
4. **边界控制**：不重复 #24 抽样；自动对齐且未被 #24 抽中不进队列；
   同一条最多复核 2 次，超限锁定且不再派单；
5. **异常去向**：文件/字段问题 → 原始隔离区；业务判定不通过 → 样本隔离库；
   人工未回写 → 人工队列（不拦其余样本）。

## 三、输出内容描述

| 产物 | 形态 | 说明 |
|---|---|---|
| `25_review_candidates.jsonl` | JSONL | 主输出，入口；透传上游 + `operator_25` / `processing["25"]` |
| `25_review_tasks.jsonl` | JSONL | 人工复核任务与派单状态（`dispatch.state/assignee`） |
| `25_review_conclusions.jsonl` | JSONL | 结论登记（`conclusion.verdict/round/human_verified`） |
| `25_sample_pool_return.jsonl` | JSONL | 复核通过 → `candidate_pool`（交 #19/#20） |
| `25_sample_quarantine.jsonl` | JSONL | 复核不通过（`isolate`）/ 超限锁定（`lock`）（交 #21） |
| `25_pending_manual.jsonl` | JSONL | 待人工队列 |
| `25_not_dispatched.jsonl` | JSONL | 未派单原因清单（含五类原因） |
| `25_original_quarantine.jsonl` | JSONL | 缺/重复 `sample_id`，保留原记录 |
| `25_rejects.jsonl` | JSONL | 坏行（≤200 字符预览） |
| `25_review_report.json` | JSON | 任务/结论/轮次/交接/统计汇总 |
| `manifest.json` / `run.log` | JSON/日志 | 运行清单、规范日志 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py` 仅保留通用工具函数、代码存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

统一台账字段：`label`、`stage`、`action`、`destination`、`event_time`、`batch_id`、
`sample_id`、`retry_count`、`uniqueId`、`runId`。取值表见 `dataset/DATA_SCHEMA.md` §2.3。

## 四、技术方案

- **无模型**：表1 #25 为系统功能，全部逻辑为确定性规则 + 上游证据解析，
  不使用模型、不产生任何分数（禁止固定分/随机分/规则分冒充模型分）；
- **入队判定顺序**：加工退回 → #24 抽检异议 → 存疑，命中即停，同时保留证据串；
- **轮次预算**：`review_count` / `review_history` / `processing["25"].review_round` 取最大值，
  ≥`max_review_count(2)` → 锁定不派单；否则 `next_round = 已复核数 + 1`；
- **人工结论解析**：`processing["25"].review`（幂等重跑）→ `operator_25.review` →
  `manual_review` / `human_review` / `review` → `metadata.*` → 扁平 `human_verdict`；
  结论取值归一化（含中文别名），任一维度 `fail` 覆盖整体，默认要求复核人身份；
- **守恒**：14 条守恒等式（见 `dataset/DATA_SCHEMA.md` §3）在运行时校验并写入
  `statistics.conservation_ok` / `conservation_failures`，任一不成立即记录失败项。

## 五、技术实现

```
25_管理人工复核任务/
  inference.py          # run / CLI / 统一阶段响应
  processor.py          # CLI 薄封装
  app.py                # 三接口（无 /downloadFile）
  configs/config.yaml   # 部署身份 + 业务参数（含冻结值校验）
  utils/
    io.py               # 流式 JSONL、路径安全、任务锁、安全解压、原子替换
    schemas.py          # pydantic 严格配置与 /predict 请求模型
    review.py           # 人工结论解析（只来自输入证据）
    dispatch.py         # 核心业务：类别判定 / 派单 / 结论回写 / 隔离 / 守恒
    lineage.py          # （v4.2 停用，存档）
    transport.py        # crossmodal-files-v1 内容对象与运行清单
    logger.py           # 规范日志
  dataset/ sample/ DATA_SCHEMA.md
  tests/  test_dispatch.py test_review(内置) test_operator.py test_transport.py
          test_api.py smoke_api.py conftest.py
  output/               # 空目录（运行时输出根）
```

关键实现约束：输出只写 `OUTPUT_DIR/<uniqueId>/`；`uniqueId` 校验为单目录名；
`inputFile` 必须落在当前任务目录内；JSONL 边读边写；坏行/缺字段/重复标识进独立台账；
UTF-8 输出 + `os.replace` 原子替换；同一 `uniqueId` 串行（跨进程文件锁）。

## 六、资源要求

| 项目 | 要求 |
|---|---|
| CPU | 1 vCPU（纯规则，样例 33 行 < 1 秒） |
| 内存 | 512 MB |
| GPU / 网络 | 不需要 |
| 磁盘 | 输入同量级（台账含记录副本，约 3–4 倍） |
| 运行环境 | Python 3.9+（实测 3.9.7），依赖见 `requirements.txt` |

## 七、性能指标

本次为算子级功能验证，**未做并发压测**。实测（Windows / CPython 3.9.7）：

- 样例（33 行 / 28 条记录）单次运行 < 1 秒；
- 单测 136 通过（4.43s）；真实 HTTP 冒烟全链路（上传 → 阶段内 → 阶段末 → 链式 → 删除）通过；
- 吞吐/并发/大文件性能待工程侧在正式环境按数据量评估（单进程 + 文件锁已保证同 `uniqueId` 串行）。

## 八、输入输出示例

**输入记录（节选，含人工结论证据）**：

```json
{"sample_id": "unc-01", "scene_id": "scene-hyd", "metadata": {"batch_id": "batch-hyd-2026w41"},
 "text": {"content": "金毛犬在草地上接飞盘（unc-01）", "language": "zh"},
 "image": {"path": "media/unc-01.png"},
 "alignment": {"type": "uncertain", "status": "uncertain", "auto_aligned": false},
 "review": {"verdict": "pass", "reviewer": "reviewer-01", "reviewed_at": "2026-10-08T10:00:00+08:00",
            "round": 1, "source": "人工复核工作台"}}
```

**主输出（节选）**：

```json
{"sample_id": "unc-01", "...": "上游字段原样保留",
 "operator_25": {"operator": "manual_review_task_management", "operator_version": "v1.0.0",
                 "stage_id": "S7", "category": "uncertain", "decision": "passed",
                 "label": "流程-复核通过", "destination": "候选池", "action": "keep",
                 "review_round": 0, "next_round": 1, "human_verified": true,
                 "review_required": false, "locked": false, "source_reference": "operator_25"},
 "processing": {"25": {"decision": "passed", "status": 1, "process_status": "passed"}}}
```

**任务台账（节选）**：

```json
{"task_id": "task-prev_task-unc-01-r1", "sample_id": "unc-01", "category": "uncertain",
 "round": 1, "previous_rounds": 0, "max_review_count": 2, "priority": "P1", "trigger": "manual",
 "status": 1, "process_status": "waiting_manual",
 "dispatch": {"state": "waiting_manual", "platform_available": false, "assignee": null,
              "dispatched_at": null, "reason": "人工复核平台未接入，任务停在待派单状态"},
 "label": "流程-复核待人工", "action": "waiting_manual", "destination": "人工复核队列",
 "uniqueId": "prev_task", "runId": "run001", "event_time": "2026-10-08T16:00:00+08:00"}
```

## 九、部署方法

```powershell
pip install -r requirements.txt
$env:OUTPUT_DIR = "output"
python -m uvicorn app:app --host 0.0.0.0 --port 8125
# 健康检查
curl http://127.0.0.1:8125/health
```

Docker（静态提供，**本次未执行 build**）：

```bash
docker build -t operator-25:1.0.0 .
docker run -p 8125:8125 -v /data/output:/data/output operator-25:1.0.0
```

## 十、响应与时间戳说明

- v4.2 起 `/predict` 形态二统一返回 `{code,msg,data{operator,status,outputFile,result}}`：
  `data.outputFile` 为 `crossmodal-files-v1` 内容对象（文本 UTF-8、二进制 base64），
  `data.result` 为业务摘要（`operator`/`operatorVersion`/`stageId`/`uniqueId`/`runId`/`status`/
  `reviewStatus`/`entrypoint`/`inputFile`/`statistics`/`artifacts`/`report`）；
- `isLastOperator` 仍必传，用于标记阶段末调用；v4.2 起中间与阶段末响应同构，
  均返回 `outputFile` + `result`，不再用 `false` 只回血缘对象；
- 时间格式 `YYYY-MM-DDTHH:MM:SS+08:00`（东八区）；
- **主产物 `25_review_candidates.jsonl` 刻意不写处理时刻**（保证同一输入逐字节可复现）；
  处理时刻记录在台账 `event_time`、报告 `generated_at`、`run.log`
  （`run()` 返回值另含 `startedAt`/`finishedAt`）；
- `data.outputFile` 为本阶段最终交付物，不得当作下一算子输入路径。

## 十一、已知限制与降级行为

见 `README.md` §7、§4；要点：

1. 人工平台未接入时任务只能停在 `waiting_manual`，`human_verified=false`，**不伪造人工确认**；
2. 类别/结论证据解析依赖上游字段口径（`processing["NN"]` 命名空间）；
3. 超限锁定按全局规则（第 3 次锁定样本隔离库）实现，统计只计一次；
4. `crossmodal-files-v1` / `crossmodal-manifest-v1` 为 demo 承载约定，非甲方正式协议；
5. 未做并发压测与 Docker 实机验证。
