# 算子 #02 核验场景编号（scene_id_verification）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

跨模态 demo 阶段「**输入核验**」环节的第 2 个算子（表1 正式编号 `#02`，阶段标识 `S1`）。

- 工具类型：**系统功能**（纯规则，**无模型、无权重、无 GPU、不联网**）
- 默认状态：**关闭**（配对集任务不执行；融合集任务才打开）
- 优先级：P1；可跳过：是（工程侧按 `isLastOperator=true` 决定阶段边界）

---

## 1. 功能边界

### 做什么

1. **场景编号核验（融合集打开后）**：取采集平台已有场景编号
   （`scene_id` / `sceneId` / `group_id` / `groupId` / `scene`，顶层或 `metadata` 内），
   按编号聚成场景组，核验**同一编号下至少 `min_modalities` 种模态**（默认 2，可选 2–4）。
   达标 → 写入 `scene_id` 与 `group_id`（`grp-scene-<scene_id>`）并归入融合集场景组。
2. **补编号（可选口径）**：配置 `scene_id_source=generate`（表1 页面「本产线补一个编号」）时，
   平台没有编号的记录会补一个**确定性派生**的编号，并**只绑定一次**
   （该编号只绑到这一条记录，不复用、不重绑；重复运行得到同一编号）。
3. **改走配对集（不隔离）**：场景组模态数不足、或沿用平台编号但平台没给编号的记录，
   **保留在产线中**并标记改走配对集，全部继续交给 #3。
4. **设备信息只读**：`capture_platform` / `device_concurrency` / `device_failover`
   只原样抄录进 `processing["02"].capture_readonly`，**不做设备调度、并发编排或故障切换**。

### 不做什么（表1 边界）

- **不新建视频流调度**，不做设备并发/故障切换控制；
- **不因为缺场景编号把样本送进隔离区**（本算子**不产出任何样本隔离清单**）；
- 不做质量打分、不做质量拦截（那属于下游质检算子）；
- 不加载模型、不产生任何分数、不伪造降级结论（表1 模型列为 `--`）；
- 不实现 `/downloadFile`。

### 与相邻算子的约定

| 方向 | 算子 | 约定 |
| --- | --- | --- |
| 上游 | #01 接入多模态批次 | 读入已加工批次记录（JSONL），本算子只读不改上游字段 |
| 下游 | #03 数据初筛 | 本算子主产物即 #03 的输入；融合集/配对集分流写在记录里 |
| 下游 | #11 生成候选配对 | #11 直接读顶层 `scene_id` / `group_id` 成组；本算子写入的 `group_id` 为 `grp-scene-<scene_id>`，与 #11 口径同名 |

---

## 2. 输入输出字段

### 输入（每条 JSONL 一行）

**兼容两种上游行粒度**（本算子对两种形态都能正确成组）：

- **样本级**（表1 #01 → #03 链路口径）：一行一个样本，模态写在顶层
  `text` / `image` / `video` / `audio`；
- **文件登记项级**（demo 10.6 的 #01 `01_workitems.jsonl` 实际口径）：一行一个文件条目，
  模态写在 `files[].modality`（缺失时由 `files[].path` 扩展名推断），
  或写在顶层 `modality` / `operator_01.modality`，文件指针在
  `files[].path|link` / `file_link` / `operator_01.file_link`。同一 `sample_id` 会有多行，
  **不视为重复**（同 `sample_id` + 同一文件重复登记才算重复）。

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `sample_id` | 是 | 样本唯一标识；缺失进原始隔离区；样本级重复进异常清单（**不隔离**） |
| `text` / `image` / `video` / `audio` | 否 | 四种模态；非空即计为一种模态。媒体可为相对路径字符串或含 `path` 的对象 |
| `files` | 否 | 文件登记项列表 `[{modality, path}]`；模态取 `modality`，缺失时按路径扩展名推断 |
| `modality` / `operator_01.modality` | 否 | 单文件记录的模态兜底取值 |
| `file_link` / `operator_01.file_link` | 否 | 单文件记录的媒体指针兜底 |
| 以上媒体指针为 `http(s)://` / `oss://` / `s3://` 等远端链接时 | - | **只登记不搬运**，计入 `media_remote`（不算错误，不下载） |
| `scene_id` / `sceneId` / `group_id` / `groupId` / `scene` | 否 | 采集平台场景编号（顶层或 `metadata` 内） |
| `processing` | 否 | 上游算子命名空间；本算子**只在 `processing["02"]` 写入**，不重置其他节点 |
| `capture_platform` / `device_concurrency` / `device_failover` | 否 | 只读抄录 |
| 其他字段 | 否 | 原样透传（如 `source_id`、`batch_id`、`authorization`、`metadata`） |

