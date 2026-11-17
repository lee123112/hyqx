# 15 评分时空一致性（temporal_consistency）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

跨模态 demo · 阶段 **S3 数据加工** · 表1 正式编号 **15** · 端口 **8115**
规则型 · **无模型（`model = null`）** · **默认关闭**

用**线性插值**把语音片段与字幕文本共用的时间轴校准到视频帧时间轴，核验
「画面 / 声音 / 字幕」两侧时间差是否在阈值内，据此判定样本是否留产线或进样本隔离库。

---

## 1. 功能边界

**做**：

- 读取 JSONL 记录中内联的或在 sidecar JSON 中的三段时间戳（视频帧、语音片段、字幕文本）；
- 用锚点对构建 `source → video` 的线性时间映射（分段线性插值，或全局最小二乘），
  逐绑定计算校准后的时间差，取三者两两差值的最大值；
- 时间差 ≤ 阈值（默认 0.5 秒，可配 0.2–2.0）→ 通过，留样本池；
- 时间差 > 阈值 → 样本隔离台账（标签 `样本-语义不通`），交 #21；
- 缺任一时间轴（含纯图文记录）→ `status = not_applicable`，留样本池、**不隔离、且绝不写成通过**；
- 损坏 / 缺字段 / 时间戳不可解析 / sidecar 不可读 / 编码异常 → 原始隔离台账，逐行记账；
- 按相对路径把被引用的媒体与 sidecar 随包搬到本次运行目录（只搬运，不解码）。

**不做**（表1 明确排除）：

- 不做目标检测、OCR、长文本配多图；
- 不把「没有时间戳的图文」判成时序已通过（记为 `not_applicable`）；
- 不做人像/声音的生物特征比对，不生成任何模型分数（本算子无模型）；
- 不解码音视频（不需要 ffmpeg / ffprobe / torch）。

**默认关闭**：这批数据若没有音视频时间戳，不要打开。关闭时样本原样透传，
只追加 `processing["15"].status = "skipped"`，不判定、不隔离、不产出主判分文件。

---

## 2. 目录结构

```text
15_评分时空一致性/
├── app.py                     # 三接口（FastAPI，端口 8115）
├── inference.py               # 业务入口 run() + CLI
├── configs/config.yaml        # 默认配置（表1 冻结项）
├── utils/
│   ├── temporal.py            # ★ 业务规则：时间轴解析、线性插值、判定、台账
│   ├── schemas.py             # 配置/请求模型（严格校验 + 定义域）
│   ├── io.py                  # 流式 JSONL、路径安全、任务锁、安全解压
│   ├── lineage.py             # v4.2 停用存档（不再构建血缘/阶段上下文）
│   ├── transport.py           # 文件内容对象与运行清单
│   └── logger.py              # 规范日志
├── dataset/
│   ├── DATA_SCHEMA.md         # 输入/输出字段契约
│   └── sample/                # make_sample.py + demo.jsonl + manifest.json + media/
├── tests/                     # 175 个用例（单测 + 接口 + 传输 + 真实 HTTP 冒烟）
├── packaging/prepare_package.py
├── map.json  requirements.txt  Dockerfile  .dockerignore
├── OPERATOR_MANUAL.md  HANDOFF.md  DEVELOPMENT_STATUS.md
└── output/                    # 运行产物根目录（保持为空）
```

---

## 3. 输入 / 输出字段

字段级契约见 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)。要点：

输入（逐行 JSON 对象）：

```json
{
  "sample_id": "tl-normal",
  "modality": ["video", "audio", "text"],
  "timeline": {
    "video": [{"id": "f0", "timestamp_sec": 0.0}],
    "audio": [{"id": "a0", "start_sec": 0.05, "end_sec": 0.95}],
    "text":  [{"id": "t0", "start_sec": 0.02, "end_sec": 0.87}]
  },
  "temporal_bindings": [{"binding_id": "b0", "video": "f0", "audio": "a0", "text": "t0"}],
  "temporal_anchors": [{"video_sec": 0.0, "source_sec": 0.0},
                       {"video_sec": 2.0, "source_sec": 2.0}],
  "media": ["media/subtitles.srt"]
}
```

输出（每条记录都会写 `temporal_alignment`）：

