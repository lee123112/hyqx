# 算子 20：人工抽检复核

为规则触发或抽样选中的句对建立**可交给人工的复核队列**，并安全接收工程侧回写的人工结果；没有人工工作台/人工结果时仍保留真实待办，**不伪造审核完成**。

治理策略 `risk_review_v2` 区分两类队列：规则触发异常为
`review_requirement=mandatory`、`delivery_blocking=true`；普通合格记录的随机抽检为
`review_requirement=audit_sample`、默认 `delivery_blocking=false`。非阻塞抽检仍保持
`review_status=waiting_manual`，但不会仅因尚未回写而把顶层执行状态改为
`waiting_manual`。`max_sample_count` 只限制普通抽样，永远不会截掉强制复核项。

- 算子编号：20（阶段「人工抽检子流程」，本阶段只有该算子，阶段末必须传 `isLastOperator=true`）
- 算子 id：`manual_spot_check_review`；版本 `v1.1.0`；类型：规则型，**不加载任何模型**
- 端口：8020；接口：`POST /uploadFile?uniqueId=`、`POST /predict`、`POST /removeFile?uniqueId=`（**不实现 `/downloadFile`**）
- 主输出：`20_review_queue.jsonl`；另有 `20_reviewed.jsonl`、`20_review_rejects.jsonl`、`artifacts/stage_20_<uniqueId>.zip`、`run.log`（**2026-10-19 起不再输出 `lineage.json`**）

## 1. 功能边界

负责：抽样入队、队列 schema、人工结果校验、幂等合并与回流字段、异常清单、阶段产物打包、三接口。

不负责（属其他节点或系统能力）：

- 标注工作台、账号认证、人工身份真实性校验（demo 明确不做）；
- 语义等价（14）、流畅度（15）、领域（16）、实体一致性（17）、分级（13）、语言检测（02/辅助）；
- 交付打包（18/19）与数据增强（21–24）。交付节点必须同时读取 `review_requirement` 与 `delivery_blocking`：强制待人工记录不得进入交付，普通风险抽检队列本身不构成交付阻断。

## 2. 输入 / 输出字段

输入：

- 主输入：待复核候选 JSONL（字段与异常口径见 `dataset/DATA_SCHEMA.md`），记录标识取 `record_id` 或 `id`；
- 可选侧输入：人工结果 JSONL，通过 `/predict` 的 `config.review_payload_file`（路径）或 `config.review_payload`（内联数组）传入。

输出：`20_review_queue.jsonl`（`review_status=waiting_manual`，含 `trigger_reasons`/`evidence_summary`/`source_ref`）、`20_reviewed.jsonl`（含 `review_status`/`reviewer_id`/`reviewed_at`/`review_reason`/`reviewed_grade`/`review_conflict`）、`20_review_rejects.jsonl`；成功分支另产出 `artifacts/stage_20_<uniqueId>.zip`（队列 + 异常清单 + `run.log`）。

**队列与已复核记录都不复制完整原文**，通过 `source_ref`（输入文件名 + 行号 + record_id）回查；`source`/`target` 只用于写入 `evidence_summary` 的长度。

## 3. 抽样与状态口径

- 触发命中（`grade_c`/`low_confidence`/`conflict`/`review_required` 或显式原因）**一定入队**；
- 其余候选按 `sample_ratio` 随机抽样，`k = ceil(池规模 × sample_ratio)`；
- `min_sample_count` 保证下限；`max_sample_count`（0 = 不限）限制普通抽样预算，但绝不截断强制复核项；
- 候选先按 `(record_id, line)` 排序，再用 `random.Random(random_seed)` 抽样，**相同输入 + 配置 + 种子必定得到同一集合**；
- 状态优先级：`failed` > `partial` > `waiting_manual` > `success`。

## 4. 人工结果与降级行为

- 允许状态：`accepted`、`rejected`、`needs_revision`、`waiting_manual`；
- 必填：`record_id`、`review_status`、`reviewer_id`、`reviewed_at`、`review_reason`；`reviewed_grade` 可选；
- 非法状态 / 缺 reviewer / 缺时间 / 时间不可解析 / 缺原因 / 目标不存在 → 进 `20_review_rejects.jsonl`，不改变记录状态；
- 重复一致提交幂等（同一任务目录内 `review_state.json` 持久化，跨次运行也幂等）；
- 互相冲突的结果**不自动覆盖**，标记 `review_conflict` 并写入异常清单，原结果保留；
- **没有人工结果时只生成队列**，不会把记录改成 `accepted`；存在强制复核待办时返回 `waiting_manual`，仅有普通风险抽检待办时返回 `success`。

