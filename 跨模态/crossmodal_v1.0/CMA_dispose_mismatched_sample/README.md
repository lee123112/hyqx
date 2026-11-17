# 算子 21：处置错配样本（`mismatch_disposal`）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

- 正式编号：表1 **#21**（`formal_operator_numbers: [21]`）
- 阶段：**S3**（数据质量评估）
- 类型：**纯规则型** —— 无模型、无权重、不联网、不需要 GPU
- 端口：**8121**（同目录算子占用 8102–8120、8122–8124）
- 接口：`POST /uploadFile`、`POST /predict`、`POST /removeFile`（**无** `/downloadFile`）

## 1. 功能边界

按表1 #21 冻结口径执行，只做“处置”，不重做上游质量判定：

| 表1 口径 | 本算子实现 |
| --- | --- |
| 统一记录台账 | 每条输入都产出一条台账条目，含 `label`/`operator`/`stage`/`action`/`destination`/`event_time`/`batch_id` + `sample_id`/`retry_count`（9 个必需键，见 §4.2） |
| 原始问题进原始隔离区 | 无法解析、损坏、编码异常、空文件的输入 → `raw_isolation`，含坏行/非法 JSON/缺 `sample_id` |
| 内容/错配进样本隔离库 | 敏感、语义不通、评分低、错配、人工不对齐、标注不完整 → `sample_isolation` |
| 评分分层不进隔离区 | `样本-评分：通过\|入池\|CLIP分` 放行（`next_stage`）；`样本-评分：待人工` 留人工队列；`流程-*` 记流程状态留产线。**即使上游写了 `action=isolate` 也不隔离** |
| 单条最多退回重跑 1 次 | `return_limit=1`：本条第 2 次回流即锁定 |
| 全产线最多 2 次，第 3 次锁定 | `max_retry_per_item=2`：`retry_count=2` 再回流 → `locked=true`，写样本隔离库且不再回流 |
| 锁定样本不得进入 Token/交付 | 锁定记录只进 `21_sample_quarantine.jsonl`，不出现在 `21_passed.jsonl` |
| 确认不达标保持 `样本-语义不通` | 锁定时标签强制为 `样本-语义不通` |
| 不得复制隔离样本 | 只搬运**放行样本**的媒体；`isolated_media_carried` 恒为 0 并纳入守恒校验（禁止负样本工厂） |
| 不伪造人工结论 | 无人工复核平台时需人工确认的记录只能显式 `waiting_manual` + `degraded=true`，或按 `allow_degraded=false` 结构化失败；`human_verified` 恒为 `false` |

不做的事：不重新打分、不生成 Token（#22）、不重写质量报告（#24）、不判定阈值。

## 2. 输入

`inputFile` 指向一个 JSONL（每行一条），或一个含 `manifest.json` 的素材目录。
两种记录形态都支持（证据抽取按固定顺序兜底）：

1. **上游台账条目**（旁路清单，扁平）：`sample_id`、`label`、`action`、`destination`、
   `stage`、`operator`、`event_time`、`batch_id`、`retry_count`、`record.image.path`、
   `requires_human_confirmation`；
2. **完整样本记录 + 嵌套 `disposition`**：`disposition.{label,action,destination,retry_count}`、
   `metadata.{batch_id,human_review_required}`、顶层/`record` 下的媒体引用。

必填只有 `sample_id`；缺它在内的坏行进入 `21_rejects.jsonl`，**绝不静默丢弃**。
`retry_count` 只接受非负整数（布尔、字符串、负数一律按 0，不猜语义）。
字数不变的口径：`return_count` 只从 `operator_21.return_count`（自有历史命名空间）读取。

媒体引用只能是 **相对路径**（相对入口文件所在目录）：拒绝 URL、绝对路径、`..`、反斜杠与符号链接逃逸。

## 3. 输出（`OUTPUT_DIR/<uniqueId>/mismatch_disposal/runNNN/`）

