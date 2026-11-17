# 算子 15 评分时空一致性 —— 数据契约（DATA_SCHEMA）

> 本文件描述**本算子实际的输入与输出字段**，与 `configs/config.yaml`、
> `utils/temporal.py` 常量、`map.json` 保持一致。字段名与编号均已按当前计划（表1 #15）核对，
> 不含旧编号或旧版四接口遗留。

- 正式编号：**15**
- 阶段：**S3 数据加工**
- 产物前缀：**`15_`**
- 服务端口：**8115**
- 模型：**无（`model = null`）**；`statistics.models_available = false`

---

## 1. 输入

入口是一个 **UTF-8 JSONL** 文件（`demo.jsonl` / `samples.jsonl`，或由素材包 `manifest.json`
的 `entrypoint` 指定）。逐行一个 JSON 对象，**每行独立处理，无跨行状态**。

### 1.1 记录级字段

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `sample_id` | string | 是 | 样本唯一标识；兼容 `sampleId` / `sample_ID` / `id`（整数会转字符串）。缺失 → 原始隔离台账 |
| `modality` | array\<string\> | 否 | 模态标记，如 `["video","audio","text"]`；仅用于记录，不参与判定 |
| `timeline` | object | 条件必需 | 内联三段时间轴，见 1.2 |
| `timeline_file` | string | 条件必需 | 时间轴 sidecar 的相对路径（相对素材包根），内容同 `timeline` |
| `temporal_bindings` | array | 条件必需 | 时序绑定关系，见 1.3；别名 `bindings` |
| `temporal_anchors` | array | 否 | 线性插值锚点对，见 1.4；别名 `anchors` |
| `media` | array\<string \| object\> | 否 | 需随包搬运的媒体/字幕相对路径；只搬运、不解码 |
| `batch_id` | string \| int | 否 | 批次号，会写入台账 `batch_id` |
| `metadata.retry_count` | int | 否 | 重试次数，会写入台账 `retry_count`（默认 0） |

时间轴来源优先级：`timeline`（内联）→ `timeline_file`（sidecar）→
顶层散列数组 `video_timeline` / `audio_timeline` / `text_timeline`（`subtitle_timeline` 等价）。
`temporal_bindings` / `temporal_anchors` 也可写在 sidecar 内。

### 1.2 `timeline` 三段时间轴

```json
{
  "video": [{"id": "f0", "timestamp_sec": 0.0}],
  "audio": [{"id": "a0", "start_sec": 0.05, "end_sec": 0.95}],
  "text":  [{"id": "t0", "start_sec": 0.02, "end_sec": 0.87}]
}
```

| 时间轴 | 时间字段（按序回退） | 说明 |
| --- | --- | --- |
| `video` | `timestamp_sec` → `time_sec` → `ts_sec` → `sec` → `timestamp` → `start_sec` | 视频帧时间戳（秒） |
| `audio` | `start_sec` → `start` → `begin_sec` → `from_sec` → `timestamp_sec` | 语音片段起始时间（秒） |
| `text` | 同 `audio` | 字幕/文本片段起始时间（秒） |

- id 字段按序回退：`id` → `frame_id` → `segment_id` → `unit_id` → `key` → `name`；
  无 id 时按位置生成 `#0`、`#1`…
- 元素也可直接写成数字（等价于 `timestamp_sec` / `start_sec`）。
- 时间戳必须是**有限非负**数字，或可被 `float()` 解析的字符串；否则进原始隔离台账。
- `end_sec` 可选，若给出且小于起始时间 → 原始隔离台账。
- 时间轴按起始时间**升序排序**后建索引。

### 1.3 `temporal_bindings` 绑定关系

```json
[
  {"binding_id": "b0", "video": "f0", "audio": "a0", "text": "t0"},
  ["f1", "a1", "t1"]
]
```

