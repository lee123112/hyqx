# 算子 8 说明书：带时间索引的语音转写

> 配套文件：`README.md`（使用概览）、`dataset/DATA_SCHEMA.md`（数据契约）、`HANDOFF.md`（测试证据）。
> 版本：v1.0.1；阶段：S2（**阶段末算子**）；平台算子编号：M08（包内节点 `processing["07"]`）。

## 1. 概述

| 项 | 内容 |
|---|---|
| 职责 | 对 03—06 处理后的完整中间记录做**记录级校验 + 媒体搬运**；对音频/带音轨视频**按需做语音转写**并输出带时间索引的 ASR 段落单元（中文转写 + `start_ms`/`end_ms` + 音频片段）；缺模态登记待补清单 |
| 输入 | 同阶段上游主清单（如 `06_audio_index.jsonl`）；阶段首亦可直接用上传素材包 `manifest.json` 定位入口；形态一支持单音频/带音轨视频 |
| 输出 | `07_candidates.jsonl`（主清单，含 `audio_transcript`/`time_index`/`transcription`）、`07_rejects.jsonl`（异常清单）、`07_supplement_requests.jsonl`（待补清单）、上游旁路清单（随包携带）、被引用的媒体、按段落裁开的 16kHz 单声道片段 `06_audio/segments/*.wav`、`manifest.json` |
| 算法 | 规则（CPU）：记录级校验 + 媒体引用收集；**语音转写（可选，默认关闭）**：固定 `whisper-base`、按中文解码、输出带起止时间的文本，再按段落把音频裁开。**不组对、不生成 semantic_unit、不打任何对齐分**（配对语义单元与评分分别属于 #12–#15） |
| 依赖 | FastAPI / Uvicorn / Pydantic / PyYAML / python-multipart（服务与测试）；转写开关打开时才需要 torch / transformers / numpy（惰性 import）。音轨探测/非 WAV 解码可选用 ffprobe/ffmpeg，缺失时回退标准库 `wave` |
| 端口 | 8107 |
| 数据落盘 | `OUTPUT_DIR/<uniqueId>/multimodal_preprocessing/runNNN/` |

> **[v1.0.1]** `semantic_unit` 字段固定 `null`（**deprecated**），本算子不再生成任何跨模态
> 配对语义单元。任务书 #8 的「语义单元」指 **ASR 段落单元**（`time_index`），与 #12–#15
> 的配对级 `semantic_units` 是两种不同概念，不双写。

## 2. 部署

### 2.1 直接运行（开发/联调）

```bash
cd G:/HYQX/算子/跨模态/demo/07_跨模态协同预处理
python -m pip install -r requirements.txt
python dataset/sample/make_sample.py                    # 可选：就地重建 fixture
export OUTPUT_DIR=/data/output                          # PowerShell: $env:OUTPUT_DIR="D:/data/output"
python -m uvicorn app:app --host 0.0.0.0 --port 8107
```

### 2.2 容器

```bash
cd packaging
python prepare_package.py
docker build -t crossmodal-multimodal-preprocessing:v1.0.0 .
docker run --rm -p 8107:8107 -v /host/output:/data/output \
  -e OUTPUT_DIR=/data/output crossmodal-multimodal-preprocessing:v1.0.0
```

- **同阶段**（S2 上游 03—06 与本算子）服务必须挂载同一共享 `OUTPUT_DIR` 且容器内路径一致，
  否则本算子无法读取上游 run 目录；跨容器路径不得假定存在。
- 环境变量：`OUTPUT_DIR`（输出根）、`OPERATOR_CONFIG`（自定义配置路径，默认 `configs/config.yaml`）。
- 镜像不安装任何模型/编码库；base64、json、shutil 均为标准库，无需系统依赖。

## 3. 接口

