# 算子 23 · 压缩交付对齐语料包（`delivery_package`）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

> 表1 v4.1 #23｜第 6 环节「数据集交付」（`stageId=S6`）｜**P0 必须**｜默认开启｜
> 类型 = **系统功能**｜**模型列 = `--`（无任何模型、不产出任何分数）**
> 正式编号：**23**（目录 `23_压缩交付对齐语料包` ←→ 计划/技术设计/`map.json` 三处一致）

---

## 1. 功能边界

**做**（表1 #23 逐条落地）：

1. 读取 #22 的 Token 样本清单（业务入口）与四类 sidecar 材料（已回写质量报告、#24 抽检结论、
   #21 隔离清单、版本说明），校验 `uniqueId`、入口、编码、任务隔离与材料完整性；
2. **只读交付门禁**：训练文件已生成 + 质量报告已写回 + 抽检已写回，**三项缺一不可**
   （请求不可覆盖、配置不可放宽）；三项齐全 → `待发布` 改 `可交付`；不齐 → **不拦样本**，
   版本**保持待发布**并显式 `degraded`；
3. **不打包隔离样本**：上游 #21 标记或隔离清单命中的样本一律不进交付包，写入旁路台账；
4. 生成**真实 ZIP** 交付包（`train/samples.jsonl` + 四份 materials + `manifest.json` + `version.json`
   + 声明的相对媒体）与包内逐文件 `sha256` 清单；
5. 版本说明必须**声明启用路线与标注粒度**，并在 `version.json` 中重新声明（供工程侧核对）；
6. 输出业务摘要 `data.result`（真实统计）、运行清单、逐行日志与统一台账（v4.2 起不再输出阶段血缘）。

**不做**（明确的职责边界）：

- ❌ 不打包 `outputFile` 之外的任何内容、不写 OSS、不做分片上传（表1：**大包由工程侧 OSS/分片承载**）；
- ❌ 不做媒体可打开/解码/静音检测（属 #20），不改写上游 `processing` / `qc` / `alignment` 结论；
- ❌ 不产生任何模型分数、相似度、概率、人工结论；`human_verified` 恒为 `false`；
- ❌ 不提供 `/downloadFile`（正式接口只有三个）；
- ❌ 不因「材料不齐」丢弃样本：材料问题只影响**版本状态**，不改变样本去向。

---

## 2. 目录与文件

```text
23_压缩交付对齐语料包/
├── DEVELOPMENT_PLAN.md      TECHNICAL_DESIGN.md   # 计划与设计（只读基准）
├── app.py                   # 三接口（FastAPI）
├── inference.py             # 编排：门禁 → 流式判定 → ZIP 打包 → manifest
├── processor.py             # CLI 入口（与 HTTP 共用实现）
├── configs/config.yaml      # 默认配置（严格校验，未知键即失败）
├── utils/
│   ├── delivery.py          # ★业务规则（材料发现/门禁/隔离/台账/适用性）
│   ├── archive.py           # ★ZIP 打包（确定性、成员路径安全、原子替换）
│   ├── io.py                # 路径安全、流式解码、原子写、任务锁、安全解压
│   ├── schemas.py           # 严格配置与请求模型（含计划 YAML 片段兼容）
│   ├── lineage.py           # 血缘/阶段上下文/配置快照（v4.2 停用，存档）
│   ├── transport.py         # crossmodal-files-v1 内容对象与运行清单
│   └── logger.py            # 逐行日志（不含原文/凭据）
├── dataset/DATA_SCHEMA.md   # 实际字段口径（与实现一致）
├── dataset/sample/          # 可复现样例 + make_sample.py（确定性合成）
├── tests/                   # pytest 单测 + smoke_api.py（真实 uvicorn 冒烟）
├── packaging/prepare_package.py
├── map.json  requirements.txt  Dockerfile  .dockerignore
├── README.md  OPERATOR_MANUAL.md  HANDOFF.md  DEVELOPMENT_STATUS.md
└── output/                  # 运行输出目录（保持为空）
```

运行期输出：`OUTPUT_DIR/<uniqueId>/delivery_package/runNNN/`（每次运行新建 `runNNN`，绝不覆盖）。

---

## 3. 输入与输出

### 3.1 输入

- **业务入口**：JSONL（#22 Token 清单）。字段 `sample_id`、`token`、`routes`、`media[].path` 等，
  其余字段（`metadata`/`qc`/`alignment`/`processing`/未知扩展）**原样保留**；
- **材料 sidecar**：`quality_report`、`sampling_conclusion`、`isolation_list`、`version_notes`
  自动发现顺序 `<包>/materials/` → `<包>/` → 递归；也可在 `configs/config.yaml` 的 `materials.*`
  显式指定（**部署级配置，请求不可覆盖**）；