媒体路径必须是**相对于入口文件的相对路径**，禁止绝对路径、`..`、反斜杠与 URL。

### 输出（`OUTPUT_DIR/<uniqueId>/scene_id_verification/runNNN/`）

| 文件 | 角色 | 内容 |
| --- | --- | --- |
| `02_scene_verified.jsonl` | **main** | 全部有效记录（融合集 + 配对集），追加 `processing["02"]`、`scene_id`、`group_id`、`scene_group` |
| `02_scene_groups.jsonl` | sidecar | 一行一个场景组：`status` / `modalities` / `member_count` / `member_sample_ids` |
| `02_routed_to_pairing.jsonl` | sidecar | 改走配对集的记录及原因（不隔离） |
| `02_origin_quarantine.jsonl` | review | 原始文件问题（坏 JSON / 非对象 / 缺 `sample_id`），含物理行号 |
| `02_rejects.jsonl` | review | 算子异常清单（同批 `sample_id` 重复），**流程状态、不隔离** |
| `02_scene_report.json` | review | 运行级汇总报告（场景组 / 分流 / 编号来源 / 守恒） |
| `manifest.json` | - | `crossmodal-manifest-v1` 运行清单 |
| `run.log` | - | 逐行动作日志 |
| `media/**` | - | 主产物引用到的媒体（按原相对路径搬运） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py`、`utils/mty_compliance.py` 代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

`processing["02"]` 字段：

```json
{
  "operator": "scene_id_verification",
  "status": 1,
  "process_status": "verified | insufficient_modalities | no_scene_id | disabled",
  "route": "fusion | pairing",
  "scene_source": "platform | supplemented | none | disabled",
  "scene_id": "scene-A",
  "group_id": "grp-scene-scene-A",
  "modalities": ["text", "image"],
  "scene_modalities": ["text", "image", "video", "audio"],
  "scene_member_count": 3,
  "min_modalities": 2,
  "bound_once": true,
  "device_control": "read_only",
  "enabled": true,
  "reason": null,
  "capture_readonly": {"capture_platform": "capture-01", "device_concurrency": 4},
  "model": null
}
```

---

## 3. 配置

配置文件：`configs/config.yaml`（结构见 `utils/schemas.py`，未知键一律报错）。

| 键 | 默认 | 定义域 | 请求可否覆盖 |
| --- | --- | --- | --- |
| `operator.name` / `operator.version` | `scene_id_verification` / `v1.0.0` | 非空字符串 | 否（部署身份） |
| `stage_id` | `S1` | 非空字符串 | 否（部署身份） |
| `enabled` | `false` | 严格布尔 | **是**（表1 总开关） |
| `scene_id_source` | `platform` | `platform` / `generate` | **是**（表1 页面控件） |
| `min_modalities` | `2` | 整数 2–4 | **是**（表1 页面控件） |
| `bind_once` | `true` | 只能为 `true` | 否（表1 只读冻结） |
| `device_control` | `read_only` | 只能为 `read_only` | 否（表1 只读冻结） |
| `transport.format` | `crossmodal-files-v1` | 固定 | 否 |
| `transport.max_response_bytes` | `67108864` | ≥1024 | 否 |

请求携带 `operator` / `stage_id` / `transport` / `bind_once` / `device_control` 或任何未知键
会**直接配置失败**（`code=1`），不静默忽略。

---

## 4. 算法/模型后端

**纯规则实现，无算法模型后端。** 版本、窗口等参数全部来自配置与输入字段；
`statistics.model = null`、`statistics.active_backend = "rule"`、`statistics.degraded = false`。

## 5. 降级行为

本算子**无外部依赖**（不加载模型、不访问网络、不调用外部服务），因此**没有降级分支**，
也**不产生任何伪造分数**。可能出现的状态只有：

| 状态 | 含义 |
| --- | --- |
| `success` | 输入无原始文件问题、无重复标识 |
| `partial` | 存在原始文件问题（坏 JSON/非对象/缺 `sample_id`）或重复 `sample_id`；这些行分别进原始隔离区与异常清单，其余正常处理 |
| `failed` | 结构性失败：非法 `uniqueId`、`inputFile` 跨任务/穿越、非 UTF-8 编码、配置非法、守恒校验不通过、任务被并发占用 |

---

## 6. 启动

```powershell
pip install -r requirements.txt            # 运行依赖
pip install -r requirements-dev.txt        # 加测试依赖