```json
{
  "sample_id": "tl-normal",
  "temporal_alignment": {
    "operator_number": 15,
    "operator_version": "v1.0.0",
    "status": 1,
    "process_status": "passed",
    "passed": true,
    "max_delta_sec": 0.05,
    "avg_delta_sec": 0.033333,
    "threshold_sec": 0.5,
    "binding_count": 3,
    "interpolation": {"type": "linear", "applied": true,
                      "requested_method": "piecewise_linear", "method": "piecewise_linear",
                      "anchor_count": 2, "min_anchors": 2, "max_residual_sec": 0.0},
    "evidence": [{"binding_id": "b0", "video_sec": 0.0, "audio_sec": 0.05,
                  "audio_calibrated_sec": 0.05, "text_sec": 0.02,
                  "text_calibrated_sec": 0.02, "delta_sec": 0.05,
                  "within_threshold": true}],
    "evidence_truncated": false,
    "reason_code": "within_threshold",
    "label": "样本-评分：通过",
    "action": "keep",
    "destination": "sample_pool",
    "review_required": false,
    "human_verified": false,
    "event_time": "2026-10-06T17:02:14+08:00",
    "source_reference": "F-ALIGN-039；F-ALIGN-040",
    "algorithm": "linear_time_interpolation_v1",
    "unit": "second"
  },
  "operator_15": {"...": "同口径的本算子命名空间摘要"},
  "processing": {"15": {"status": 1, "process_status": "passed", "...": "..."}}
}
```

运行产物（`OUTPUT_DIR/<uniqueId>/temporal_consistency/<runId>/`）：
`15_temporal_alignment.jsonl`（或关闭时的 `15_passthrough.jsonl`）、
`15_sample_quarantine.jsonl`、`15_pending.jsonl`、`15_rejects.jsonl`、
`manifest.json`、`<runId>.log`、`media/**`。

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py`、`utils/mty_compliance.py` 的血缘/归档接口，代码保留存档；`utils/transport.py` 仅保留 `crossmodal-files-v1` 文件对象与 `manifest.json` 打包能力）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

---

## 4. 配置

`configs/config.yaml`：

| 键 | 默认 | 定义域 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `false` | bool | 表1 总开关，默认关闭 |
| `max_time_delta_sec` | `0.5` | 0.2–2.0 | 画面的画面/声音/字幕最大允许时间差（秒） |
| `interpolation` | `linear` | 固定 `linear` | 表1 固定插值类型，写其它值直接失败 |
| `interpolation_method` | `piecewise_linear` | `piecewise_linear` / `global_linear` | 线性插值的实现方式 |
| `min_anchors_for_calibration` | `2` | 2–100 | 少于该锚点数视为无可用校准 |
| `require_calibration` | `false` | bool | true 时锚点不足 → 待人工校准（留产线、不隔离） |
| `max_bindings` | `5000` | 1–100000 | 单条记录绑定数上限，超出进原始隔离台账 |
| `max_evidence_bindings` | `20` | 0–1000 | 证据条数上限（计数不受影响） |

`/predict` 的 `config` 只能覆盖**业务子集**；`operator`、`stage_id`、`interpolation`
为只读（表1 固定口径），出现即返回 `code=1`。

---

## 5. 算法与后端

**后端：无模型、无外部服务。** 纯规则计算，离线可跑，不联网，不下载任何权重或数据。

1. **时间轴解析**：三段时间戳归一为「有限非负秒」，按 id / 下标建索引。
2. **线性插值校准**：锚点对 `(source_sec, video_sec)` 构建映射 \(M\)：
   - 分段线性插值（默认）：按 source 排序，段内线性插值，区间外按边界段斜率外推；
   - 全局最小二乘（可选）：\(v = a \cdot s + b\)。
   - 有效锚点不足 → `applied = false`，\(M\) 退化为恒等映射并在结果中显式标注。
3. **逐绑定时间差**：\({a}'=M(a)\)、\({t}'=M(t)\)，
   \(\delta=\max(|v-{a}'|, |v-{t}'|, |{a}'-{t}'|)\)（保留 6 位小数）。
4. **判定**：\(\delta_{\max} \le\) 阈值 → `passed`（含等号边界）；否则 `failed` → 样本隔离库。

---

## 6. 降级行为

| 情况 | 行为 |
| --- | --- |
| 锚点不足，`require_calibration=false`（默认） | 按**原始时间轴**计算真实时间差，`interpolation.applied=false`、`method=identity` 如实标注，正常判定；运行 `status` 不变 |
| 锚点不足，`require_calibration=true` | 记录进 `15_pending.jsonl`，`action=waiting_manual`、`destination=pipeline_wait`；**不判通过、不进样本隔离库**；运行 `status=degraded` |
| 输入文件不是严格 UTF-8 | 立即停止读取，`encoding_error=true`、`file_read_incomplete=true`，写 1 条 `原始文件-编码异常` 台账；没有任何通过记录时 `status=failed` |
| sidecar 不可读 / 非 JSON | 该记录进原始隔离台账（`原始文件-损坏`），其余记录照常处理 |
| 时间戳不可解析 / 绑定引用不存在 | 该记录进原始隔离台账（`原始文件-无法解析`） |

**不存在**「模型不可用但仍标 model」「固定分数」「随机分数」这类伪造降级：本算子无模型，
`statistics.model` 恒为 `null`、`models_available` 恒为 `false`。

---

## 7. 启动与用法

### 7.1 服务

```bash
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8115
```

环境变量：`OUTPUT_DIR`（默认 `<算子目录>/output`）、`OPERATOR_CONFIG`、
`PORT`（仅 `python app.py` 时生效）。

### 7.2 CLI

```bash
# 默认关闭路线：样本原样透传
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

