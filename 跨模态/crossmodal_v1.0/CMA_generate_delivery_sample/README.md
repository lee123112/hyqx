# 算子 26 生成交付样例（delivery_sample）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

表1 v4.2 第 8 环节「交付样例」（`stageId = S8`，§4.26 修正原 S7），
正式编号 **#26**，优先级 P2、可选、默认开启，类型 = 系统功能，**模型列 = `--`**
（无模型、不产出任何分数）。业务代码为 `inference.py` + `utils/sampling.py`。

> 一句话：**从 #23 可交付语料包里，每条已开启路线抽至多 10 条，导出一份验收人员
> 打开即可看全本版样貌的 Excel 样例清单，并附样例报告。**

---

## 1. 功能边界

| 维度 | 口径 |
| --- | --- |
| 输入 | #23 可交付语料包：ZIP（`23_deliverable_package.zip`）、包含该包的目录，或包入口 `train/samples.jsonl` |
| 门禁 | **只从可交付包抽样**：包 `version_state=deliverable` 或 `status=可交付`；`require_deliverable_source=true` 时非可交付直接 `failed` |
| 抽样 | 每条**已开启路线**最多 10 条（表1 只读上限，请求与配置均不可放宽）；不足 10 条按实际条数导出并在报告中写明 |
| 输出 | Excel 样例清单（主交付物）+ 机器可读样例清单 + 样例报告 + 三类独立台账 + 清单/日志 |
| 不做 | 预览页、波形图、HTML；不生成任何分数；不写回 #23 包；不改用隔离区数据 |
| 隔离样本 | #21 处理标记或包内 `materials/isolation_list.jsonl` 命中 → 只登记进 `26_excluded.jsonl`，**绝不参与抽样、绝不顶替** |
| 完成态 | 验收人员只打开 `26_sample_list.xlsx` 就能看到这一版由哪些样本组成、模态、描述摘要、标注结论与评分结论 |
| 状态 | `success` / `degraded`（来源包不可交付、入口为空、算子被关闭、读取中断）/ `failed`（全部行不可用、守恒校验失败、strict 来源门禁） |

**降级与失败一律不伪造**：取不到的评分/标注字段写「未提供」，`human_verified=false`，
`review_required=true`，等待人工抽检确认。

## 2. 输入字段

包级（可选，缺失时按记录级标记判定并在报告 `source.deliverable_evidence` 说明）：

| 来源 | 字段 | 用途 |
| --- | --- | --- |
| `manifest.json` / `version.json` | `version_state`、`status` | 判定「可交付」 |
| `version.json` / `materials/version_notes.json` | `enabled_routes` | 定义「已开启路线」（默认以包为准） |
| `version.json` / `version_notes.json` | `dataset_version`、`annotation_granularity` | 报告与 Excel「数据版本」列 |
| `materials/isolation_list.jsonl` | `sample_id` | 只读，用于排除隔离样本 |

记录级（保留上游全部字段，按优先级取值，取不到即「未提供」）：

| 目标列 | 取值优先级 |
| --- | --- |
| 样本ID | `sample_id` → `id` → `pair_id`（缺失 → `26_rejects.jsonl`） |
| 路线 | `routes`/`route` → `operator_22`/`operator_23` → `processing["22"]`/`["23"]` → 由 `modalities`/`image`/`audio`/`video` 推导 |
| 模态 | `modalities` → 由 `image`/`audio`/`video`/`text` 键推导；Excel 列取路线中文标签（图文/音文/视文） |
| 描述摘要 | `description` → `text.content` → `text.description` → `text.caption` → `caption` → `alt` → `title` → `transcript` → `metadata.description`（按 `description_summary_chars` 截断，`…` 结尾） |
| 标注结论 | `processing["16"]` → `operator_16` → `alignment.whole` → `alignment` → `processing["17"/"18"]` 的 `conclusion`/`decision`/`status` |
| 评分结论 | 按路线取 `processing["12"/"13"/"14"]` → `operator_12/13/14` → `image_text_score`/`audio_text_score`/`video_text_score` 的 `score`/`value`/`clip_score`/`similarity`/`cosine`/`semantic_score` |
| 交付标记 | `operator_23.decision/version_state/status` → `processing["23"]` |
| 隔离 | `processing["21"]`、`isolated`、`status/decision ∈ {isolated, quarantined, …}`、隔离清单命中 |