# HTTP 服务（默认端口 8108：8102 已被 03_数据初筛 占用，8109–8126 亦已占用）
python -m uvicorn app:app --host 0.0.0.0 --port 8108
# 或
python app.py
```

Docker：

```powershell
docker build -t scene-id-verification:1.0.0 .
docker run --rm -p 8108:8108 -v "${PWD}/output:/data/output" scene-id-verification:1.0.0
```

> 本次开发**未实际执行 `docker build`**（本机无 Docker 运行时），镜像未经构建验证。

---

## 7. CLI 示例

```powershell
# 生成确定性样例（仓库已带 dataset/sample，可重复生成相同字节）
python dataset/sample/make_sample.py

# 默认口径（工具关闭，配对集不执行；记录原样交 #3）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 融合集任务，沿用平台编号，至少 2 种模态
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli `
  --output-root output_smoke --override '{\"enabled\": true}'

# 融合集任务 + 本产线补编号 + 至少 3 种模态
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli `
  --output-root output_smoke --override '{\"enabled\": true, \"scene_id_source\": \"generate\", \"min_modalities\": 3}'

# 阶段末标记（--is-last-operator 仅参数兼容；[v4.2] 阶段内与阶段末响应同形，均返回 outputFile 内容对象）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli `
  --output-root output_smoke --is-last-operator
```

CLI 是本地工具，`--input` 默认允许直接读样例路径；`--strict-input` 可强制把输入限制在
`OUTPUT_DIR/<uniqueId>/` 内。HTTP 层**始终**强制任务内寻址。

## 8. API 示例

```powershell
# 1) 上传数据集包（zip）
curl -X POST "http://127.0.0.1:8108/uploadFile?uniqueId=task_001" -F "file=@bundle.zip"

# 2) 阶段内中间算子（不传 inputFile）
curl -X POST http://127.0.0.1:8108/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_001","isLastOperator":false,"config":{"enabled":true}}'

# 3) 阶段内后续算子（传上一响应 outputFile 恢复出的任务内入口路径）
curl -X POST http://127.0.0.1:8108/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_001","isLastOperator":false,"inputFile":"<任务内入口>"}'

# 4) 阶段末算子
curl -X POST http://127.0.0.1:8108/predict -H "Content-Type: application/json" `
  -d '{"uniqueId":"task_001","isLastOperator":true}'

# 5) 形态一：单文件流（multipart/form-data），处理型 status（1 正常/0 失败）
curl -X POST "http://127.0.0.1:8108/predict?detection=M02&enabled=true" -F "file=@record.json"
# → {"status":1,"uniqueId":"...","group_id":"grp-scene-scene-A","synthetic":false,"process_status":"verified"}