| 文件 | 内容 |
| --- | --- |
| `21_passed.jsonl` | **主产物**：放行样本（`next_stage` + `pipeline`）。保留上游字段，只追加 `operator_21` 与 `processing["21"]`，不改写上游键 |
| `21_ledger.jsonl` | 统一台账：每条输入一条（有效 + 坏行），可查询 |
| `21_origin_quarantine.jsonl` | 原始隔离区清单（原始文件问题） |
| `21_sample_quarantine.jsonl` | 样本隔离库清单（业务问题 + 锁定） |
| `21_return.jsonl` | 回流指令（含 `upstream_destination` 原样去向） |
| `21_pending_manual.jsonl` | 人工队列清单（`degraded=true`） |
| `21_rejects.jsonl` | 拒绝清单（坏行 / 非对象 / 缺 `sample_id`，带行号与原因码） |
| `manifest.json` | `crossmodal-manifest-v1` 运行清单（编号、阶段、产物、口径说明） |
| `run.log` | 逐行「时间 + 动作 + 真实数量」日志 |
| `media/...` | 放行样本的原始媒体，按原相对路径搬运（隔离样本媒体**不搬运**） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止调用 `utils/lineage.py`、`utils/mty_compliance.py`，代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 响应对象与 `manifest.json`）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

## 4. 关键字段

### 4.1 主产物追加的命名空间（不改写上游）

```json
{
  "sample_id": "s-pass-1",
  "label": "样本-评分：通过",          // 上游标签原样保留
  "operator_21": {
    "operator": "mismatch_disposal",
    "operator_number": 21,
    "operator_version": "v1.0.0",
    "decision": "pass",
    "status": "success",
    "label": "样本-评分：通过",
    "action": "keep",
    "destination": "next_stage",
    "reason_code": "scoring_tier",
    "retry_count": 0,
    "return_count": 0,
    "locked": false,
    "duplicate": false,
    "human_verified": false,
    "review_required": false,
    "source_reference": { "upstream_operator": "image_text_scoring", "upstream_stage": "S3",
                          "upstream_label": "样本-评分：通过", "upstream_action": "keep",
                          "upstream_destination": "样本池",
                          "upstream_event_time": "2026-10-07T09:00:00+08:00" }
  },
  "processing": { "21": { "status": "success", "action": "keep", "decision": "pass",
                          "destination": "next_stage", "label": "样本-评分：通过",
                          "reason_code": "scoring_tier", "retry_count": 0, "locked": false } }
}
```

> 主产物**刻意不写处理时刻**：同一输入必须产出逐字节相同的主清单（可回归比对）。
> 处置时刻记录在台账的 `event_time`（表1 的“时间”）与 `run.log` 中。

### 4.2 台账条目（六类字段 + 决策上下文）

必需键（`utils.ledger.LEDGER_FIELDS`，测试逐项断言）：
`sample_id`、`label`、`operator`、`stage`、`action`、`destination`、`event_time`、`batch_id`、`retry_count`。
附加：`line`、`return_count`、`decision`、`route`、`reason_code`、`locked`、`duplicate`、
`degraded`、`degraded_reason`、`human_verified`、`review_required`、`record_truncated`、
`record_bytes`、`upstream_destination`、`source_reference`、`evidence`、有界 `record` 快照。

**有界快照**：`record` 内嵌原记录（默认上限 `max_embedded_record_bytes=32768`）；
超限时 `record=null`、`record_truncated=true`，只保留 `record_bytes` —— 既不丢台账条目，
也不复制隔离样本本体。

### 4.3 决策 / 去向枚举

- `decision`：`pass`、`process_kept`、`not_applicable`、`waiting_manual`、`returned`、
  `locked`、`sample_isolated`、`raw_isolated`、`rejected`
- `destination`：`next_stage`、`pipeline`、`human_queue`、`return_to_upstream`、
  `sample_isolation`、`raw_isolation`
- `route`（内部路由）：`released`、`origin`、`sample`、`return`、`pending`

## 5. 配置（`configs/config.yaml`）