# 打开路线（可用 --override 覆盖业务参数）
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_on \
  --output-root output_smoke --override '{"enabled": true}'
```

CLI 的 stdout 只输出一行 JSON：`{"code": 0|1, "msg": "...", "data": {...}}`；
`--is-last` 仅保留为阶段末标记（输出 `data` 与中间态同构），`--quiet` 只输出核心摘要。日志走 stderr。

### 7.3 API 示例

```bash
curl -X POST "http://127.0.0.1:8115/uploadFile?uniqueId=task_001" \
     -F "file=@bundle.zip"

# 形态一（单文件流）：直接上传 JSONL/JSON，?detection 可省略
curl -X POST "http://127.0.0.1:8115/predict?detection=score_spatiotemporal_consistency" \
     -F "file=@demo.jsonl"
# → {"status":1,"process_status":"success","uniqueId":"upload_...","pair_id":"tl-normal",
#    "applicable":true,"max_delta_sec":0.05,"threshold_sec":0.5,"decision":"pass",
#    "interpolation_method":"linear"}

# 形态二（阶段文件传递）：false/true 响应同构，统一 outputFile + result
curl -X POST http://127.0.0.1:8115/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task_001","isLastOperator":false,"config":{"enabled":true}}'
# → data = {operator, status, outputFile(crossmodal-files-v1), result(业务摘要)}

# 阶段内后续算子：传入上一运行目录内的入口文件
curl -X POST http://127.0.0.1:8115/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task_001","isLastOperator":false,
       "inputFile":"<task>/temporal_consistency/run001/15_temporal_alignment.jsonl"}'

# 阶段末
curl -X POST http://127.0.0.1:8115/predict -H "Content-Type: application/json" \
  -d '{"uniqueId":"task_001","isLastOperator":true,"config":{"enabled":true}}'

curl -X POST "http://127.0.0.1:8115/removeFile?uniqueId=task_001"
```

**只有三个接口**：`/uploadFile`、`/predict`、`/removeFile`；**不实现 `/downloadFile`**。
形态一可附带白名单表单标量：`enabled`、`max_time_delta_sec`、`interpolation_method`、
`min_anchors_for_calibration`、`require_calibration`（未知字段忽略）。

### 7.4 响应示例（v4.2 统一结构）

```json
{"code":0,"msg":"时空一致性评分完成","data":{
  "operator":"temporal_consistency",
  "status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"15_temporal_alignment.jsonl",
    "files":[{"path":"15_temporal_alignment.jsonl","encoding":"utf-8","content":"…"}]},
  "result":{"uniqueId":"task_001","runId":"run001",
    "entrypoint":"15_temporal_alignment.jsonl",
    "outputFile":"temporal_consistency/run001/15_temporal_alignment.jsonl",
    "statistics":{"input_records":5,"passed_records":3,"failed_records":1,
                  "not_applicable_records":1,"conservation_ok":true},
    "model":null}}}
