# 算子 #24：抽检并回写质量报告（`spot_check_writeback` v1.0.0）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

对应表1 #24：第 7 环节「人工抽检复核」（`stageId=S7`），P0 **必须算子**、默认开启且不能关。
无自训练模型（模型列 `--`），**不做任何模型打分**。

上游：#19 候选池（含 #16 自动对齐结果） + #20 待回写质量报告。
下游：#23（按回写状态二次封版）、#21（判错样本隔离）、#25（异议/待人工复核）。

## 1. 功能边界

**做**（严格按表1 #24，不吸收相邻算子职责）：

- 按**场景 / 模态 / 对齐类型**三个维度分层抽检，默认 10%、可选 10%–20%，自动对齐样本也进入抽样框；
- 只从**输入证据**读取资深标注员复核结论（语义 / 时序 / 场景），并做归一化与合法性校验；
- 判对 → 结论写回质量报告；判错 → 标签【样本-语义不通】、进样本隔离库、**取消自动对齐**；
  存疑 → 异议清单（#21/#25）；待人工 → 待人工清单（#25）；
- 回写状态交 #23：抽检样本全部有最终结论才 `status=written_back` / `version_state=deliverable`，
  否则保持 `waiting_writeback` / `pending_release`（**未回写不得封版**，但不拦其余样本）；
- 只写 `OUTPUT_DIR/<uniqueId>/`，三接口，UTF-8 原子落盘，统计守恒。

**不做**：

- 不实现 `/downloadFile`，不实现旧版四接口；
- **不产生任何人工结论或分数**：未接入人工平台时全部抽检样本判「待人工」，报告保持待回写；
- 不重复 #20 的媒体可打开性/解码/困惑度检查；不读媒体内容，也不在算子间复制媒体；
- 不因本算子判定把非抽检样本移出主输出（判错样本除外）。

## 2. 快速开始

```powershell
cd "G:\HYQX\算子\跨模态\demo_10.6\24_抽检并回写质量报告"

# 0) 生成确定性样例（不下载任何外部数据）
python dataset/sample/make_sample.py

# 1) 单元测试（真实结果：154 passed）
python -m pytest tests/ -q

# 2) CLI 冒烟（与 HTTP /predict 共用同一实现）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 3) 真实 HTTP 冒烟（uvicorn 子进程 + httpx，真实结果：34/34）
python tests/smoke_api.py

# 4) 静态检查
python -m json.tool map.json
python -m compileall -q .

# 5) 启动服务
python -m uvicorn app:app --host 0.0.0.0 --port 8124
```

## 3. 目录结构

```
24_抽检并回写质量报告/
├── app.py                 # 三接口：/uploadFile、/predict、/removeFile（无 /downloadFile）
├── inference.py           # 编排入口（两遍流式扫描 + 抽样 + 回写）
├── processor.py           # CLI 薄封装（--input/--unique-id/--output-root/--override/--is-last-operator）
├── configs/config.yaml    # 部署配置（身份 + 表1 冻结值 + 业务可调项）
├── utils/
│   ├── strata.py          # 分层维度解析 + 配额分配 + 确定性层内选取（纯标准库）
│   ├── review.py          # 人工复核结论归一化与校验
│   ├── spot_check.py      # 核心业务：判定、台账、回写报告、统计守恒
│   ├── schemas.py         # 严格配置模型与 /predict 请求模型
│   ├── io.py              # 流式 UTF-8、任务目录、任务锁、原子写、安全解压
│   ├── transport.py       # crossmodal-files-v1 内容对象与运行清单
│   ├── lineage.py         # （v4.2 停用，存档）
│   └── logger.py          # 规范日志
├── dataset/
│   ├── DATA_SCHEMA.md     # 与实现一致的数据契约
│   └── sample/            # demo.jsonl、demo_pending.jsonl、20_quality_report.json、manifest.json、make_sample.py
├── tests/                 # 154 条单测 + tests/smoke_api.py 真实 HTTP 冒烟
├── packaging/prepare_package.py
├── output/                # 运行时输出根目录（交付时为空）
├── map.json / requirements.txt / Dockerfile / README.md / OPERATOR_MANUAL.md / HANDOFF.md
└── DEVELOPMENT_STATUS.md
```