```yaml
operator: { name: mismatch_disposal, version: v1.0.0 }
stage_id: S3
enabled: true                 # 表1：必须开启；设为 false 直接配置失败
return_limit: 1               # 单条最多退回重跑次数
max_retry_per_item: 2         # 全产线单条最多重跑次数（第 3 次锁定）
raw_issue_destination: raw_isolation        # 只读
quality_issue_destination: sample_isolation # 只读
scoring_tiers_not_isolated: true            # 只读
manual_platform_available: false            # demo 未接入人工复核平台
allow_degraded: false                       # 默认：禁止降级 → 需人工确认的记录触发 DependencyError（退出码 1）
max_embedded_record_bytes: 32768
transport: { format: crossmodal-files-v1, max_response_bytes: 67108864 }
```

> **默认口径（重要）**：`manual_platform_available=false` + `allow_degraded=false` 时，
> 样例中的 `s-proc-1`（`requires_human_confirmation`）无法处置 → 整次运行以
> `DependencyError` **结构化失败**（CLI 退出码 1；`/predict` 返回 HTTP 400 + `code=1`），
> 并清理半截产物、不留空运行目录。测试/联调如要跑通样例，必须**显式**允许降级：
> CLI 加 `--override '{"allow_degraded": true}'`，HTTP 在请求 `config` 里传
> `allow_degraded=true`；此时该记录 `decision=waiting_manual`、`degraded=true`、
> `degraded_reason=manual_platform_unavailable`，进 `21_pending_manual.jsonl` 并留在产线，
> 整次运行 `status=degraded`、CLI 退出码 0。也可传 `manual_platform_available=true`
> 模拟人工平台已接入（此时不降级，运行 `status=partial`）。

请求 `/predict` 的 `config` 只允许覆盖：`return_limit`、`max_retry_per_item`、
`manual_platform_available`、`allow_degraded`、`max_embedded_record_bytes`。
携带 `operator`/`stage_id`/`enabled`/去向/评分分层口径或**任何未知键**都会失败
（`extra="forbid"`，不静默采用另一套阈值）。未知字段、布尔/整数类型错误一律拒绝。

## 6. 算法 / 模型后端

**无模型、无权重、无外部服务**：全部为确定性规则
（标签前缀与关键词、动作/去向归一、退回预算、锁定判定、有界快照、媒体按引用搬运）。
不产生任何模型分数，也不存在“模型不可用仍标 model”的伪造降级。

## 7. 降级行为

| 情形 | 行为 |
| --- | --- |
| 人工复核平台未接入，但有记录需人工确认 | `manual_platform_available=false` + `allow_degraded=true` → 该记录 `decision=waiting_manual`、`degraded=true`、`degraded_reason=manual_platform_unavailable`，进 `21_pending_manual.jsonl` 并**留在产线**（不隔离），整次运行 `status=degraded` |
| 同上但禁止降级 | `allow_degraded=false` → `DependencyError` 结构化失败，清理本次半截产物与空运行目录 |
| 输入不是严格 UTF-8 | `InputError`（不猜测编码、不静默替换） |
| 坏行 / 非对象 / 缺 `sample_id` | 进 `21_rejects.jsonl`（`invalid_json`/`invalid_record`/`missing_sample_id`），`status` 记 `partial` |
| 结果守恒校验不通过 | `InputError`，删除本次已写产物，不留半截文件 |

运行状态：`degraded`（优先，存在人工平台降级）> `partial`（存在隔离/回流/坏行）> `success`。

## 8. 启动

```bash
# 依赖（Python 3.9+，本机实测 3.9.7；容器基线 3.11）
pip install -r requirements.txt

# 本地启动（默认 8121）
PORT=8121 python -m uvicorn app:app --host 0.0.0.0 --port 8121
# 或
python app.py

# 容器（本环境未安装 Docker，未实际构建）
docker build -t mismatch_disposal:1.0.0 .
docker run --rm -p 8121:8121 -v <数据目录>:/app/output mismatch_disposal:1.0.0
```

## 9. CLI 与 API 示例

### 9.1 CLI