# 6) 清理任务
curl -X POST "http://127.0.0.1:8108/removeFile?uniqueId=task_001"
```

所有响应外层固定为 `{"code":0|1,"msg":"...","data":{...}}`。

[v4.2] `isLastOperator=false` 与 `=true` 同形：`data = {operator, status, outputFile, result}`，
`outputFile` 为 `crossmodal-files-v1` 内容对象，`result` 含 `uniqueId/runId/status/statistics/group_id/synthetic`；
不再返回 `lineageFile`，也不再写 `lineage.json` / `stage_context.json`。

```json
// 形态二响应（节选，stats 为抽样字段）
{"code":0,"msg":"partial","data":{
  "operator":"scene_id_verification","status":"partial",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"02_scene_verified.jsonl",
    "files":[{"path":"02_scene_verified.jsonl","encoding":"utf-8","content":"..."},
             {"path":"manifest.json","encoding":"utf-8","content":"..."}]},
  "result":{"uniqueId":"task_001","runId":"run001","status":"partial",
            "group_id":"grp-scene-scene-A","synthetic":false,
            "statistics":{"input_lines":16,"main_entries":11,"fusion_records":4,
                          "pairing_records":7,"scene_groups_total":4,"verified_groups":2}}}}
```

## 9. 输出示例（真实运行结果，`enabled=true`）

```json
// 02_scene_verified.jsonl 第 1 行（节选）
{
  "sample_id": "s-001", "scene_id": "scene-A",
  "text": {"content": "一只金毛犬在草地上接飞盘", "tokens": ["金毛犬", "草地", "飞盘"]},
  "image": {"path": "media/img_a.png", "width": 4, "height": 4},
  "group_id": "grp-scene-scene-A",
  "scene_group": {"scene_id": "scene-A", "group_id": "grp-scene-scene-A",
                  "status": 1, "process_status": "verified",
                  "modalities": ["text","image","video","audio"],
                  "member_count": 3, "min_modalities": 2},
  "processing": {"02": {"status": 1, "process_status": "verified", "route": "fusion",
                        "scene_source": "platform", "scene_member_count": 3,
                        "bound_once": true, "capture_readonly": {"capture_platform": "capture-01",
                        "device_concurrency": 4}, "model": null}}
}
```

```json
// 02_scene_groups.jsonl 第 1 行
{"scene_id":"scene-A","group_id":"grp-scene-scene-A","status":1,"process_status":"verified",
 "scene_source":"platform","modalities":["text","image","video","audio"],"modality_count":4,
 "member_count":3,"member_sample_ids":["s-001","s-002","s-009"],"min_modalities":2,
 "route":"fusion","operator":"scene_id_verification","formal_operator_number":2,"stage_id":"S1"}
```

```json
// 02_scene_report.json
{"report_format":"crossmodal-scene-verification-v1","operator":"scene_id_verification",
 "formal_operator_number":2,"stage_id":"S1","enabled":true,"scene_id_source":"platform",
 "min_modalities":2,"scene_groups_total":4,"verified_groups":2,"insufficient_groups":2,
 "platform_scene_ids":7,"supplemented_scene_ids":0,"fusion_records":4,"pairing_records":7,
 "pairing_no_scene_id":4,"pairing_insufficient_modalities":3,"pairing_disabled":0,
 "origin_rejected":3,"duplicate_records":1,"media_carried":10,"media_missing":1,
 "media_errors":1,"conservation_ok":true,"model":null,"degraded":false}