| 方法 | 路径 | 请求 | 成功返回 |
|---|---|---|---|
| POST | `/uploadFile?uniqueId=<任务id_文件id>` | multipart `file`（zip 素材包或单个 JSONL） | `{uniqueId, entrypoint, fileCount, bytes}` |
| POST | `/predict?operatorId=M08` | JSON `{uniqueId, isLastOperator, inputFile?, config?}` | 统一响应（中间/阶段末同构，见下方） |
| POST | `/predict?detection=align_semantic_unit` | multipart `file`（单音频/带音轨视频，或 JSON/JSONL/zip）+ 可选 `uniqueId`、`enabled`/`transcribe` | `{status:1, process_status, text, segments, model_version}`；失败 `{"status":0,"msg":"..."}` |
| POST | `/removeFile?uniqueId=<任务id_文件id>` | query | `{}`（幂等） |
| * | `/downloadFile` | — | **契约要求不实现**，明确 404 |

统一信封：成功 `{"code":0,"msg":"success","data":{...}}`；失败 `{"code":1,"msg":"...","data":{}}`。
参数错误返回 HTTP 422，但仍是规范信封（`data` 为 `{}`）。

`/predict` 请求字段：`uniqueId`（string，必传）、`isLastOperator`（**严格** bool，必传；
字符串 `"false"`、数字 `1` 均报错）、`inputFile`（可选，**任务根内**路径，越界/跨任务拒绝）、
`config`（可选，仅业务参数，未知键与 `operator`/`stage_id` 一律拒绝）。

- **统一响应（v4.2）**：`isLastOperator` 仍必传（阶段末标记），但**中间与阶段末响应同构**，
  均返回 `data{operator,status,outputFile,result}`：`data.outputFile` 为 `crossmodal-files-v1`
  文件内容对象（入口 `07_candidates.jsonl` + 全部被引用媒体 + `07_rejects.jsonl` + 上游旁路清单
  + `manifest.json`）；`data.result` 为业务摘要（int `status` + `process_status` + `statistics`
  + `entrypoint` + `input`），不再用 `isLastOperator=false` 只回血缘对象。
- 形态一（`detection`）成功返回 `{"status":1,"process_status":...,"text":...}` 的 JSON（不是文件流），
  失败返回 `{"status":0,"msg":"..."}`；`transcribe` 默认关闭，关闭时不加载模型。

`status` 取 `success` / `partial`（有记录进异常清单，仍为 `code=0`）/ `degraded` / `failed`；
输出记录的业务 `status` 为 int（1/0）+ `process_status`。

## 4. 配置说明

`configs/config.yaml`：

| 键 | 默认 | 说明 |
|---|---|---|
| `operator.name` / `operator.version` | `multimodal_preprocessing` / `v1.0.0` | 部署身份，请求不可改写 |
| `stage_id` | `S2` | 阶段号 |
| `enabled` | `false` | **总开关**，默认关闭（需显式打开）；关闭时不加载任何模型（含 Whisper-base） |
| `transcription.enabled` | `false` | **「是否做语音转写」开关**；默认关闭，关闭时不加载模型 |
| `transcription.model` | `whisper-base` | 表1 **固定项**，写其他值直接配置错误（边界：不换成更大的 Whisper） |
| `transcription.language` | `zh` | 表1 **固定项**：按中文解码，请求不可覆盖 |
| `transcription.model_root` / `model_dir` | `$MODEL_ROOT`（默认算子本地 `<算子目录>/weight`）/ `whisper-base` | 本地权重目录（`model_root: .` 即本算子 `weight/`），只 `local_files_only` 加载，运行期不联网（不补采） |
| `transcription.device` | `auto` | `auto` / `cpu` / `cuda`；`auto` 有 CUDA 时用 GPU |
| `transcription.max_segment_seconds` | `30.0` | 单段落最大时长；超过即按段落切开 |
| `transcription.paragraph_silence_seconds` | `0.6` | 相邻转写片段间隔超过该秒数即切为新段落 |
| `transcription.beam_size` | `1` | 解码 beam 宽度 |
| `transcription.initial_prompt` | `""` | 可选简体中文提示；识别到提示词回声时回退无提示解码，不把提示词写入转写 |
| `transcription.precheck` | `true` | 送 ASR 前做时长/信噪比预检；命中即不加载模型、不产出文本，记 `no_speech` + 原因码 |
| `transcription.min_duration_seconds` | `0.4` | 预检时长下限（秒）；更短的音频易触发 Whisper 幻觉/空文本 |
| `transcription.min_snr_db` | `6.0` | 预检信噪比下限（dB）；仅当帧 RMS 波动系数 < 0.10（波形接近稳态）时才判 `low_snr` |
| `transcription.fail_action` | `keep_in_pipeline` | 转写失败处置：留在产线、进候选描述、不能直接评分 |
| `unit_policy` | `per_sample` | **[deprecated]** 保留解析，不再参与判定 |
| `max_av_duration_diff_ratio` | `0.20` | **[deprecated]** 不再做音视频时长比对 |
| `av_mismatch_action` | `review` | **[deprecated]** 不再 review/reject 配对记录 |
| `require_existing_media` | `true` | run/HTTP/CLI **强制为 true**，写为 false 直接报错，请求不可关闭 |
| `validate_frame_timestamps` | `true` | **[deprecated]** 不再校验上游帧时间戳 |