```bash
# 默认配置（allow_degraded=false）：样例含需人工确认的 s-proc-1 → 结构化失败
python processor.py --input dataset/sample/demo.jsonl \
                    --unique-id smoke_cli --output-root output_smoke
# → {"code": 1, "msg": "DependencyError: 人工复核平台未接入，且未允许降级：需人工确认的样本 s-proc-1 无法处置", "data": {}}
#   退出码 1；清理半截产物，不留空运行目录

# 测试/联调：显式允许降级后再跑同一份样例 → 退出码 0、status=degraded
python processor.py --input dataset/sample/demo.jsonl \
                    --unique-id smoke_cli --output-root output_smoke \
                    --override '{"allow_degraded": true}'

# 与 HTTP 层一致的任务内寻址（拒绝跨任务/越界路径；参数可叠加 --override allow_degraded=true）
python processor.py --input dataset/sample/demo.jsonl \
                    --unique-id smoke_cli --output-root output_smoke --strict-input

# 覆盖允许的业务参数（模拟人工复核平台已接入：不降级，status=partial）
python processor.py --input dataset/sample/demo.jsonl --unique-id cli_cfg \
                    --output-root output_smoke \
                    --override '{"manual_platform_available": true}'
```

CLI 输出一次结构化 JSON：`{"code":0,"msg":"<status>","data":{"status":"success|partial|degraded","operator":"mismatch_disposal","uniqueId":"...","runId":"run001","inputFile":"...","outputFile":"...","statistics":{...},"artifacts":[...]}}`（v4.2 起不再回传 `lineageFile`）。结构化失败（如默认配置下样例触发的 `DependencyError`）为 `{"code":1,"msg":"DependencyError: ...","data":{}}`，退出码 1；`--override` 不是合法 JSON 时退出码 2。

### 9.2 HTTP

> `isLastOperator` 仍必传（严格布尔），仅用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，
> 均返回 `outputFile`+`result`，不再用 `false` 只回血缘对象。
>
> **默认口径**：`allow_degraded=false` 时，含需人工确认记录的样例（`s-proc-1`）会让 `/predict`
> 返回 **HTTP 400** + `{"code":1,"msg":"DependencyError: ...","data":{}}`。下面示例中的
> `code=0`/`degraded` 均为**显式联调口径**（请求 `config.allow_degraded=true`，或已接入人工平台）。

```bash
# 1) 上传数据集包（zip；入口由 manifest.json 指定，无 manifest 时要求唯一 JSONL 候选）
curl -s -X POST "http://127.0.0.1:8121/uploadFile?uniqueId=task_001" \
     -F "file=@bundle.zip"
# → {"code":0,"msg":"上传成功","data":{"entrypoint":"demo.jsonl","files":6,...}}

# 2) 形态一：multipart/form-data 单文件流（处理型 status：1 成功/0 失败）
curl -s -X POST "http://127.0.0.1:8121/predict?detection=dispose_mismatched_sample" \
     -F "file=@demo.jsonl" -F "return_limit=1"
# → {"status":1,"disposal_records":[{"sample_id":"s-pass-1","label":"样本-评分：通过",
#      "action":"keep","destination":"next_stage","retry_count":0,
#      "reason_code":"scoring_tier"}, ...],"token_eligible":true}
# 失败 → {"status":0,"msg":"<原因>"}

# 3) 形态二：application/json 阶段链式（阶段内中间）
curl -s -X POST "http://127.0.0.1:8121/predict?operatorId=m21" \
     -H 'Content-Type: application/json' \
     -d '{"uniqueId":"task_001","isLastOperator":false}'
# → {"code":0,"msg":"degraded","data":{"operator":"dispose_mismatched_sample",
#      "status":"degraded","outputFile":{"format":"crossmodal-files-v1",...},
#      "result":{"inputFile":"...","outputFile":".../21_passed.jsonl","runId":"run001",
#                "statistics":{...},"startedAt":"...","finishedAt":"..."}}}

# 4) 阶段内后续算子：把上一步 result.outputFile 作为 inputFile 传来
curl -s -X POST http://127.0.0.1:8121/predict -H 'Content-Type: application/json' \
     -d '{"uniqueId":"task_001","isLastOperator":false,"inputFile":"<上一步 result.outputFile>"}'
# → 新建 run002；中间/阶段末响应同构，均返回 outputFile + result

# 5) 阶段末（isLastOperator=true）：响应结构与中间调用一致
curl -s -X POST http://127.0.0.1:8121/predict -H 'Content-Type: application/json' \
     -d '{"uniqueId":"task_001","isLastOperator":true}'
# → {"code":0,"msg":"degraded","data":{"operator":"dispose_mismatched_sample","status":"degraded",
#      "outputFile":{"format":"crossmodal-files-v1","entrypoint":"21_passed.jsonl",...},
#      "result":{...同上...}}}

# 6) 幂等清理（重复调用都返回 code=0）
curl -s -X POST "http://127.0.0.1:8121/removeFile?uniqueId=task_001"
```

