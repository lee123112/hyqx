# 算子 8：带时间索引的语音转写（`multimodal_preprocessing`）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

demo 阶段的第 7 个处理节点（平台 M08），也是 **S2 阶段末算子**：把 03—06 处理后的完整
中间记录做记录级校验与媒体搬运，对**音频/带音轨视频**生成带时间索引的 ASR 段落单元，
并返回**实际输出文件内容**（中间/阶段末响应同构）。

> **[v1.0.1 职责收缩]** 按表1 #8 边界，本节点只产出「带时间索引的语音转写」：
> `semantic_unit` 固定 `null`（deprecated），**不再组对、不再生成跨模态语义单元**；
> 图文/音文/视文的配对级 `semantic_units` 由 #12–#15 生成。

- 交付定位：`demo`（不是正式算子），文件内容对象格式与限额为 demo 约定；
- 带音频/音轨且打开转写开关时，由本算子惰性加载本地 Whisper-base（固定 `whisper-base`、
  固定中文解码）生成转写、`start_ms`/`end_ms` 时间索引和 16kHz 单声道音频片段；
  开关关闭时不 import/初始化/加载模型；纯图文任务不加载模型；
- 转写失败/无语音/无音轨都留在产线（进候选描述，不直接评分），不重试、不补采、不联网；
- 无外部服务依赖：不需要上游算子已实现即可独立开发（fixture 自包含），也不访问网络。

## 1. 目录结构

```
07_跨模态协同预处理/
├── app.py                  # FastAPI 三接口
├── inference.py            # 配置加载 + run 主流程 + CLI
├── utils/
│   ├── coordination.py     # 业务：记录级校验、媒体引用收集（不再组对/生成 semantic_unit）
│   ├── schemas.py          # 部署配置 / 请求覆盖 / 请求模型（extra=forbid）
│   ├── io.py               # 任务锁、原子写、JSONL 流式读写、媒体路径解析
│   ├── transport.py        # crossmodal-files-v1 文件内容对象 + 运行清单
│   ├── lineage.py          # 血缘构造/裁剪/阶段上下文（v4.2 停用，存档）
│   └── logger.py           # run.log
├── configs/config.yaml     # 部署默认配置
├── dataset/                # fixture（含 DATA_SCHEMA.md 与生成脚本）
├── tests/                  # test_operator / test_api / test_transport
├── packaging/              # Dockerfile + 打包准备脚本 + 代码副本
├── map.json                # demo 平台映射（id/tag/port/entrypoint）
├── requirements.txt
├── OPERATOR_MANUAL.md
└── HANDOFF.md              # 实际命令、结果、未验证项与遗留问题
```

## 2. 三接口（与 `DEVELOPMENT_CONTRACT.md` 一致）

统一信封：成功 `{"code":0,"msg":"success","data":{...}}`；失败 `code=1` 且 `data` 为 `{}`。
参数错误可返回 HTTP 422，但同样是规范信封。

| 接口 | 请求 | 成功 `data` |
| --- | --- | --- |
| `POST /uploadFile?uniqueId=<id>` | multipart `file`（zip 素材包或单个 JSONL） | `{uniqueId, entrypoint, fileCount, bytes}` |
| `POST /predict`（形态二） | `{uniqueId, isLastOperator, inputFile?, config?}` | `{operator, status, outputFile, result}`（中间/阶段末同构） |
| `POST /predict?detection=align_semantic_unit`（形态一） | multipart `file`（单音频/带音轨视频，或 JSON/JSONL/zip）+ 可选 `uniqueId`、`enabled`/`transcribe` | `{status:1, process_status, text, segments, model_version}`（成功）；失败 `{"status":0,"msg":"..."}` |
| `POST /removeFile?uniqueId=<id>` | query | `{}`（幂等） |

- **不实现 `/downloadFile`**：请求该路径返回 404（有测试覆盖）。
- `isLastOperator` 是严格布尔（字符串 `"false"`、数字 `1` 均报错）；未知字段一律报错。
- `inputFile` 只能是**任务根内**路径（同 `uniqueId` 的上游运行目录），跨任务/越界一律拒绝。
- `config` 只覆盖业务参数，`operator`/`stage_id` 与未知键会被拒绝（部署身份不可由请求改写）。