## 3. 配置（`configs/config.yaml`）

只读口径（写错即启动失败，见 `utils/schemas.py`）：

| 配置项 | 允许值 | 依据 |
| --- | --- | --- |
| `format` | 只能是 `xlsx` | 表1 #26「导出 Excel」 |
| `source_status` | 只能是 `可交付` | 表1 #26「只从可交付包抽样」 |
| `max_samples_per_route` | `1..10` | 表1 #26「每条已开启路线最多抽 10 条」 |
| `transport.format` | 只能是 `crossmodal-files-v1` | 跨模态共享契约 |

可调项：`enabled`、`sampling_mode`（`random`/`first`）、`sampling_seed`、
`description_summary_chars`、`max_embedded_record_bytes`、`require_deliverable_source`、
`require_deliverable_record`、`route_source`（`package`/`config`）、`routes.*`、
`transport.max_response_bytes`。未知键一律报错（`extra=forbid`）。

`POST /predict` 的请求体只允许覆盖 `max_samples_per_route` 与 `sampling_seed`
（白名单），且仍受表1 上限与下限约束；**已开启路线由交付包 `enabled_routes` 决定**
（`route_source=package`），请求 `config.routes`/`config.route_source` 不参与覆盖、也不被接受。
兼容计划文档写法：`operator.settings` 里的 `key=value` 字符串会被提升为顶层配置（不覆盖显式项）。

## 4. 算法/模型后端

**无模型**。全部为确定性规则判定：JSONL 流式解析 + 字段证据抽取 + 每条已开启路线
确定性伪随机抽样（`sha256(seed|sample_id)` 排序取前 N，可复现并给出抽样指纹）。
`statistics.model = null`、`rule_based = true`、`model_scores_generated = false`。

## 5. 输出

写入 `OUTPUT_DIR/<uniqueId>/delivery_sample/runNNN/`：

| 文件 | 说明 |
| --- | --- |
| `26_sample_list.xlsx` | **主交付物**：工作表「交付样例」（序号/样本ID/模态/描述摘要/标注结论/评分结论/路线/来源批次/交付标记/数据版本）+「样例报告」（来源包、抽样、路线不足、边界口径） |
| `26_samples.jsonl` | 机器可读样例清单（上游字段全保留 + `operator_26` 命名空间 + `processing["26"]`），阶段内传递入口 |
| `26_skipped.jsonl` | 旁路台账：路线未开启、无可映射路线、超出每路线上限 |
| `26_excluded.jsonl` | 旁路台账：隔离样本、非可交付记录 |
| `26_rejects.jsonl` | 原始隔离区：坏 JSON、非对象、编码错误、缺 `sample_id`、重复 `sample_id`、非法媒体引用 |
| `26_sample_report.json` / `26_sample_report.md` | 样例报告（机器/人类可读） |
| `manifest.json`、`26_run.log` | 产物清单、逐行日志 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py` 仅保留通用工具函数、代码存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### Excel 样式

- 真实 OOXML，由标准库 `zipfile` 组装（`utils/xlsx.py`）：**运行期不需要 openpyxl/xlsxwriter**；
- 同一输入 + 同一时间戳 → 字节完全一致（可校验 sha256）；
- 表头加粗+底色、列宽、冻结首行、首行筛选；`None` → 空单元格，数字/布尔保留原生类型；
- 单元格超过 32767 字符自动截断加 `…`；XML 非法控制字符剔除；表名非法/超长/重名显式失败。

### 响应示例（v4.2）

形态一（`POST /predict?detection=generate_delivery_sample`，`multipart/form-data`，`file` 为最终可交付包 ZIP）：

```json
{"status":1,"uniqueId":"region_a","process_status":"success",
 "delivery_sample":"26_sample_list.xlsx",
 "routes":{"image_text":{"sample_count":6,"max_count":10,"available":13,"insufficient":true},
           "audio_text":{"sample_count":3,"max_count":10,"available":5,"insufficient":true},
           "video_text":{"sample_count":2,"max_count":10,"available":4,"insufficient":true}},
 "source_package_version":"v1.0.0-demo","sample_count":11}