### 9.3 形态二响应示例（v4.2 统一结构，字段与 `app.py`/`inference.py` 一致）

```json
{"code":0,"msg":"degraded","data":{
  "operator":"dispose_mismatched_sample",
  "status":"degraded",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"21_passed.jsonl",
    "files":[{"path":"21_passed.jsonl","encoding":"utf-8","content":"..."},
             {"path":"21_ledger.jsonl","encoding":"utf-8","content":"..."},
             {"path":"manifest.json","encoding":"utf-8","content":"..."},
             {"path":"media/pass_ok.png","encoding":"base64","content":"..."}]},
  "result":{"inputFile":"<...>/input/demo.jsonl","outputFile":"<...>/run001/21_passed.jsonl",
            "runId":"run001",
            "statistics":{"input_lines":18,"blank_lines":1,"input_records":17,
                          "valid_records":14,"bad_lines":3,"released":7,"passed":3,
                          "process_kept":1,"not_applicable":1,"waiting_manual":2,
                          "origin_isolated":2,"sample_isolated":4,"returned":1,"locked":2,
                          "main_entries":7,"ledger_entries":17,"origin_entries":2,
                          "sample_entries":4,"return_entries":1,"pending_entries":2,
                          "reject_entries":3,"media_carried":2,"isolated_media_carried":0,
                          "degraded_reasons":{"manual_platform_unavailable":2},
                          "conservation_ok":true,"status":"degraded"},
            "startedAt":"2026-10-08T14:52:10+08:00",
            "finishedAt":"2026-10-08T14:52:11+08:00"}}}
```

> 说明：`outputFile.files` 实际包含主产物 + 6 份台账清单 + `manifest.json` + 随包媒体（示例仅节选 4 项）；
> 本示例为 `allow_degraded=true` 的联调口径。业务失败为 `{"code":1,"msg":"<原因>","data":{}}`
> （`DependencyError`/输入错误为 HTTP 400），参数不合法为 HTTP 422 + `code=1`。

### 9.4 守恒校验（全部必须成立，否则整次运行失败并清理产物）

```
input_records == valid_records + bad_lines
valid_records == released + origin_isolated + sample_isolated + returned
released      == passed + process_kept + not_applicable + waiting_manual
main_entries  == released
ledger_entries == valid_records + bad_lines
pending_entries == waiting_manual + pending_process
isolated_media_carried == 0
（其余：origin/sample/return/reject 清单逐项对齐，sample_isolated >= locked）
```

## 10. 资源需求

- CPU 单核即可；内存峰值与单条 JSONL 行长相关（流式处理，不整文件载入）。
- 无 GPU、无模型权重、无外部网络、无 ffmpeg（不解码媒体，只按相对路径复制）。
- 磁盘：台账 ≈ 输入行数 × 单条台账条目大小；媒体按放行样本原文件复制一份。

## 11. 已知限制

1. **人工复核平台未接入**：需要人工确认的记录只能 `waiting_manual`（留产线 + `degraded`），
   无法闭环；接入后应把人工回写结果作为输入证据，届时再放开 `human_verified`。