- 对象写法：同时给出 `video` / `audio` / `text` 三个引用
  （引用可以是时间点 id，或是下标整数 / 数字字符串，或 `#<序号>`）；
  也接受 `video_id` / `audio_id` / `text_id`、`video_segment` 等别名。
- 三元数组写法：`[video, audio, text]`。
- 引用不存在、字段不全 → **原始隔离台账**（结构非法，不静默丢弃）。
- 一条绑定都没有 → `status = not_applicable`（无法判定，留样本池、不隔离）。
- 绑定数超过 `max_bindings` → 原始隔离台账。

### 1.4 `temporal_anchors` 线性插值锚点

```json
[{"video_sec": 0.0, "source_sec": 0.9}, {"video_sec": 2.0, "source_sec": 2.9}]
```

- `video_sec`：视频时钟（参考时钟）；`source_sec`：语音/字幕共用时钟（别名 `audio_sec` /
  `text_sec` / `subtitle_sec` / `source` / `sec`）。
- **口径约定**：语音与字幕被视为共用同一 `source` 时钟，通过同一组锚点线性映射到视频时钟。
  同一时钟场景下锚点为恒等映射，或直接省略锚点（则按未校准处理，见 §3）。
- 去重后锚点数 `< min_anchors_for_calibration`（默认 2）时 → `interpolation.applied=false`。

---

## 2. 输出

全部产物只写入 `OUTPUT_DIR/<uniqueId>/temporal_consistency/<runId>/`。

| 文件 | 何时产生 | 内容 |
| --- | --- | --- |
| `15_temporal_alignment.jsonl` | 开关打开（主产物） | 判定结果为 `passed` / `not_applicable` 的记录（两者都留样本池） |
| `15_passthrough.jsonl` | 开关关闭（主产物） | 原记录 + `processing["15"].status = "skipped"`，**不写任何时序结论** |
| `15_sample_quarantine.jsonl` | 开关打开 | 时间差超限记录（标签 `样本-语义不通`），交 #21 样本隔离库 |
| `15_pending.jsonl` | `require_calibration=true` 且锚点不足 | 待人工校准台账（`action=waiting_manual`），留产线、不隔离 |
| `15_rejects.jsonl` | 始终 | 损坏/缺字段/时间戳不可解析/sidecar 不可读/编码异常/坏行 |
| `manifest.json` | 始终 | 运行清单 `crossmodal-manifest-v1` |
| `<runId>.log` | 始终 | 规范日志，每行 `[时间] [动作] 消息` |
| `media/**` | 有可搬运引用时 | 按原相对路径复制的媒体/sidecar |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（已停止调用 `utils/lineage.py`、`utils/mty_compliance.py` 的血缘/归档接口，代码保留存档；`utils/transport.py` 仅保留 `crossmodal-files-v1` 文件对象与 `manifest.json` 打包能力）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

### 2.1 记录级新增字段

**`temporal_alignment`**（主产物每条记录都写）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `operator_number` | int | 固定 `15` |
| `operator_version` | string | 如 `v1.0.0` |
| `status` | int | 记录级结论：1=通过/不适用/待校准/透传，0=不通过（隔离） |
| `process_status` | string | 原字符串流程态：`passed` / `failed` / `not_applicable` / `pending_calibration` / `skipped` |
| `passed` | bool \| null | 通过为 `true`，隔离为 `false`，**不适用为 `null`**（绝不写成通过） |
| `max_delta_sec` | number \| null | 各绑定时间差的最大值（秒）；不适用为 `null` |
| `avg_delta_sec` | number \| null | 各绑定时间差的算术平均（秒）；不适用为 `null` |
| `threshold_sec` | number | 本次生效阈值 |
| `binding_count` | int | 实际参与计算的绑定数 |
| `interpolation` | object | `{type, applied, requested_method, method, anchor_count, min_anchors, max_residual_sec}` |
| `evidence` | array | 逐绑定证据（`binding_id` / `video_sec` / `audio_sec` / `audio_calibrated_sec` / `text_sec` / `text_calibrated_sec` / `delta_sec` / `within_threshold`），上限 `max_evidence_bindings` |
| `evidence_truncated` | bool | 证据是否被截断（计数仍完整） |
| `reason_code` | string | 见 §4 |
| `label` | string | 统一标签，见 §4 |
| `action` | string | `keep` / `isolate` / `waiting_manual` |
| `destination` | string | `sample_pool` / `sample_quarantine` / `pipeline_wait` |
| `review_required` | bool | 仅待人工校准为 `true` |
| `human_verified` | bool | 固定 `false`（本算子不做人工复核） |
| `event_time` | string | ISO8601（+08:00） |
| `source_reference` | string | 表1 来源功能点 `F-ALIGN-039；F-ALIGN-040` |
| `algorithm` | string | `linear_time_interpolation_v1` |
| `unit` | string | `second` |