- 业务参数（`enabled`、`transcription_enabled`）可被请求 `config` 覆盖；
  `operator`、`stage_id`、`require_existing_media` 不可被请求放宽；
  **`transcription.model` 与 `transcription.language` 是表1 固定项，请求侧只能切换开关
  `transcription_enabled`，不能改模型或解码语言。**
- **[deprecated]** `unit_policy`、`max_av_duration_diff_ratio`、`av_mismatch_action`、
  `validate_frame_timestamps` 仍可被解析（旧请求不报错），但不再参与任何判定。
- 未知字段/枚举、越界数值、字符串数值（如 `"0.5"`）、NaN/Inf → 报错（配置错误、HTTP 422、CLI 退出码 1）。

## 5. 处理与输出

`07_candidates.jsonl`：完整记录透传 + `semantic_unit`（**固定 `null`，deprecated**）+
`processing["07"]`（`status` / `process_status` / `reason` / `metrics`）；02—06 的
`processing` 结果与自定义字段**原样保留**。

记录级 `process_status` 完全由 ASR 结果决定：

| process_status | status | 条件 |
|---|---|---|
| `transcribed` | 1 | 有音频/音轨且转写成功 |
| `skipped` | 1 | 总开关或转写开关关闭（不加载模型） |
| `not_applicable` | 1 | 无音频来源（图片/纯文本），或视频无音轨 |
| `no_speech` | 1 | 有音轨但空转写，或预检命中（`audio_silent`/`audio_too_short`/`low_snr`）；不伪造文本 |
| `failed` | 0 | 解码失败/模型缺失/后端不可用（留在产线，不能直接评分） |

`07_rejects.jsonl`：坏行与记录级缺陷带 `reason` + `message` + `line` + 完整原记录。
拒绝原因：`invalid_json`、`invalid_record`、`duplicate_sample_id`、
`unsupported_media_path`、`media_missing`、`media_unreadable`。

`07_supplement_requests.jsonl`：缺模态待补清单（`role=supplement`），只登记不补采。

统计守恒（不成立即整次运行失败，不留半成品 run 目录）：

```
input  == output + rejected
output == transcribed + transcription_skipped + not_applicable + no_speech + transcription_failed
```

媒体搬运：仍被引用的媒体**按原相对路径**复制进 run 目录，不重编码、不改名、不做内容哈希去重，
因此下游恢复后自包含可读。`media/original/*` 原视频引用一并搬运。字段级说明见 `dataset/DATA_SCHEMA.md`。

### 5.1 语音转写（表1 #8，默认关闭）

