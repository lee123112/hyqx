# 算子 #20：质检对齐产物（`alignment_qc` v1.0.0）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

表1 第 4 环节「数据质量评估」（`stageId=S4`）／正式编号 **20**／端口 **8120**。

对候选池样本做**跨模态质量质检**：全量文件可打开、≥2 模态、视频可解码、音频非全静音、文本非空；
困惑度 / BRISQUE / SNR **优先复用上游四条预训练产线**，无上游才在 **10% 子集**本地补测；
图文子集用 **CLIP 反向检索**；**偏科 > 70% 只出待补清单，不整批隔离**。

> 本算子为**规则型 + 上游指标复用**算子：无自训练模型、不联网、不下载权重。
> 模型/后端不可用时**显式降级或显式失败**，绝不产出固定/随机/规则伪造的分数。

## 快速开始

```powershell
cd G:\HYQX\算子\跨模态\demo_10.6\20_质检对齐产物

# 1) 生成确定性样例（真实 PNG/WAV/MP4 + demo.jsonl，不下载任何外部数据）
python dataset/sample/make_sample.py

# 2) 单元测试（131 passed）
python -m pytest tests/ -q

# 3) CLI 冒烟（与 HTTP /predict 共用同一实现）
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 4) 真实 HTTP 冒烟（uvicorn 子进程 + httpx，26/26）
python tests/smoke_api.py

# 5) 交付门禁
python -m json.tool map.json
python -m compileall -q .

# 6) 启服务
uvicorn app:app --host 0.0.0.0 --port 8120
```

## 目录结构

```text
20_质检对齐产物/
  inference.py            # 业务编排 + CLI（run/_execute）
  processor.py            # CLI 入口（与 inference 共用实现）
  app.py                  # FastAPI 三接口 + /health
  configs/config.yaml     # 部署配置
  utils/
    alignment_qc.py       # ★核心业务规则（分诊/检查/报告/待补清单）
    media_probe.py        # 真实媒体探测（Pillow/OpenCV/wave）
    upstream_metrics.py   # 上游指标复用 + 10% 子集补测
    reverse_verify.py     # CLIP 反向检索（open_clip 适配 + 共享实例）
    media_carry.py        # 媒体搬运
    transport.py          # crossmodal-files-v1 / manifest
    io.py schemas.py lineage.py logger.py __init__.py   # lineage.py（v4.2 血缘输出停用，通用工具仍在用）
  dataset/
    DATA_SCHEMA.md
    sample/{make_sample.py,demo.jsonl,manifest.json,media/*}
  tests/                  # 10 个测试文件（含 smoke_api.py）
  output/                 # 运行产物根（保持为空）
  README.md OPERATOR_MANUAL.md HANDOFF.md DEVELOPMENT_STATUS.md
  map.json requirements.txt Dockerfile .dockerignore
  packaging/prepare_package.py
```

## 三接口（共享契约）

统一信封 `{"code": 0|1, "msg": "...", "data": {...}}`，**无 `/downloadFile`**。

| 接口 | 说明 |
|---|---|
| `POST /uploadFile?uniqueId=` | 上传 ZIP/多文件，安全解压到 `OUTPUT_DIR/<uniqueId>/input/` |
| `POST /predict` | 请求体 `uniqueId` + **严格布尔** `isLastOperator` + 可选 `inputFile`/`config` |
| `POST /removeFile?uniqueId=` | 持锁删除本任务目录（幂等，不影响其他任务） |
| `GET /health` | 身份与状态 |

`/predict` 响应形态（v4.2）：

- **中间与阶段末响应同构**：`isLastOperator` 仍必传（严格布尔），只标记阶段末调用；两种取值均返回
  `data = {"operator":"alignment_qc","status":<运行状态>,"outputFile":<crossmodal-files-v1 文件内容对象>,"result":{...}}`，不再用 `false` 只回血缘对象，也**不再返回 `lineageFile`**。
- `result` 业务摘要：`uniqueId`、`runId`、`status`、`message`、`statistics`。
- 形态一（`POST /predict?detection=alignment_qc`，`multipart/form-data`，`file` 必传）：处理型 int `status`（1 质检完成/0 处理失败），成功返回 `uniqueId`、`quality_report_id`、`report_status`、`hard_failures`、`diagnostics`（`modality_completeness`/`topic_skew`/`reverse_verification`/`flagged`）、`sample_count`。

```json
{"code":0,"msg":"全部样本通过质检","data":{
  "operator":"alignment_qc","status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"20_alignment_qc.jsonl",
    "fileCount":8,"totalBytes":0,
    "files":[{"path":"20_alignment_qc.jsonl","encoding":"utf-8","size":0,"content":"..."}]},
  "result":{"uniqueId":"task_001","runId":"run001","status":"success","message":"全部样本通过质检",
            "statistics":{"input":8,"output":8,"rejected":0,"origin_quarantined":0,
                          "sample_quarantined":0,"flagged":0,"conservation_ok":true}}}}
```

## 分诊结果（五类，守恒不重复）

| 去向 | 判定 | 台账/产物 | 标签 |
|---|---|---|---|
| 主输出 | 检查通过 | `20_alignment_qc.jsonl` | `样本-质检通过` |
| 旁路 flagged | 上游反向验证不一致 | `20_flagged.jsonl`（**仍在主输出**） | `流程-待人工复核` |
| 样本隔离库 | 模态不足 / 文本空 | `20_sample_quarantine.jsonl` | `样本-语义不通` |
| 原始隔离区 | 引用坏/缺/不可解码/整段无声 | `20_original_quarantine.jsonl` | `原始文件-无法解析` / `原始文件-损坏` |
| 拒绝台账 | 坏 JSON / 非对象 | `20_rejects.jsonl` | `原始文件-无法解析` |

偏科（单一路由占比 **> 0.70**）→ `20_supplement_list.json` 待补清单，**`batch_isolation=false`**。

## 配置要点（`configs/config.yaml`）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 表1 #20 默认开启且必须 |
| `quality_subset_fallback` | `0.10` | 无上游时才补测的子集比例（`blake2b(sample_id)%10000` 确定性选取） |
| `topic_skew_threshold` | `0.70` | 偏科阈值（严格大于才触发） |
| `report_status` | `pending_writeback` | 报告状态固定，不能提前封版 |
| `allow_degraded` | `true` | 依赖缺失是否允许显式降级 |
| `min_text_chars` | `1` | 文本非空最小字符数 |
| `silence_rms_threshold` | `0.0001` | 整段无声 RMS 阈值 |
| `metrics.{ppl,brisque,snr,ffmpeg}` | `upstream_first` | `upstream_first`/`local_required`/`upstream_only`/`disable` |
| `reverse_verify.{enabled,top_k,min_consistency}` | `true,5,0.25` | CLIP 反向检索 |

## 依赖

运行：`fastapi`/`uvicorn`/`pydantic>=2`/`PyYAML`/`python-multipart`；测试：`pytest`/`httpx`。
可选媒体后端：`Pillow`（PNG）、`opencv-python`（MP4）、标准库 `wave`（WAV）；`numpy`（样例合成）。
可选 CLIP：`open_clip_torch` + 本算子 `weight/` 本地权重（默认 `clip/ViT-B-32.pt`，`$MODEL_ROOT` 覆盖优先；缺失即显式降级）。详见 `requirements.txt`。