## 4. 输入输出（详见 `dataset/DATA_SCHEMA.md`）

输入：一个严格 UTF-8 的 JSONL 候选池，记录级需要 `sample_id`，可选 `scene*`、
`text/image/audio/video`、`alignment.*`，以及**随记录回传的人工复核结论**；
入口文件同目录可放上游 `20_quality_report.json`。

输出（`OUTPUT_DIR/<uniqueId>/spot_check_writeback/<runId>/`）：

| 文件 | 说明 |
| --- | --- |
| `24_spot_check.jsonl` | 主输出（全量有效记录 + `processing["24"]` 注解） |
| `24_sampled.jsonl` | 分层抽检样本 + 人工结论 |
| `24_sample_quarantine.jsonl` | 判错样本 → #21（已取消自动对齐） |
| `24_disputes.jsonl` | 异议（判错 + 存疑）→ #21/#25 |
| `24_pending_manual.jsonl` | 待人工 → #25 |
| `24_original_quarantine.jsonl` / `24_rejects.jsonl` | 原始隔离 / 坏行 |
| `24_quality_report.json` | **已回写质量报告** |
| `manifest.json` / `run.log` | 运行清单 / 规范日志 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py`、`utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

## 5. 配置（`configs/config.yaml`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `operator.name` / `operator.version` | `spot_check_writeback` / `v1.0.0` | 部署身份，不接受覆盖 |
| `stage_id` | `S7` | 阶段标识 |
| `enabled` | `true` | 表1 冻结：必须为真，配置为 `false` 直接报错 |
| `sample_rate` | `0.10` | 表1 允许 0.10–0.20，越界报错 |
| `strata` | `[scene, modality, alignment_type]` | 表1 冻结，改成别的直接报错 |
| `include_auto_aligned` | `true` | 表1 冻结：自动对齐必须进抽样框 |
| `sampling_salt` | `operator24` | 抽样哈希盐，保证可复现 |
| `report_source_names` | `20_quality_report.json` 等 3 个 | 纯文件名，按顺序查找 |
| `allow_degraded` | `false` | `false` 时缺上游报告直接结构化失败 |
| `require_reviewer_identity` | `true` | 关闭后允许无复核人的结论（默认**必须**有身份） |

`/predict` 的 `config` 只接受 `sample_rate`、`sampling_salt`、`report_source_names`、
`allow_degraded`、`require_reviewer_identity`；其余键（含 `enabled`/`strata`）一律拒绝。

## 6. 算法/模型后端与降级

- 无模型、无外部服务；全是确定性真实计算；
- 抽样：`allocate_quotas`（比例取整 + 最大余数，总量夹在 `[10%, 20%]`）+ 层内
  `blake2b(salt|sample_id)` 升序选取。**不使用随机数**，与输入顺序无关，可复现；
  样本量过小导致百分比区间为空（`floor(0.20*N) < ceil(0.10*N)`）时至少抽 1 条；
- 降级路径（全部显式、可审计，绝不伪造成功）：

  | 情况 | 表现 |
  | --- | --- |
  | 未接入人工平台（无复检结论） | 抽检样本 → `pending_manual`；报告 `waiting_writeback`，运行 `status=waiting_manual` |
  | 复核结论缺复核人/取值非法 | 按待人工处理，`reason_code=review_invalid:*`，`human_verified=false` |
  | 缺上游待回写报告 | `allow_degraded=true` → 运行 `status=degraded`，报告 `source_report.available=false`；`false` → 结构化失败 |
  | 编码非法 / 文件缺失 | 结构化失败（`code=1`，`failure.json` 记录 `errorType`，不写全文与堆栈） |

## 7. CLI / API 示例