- 可选：`manifest.json`（声明 entrypoint）；v4.2 起不再读取 `lineage.json`/`stage_context.json`（血缘输入已停用）。

### 3.2 输出（`runNNN/`）

| 产物 | 说明 |
|---|---|
| `23_delivery_package.jsonl` | 主输出（entrypoint）：入包记录 + `operator_23` 命名空间 |
| `23_excluded_isolated.jsonl` | 已隔离样本台账（不进包） |
| `23_rejects.jsonl` | 原始隔离台账（坏行/缺字段/非法路径/空 Token/编码错误） |
| `23_materials.json` | 材料发现与门禁明细、缺失清单 |
| `23_deliverable_package.zip` / `23_pending_package.zip` | 可交付 / 待发布包 |
| `manifest.json`、`23_run.log`、`media/**` | 清单、日志、随包媒体 |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止写入血缘/阶段上下文、不再调用 `utils/mty_compliance.py`；`utils/lineage.py` 的时间戳/配置快照工具与 `utils/transport.py` 的响应打包仍在用，血缘写入代码保留存档）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

字段级口径见 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)。

### 3.3 交付包成员

`train/samples.jsonl`（训练文件，已剔除隔离样本）、`materials/{quality_report,sampling_conclusion,
isolation_list,version_notes}`、`manifest.json`（`crossmodal-delivery-package-v1`，逐文件 `sha256`）、
`version.json`（`enabled_routes` / `annotation_granularity` / `status`）、`media/**`。

---

## 4. 配置（`configs/config.yaml`）

| 键 | 默认 | 说明 |
|---|---|---|
| `operator.number` / `id` / `label` / `version` | 23 / `delivery_package` / 压缩交付对齐语料包 / v1.0.0 | 身份（`name` 兼容计划中的中文字面值） |
| `enabled`（及 `operator.enabled`） | `true` | **必须为 true**，否则配置失败 |
| `archive_format` | `zip` | 表1「只有 zip」，其他值报错 |
| `exclude_isolated` | `true` | 表1 只读，置 false 报错 |
| `status_before_review` / `status_after_review` | `待发布` / `可交付` | 表1 只读字面值 |
| `include_media` | `true` | 是否随包携带声明的相对媒体（demo 口径；大包场景置 false 只登记引用） |
| `require_deliverable` | `false` | 严格模式：不可交付即 `failed` |
| `require_token` | `true` | Token 为空 → 原始隔离区 |
| `materials.*` | `null` | 材料显式路径（自动发现留空） |
| `max_media_bytes` / `max_embedded_record_bytes` | 32 MiB / 32 KiB | 单文件媒体上限 / 台账内嵌记录上限 |
| `transport.format` / `max_response_bytes` | `crossmodal-files-v1` / 64 MiB | 共享契约，禁止改动 |

- 未知键直接失败（`extra=forbid`），不静默忽略；
- 计划文档 §3 的 `operator.settings: ["enabled=true", "archive_format=zip", …]` 写法**可直接使用**
  （会提升为顶层配置，显式顶层键优先）；
- **请求级覆盖白名单**：`include_media`、`require_deliverable`、`require_token`（`overrides` 或 `config`）；
  门禁三项、`archive_format`、`exclude_isolated`、状态字面值、材料路径**不可**被请求改写。

---

## 5. 算法/后端

无模型、无外部服务、无网络访问。全部为确定性规则：

1. 材料发现与校验（文件名/内容签名分类 + JSON 结构校验）；
2. 记录三路分流（入包 / 排除隔离 / 原始隔离）与模态适用性标注；
3. ZIP 打包（标准库 `zipfile`，`ZIP_DEFLATED`，固定时间戳，成员路径唯一且必须为相对路径，
   先写 `.partial` 再 `os.replace` 原子替换）。

---

## 6. 降级与失败行为（不伪造）

| 情形 | 状态 | 行为 |
|---|---|---|
| 三项门禁全满足 + 5 项材料齐全 | `success` | 生成**可交付** ZIP |
| 材料不齐 / 质量报告或抽检未回写 | **`degraded`** | 生成**待发布** ZIP；缺失项逐条登记；**不拦样本** |
| `require_deliverable=true` 且不可交付 | `failed` | 显式失败，退出码 1 |
| 全部行不可解析（无有效记录） | `failed` | 显式失败，坏行进原始隔离 |
| 统计守恒校验失败 | `failed` | 失败项写入 `statistics.conservation_failures` |
| 非 UTF-8 输入 | `degraded`/`failed`（取决于其余行） | 记 `encoding_error=true` + `file_read_incomplete=true`，已解析部分照常处理 |