**本算子无模型、无外部服务**，因此不存在「模型不可用降级」；唯一的非正常业务状态是 `waiting_manual`（等待人工）与 `partial`（存在拒绝/冲突），均不伪造任何分数。

## 5. 运行

```bash
pip install -r requirements.txt

# CLI：默认抽样（waiting_manual）
python processor.py --input dataset/sample/demo.jsonl --unique-id demo_file1

# CLI：带工程侧人工结果
python processor.py --input dataset/sample/demo.jsonl --unique-id demo_file1 \
    --review-file dataset/sample/reviews.jsonl

# 服务
python inference.py
# 或 uvicorn inference:app --host 0.0.0.0 --port 8020
```

输出根目录默认为算子目录下 `output/`，部署时用 `OUTPUT_DIR` 指定；任务目录为 `OUTPUT_DIR/<uniqueId>/`。

## 6. 接口示例

外层响应固定 `{code,msg,data}`。无血缘改造（2026-10-19）后，成功时 `data` 固定为
`outputFile` / `rejectFile` / `stageZip` / `storageUri` 四个字段（**不再有 `lineageFile`**）；
中间调用 `data.outputFile` 是当前临时队列路径，阶段末 `data.outputFile` 是队列文件的 UTF-8 文本内容（不是路径）。

`/predict` 请求体支持 `operatorId`（可选，必须等于 `"20"`）、`config` 与 `params`（先合并 `config`，再用 `params` 覆盖，`params` 优先）。

```bash
# 1) 上传候选（同一 uniqueId 可再上传 reviews.jsonl）
curl -X POST "http://127.0.0.1:8020/uploadFile?uniqueId=task1_file1" -F "file=@dataset/sample/demo.jsonl"

# 2) 阶段中间（isLastOperator=false）：返回四字段，outputFile 为临时路径
curl -X POST "http://127.0.0.1:8020/predict" -H "Content-Type: application/json" -d "{\"uniqueId\":\"task1_file1\",\"isLastOperator\":false,\"inputFile\":\"input/demo.jsonl\",\"config\":{\"sample_ratio\":1.0,\"min_sample_count\":0}}"

# 3) 阶段末（isLastOperator=true）并回写人工结果
curl -X POST "http://127.0.0.1:8020/predict" -H "Content-Type: application/json" -d "{\"uniqueId\":\"task1_file1\",\"isLastOperator\":true,\"inputFile\":\"input/demo.jsonl\",\"config\":{\"sample_ratio\":1.0,\"min_sample_count\":0,\"review_payload_file\":\"input/reviews.jsonl\"}}"

# 4) 删除任务目录
curl -X POST "http://127.0.0.1:8020/removeFile?uniqueId=task1_file1"
```

`/predict` 必传 `uniqueId` 与布尔 `isLastOperator`；阶段内后续算子必须传 `inputFile`（取上一中间响应 `data.outputFile`）。`/downloadFile` 返回 404。

## 7. 输出与阶段产物示例

队列记录（节选）：

```json
{"record_id": "r02", "review_status": "waiting_manual", "trigger_reasons": ["grade_c", "low_confidence"],
 "selection_source": "triggered", "evidence_summary": {"quality_grade": "C", "alignment_confidence": 0.55, "source_chars": 17},
 "source_ref": {"input": "demo.jsonl", "line": 2, "record_id": "r02"},
 "sampling_config": {"sample_ratio": 0.1, "min_sample_count": 1, "max_sample_count": 0, "random_seed": 20261005}}
```

阶段产物（成功/部分成功/待人工分支）：

```
OUTPUT_DIR/<uniqueId>/
  20_review_queue.jsonl
  20_reviewed.jsonl
  20_review_rejects.jsonl
  run.log
  artifacts/stage_20_<uniqueId>.zip    # 成员：20_review_queue.jsonl / 20_review_rejects.jsonl / run.log
```

`storageUri`：设置 `OBJECT_STORE_DIR` 时为 `file://...`，否则为 `local-mirror://<uniqueId>/<zip>`。
【无血缘改造 2026-10-19】任务目录不再产出 `lineage.json`，zip 内也不含任何血缘成员；原血缘实现保留在代码注释中便于回溯。

## 8. 配置

配置项位于 `configs/config.yaml`，可通过 `/predict` 的 `config` 对象覆盖（无血缘改造后 `params` 亦可覆盖且优先生效）；未知键、越界比例、`min > max`、非法状态枚举都会被拒绝并返回 `failed`。主要项：`sample_ratio`、`min_sample_count`、`max_sample_count`、`random_seed`、`trigger_reasons`、`review_statuses`、`require_reviewer`、`require_reviewed_at`、`require_review_reason`、`validate_reviewed_at`、`require_known_target`、`mask_reviewer_id`、`max_reason_chars`。
【无血缘改造 2026-10-19】配置中无血缘专用项，无需注释停用。