```

不可生成（如包仍为待发布）时业务响应为
`{"status":0,"uniqueId":"region_a","process_status":"degraded","msg":"来源包版本状态为 pending_release（非可交付），按表1 #26 不从中抽样；也未改用隔离区数据。","source_package_version":"v1.0.0-demo"}`
（`require_deliverable_source=true` 时 `process_status=failed`）；
缺 `file`、非 ZIP、空内容等参数错误只回 `{"status":0,"msg":"..."}`。
形态一可带表单标量 `per_route_max`（映射 `max_samples_per_route`）与 `sampling_seed`，未知字段忽略。

形态二（`POST /predict`，JSON body，`isLastOperator` 必传）：

```json
{"code":0,"msg":"样例清单已生成：11 条。","data":{
  "operator":"delivery_sample","status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"26_samples.jsonl",
    "file_count":9,"total_bytes":0,
    "files":[{"path":"26_samples.jsonl","size":0,"sha256":"...","encoding":"utf-8","content":"..."}]},
  "result":{"uniqueId":"region_a","runId":"run001","status":"success",
            "message":"样例清单已生成：11 条。",
            "statistics":{"input_file":"demo.jsonl","input_records":18,"valid_records":13,
                          "sampled_records":11,"excluded_records":2,"bad_lines":2,
                          "origin_quarantined":3,
                          "route_sampled":{"image_text":6,"audio_text":3,"video_text":2},
                          "route_insufficient":["image_text","audio_text","video_text"],
                          "conservation_ok":true,"model":null,"rule_based":true},
            "report":{"format":"crossmodal-delivery-sample-report-v1","status":"success",
                      "route_source":"package",
                      "source":{"version_state":"deliverable","status":"可交付",
                                "dataset_version":"v1.0.0-demo",
                                "enabled_routes":["image_text","audio_text","video_text"]},
                      "routes":{"image_text":{"route":"image_text","modal":"图文",
                                              "available":13,"sampled":6,"limit":10,
                                              "insufficient":true}}},
            "delivery_sample":"26_sample_list.xlsx",
            "routes":{"image_text":{"sample_count":6,"max_count":10,"available":13,
                                    "insufficient":true}},
            "source_package_version":"v1.0.0-demo"}}}
```

`isLastOperator` 仍必传，用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile` + `result`，不再用 `false` 只回血缘对象。`data.outputFile` 是 `crossmodal-files-v1` 内容对象（入口 `26_samples.jsonl` + Excel + 报告 + 台账 + manifest，共 9 个文件）；`data.result.status` 为运行状态字符串，记录级 `operator_26.status` 为 int（1/0）+ `process_status`。

## 6. 启动

```bash
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8126     # 或 python app.py
```

环境变量：`OUTPUT_DIR`（输出根目录，默认 `./output`）、`PORT`（默认 8126）、
`MAX_UPLOAD_BYTES`（默认 512 MiB）、`OPERATOR_LOG_LEVEL`。

## 7. 三接口

| 接口 | 说明 |
| --- | --- |
| `POST /uploadFile?uniqueId=` | 上传输入包（`.zip`），解压到 `OUTPUT_DIR/<uniqueId>/input`，返回入口路径 |
| `POST /predict` | `{uniqueId, isLastOperator, inputFile?, config?/overrides?}` |
| `POST /removeFile?uniqueId=` | 幂等清理本任务目录 |

`/downloadFile` **未注册**（`/health` 返回 `"downloadFile": "not_registered"`）。
所有响应为 `{"code": 0|1, "msg": "...", "data": {...}}`。