### 统一响应与调用语义（v4.2）

- `isLastOperator` 仍必传，用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回
  `data{operator,status,outputFile,result}`，不再用 `false` 只回血缘对象。
- `data.outputFile` 是 `crossmodal-files-v1` 文件内容对象，内含入口 `07_candidates.jsonl`、
  全部被引用媒体、异常清单、上游旁路清单与 `manifest.json`。
- `data.result` 为业务摘要：int `status`（1 处理成功）+ `process_status`
  （`success`/`partial`/`degraded`）+ `statistics` + `entrypoint` + `input`（任务相对路径）。
- 形态一成功返回 JSON（`status=1`，含 `process_status`/`text`/`segments`/`model_version`），
  转写成功时另含 `transcript`；转写关闭时 `segments=[]`、`model_version=null`；不是文件流。
- 同一 `uniqueId` 重复调用会持续产生 `run001`、`run002`…，互不覆盖。

## 3. 启动与调用

```powershell
# 依赖
pip install -r requirements.txt

# 启动服务（默认端口 8107，与 map.json 一致）
python app.py --host 127.0.0.1 --port 8107
```

```bash
# 1) 上传素材包（zip 内含 demo.jsonl + 媒体 + manifest.json）
curl -X POST "http://127.0.0.1:8107/uploadFile?uniqueId=demo_task_001" \
     -F "file=@package.zip"

# 2) 阶段内调用（isLastOperator=false，响应与阶段末同构）
curl -X POST http://127.0.0.1:8107/predict \
     -H "Content-Type: application/json" \
     -d '{"uniqueId":"demo_task_001","isLastOperator":false}'

# 3) 阶段末调用（isLastOperator=true，同样返回文件内容对象 + result）
curl -X POST http://127.0.0.1:8107/predict \
     -H "Content-Type: application/json" \
     -d '{"uniqueId":"demo_task_001","isLastOperator":true}'

# 3') 形态一单文件（multipart；成功返回 status=1 JSON）
curl -X POST "http://127.0.0.1:8107/predict?detection=align_semantic_unit" \
     -F "file=@clip.wav" -F "transcribe=false"

# 4) 清理任务目录（幂等）
curl -X POST "http://127.0.0.1:8107/removeFile?uniqueId=demo_task_001"
```

CLI（与 API 共用同一 `run` 与配置校验）：

```powershell
python inference.py --input dataset/sample/demo.jsonl --unique-id demo_task_001 --output-root output
python inference.py --input dataset/sample/demo.jsonl --unique-id demo_task_002 --output-root output `
  --override '{\"transcription_enabled\":true}' --is-last