输入为音频或带音轨的视频时，本算子按需产出语音转写与**时间索引**：

```
"transcription": {
  "status": "ok",                 // ok / failed / skipped / not_applicable / no_speech
  "model": "whisper-base",        // 表1 固定
  "model_version": "whisper-base",// 表1 要求；与 model 同值
  "language": "zh",               // 表1 固定：中文解码
  "device": "cuda",
  "text": "大家好，这是……",
  "paragraph_count": 2,
  "paragraphs": [{"index":0,"start":0.0,"end":5.0,"text":"…",
                  "segment_path":"06_audio/segments/<sid>_000.wav","segment_bytes":160000}],
  "reason": null
},
"time_index": [{"index":0,"start":0.0,"end":5.0,"start_ms":0,"end_ms":5000,
                "text":"…","segment_path":"06_audio/segments/<sid>_000.wav"}],
"transcription_gate": {"transcript_required":true,"transcript_available":true,
                       "direct_scoring_allowed":true,"reason":null}
```

- `start_ms`/`end_ms` 为整数毫秒、左闭右开 `[start, end)`（秒×1000 四舍五入），按索引
  单调、相邻不重叠；`start`/`end` 秒字段保留一版给现有消费方。
- `not_applicable`（无音轨）与 `no_speech`（空转写）都不算 `failed`，都不伪造文本；
  `failed` 才把 `status` 记为 0，三者均留在产线并进候选描述。

**无声视频（容器不含音轨）**：`process_status=not_applicable`、`reason=no_audio_track`、
`time_index=[]`，按 #6 已切好的 `video.segments` 生成**视觉时间索引** `visual_time_index`
（与语音时间轴分字段存放，条目带 `source=visual_segment`、`text=null`）；
`transcription_gate` 增加 `visual_time_index_available=true` 与
`visual_time_index_source=m08_visual_index`，记录级同时写 `missing_modality=["audio"]` 并登记
待补清单。该字段仅供 #17 在语音时间轴缺失时预填时间段，**不是转写文本，也不得直接用于
音文评分**；#11/#19 等既有 `time_index` 消费方不受影响。没有任何视频分段时写空列表并标
`visual_time_index_available=false`。

```json
"visual_time_index": [{"index":0,"start":0.0,"end":6.4,"text":null,
                       "segment_path":null,"source":"visual_segment"}],
"transcription_gate": {"transcript_required":true,"transcript_available":false,
                       "direct_scoring_allowed":false,"reason":"no_audio_track",
                       "visual_time_index_available":true,
                       "visual_time_index_source":"m08_visual_index",
                       "visual_time_index_entries":1}
```

- **开关语义**：总开关 `enabled` **或** `transcription.enabled` 任一为 `false` ⇒
  完全不 import / 不加载模型，记录 `status=skipped`、`reason=master_switch_off|transcription_disabled`，
  `time_index=[]`、`direct_scoring_allowed=false`。日志中出现
  `whisper_not_loaded` 与 `未加载 Whisper-base`。
- **中文解码**：固定 `language="zh"`，`return_timestamps=true`，段落起止时间由 Whisper
  chunk 时间戳归一化而来（缺失时间戳按前一段结束时间补齐，保证时间索引始终可用）。
- **中文书写稳定性**：可选 `initial_prompt` 与 `beam_size` 只调整 Whisper-base 解码；若生成结果
  含提示词回声，立即用同一 Whisper-base 无提示重解码，并在 `transcription.prompt_fallback=true`
  留痕；不改变模型、阈值或 #13 的评分职责。
- **段落切分**：相邻片段间隔 > `paragraph_silence_seconds`，或累计时长 > `max_segment_seconds`
  即切为新段落，并把该段落裁成 16kHz 单声道 PCM WAV 落到 `06_audio/segments/`，随包搬运。