```bash
# 上传 + 形态二（isLastOperator=false/true 同构，均返回 outputFile + result）
curl -X POST "http://127.0.0.1:8126/uploadFile?uniqueId=region_a" -F "file=@23_deliverable_package.zip"
curl -X POST http://127.0.0.1:8126/predict -H "Content-Type: application/json" \
     -d '{"uniqueId":"region_a","isLastOperator":false}'

# 阶段内续跑：inputFile 用上一步 outputFile 内容对象恢复出的任务内相对路径
curl -X POST http://127.0.0.1:8126/predict -H "Content-Type: application/json" \
     -d '{"uniqueId":"region_a","isLastOperator":true,
          "inputFile":"delivery_sample/run001/26_samples.jsonl"}'

# 形态一：单文件流（最终可交付包 ZIP，?detection=generate_delivery_sample）
curl -X POST "http://127.0.0.1:8126/predict?detection=generate_delivery_sample&uniqueId=region_b" \
     -F "file=@23_deliverable_package.zip"

curl -X POST "http://127.0.0.1:8126/removeFile?uniqueId=region_a"
```

`isLastOperator=true` 时不再返回 `lineageFile`：`data.outputFile` 为 `crossmodal-files-v1`
文件内容对象（入口 `26_samples.jsonl` + Excel + 报告 + 台账 + manifest），
`data.result` 为业务摘要；`false` 与 `true` 的 `data` 结构完全同构。

## 8. CLI

```bash
python processor.py --input dataset/sample/demo.jsonl \
    --unique-id smoke_cli --output-root output_smoke
python processor.py --input E:/pkg/23_deliverable_package.zip \
    --unique-id region_a --output-root D:/out --is-last-operator
python processor.py --input package_dir --unique-id t1 --max-samples-per-route 3
```

退出码：`0` 成功/降级，`1` 失败或参数错误。

## 9. 资源需求

纯 CPU、单进程；内存与「同时保留的候选记录数」成正比（每条路线收集候选后再抽样；
10 万行量级建议 `max_samples_per_route` 保持默认并关注内存）。Excel 生成占内存
与样例条数成正比（至多 3 × 10 = 30 行，可忽略）。无 GPU、无网络、无模型下载。

## 10. 已知限制

1. 每条路线只按记录的**主路线**归属一次，不做二次路线回填（多路线记录计入
   `multi_route_records`），避免重复计数。
2. 候选池需按路线聚合后再抽样，因此不是「零缓冲」流式；台账（skipped/excluded/rejects）
   已随读随写，主清单在抽样之后写出。
3. 隔离判定同时依据记录标记与包内隔离清单（只读），不读取隔离区目录本身。
4. 「路线不足 10 条」在本样例数据中三条路线都会出现，属正常业务情形（表1 要求写明）。
5. 上游字段命名在不同算子版本间存在差异，本算子按上表优先级兼容；若上游新增字段
   命名口径，需要同步更新 `utils/sampling.py` 的取值链。

## 11. 测试与验证（真实结果）

```bash
python -m pytest tests/ -q                                  # 207 passed
python processor.py --input dataset/sample/demo.jsonl \
    --unique-id smoke_cli --output-root output_smoke        # 样例条数=11，状态 success
python tests/smoke_api.py 8126                              # 真实 uvicorn + httpx 全链路
python -m json.tool map.json
python -m compileall -q .
```

样例数据（`dataset/sample/demo.jsonl`，仓库自带合成样例）：18 条非空记录中
11 条可交付样本（图文 6 / 音文 3 / 视文 2）、2 条隔离样本（1 条 #21 标记、1 条隔离清单）、
3 条原始隔离（缺 ID / 重复 ID / 绝对路径引用）、2 条坏行（损坏 JSON / 非对象）、1 空行；
输出 11 条样例，统计守恒校验通过。

Docker：提供 `Dockerfile`，本次**未运行** `docker build`（见 `DEVELOPMENT_STATUS.md`）。