**`operator_15`**（本算子命名空间）：`operator_number`、`operator_version`、`status`、
`process_status`、`decision`、`reason_code`、`evidence`（含 `max_delta_sec`、`avg_delta_sec`、
`threshold_sec`、`binding_count`、`interpolation`、`evidence_truncated`）、`human_verified`、
`review_required`、`source_reference`、`event_time`。

**`processing["15"]`**：`operator`、`operator_version`、`status`、`process_status`、`decision`、
`reason_code`、`label`、`review_required`。上游字段（如 `processing["12"]`、`alignment`、`license`）一律保留。

### 2.2 台账条目（隔离/待校准）

```json
{
  "label": "样本-语义不通",
  "stage": "S3",
  "operator": 15,
  "operator_name": "temporal_consistency",
  "operator_version": "v1.0.0",
  "action": "isolate",
  "destination": "sample_quarantine",
  "event_time": "2026-10-06T17:02:14+08:00",
  "batch_id": "batch-a",
  "sample_id": "tl-over-threshold",
  "retry_count": 0,
  "reason_code": "max_delta_exceeded",
  "line_number": 3,
  "record": { "...": "含 temporal_alignment 的完整记录" }
}
```

坏行（JSON 解析失败 / 非对象）没有 `record`，但仍有 `line_number` 与 `reason_code`。

---

## 3. 线性插值口径

设某绑定解出视频时间 \(v\)、语音起始 \(a\)、字幕起始 \(t\)。

1. 用锚点构建 `source → video` 映射 \(M\)：
   - `piecewise_linear`（默认）：对锚点按 `source` 升序做分段线性插值，区间外按边界段斜率外推；
   - `global_linear`：对全部锚点做最小二乘直线拟合。
   - 有效锚点（`source` 去重后）少于 `min_anchors_for_calibration` → `applied=false`，\(M\) 退化为恒等映射。
2. 校准：\({a}' = M(a)\)，\({t}' = M(t)\)。
3. 绑定时间差：\(\delta = \max(|v - {a}'|,\ |v - {t}'|,\ |{a}' - {t}'|)\)，保留 6 位小数。
4. 记录级 \( \delta_{\max} = \max \delta\)，\( \delta_{\text{avg}} = \text{mean }\delta\)。
5. **判定**：\(\delta_{\max} \le \text{threshold}\) → `passed`（含等号边界）；否则 `failed` → 样本隔离库。
   0.5 s 默认阈值的中间漂移标定证据见 `03_m15_drift_calibration.jsonl`
   （2026-11-05：0.3/0.5 s 通过、0.7 s 隔离）。

`interpolation.applied=false` 时仍然计算**真实**时间差（不做插值），并在证据中如实标注；
`require_calibration=true` 时此类记录改判 `pending_calibration`（留产线、不隔离、不判通过）。

---

## 4. 状态、标签、原因码、去向