```

`result.outputFile` 为任务内相对路径；`outputFile` 为 `crossmodal-files-v1` 文件内容对象（仅包内相对路径，文本 UTF-8、媒体 base64）。中间调用与阶段末调用返回同构，`isLastOperator` 只作阶段末标记。

> 注：原 `lineage.json` / 阶段血缘示例已随 v4.2 停用，相关文件不再产出；统计与配置快照改由 `data.result`、`manifest.json`、`run.log` 承载。

---

## 8. 统计与守恒

详见 `dataset/DATA_SCHEMA.md` §5。四条守恒式全部成立时 `statistics.conservation_ok = true`
（测试逐例断言）。统计全部来自真实处理结果，**不使用文件大小充当业务记录数**。

---

## 9. 资源需求

| 项 | 需求 |
| --- | --- |
| CPU / 内存 | 单核即可；`demo.jsonl` 级数据 < 100 MB |
| GPU | 不需要 |
| 外部依赖 | 无模型、无网络、无 ffmpeg |
| 磁盘 | 与输入 JSONL 同量级 + 被引用的媒体副本 |
| 流式 | 逐行读入，内存占用与**单行规模**相关，与文件行数无关 |

---

## 10. 已知限制

1. **语音与字幕共用 source 时钟**：本算子用同一组锚点把语音与字幕映射到视频时钟。
   若某数据集里字幕已在视频时钟上、仅语音有偏移，需要为该数据集改写锚点约定
   （当前不支持为两个时钟分别建锚点）。
2. **只处理时间戳，不校验媒体内容**：不验证被引用的视频/音频文件是否真的存在对应帧或片段；
   媒体只按相对路径尽力搬运，缺失只计入 `media_missing`，不影响判定。
3. **不解析容器时间基**：若时间戳与媒体实际时间基不一致（如非 1 秒基准），
   结果只是「按记录里给定的数值」计算，需上游保证单位是秒。
4. **`not_applicable` 不进隔离**：按表1「不适用不拦」；下游若要求统计时序覆盖率，
   需读取 `temporal_alignment.status` 而非只看隔离台账。
5. **待人工校准需人工介入**：`require_calibration=true` 时没有自动补锚点逻辑，
   人工补锚点后需重跑该批数据。
6. **重复 `sample_id` 不去重**：表1 未要求去重，重复样本各自判定。
7. **`unit` 固定为秒**：所有时间字段口径为秒。

---

## 11. 真实测试结果

环境：Windows / CPython 3.10。

```powershell
python -m pytest tests/ -q
# 143 passed, 58 skipped in 10.31s

python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
# 本算子按计划未提供 processor.py（CLI 入口为 inference.py），该命令报 FileNotFoundError；
# 实际 CLI 冒烟改为：
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

python -m json.tool map.json
python -m compileall -q .
python tests/smoke_api.py          # 真实 uvicorn HTTP 冒烟
```

覆盖范围：正常样例、阈值边界（等于阈值）、超阈值隔离、时钟整体偏移经插值校准后通过、
未校准路径、缺三段时间轴、纯图文、无绑定、时间戳非法、sidecar 缺失/损坏、坏行、
非对象行、缺 `sample_id`、空文件、仅空行、错误编码、错误配置、只读覆盖项、
任务隔离与 `uniqueId` 安全、两种 `isLastOperator` 调用（响应同构）、文件内容对象往返、
统计守恒。

**阈值标定（2026-11-05）**：新增中间漂移标定样本 `03_m15_drift_calibration.jsonl`
（4 组时序窗口 × 0.3 / 0.5 / 0.7 s，共 12 条；视频固定 0.5 / 2.5 s，音频与字幕整体后移）。
实测 0.3 s → `passed`（max_delta=0.3）、0.5 s → `passed`（等号边界，max_delta=0.5）、
0.7 s → `isolated`（max_delta=0.7）；默认阈值 **0.5 s 保持不变**，标定只补充边界证据，
不改变判定口径。

**抖动 / 错配标定（2026-11-06）**：新增 `04_m15_jitter_mismatch.jsonl`
（4 组时序窗口 × 4 档抖动 + 4 组 × 2 类错配，共 24 条）。抖动为字幕起止按 ±1/±2 帧
（24fps，约 41.7 ms/帧）平移、音频与视频不动：16 条全部 `passed`，max_delta 分别为
0.041667 s / 0.083333 s。错配为字幕（或音频）整体错位一个分段（s0↔s1）：
8 条全部 `isolated`，max_delta=2.0 s。证明阈值在帧级抖动下不误杀、在分段错位下能拦截。