无模型可用性问题（本算子不依赖模型）；**不存在**「模型不可用仍标 model」的伪造路径。

---

## 7. 启动与调用

### 7.1 服务

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8123
# 或：python app.py
```

### 7.2 三接口（统一信封 `{"code":0|1,"msg":"...","data":{...}}`）

```bash
# 1) 上传任务输入包（zip：entrypoint + manifest + 材料 + 媒体）
curl -F "file=@task.zip" "http://127.0.0.1:8123/uploadFile?uniqueId=batchA_0001"

# 2) 形态一：multipart/form-data 单文件流（处理型 status：1 成功/0 失败）
#    两阶段发布用 publish_phase=prepare（允许待发布）/ finalize（严格必须可交付）
curl -F "file=@task.zip" -F "publish_phase=prepare" \
     "http://127.0.0.1:8123/predict?detection=package_aligned_corpus"
# → {"status":1,"publish_status":"pending","package":"23_pending_package.zip",
#    "manifest":"manifest.json","missing_materials":["quality_report_written_back",
#    "sampling_written_back"],"package_zip":"23_pending_package.zip",
#    "package_manifest":"manifest.json","status_label":"待发布","media_count":4,
#    "sample_count":10}
# finalize 不可交付时 → {"status":0,"msg":"<原因>"}

# 3) 形态二：application/json 阶段链式（阶段内中间）
curl -H "Content-Type: application/json" -d '{"uniqueId":"batchA_0001","isLastOperator":false}' \
     "http://127.0.0.1:8123/predict?operatorId=m23"
# → {"code":0,"msg":"...","data":{"operator":"package_aligned_corpus","status":"degraded",
#    "outputFile":{"format":"crossmodal-files-v1",...},"result":{"uniqueId":"batchA_0001",
#    "inputFile":"...","outputFile":"...","packagePath":"...","packageName":"...",
#    "versionState":"pending_release","deliverable":false,"statusLabel":"待发布",
#    "runId":"run001","statistics":{...},"startedAt":"...","finishedAt":"..."}}}

# 4) 阶段末（isLastOperator=true）：响应结构与中间调用一致
curl -H "Content-Type: application/json" -d '{"uniqueId":"batchA_0001","isLastOperator":true}' \
     "http://127.0.0.1:8123/predict"

# 5) 清理任务（幂等，只影响本 uniqueId）
curl -X POST "http://127.0.0.1:8123/removeFile?uniqueId=batchA_0001"
```

阶段内续跑（同一阶段后续算子）把**上一节点** `data.result.outputFile` 路径作为 `inputFile` 传入；
`data.outputFile` 是 `crossmodal-files-v1` 文件内容对象（不是路径），**不作为**下一算子输入。
`isLastOperator` 仍必传（严格布尔），仅标记阶段末调用；v4.2 起中间与阶段末响应同构。
形态一字段：`publish_status` 取 `deliverable`（可交付）/`pending`（待发布）；`package`/`manifest` 为运行目录内相对路径，
`package_zip`/`package_manifest` 为兼容别名，`missing_materials` 为未满足项清单。
`GET /health` 仅为存活探针，不属于业务接口；`/downloadFile` **未注册**（404 + 统一信封）。

### 7.3 CLI

```bash
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \
    --output-root output_smoke
python processor.py --input dataset/sample/deliverable/demo.jsonl --unique-id t1 \
    --output-root output_smoke --is-last-operator   # 阶段末：仅输出文件内容对象
```

退出码：`0` = success/degraded（可解释完成），`1` = failed，`2` = 输入或配置非法。
CLI 会把 stdout 固定为 UTF-8，工程侧可直接重定向解析。

---

## 8. 输出示例（v4.2 统一响应，真实运行节选，`uniqueId=smoke_cli`）

```json
{"code": 0,
 "msg": "版本保持待发布（23_pending_package.zip）：训练文件 10 条；排除隔离样本 3 条；原始隔离 3 条；待补/未回写：quality_report_written_back；sampling_written_back",
 "data": {"operator": "package_aligned_corpus", "status": "degraded",
   "outputFile": {"format": "crossmodal-files-v1", "entrypoint": "23_delivery_package.jsonl",
     "files": [{"path": "23_delivery_package.jsonl", "role": "records", "encoding": "utf-8",
                "size": 16008, "content": "..."},
               {"path": "23_pending_package.zip", "role": "package", "encoding": "base64",
                "size": 5706, "content": "..."},
               {"path": "media/cm-0001.png", "role": "media", "encoding": "base64",
                "size": 76, "content": "..."},
               {"path": "manifest.json", "role": "sidecar", "encoding": "utf-8",
                "size": 0, "content": "..."}],
     "totalBytes": 0},
   "result": {"uniqueId": "smoke_cli", "inputFile": "…/input/demo.jsonl",
     "outputFile": "…/run001/23_delivery_package.jsonl",
     "packagePath": "…/run001/23_pending_package.zip", "packageName": "23_pending_package.zip",
     "versionState": "pending_release", "deliverable": false, "statusLabel": "待发布",
     "runId": "run001",
     "statistics": {"input_records": 18, "valid_records": 16, "bad_lines": 2, "blank_lines": 1,
       "packaged_records": 10, "excluded_isolated": 3, "origin_quarantined": 3,
       "rejects_entries": 5, "media_carried": 4, "media_missing": 1, "external_references": 1,
       "materials_found": ["training_files","quality_report","sampling_conclusion",
                           "isolation_list","version_notes"],
       "materials_missing": [], "zip_entries": 11, "zip_bytes": 5706,
       "deliverable_requirements": {"training_files_present": true,
         "quality_report_written_back": false, "sampling_written_back": false},
       "conservation_ok": true, "model": null, "rule_based": true},
     "startedAt": "…", "finishedAt": "…"}}}