2. 证据抽取依赖上游客口命名（`label`/`action`/`destination` 及 `disposition` 嵌套），
   未知标签按“业务问题”保守处置（交人工复核），不做阈值猜测。
3. 媒体**只搬运、不校验内容**：按引用复制原文件；不看画质、不解码。
4. 主产物不含处理时刻（为保证可复现的逐字节一致），时刻见台账 `event_time`。
5. `crossmodal-files-v1` / `crossmodal-manifest-v1` 是并行开发阶段约定的 demo 承载格式，
   **不是甲方正式协议**；正式二进制协议到位时只需替换 `utils/transport.py`。
6. 本环境未安装 Docker，镜像**未实际构建**（Dockerfile 仅静态提供）。

## 12. 真实测试结果（2026-10-08）

```bash
$ python dataset/sample/make_sample.py
样例已生成：.../dataset/sample/demo.jsonl（18 行）

$ python -m pytest tests/ -q
140 passed in 6.92s      # 同一命令多次实测 6.9s–9.6s，随机器负载浮动

# 2026-10-31 复核（当前默认配置 allow_degraded=false）：样例会结构化失败，退出码 1
$ python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
{"code": 1, "msg": "DependencyError: 人工复核平台未接入，且未允许降级：需人工确认的样本 s-proc-1 无法处置", "data": {}}

# 测试/联调：显式允许降级后同一份样例跑通，退出码 0、status=degraded
$ python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke \
    --override '{"allow_degraded": true}'
{"code": 0, "msg": "degraded", "data": {... "released": 7, "origin_isolated": 2,
 "sample_isolated": 4, "locked": 2, "returned": 1, "waiting_manual": 2, "bad_lines": 3,
 "conservation_ok": true ...}}   # 退出码 0

$ python -m compileall -q .        # 退出码 0
$ python -m json.tool map.json     # 解析通过
```

真实 HTTP smoke（`uvicorn` 8121 + 真实 multipart 上传；接口层字段已按 v4.2 代码更新，
业务统计仍为 2026-10-08 实测）：

```
1) openapi paths = ['/predict', '/removeFile', '/uploadFile']
2) upload        -> 200 code=0 entrypoint=demo.jsonl files=6
3) predict(json) -> 200 code=0 data keys=['operator','status','outputFile','result'] runId=run001 conservation=True
4) predict(json) -> 200 code=0 中间/阶段末同一结构，crossmodal-files-v1，10 个文件（7 产物 + manifest + 2 媒体）
5) predict(form) -> 200 status=1 disposal_records=N token_eligible=true（detection=dispose_mismatched_sample）
6) predict(chain)-> 200 code=0 runId=run003（inputFile 链式续跑）
7) downloadFile  -> 404 code=1
8) illegal uniqueId -> 422 code=1
9) cross-task inputFile -> 400 code=1
10) readonly override    -> 200 code=1
11) removeFile   -> code=0 / code=0（幂等），任务目录已删除
```

> 注：以上为 2026-10-08 联调快照。当前默认 `allow_degraded=false` 下，第 2) 步上传的
> `demo.jsonl` 含需人工确认的 `s-proc-1`，第 3)/4)/6) 步（JSON `/predict`）会返回
> **HTTP 400** + `{"code":1,"msg":"DependencyError: ...","data":{}}`；需在请求
> `config` 中显式 `allow_degraded=true`（或模拟平台已接入）才能得到上列 `code=0`。

样例回归逐项核对（`tests/test_operator.py::EXPECTED` 与 `dataset/DATA_SCHEMA.md` §4 一致）：
`input_lines=18 / blank_lines=1 / input_records=17 / valid_records=14 / bad_lines=3 /
released=7（passed=3、process_kept=1、not_applicable=1、waiting_manual=2）/
origin_isolated=2 / sample_isolated=4（locked=2）/ returned=1 / pending_entries=2 /
reject_entries=3 / media_carried=2 / isolated_media_carried=0`。