| 场景 | status | process_status | passed | label | action | destination |
| --- | --- | --- | --- | --- | --- | --- |
| 时间差 ≤ 阈值 | `1` | `passed` | `true` | `样本-评分：通过` | `keep` | `sample_pool` |
| 时间差 > 阈值 | `0` | `failed` | `false` | `样本-语义不通` | `isolate` | `sample_quarantine` |
| 缺任一时间轴 / 纯图文 / 无绑定 | `1` | `not_applicable` | `null` | `流程-时序不适用` | `keep` | `sample_pool` |
| 锚点不足且要求校准 | `1` | `pending_calibration` | `null` | `流程-时序待人工校准` | `waiting_manual` | `pipeline_wait` |
| 损坏 / 时间戳不可解析 / 坏行 | — | — | — | `原始文件-无法解析` | `record` | `origin_quarantine` |
| sidecar 不可读 / 非 UTF-8 / 非 JSON | — | — | — | `原始文件-损坏` | `record` | `origin_quarantine` |
| 缺 `sample_id` | — | — | — | `原始文件-缺字段` | `record` | `origin_quarantine` |
| 文件不是严格 UTF-8 | — | — | — | `原始文件-编码异常` | `record` | `origin_quarantine` |
| 路线关闭 | `1` | `skipped` | — | — | — | — |

原因码：`within_threshold`、`max_delta_exceeded`、`no_timestamp_timeline`、
`missing_video_timeline`、`missing_audio_timeline`、`missing_text_timeline`、
`no_temporal_binding`、`timeline_invalid`、`timeline_unreadable`、`too_many_bindings`、
`calibration_unavailable`、`missing_sample_id`、`json_parse_error`、`record_not_object`、
`encoding_error`、`route_disabled`。

---

## 5. 统计与守恒

`statistics` 关键字段：`enabled`、`entrypoint`、`input_records`、`blank_lines`、
`valid_records`、`bad_lines`、`origin_quarantined`、`rejects_entries`、`passed_records`、
`failed_records`、`not_applicable_records`、`pending_calibration_records`、`skipped_records`、
`main_records`、`calibrated_records`、`uncalibrated_records`、`media_carried`、`media_missing`、
`max_time_delta_sec`、`interpolation`、`interpolation_method`、`require_calibration`、
`model`（`null`）、`models_available`（`false`）、`degraded`、`encoding_error`、
`file_read_incomplete`、`empty_input`、`conservation_ok`、`reasons`。

四条守恒式（`conservation_ok` 为四者之合取）：

1. `input_records == valid_records + bad_lines`
2. `valid_records == passed + failed + not_applicable + pending_calibration + skipped + origin_quarantined`
3. `rejects_entries == bad_lines + origin_quarantined`
4. 开关打开：`main_records == passed + not_applicable`；开关关闭：`main_records == skipped`

---

## 6. 接口壳（三接口，无 `/downloadFile`）

外层统一 `{"code": 0|1, "msg": "...", "data": {...}}`。

- `POST /uploadFile?uniqueId=` —— 单个 JSONL/媒体/sidecar，或含 `manifest.json` 的 zip（安全解压）。
- `POST /predict` —— 双协议：
  - 形态一（`multipart/form-data` + `?detection=`）：`file` 必传，返回 `{"status": 1, "process_status", "uniqueId", "pair_id", "applicable", "max_delta_sec", "threshold_sec", "decision", "interpolation_method"}`；失败 `{"status": 0, "msg"}`；
  - 形态二（`application/json`）：请求体 `{uniqueId, isLastOperator, inputFile?, config?}`，返回 `{code,msg,data{operator,status,outputFile,result{uniqueId,runId,entrypoint,outputFile,statistics,model}}}`；`isLastOperator` 仍必传（严格布尔），v4.2 起中间/阶段末响应同构，不再返回 `lineageFile`；
  - 阶段首不传 `inputFile`（从 `<task>/input` 取）；阶段内后续算子传上一运行目录内的入口文件路径。
- `POST /removeFile?uniqueId=` —— 幂等清理任务目录。