```

## 10. 统计守恒

所有统计都来自真实产物计数，并逐项校验（`statistics.conservation_ok`）：

```
input_lines     = input_records + blank_lines
input_records   = valid_records + origin_rejected
valid_records   = main_entries + duplicate_records
main_entries    = fusion_records + pairing_records
pairing_records = pairing_no_scene_id + pairing_insufficient_modalities + pairing_disabled
scene_groups_total = verified_groups + insufficient_groups
fusion_records  = scene_groups_verified_members
origin_entries  = origin_rejected      （原始隔离清单行数）
reject_entries  = duplicate_records    （异常清单行数）
pairing_entries = pairing_records      （配对集清单行数）
group_entries   = scene_groups_total   （场景组清单行数）
enabled=false ⇒ fusion_records = 0 且 scene_groups_total = 0
```

任一项不成立即整次运行按 `failed` 处理并清理临时产物，不留半截文件。

---

## 11. 资源需求

| 项 | 需求 |
| --- | --- |
| CPU | 单核即可（纯规则 + JSON 流式处理） |
| 内存 | 与单批记录量相关；场景组成组需要一次全批扫描，记录常驻内存（见已知限制） |
| GPU | 不需要 |
| 磁盘 | 约等于输入 JSONL + 被引用媒体大小（媒体按原相对路径复制到运行目录） |
| 网络 | 不需要 |

## 12. 已知限制

1. **场景组成组需整批扫描**：组模态并集要看到全批记录后才知道，因此本算子把有效记录读入
   内存后再落盘（与 #11 生成候选配对同策略）。超大单批（百万级）需按批切分输入。
2. **场景组只能按已有编号成组**：采集平台没给编号时本算子**不猜组**。
   `scene_id_source=generate` 采用「一条记录补一个编号、只绑定一次」的保守口径
   （见 `DEVELOPMENT_STATUS.md` 的待决策项），因此补编号得到的组每组只有 1 条记录。
3. **模态只按字段存在性判定**：`text`/`image`/`video`/`audio` 非空即计一种模态，
   不校验媒体文件内容、不校验时长/采样率/分辨率（媒体内容校验属下游质检）。
4. **媒体只搬运不校验**：源文件缺失计入 `media_missing`、路径非法（绝对路径/URL/越界）
   计入 `media_errors`，**不伪造空文件**。
5. **`transport` 为 demo 约定**：`crossmodal-files-v1` 与 64MiB 上限是并行开发阶段的
   过渡协议，非甲方正式协议；正式二进制承载协议到位时只替换 `utils/transport.py`。
6. **端口 8108**：表1 #02 的自然端口 8102 已被 `03_数据初筛` 占用、8109–8126 亦已占用，
   故取唯一空缺的 8108；若工程侧有统一端口表，以工程侧为准。
7. **换行风格**：`tests/support.py` 与少量断言使用 LF 期望值；本机实测全绿，
   若在 CRLF 强制转换的环境运行需注意。
8. **上游行粒度不一致（需主 agent 决策）**：`demo_10.6/01_接入多模态批次` 的主产物
   `01_workitems.jsonl` 是**一行一个文件登记项**（`files:[{modality,path}]`，同 `sample_id` 多行），
   而 `demo_10.6/03_数据初筛` 及之后算子的 schema 是**一行一个样本**
   （`sample_id` 唯一 + `text`/`image`/`video`/`audio` 字段）。
   本算子两种形态都做了兼容（见 §2 输入字段说明），但该差异会影响「阶段内链式调用」的
   整体一致性，需工程侧统一口径。
9. **`media_remote`**：远端链接（`oss://` 等）只登记不搬运。若下游需要本地字节，
   需要工程侧先落地一个下载/落盘环节（本算子按表1 只做核验，不做取数）。

## 13. 真实测试结果（本机实测）

环境：Windows + Python 3.9.7，fastapi 0.128.8 / pydantic 2.13.5 / PyYAML 6.0.1 / pytest 8.4.2 / httpx 0.28.1。

```text
$ python -m pytest tests/ -q
277 passed in 13.59s                     # 覆盖正常/边界/空文件/坏 JSONL/缺字段/错误配置/
                                         # 错误编码/任务隔离/路径安全/两种 isLastOperator 形态/
                                         # #01 文件登记项级输入兼容/专项降级与失败路径/守恒与路由

$ python tests/smoke_api.py --port 8108
smoke exit=0                             # 真实 uvicorn 子进程，三接口 + 中间/阶段末两种形态 +
                                         # 两个 uniqueId 隔离 + 非法路径拒绝 + 幂等清理

$ python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
{"code": 0, "msg": "partial", ...}       # 默认关闭：main=11，fusion=0，pairing=11

$ python -m json.tool map.json           # OK
$ python -m compileall -q .              # exit=0
```

TDD 记录：实现前 `python -m pytest tests/ -q` = `4 errors（ModuleNotFoundError: utils）`，
即先红后绿；真实失败与修复过程见 `DEVELOPMENT_STATUS.md`。