```powershell
# 阶段内（默认）：data 返回 outputFile（运行目录内主输出路径）+ runId/status/reportStatus/statistics
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 阶段末：data 的 outputFile 换成 crossmodal-files-v1 内容对象，[v4.2] 不再返回 lineageFile
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke --is-last-operator

# 覆盖业务参数（抽样率 / 盐 / 报告名 / 降级开关）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_rate --output-root output_smoke --override sample_rate=0.20
```

```bash
# 形态一：单文件流（multipart/form-data，detection=<算子 id>，file 必传）
curl -X POST "http://127.0.0.1:8124/predict?detection=writeback_sampling_quality_report" -F "file=@candidate_pool.zip"

# 形态二：isLastOperator=false 与 =true 均返回 outputFile + result
curl -X POST "http://127.0.0.1:8124/uploadFile?uniqueId=task_1" -F "file=@bundle.zip"
curl -X POST "http://127.0.0.1:8124/predict" -H "Content-Type: application/json" \
     -d '{"uniqueId":"task_1","isLastOperator":false}'
curl -X POST "http://127.0.0.1:8124/predict" -H "Content-Type: application/json" \
     -d '{"uniqueId":"task_1","isLastOperator":true}'
curl -X POST "http://127.0.0.1:8124/removeFile?uniqueId=task_1"
```

## 8. 响应示例（v4.2）

形态一（`POST /predict?detection=writeback_sampling_quality_report`，`multipart/form-data`，处理型 `status`）：

```json
{"status": 1, "uniqueId": "task_1", "sampling_report": "24_sampling_conclusion.json",
 "reviewed_samples": [{"sample_id": "sp-img-07", "conclusion": "fail", "decision": "rejected",
                       "stratum": {"scene": "scene-hyd", "modality": "image+text",
                                   "alignment_type": "auto_aligned"}}],
 "quality_report_status": "written_back", "process_status": "partial", "sampled": 4}
```

- `status`：处理型 int，1 = 抽检与回写完成，0 = 参数/输入/执行失败（失败为 `{"status":0,"msg":"..."}`）；
- `process_status`：运行状态字符串（`success`/`partial`/`degraded`/`waiting_manual`）；
  `quality_report_status`：已回写报告状态（`written_back`/`waiting_writeback`）。
- 形态一可带表单标量 `sampling_ratio`（映射 `sample_rate`）与 `sampling_seed`（映射 `sampling_salt`），未知字段忽略。

形态二（`POST /predict`，JSON body，`isLastOperator` 必传）：

```json
{"code":0,"msg":"抽检并回写质量报告完成","data":{
  "operator":"spot_check_writeback","status":"partial","reportStatus":"written_back",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"24_spot_check.jsonl",
    "fileCount":10,"totalBytes":0,
    "files":[{"path":"24_spot_check.jsonl","encoding":"utf-8","size":0,"content":"..."}]},
  "result":{"uniqueId":"task_1","runId":"run001","status":"partial","reportStatus":"written_back",
            "statistics":{"input_lines":45,"input":44,"valid":42,"output":39,"sampled":4,
                          "sampled_passed":3,"sampled_rejected":1,"disputes":1,
                          "pending_manual":0,"conservation_ok":true}}}}
```