- **失败处置**：权重缺失 / 解码失败 / 推理失败 / 空转写 / 无音轨 ⇒ 样本**仍留在主清单**（不剔除），
  写 `transcription.status=failed` + `reason` + `candidate_description_required=true`，
  并把 `direct_scoring_allowed` 置为 `false`（进入候选描述，不得直接评分）。**绝不回填占位文本。**
- **音轨探测与解码兜底**：转写前先用 `ffprobe`（`$FFPROBE_PATH` 或 PATH）探测音轨；
  确认无音轨 ⇒ `no_audio_track`（无声视频不是坏文件），不再白跑 Whisper。
  解码顺序：WAV 标准库 → `soundfile` → `torchaudio` → **`ffmpeg` CLI**（`$FFMPEG_PATH` 或 PATH，
  `-f f32le` 直接进内存，不落临时文件）；都不可用时记 `audio_decoder_unavailable`。
  探测不到音轨信息（无 ffprobe / 探测出错）时返回「未知」，由解码结果兜底，绝不误判为无声。
- **送 ASR 前预检**（`transcription.precheck=true`，2026-11-05 新增，11-06 修正误杀）：
  解码后、加载模型前，用 20ms 帧 RMS 粗估时长、信噪比（`20·log10(P90/P10)`）与
  帧 RMS 波动系数（`std/mean`），命中即直接返回 `status=no_speech` +
  `reason` ∈ {`audio_silent`（整段低于静音阈值）、`audio_too_short`
  （< `min_duration_seconds`）、`low_snr`（< `min_snr_db` **且**波动系数 < 0.10，
  即接近稳态）}，**不加载 Whisper、不进入解码循环**；记录仍留主清单并进候选描述。
  低 SNR 但能量起伏明显的语音/伴奏**不拦截**，交给 ASR 判断（真实素材回归：
  时序窗口 w04/w06 的 SNR 5.07/5.54 dB、CV 0.21/0.26，Whisper 仍能转出有效文本）。
  预检只做「明显不可转写」的粗筛，不判断语音内容；置 `false` 即回退为全部送 ASR。
  `transcription.precheck` 写回时长、SNR、波动系数与阈值证据。
- 统计字段：`audio_units`、`transcribed`、`transcription_failed`、`transcription_skipped`、
  `transcription_segments`、`segments_written`、`transcription_model_loaded`、
  `precheck_skipped`、`precheck_reasons{}`、`visual_time_index_built`、
  `visual_time_index_entries`，
  以及按原因的 `transcription_fail_reasons` / `transcription_skip_reasons`。
- **边界**：不换更大的 Whisper（配置写死）、不在本算子打对齐分、失败不重试不补采。

## 6. 运行与运维

- 同一 `uniqueId` 重复调用产生递增 `run001`、`run002`…，**不覆盖**上游或历史 run。
- 同一 `uniqueId` 同时只允许一个写操作（任务锁），锁冲突报错；不同 `uniqueId` 可并发。
- `/removeFile` 幂等：任务不存在同样返回成功。
- 主清单与异常清单先写 `.partial` 再 `os.replace` 原子提交；失败且无产物时清理半成品 run 目录。
- `run.log` 只记录动作、样本标识与必要统计；不打印完整原文、密钥或 token。