```

## 4. 配置

`configs/config.yaml`：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `operator.name` / `operator.version` | `multimodal_preprocessing` / `v1.0.0` | 部署身份，请求不可改写 |
| `stage_id` | `S2` | 阶段号 |
| `enabled` | `false` | 表1 #8 总开关；关闭时不加载任何模型 |
| `transcription.enabled` | `false` | 音频/音轨转写开关；与总开关同时打开才加载 Whisper-base |
| `transcription.model` / `language` | `whisper-base` / `zh` | 表1 固定模型和中文解码语言（请求不可覆盖） |
| `transcription.beam_size` | `1` | Whisper 解码 beam 宽度；第三轮专项配置使用 3 |
| `transcription.initial_prompt` | `""` | 可选简体中文提示；检测到提示词回声时自动回退无提示解码 |
| `transcription.precheck` | `true` | 送 ASR 前的时长/信噪比预检；命中即不加载模型、不产出文本，记 `no_speech` + 原因码 |
| `transcription.min_duration_seconds` | `0.4` | 预检时长下限；更短的音频易触发 Whisper 幻觉/空文本 |
| `transcription.min_snr_db` | `6.0` | 预检信噪比下限（20·log10 帧 RMS 的 P90/P10）；仅当波形接近稳态（帧 RMS 波动系数 < 0.10）才判 `low_snr`，低 SNR 但有起伏的语音不拦截 |
| `unit_policy` | `per_sample` | **[deprecated]** 保留解析，不再参与判定 |
| `max_av_duration_diff_ratio` | `0.20` | **[deprecated]** 不再做音视频时长比对 |
| `av_mismatch_action` | `review` | **[deprecated]** 不再 review/reject 配对记录 |
| `validate_frame_timestamps` | `true` | **[deprecated]** 不再校验上游帧时间戳 |
| `require_existing_media` | `true` | run/HTTP/CLI **强制为 true**，请求不可关闭 |

## 5. 处理与输出

`07_candidates.jsonl`：完整记录透传 + `semantic_unit`（**固定 `null`，deprecated**）+
`processing["07"]`（`process_status` 取 `transcribed`/`skipped`/`not_applicable`/
`no_speech`/`failed`）。有 ASR 来源且打开转写时增加 `audio_transcript`、`time_index`
（`start_ms`/`end_ms` 整数毫秒、`[start, end)`，同时保留 `start`/`end` 秒）和
`transcription`（含 `model_version`）。无声视频（`no_audio_track`）归 `not_applicable`，
额外写 `visual_time_index`（由 #6 视频分段生成，来源 `m08_visual_index`）并登记
`missing_modality=["audio"]`；`processing` 中 02—06 结果与自定义字段原样保留。

送 ASR 前先做过一次时长/信噪比预检（`transcription.precheck`）：静音、过短、
低信噪比**且接近稳态**的样本直接记 `no_speech` + `audio_silent`/`audio_too_short`/`low_snr`，
**不加载模型、不进入解码循环**；低 SNR 但有明显能量起伏的语音/伴奏不拦截（避免误杀），
交给 ASR 正常判断。`transcription.precheck` 携带时长、SNR 与帧 RMS 波动系数证据，
统计里另有 `precheck_skipped`（命中数）与 `precheck_reasons{}`（原因分布）。

`07_supplement_requests.jsonl`：待补清单（`role=supplement`），只登记缺模态与来源，
**不回采**；无缺模态时为空文件。

`07_rejects.jsonl`：坏行、非法记录、重复 `sample_id`、缺媒体、路径越界，均带
`reason`+`message`+`line`+完整原记录。字段级说明见 `dataset/DATA_SCHEMA.md`。

统计守恒（不成立即整次运行失败，不留半成品）：

```
input  == output + rejected
output == transcribed + transcription_skipped + not_applicable + no_speech + transcription_failed
```

## 6. 测试

```powershell
python -m pytest tests -q
```

- `tests/test_operator.py`：业务判定表 + run 集成（统计守恒、媒体搬运、任务隔离、配置规则）；
- `tests/test_api.py`：三接口契约、`isLastOperator` 两种取值的同构响应、失败信封、`/downloadFile` 404、
  幂等清理、打包恢复后媒体可读；
- `tests/test_transport.py`：文件内容对象编解码/严格校验、清单去重、入口定位、超限不截断。

## 7. 已知限制（demo 阶段）

1. 文件内容对象是 base64/UTF-8 的 JSON 承载，**不是**正式二进制协议，限额 64MiB；
2. 单一 run 目录内的媒体按相对路径原样落盘，不做内容哈希去重；
3. 不做跨模态配对单元（已移交 #12–#15）；ASR 段落单元按静音间隔/最长时长切分，
   不保证与视觉事件对齐；
4. 视频多音轨时取 ffmpeg/音轨探测到的首条可用音轨，多声道统一下混为单声道；
   不做真正的视频裁剪；
5. 目录内“唯一 JSONL 自动推断入口”只认 `demo.jsonl` / `samples.jsonl`（共享适配层行为），
   本算子的产物必须通过 `manifest.json` 或显式 `inputFile` 定位；
6. fixture 中 `media/original/*.mp4` 为占位容器，不能验证任何解码/媒体质量。