`isLastOperator` 仍必传，用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile` + `result`，不再用 `false` 只回血缘对象。`data.outputFile` 是 `crossmodal-files-v1` 内容对象（`entrypoint=24_spot_check.jsonl`，含 9 个产物 + `manifest.json`）；`data.result.status` 是运行状态字符串，记录级 `processing["24"].status` 为 int（1/0）+ `process_status`。

失败：`{"code":1,"msg":"输入文件不存在","data":{}}`（参数不合法为 HTTP 422 + `code=1`）。

## 9. 资源需求

- 纯 CPU，单进程即可；无 GPU、无网络、无外网依赖；
- 内存：与「分层键 + 抽样框计数」同阶（每条记录只留 `sample_id + 分层键`），
  记录体走临时文件两遍流式扫描，不在内存里堆全量记录；
- 磁盘：约等于输入体量（主输出 + 旁路清单 + 报告）；
- 响应上限：`MAX_RESPONSE_BYTES` 默认 64 MiB，超限返回结构化失败而不是截断。

## 10. 已知限制

1. **未接入人工复核平台**：本算子只消费回传结论，不会自行判定质量。生产落地前需要
   工程侧把标注平台的结论按 `DATA_SCHEMA.md` §1.3 落到记录上；否则报告会一直停在
   `waiting_writeback`（这是设计上的正确行为，不是缺陷）。
2. **不校验媒体文件本身**：媒体可打开性/解码属于 #20；本算子只做分层抽样与人工结论回写。
3. **复核人身份**：`require_reviewer_identity=true` 时无 `reviewer` 的结论一律按待人工，
   如需放开须由主 agent 决策并走配置覆盖。
4. **缺上游报告时的封版语义**：`allow_degraded=true` 时上游报告缺失仍会产出自己的报告并标记 degraded；
   默认 `false` 时直接结构化失败；是否允许据此继续封版交 #23 判断。
5. **`sampled == 0` 的语义**：输入为空或无可抽检样本时，本算子按「无待回写结论」视为
   回写完成（`gate.no_sample=true`）。是否允许在该情况下封版**待主 agent 决策**。
6. **样例无真实媒体**：`dataset/sample` 只含 JSONL 与报告；本算子不读媒体，故样例可完整复现。
7. **未执行 Docker 构建**：本机未安装 `docker` CLI（`docker --version` 不可用），
   因此**没有**做过镜像验证，`Dockerfile` 仅供参考。

## 11. 真实测试结果（2026-10-08，Windows + CPython 3.9.7）

```text
python -m pytest tests/ -q
  -> 154 passed in 4.01s

python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
  -> exit=0, code=0
     status=partial reportStatus=written_back
     input_lines=45 blank_lines=1 input=44 rejected=2 valid=42
     output=39 origin_quarantined=2 sample_quarantined=1 duplicates=1
     sampled=4 sampled_in_output=3 not_sampled=36
     sampled_passed=3 sampled_rejected=1 sampled_uncertain=0 sampled_pending=0
     disputes=1 pending_manual=0 auto_alignment_cancelled=1
     human_verified_records=4 review_required_records=0 conservation_ok=True

python processor.py ... --override sample_rate=0.20
  -> status=waiting_manual reportStatus=waiting_writeback
     sampled=8 sampled_passed=4 sampled_rejected=2 sampled_uncertain=1 sampled_pending=1
     output=38 sample_quarantined=2 disputes=3 cancellation=1 conservation_ok=True

python processor.py --input dataset/sample/demo_pending.jsonl --unique-id smoke_pending ...
  -> status=waiting_manual reportStatus=waiting_writeback
     sampled=4 sampled_pending=4 pending_manual=4 human_verified_records=0 conservation_ok=True

python tests/smoke_api.py（真实 uvicorn 子进程 + httpx，端口 8124）
  -> 共 34 项检查，通过 34 项，失败 0 项

python -m json.tool map.json      -> 通过
python -m compileall -q .         -> 通过（退出码 0）
```

`24_quality_report.json` 回写示例（`smoke_cli/run001`）：
`status=written_back`、`version_state=deliverable`、`original_status=waiting_writeback`、
`cancelled_auto_alignment=["sp-img-07"]`，且上游 `checks`/`metrics`/`statistics` **逐字段未被修改**。

## 12. 交付打包

```powershell
python packaging/prepare_package.py          # 输出 dist/24_spot_check_writeback_v1.0.0.zip
```

打包前会校验必需文件齐全、`map.json` 编号 24 / 端口 8124、`output/` 为空、
无 `__pycache__`/`.pytest_cache`/`output_smoke` 残留、`app.py` 未注册 `/downloadFile`。