```

同一输入在 `dataset/sample/deliverable/demo.jsonl`（三项门禁满足）下：

```text
"已生成可交付包 23_deliverable_package.zip：训练文件 10 条；排除隔离样本 3 条；原始隔离 3 条；包内 11 个成员"
status=success  versionState=deliverable  deliverable=true  conservation_ok=true
```

---

## 9. 资源需求

| 项 | 需求 |
|---|---|
| CPU/内存 | 单核即可；流式处理，内存与文件规模无关（媒体随包携带时按单文件上限 32 MiB 读取） |
| 磁盘 | 输出 ≈ 输入 + 交付包（ZIP_DEFLATED 压缩，样例 6 KB / 原始约 25 KB） |
| GPU | **不需要** |
| 网络 | **不需要**（不联网、不下载权重或外部数据） |
| 运行环境 | Python ≥ 3.9（实测 3.9.7）；`fastapi`、`uvicorn`、`pydantic>=2`、`PyYAML`、`python-multipart` |

---

## 10. 测试与真实结果

```powershell
# 1) 单测（本机实测）
python -m pytest tests/ -q
# → 143 passed in 10.39s

# 2) CLI（模板要求命令）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
# → code=0，status=degraded，versionState=pending_release，packaged_records=10，conservation_ok=true

# 3) 清单与语法
python -m json.tool map.json          # 通过
python -m compileall -q .             # 通过（无语法错误）

# 4) 真实服务冒烟（uvicorn 子进程 + httpx，非 TestClient）
python tests/smoke_api.py 8123
# → 共 22 项检查，通过 22 项，失败 0 项（exit 0）
```

覆盖项：正常/空文件/全坏行/非 UTF-8、缺字段、错误配置（`enabled=false`、非 zip、未知键、
`exclude_isolated=false`、状态字面值不符）、`not_applicable` 标注、重复 `sample_id`、
非法 `uniqueId`、跨任务 `inputFile`、ZIP slip、非法成员路径、中间/阶段末统一返回形态、
文件内容对象逐字节恢复、两个 `uniqueId` 物理隔离、`/removeFile` 幂等、`/downloadFile` 未注册、
ZIP 内容（含隔离样本不入包）、守恒等式与门禁三态。

## 11. 已知限制

1. 阶段编号取 `S6`（第 6 环节）；若工程侧阶段划分不同，改 `configs/config.yaml` 的 `stage_id` 即可（待确认）。
2. 端口取 `8123`（沿用 8120/8121/8122 顺延习惯），需工程侧确认归属。
3. 待发布状态**同样生成 ZIP**（`23_pending_package.zip`，包内 `manifest.version_state=pending_release`）。
   表1 表述「改可交付并压缩」可解读为「待发布不产包」，本实现选择产包并显式标注，属**待确认口径**。
4. `include_media=true` 为 demo 口径：媒体随包携带；表1 明确「大包由工程侧 OSS/分片承载」，
   生产大包请置 `include_media=false`（只登记引用，包内不复制媒体）。
5. 媒体只做**存在性/大小/路径安全**校验，不校验可解码性（属 #20 职责）。
6. 单次响应上限 64 MiB（含 base64；ZIP 与媒体会被 base64 膨胀约 33%）；
   超出返回 `code=1` + `errorType=OutputTooLarge`，不截断后仍报成功。
7. 万级行/GB 级媒体未压测（实现为流式；媒体按文件逐个读入并写入 ZIP）。
8. 隔离样本的「隔离原因」沿用上游 `#21` 的 `reason_code`（写入 `upstream_reason_code`），
   本算子不自造样本级隔离原因。