## 7. 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| `config 非法——...` | 业务参数非法或写死 `require_existing_media=false` | 按 §4 取值域修正 |
| `inputFile 非法：...` | 越出任务根 / 跨任务 | 传同 `uniqueId` 任务根内路径 |
| `任务目录缺少 input/ 上传内容` | 未上传素材且无 `inputFile` | 先 `/uploadFile` 或传正确 `inputFile` |
| `输入不是严格 UTF-8` | 输入编码非 UTF-8 | 重新导出为 UTF-8 |
| `统计不守恒：...` | 业务判定与计数不一致 | 属实现缺陷，保留现场并上报 |
| `output_too_large` | 文件内容对象超 `max_response_bytes`(64MiB) | 减小媒体体积或提高限额 |
| `输出产物缺失：...` | 返回前门禁：清单中媒体引用未落盘 | 检查搬运，勿手工删媒体 |
| 记录 `reason=media_missing` | 引用的媒体文件不存在 | 检查上游产出与搬运路径 |
| 记录 `reason=invalid_frame_timestamp` | 帧/片段时间戳越界或不递增 | 检查 05/06 处理结果 |
| 记录 `reason=av_duration_mismatch` | 音视频时长差超阈值且策略为 `reject` | 改 `review` 或修数据 |
| 记录标 `review` + `flags=[av_duration_mismatch]` | 超阈值但策略为 `review` | 正常保留，下游质检处理 |
| 记录 `transcription.reason=whisper_weights_missing` | `model_root/model_dir` 下缺 `config.json` | 检查本地权重目录，勿改成联网下载 |
| 记录 `transcription.reason=whisper_backend_unavailable` | 环境缺 torch / transformers | 安装 `requirements.txt` 中的转写依赖 |
| 记录 `transcription.reason=no_audio_track` | ffprobe 确认容器无音轨（无声视频） | 正常；`visual_time_index` 供 #17 兜底，不做音文评分 |
| 记录 `transcription.reason=audio_decode_failed` | 有音轨但所有解码后端都失败 | 检查文件完整性，或安装 `ffmpeg`/音频后端 |
| 记录 `transcription.reason=audio_decoder_unavailable` | `soundfile`/`torchaudio`/`ffmpeg` 均不可用 | 安装 `ffmpeg`（或 `$FFMPEG_PATH` 指向可执行文件） |
| 日志出现 `whisper_not_loaded` | 开关关闭或本批无音频 | 正常；纯图文任务的预期表现（表1 完成判据） |

## 8. 验收（本目录内）

```bash
python -m pytest tests/ -q
python dataset/sample/make_sample.py
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_07 --output-root output/smoke
python -m json.tool map.json
python -m compileall -q inference.py app.py utils
python -m uvicorn app:app --host 127.0.0.1 --port 8107
```

验收关注：记录/计数一致、统计守恒、媒体可恢复可读、`data.result` 统计完整（上游 + 07）。
实际结果见 `HANDOFF.md`。

## 9. 边界与已知限制

- `semantic_unit` 已 deprecated（固定 `null`）：本算子不生成跨模态配对单元，也不提供任何
  语义等价/相似度结论或虚假训练命令；配对级 `semantic_units` 由 #12–#15 生成。
- 语音转写边界（表1 #8）：**只允许** `whisper-base`（不换成更大的 Whisper）；**只按中文解码**；
  **不在本算子打对齐分**（转写文本与描述的一致性由算子 13 评分）；转写失败**不重试、不补采、不联网下载**；
  不做说话人分离、不做标点/翻译，转写文本按模型原样输出（`base` 模型存在识别误差属预期）。
- 带音轨的视频只在本地存在可用音频后端（`wave` 可直接读 WAV，其余容器需 soundfile/torchaudio
  或 ffmpeg）时才能转写；无 ffmpeg 时，非 WAV 音轨会记为 `audio_decode_failed` 并留在产线，
  不做静默跳过。视频多音轨取第一条可用音轨，多声道下混为单声道。
- 缺音轨/缺模态只登记 `07_supplement_requests.jsonl` 并退回来源侧，**不补采**。
- `unit_policy` 等旧配对参数已 deprecated，不再参与判定。
- 文件内容对象是 base64/UTF-8 的 JSON 承载，**不是**正式二进制协议，限额 64MiB；收敛于 `utils/transport.py` 单点适配。
- 单一 run 目录内媒体按相对路径落盘，不做内容哈希去重。
- 目录内“唯一 JSONL 自动推断入口”只认 `demo.jsonl` / `samples.jsonl`（共享适配层行为），
  本算子产物必须经 `manifest.json` 或显式 `inputFile` 定位。
- 最终质检与分流属算子 21；本算子只做记录级校验与组织。