### 8.1 规范参数映射

| 规范参数 | 本算子配置键 | 说明 |
| --- | --- | --- |
| `param_抽样比例`（规范 4.19） | `sample_ratio` | 0~1 的比例；`k = ceil(池规模 × sample_ratio)` |
| `param_抽样策略`（规范 4.19） | `trigger_reasons` + `ordinary_sample_blocking` + `random_seed` | 规则触发命中必入队；普通记录按种子随机抽检；是否阻塞由策略开关控制 |
| `param_复核指标`（规范 4.19） | `review_statuses` + `require_reviewer`/`require_reviewed_at`/`require_review_reason` | 人工复核结果四态与必填校验口径 |

## 9. 资源需求

纯 CPU 规则型算子：单核 + 512MB 内存即可；记录数规模受候选数量影响，抽样只保留证据摘要（不含原文），内存占用与候选数线性且远小于原始语料。**无 GPU / 无模型权重需求。**

## 10. 测试与真实结果

```bash
python -m pytest tests/ -q          # 51 passed（无血缘改造后实测）
python -m json.tool map.json        # OK（port 8020）
python -m compileall -q .           # exit 0
```

真实结果（无血缘改造 2026-10-19 后实测）：

- 当前回归测试 **51 passed in 3.77s**（Python 3.9.7，`D:\python\python.exe -m pytest tests -q`）；无血缘改造前基线为 50 passed，新增 `operatorId`/`params` 覆盖用例与阶段产物断言。
- CLI（默认抽样）：`status=waiting_manual`，`candidate_count=10 triggered_count=4 sampled_count=5 queue_written_count=5`；产出 `artifacts/stage_20_smoke_cli.zip`（成员 `20_review_queue.jsonl`/`20_review_rejects.jsonl`/`run.log`），`storageUri=local-mirror://smoke_cli/stage_20_smoke_cli.zip`，任务目录无 `lineage.json`。
- API 契约测试：中间/阶段末 `data` 均为四字段；`/downloadFile` → 404；缺 `isLastOperator`/缺 `inputFile`/非法 `uniqueId`/目录穿越均 `code=1`；两个 `uniqueId` 队列与已复核文件互不污染。

测试覆盖：抽样比例边界（0.0/1.0）、固定种子可复现、`min`/`max` 边界、触发命中必入队且不受上限截断、强制复核阻塞、普通风险抽检非阻塞、队列 ID 一致性、队列不复制原文、四态回写、缺 reviewer/时间/原因、非法状态、时间不可解析、目标不存在、重复一致幂等、跨次运行幂等、冲突不覆盖、payload 内冲突不落地、内联 payload、payload 文件缺失、损坏 JSONL/非对象行/缺 id/重复 id、空文件、错误编码、非法配置、输入=输出路径保护、任务隔离、阶段产物 zip 与无血缘文件、UTF-8、日志不泄露人工备注、三接口契约与路径安全。

## 11. 已知限制与未确认口径

- **无模型、无降级分数**：本节点不推理、不产生任何分数，因此不存在伪造分数的代码路径。
- 「本 demo 不提供标注工作台」是计划明确写明的边界；`review_payload_file` 的正式传递方式（独立上传文件 vs 工程侧直接注入内联数组）待集成确认。
- `sample_ratio=0.1`、`min_sample_count=1`、`random_seed=20261005`、`low_confidence_threshold=0.7`、`c_grade_values=[C]` 均为 demo 默认值，正式抽检比例与阈值需任务方确认；【无血缘改造 2026-10-19】配置快照仍写入 `run.log` 与阶段产物，但不再写入 `lineage.json`。
- `risk_review_v2` 是本轮明确的正式治理变更：强制复核未完成才阻塞；普通合格记录的风险抽检默认不阻塞，不再采用「任意队列都 waiting_manual」的旧口径。
- `reviewer_id` 脱敏规则（首尾 2 字符 + `***`）为本实现口径；真实身份校验不在本算子范围。
- 输入路径与输出路径相同会被拒绝（防截断）；`removeFile` 仍按 MTY 口径按 `uniqueId` 删除整个任务目录。
- 上游字段名（13 的 `quality_grade`、14–17 的低置信/冲突字段）已做成可配置项，正式字段名待集成确认。
- 未运行 `docker build`（本机无可用 Docker 守护进程），Dockerfile 仅静态编写。
