# 算子 6：音频标准化（audio_standardization）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传；本算子成功返回标准化 WAV 文件流，失败返回 JSON `{"status":0,"msg":"..."}`，详见 §2.2.1）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

把上游（S2）清单里 `audio` 字段引用的音频，统一重采样、混声道、量化成**指定采样率/声道数/位宽的 PCM WAV**，
记录真实时长与解码后端，并把清单中出现的 `image` / `video` / `audio` 媒体按样本搬运到本次运行目录；
输出一份**保留上游原始记录结构**的主清单 `06_audio_index.jsonl` 与可解释的拒绝清单 `06_rejects.jsonl`。

- 映射 ID：`audio_standardization`，服务端口 **8106**（见 `map.json`）
- 正式算子编号：6
- 所属阶段：S2（`isLastOperator` 两种取值都实现且响应同构，不按编号写死）
- 当前位置：`G:\HYQX\算子\跨模态\demo\06_音频标准化\`

本算子**不做**：语音识别/转写、说话人分离、降噪与增益、响度归一（LUFS）、静音切分、
音频内容理解、模型推理（不加载任何权重、不需要训练）、格式转码成非 PCM 格式。

---

## 1. 输入与输出

### 1.1 输入

| 来源 | 说明 |
| --- | --- |
| `input/demo.jsonl`（推荐名，也接受 `samples.jsonl`） | 上游 05 输出的 `05_frame_index.jsonl` 改名/原名上传；每行一个 JSON 对象，见 `dataset/DATA_SCHEMA.md` §7 |
| `input/media/...` | 清单里 `image` / `video` / `audio` 的 `path` 指向的媒体，相对入口清单所在目录解析 |
| `input/manifest.json`（可选） | 声明入口与产物，避免多入口歧义 |

阶段内后续调用用请求体 `inputFile` 指向**同一 `uniqueId`** 上一次运行的主清单
（`audio_standardization/runNNN/06_audio_index.jsonl`）。

单独上传一个 `.jsonl` 也可，但**只适合不引用媒体的轻量输入**：`_store_single` 会直接拒绝带媒体引用的
单文件（避免拿到一份必然 `media_missing` 的清单）；`audio` 为 `null` 的记录标 `not_applicable` 后正常输出。

### 1.2 输出（单次运行目录 `output/<uniqueId>/audio_standardization/runNNN/`）

| 产物 | 说明 |
| --- | --- |
| `06_audio_index.jsonl` | **主清单**，下一节点的 `inputFile` 目标。逐条保留上游记录所有字段，只做增量更新 |
| `06_rejects.jsonl` | 拒绝清单：缺失 `sample_id`、重复样本、坏 JSON、媒体缺失/损坏、时长超限等，逐条可解释 |
| `06_audio/<sample_id>.wav` | 标准化后的 PCM WAV（目标采样率/声道/位宽） |
| `media/<sample_id>/<原文件名>` | 从上游搬进来的 `image` / `video` / `audio` 文件（含被 `original_path` 引用的源音频） |
| `manifest.json` | 本次运行产物清单（`crossmodal-manifest-v1`：`entrypoint` + `artifacts`） |
| `run.log` | 本次运行的动作日志（旁路排障文件，**不属于**交付产物） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘构造与阶段归档调用；`utils/lineage.py` 仅保留 `now_iso` 等时间辅助函数、`utils/mty_compliance.py` 保留存档，`utils/transport.py` 仅保留文件内容对象打包与入口定位等公共函数）；原血缘中的统计/配置快照等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

主清单里被本算子改动的字段（其余原样透传）：

```json
{
  "sample_id": "as_001",
  "audio": {
    "path": "06_audio/as_001.wav",
    "sample_rate": 16000,
    "channels": 1,
    "duration": 1.0,
    "segments": [],
    "original_path": "media/as_001/aud_sine_22050_stereo_1s.wav",
    "pcm_bits": 16,
    "clipped": false,
    "clip_start": null,
    "clip_end": null,
    "decoder_backend": "wave",
    "source_sample_rate": 22050,
    "source_channels": 2,
    "source_duration": 1.0,
    "frames": 16000,
    "bytes": 32044,
    "rms": 0.198
  },
  "processing": {
    "05": {"status": 1, "process_status": "success", "reason": null, "metrics": {"frames_saved": 0}},
    "06": {"status": 1, "process_status": "success", "reason": null,
           "metrics": {"source_sample_rate": 22050, "target_sample_rate": 16000,
                       "frames": 16000, "decoder_backend": "wave", "clipped": false}}
  }
}
```

- 媒体路径一律写成**本次运行目录内的相对路径**（`media/...`、`06_audio/...`），
  同时用 `original_path` 记录上游原值，便于回溯；
- `processing["06"]` 只写本算子这一段（int `status` + `process_status` / `reason` / `metrics`），
  `process_status` 取值 `success` / `not_applicable`，不覆盖上游写入的 `processing["05"]` 等块；
- `audio.duration` 是**从写出的 WAV 复读得到的真实时长**（`wave.getnframes() / framerate`），
  不是上游声明值；`rms` 用于佐证输出不是静音（不是任何模型分数）。

### 1.3 拒绝清单条目

```json
{
  "operator": "audio_standardization",
  "reason": "media_unreadable",
  "message": "媒体文件缺失",
  "sample_id": "as_005",
  "line": 6,
  "record": {"...": "完整原始记录"},
  "source_reference": ["media/missing_audio.wav"],
  "media_errors": [{"path": "media/missing_audio.wav", "reason": "media_unreadable"}]
}
```

全部拒绝原因：

| reason | 含义 |
| --- | --- |
| `invalid_json` | 该行不是合法 JSON（带 `line` 行号，无 `record`） |
| `invalid_record` | 合法 JSON 但不是对象；或 `sample_id` 缺失/为空；或 `audio` 非 `null` 却没有可用 `path` |
| `duplicate_sample_id` | 同一次运行内 `sample_id` 重复，只保留首次出现 |
| `unsupported_media_path` | 媒体路径是绝对路径、含 `..`、反斜杠、`~`、URL 等，拒绝搬运 |
| `media_unreadable` | 媒体文件不存在，或存在但无法解码（截断、伪容器、0 字节） |
| `invalid_audio_metadata` | 能打开但参数非法：0 帧空音频、非法采样率/声道/位宽、非有限时长 |
| `invalid_clip_range` | 裁剪区间反转，或 `clip_end` 超出原音频时长，或裁剪后区间为空 |
| `resource_limit` | 时长超过 `max_duration_seconds`（含裁剪后），**不截断、直接拒绝** |
| `dependency_missing` | 该输入需要 ffmpeg/ffprobe，但部署环境没有配置可用的可执行文件 |
| `unsupported_format` | 有 ffmpeg 但容器/位宽仍不受支持 |
| `audio_save_failed` | 写出的 WAV 复读校验不通过（采样率/声道/位宽/帧数对不上） |
| `audio_processing_failed` | 其余未细分的音频处理失败（保留原因，不吞错） |

> `audio` 为空（`null`）**不是拒绝**，而是标记 `not_applicable` 正常输出，这样纯文本/仅图像样本
> 在本阶段不会被误杀；本算子**不会**为它生成假的 `06_audio/*.wav`。

---

## 2. 三个接口

统一响应信封，`code=0` 表示本算子这一环成功（业务上的 partial 也算 0），`code=1` 表示本轮执行失败：

```json
{"code": 0, "msg": "success", "data": { ... }}
```

`/uploadFile`、`/removeFile` 失败时 `data` 为 `{}`；`/predict` 运行失败时 `data` 为
`{"status": "failed"}`（参数错误为 HTTP 422，`data` 为 `{}`）。`msg` 形如 `"<detail>"`。

### 2.1 `POST /uploadFile?uniqueId=<id>`：上传阶段首素材

- `multipart/form-data`，字段名 **`file`**，内容可以是 zip 包，也可以是单个 `.jsonl` / `.json`；
- zip 必须能唯一定位入口（含 `demo.jsonl` / `samples.jsonl`，或有 `manifest.json`），否则 400；
- zip 按白名单解压，拒绝目录穿越、绝对路径、软链接、大小写碰撞的成员名与超限额；
- 提交是**原子的**：解压到 `<root>/.tmp/upload_*/`，校验通过后才替换 `input/`，失败不留半成品；
- 返回 `data.entrypoint` 回显定位到的入口。

```json
{"code": 0, "msg": "success", "data": {"entrypoint": "demo.jsonl"}}
```

### 2.2 `POST /predict`：执行一轮

```json
{
  "uniqueId": "docS2_1",
  "isLastOperator": false,
  "inputFile": "C:/.../output/docS2_1/audio_standardization/run001/06_audio_index.jsonl",
  "config": {"sample_rate": 16000, "channels": 1, "pcm_bits": 16}
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `uniqueId` | 是 | 任务 ID，必须是单个合法目录名（拒绝空串、`..`、含 `/` `\`、Windows 保留名、前后空白） |
| `isLastOperator` | 是 | **严格布尔**，只接受 `true`/`false`（`"false"`/`1` 一律 422 拒绝） |
| `inputFile` | 否 | 阶段内后续调用的输入；必须**在本任务目录内**，否则 422。阶段首不传 |
| `config` | 否 | 覆盖业务参数；未知键、数字字符串、非法取值一律 400/422 |

**统一返回（v4.2：中间与阶段末响应同构）**：

`isLastOperator` 仍必传，用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回
`outputFile`+`result`，不再用 `false` 只回血缘对象。

```json
{"code": 0, "msg": "success",
 "data": {
   "operator": "normalize_audio",
   "status": "success",
   "outputFile": {"format": "crossmodal-files-v1",
                  "entrypoint": "06_audio_index.jsonl",
                  "files": [{"path": "06_audio_index.jsonl", "encoding": "utf-8", "content": "..."},
                            {"path": "06_audio/as_001.wav", "encoding": "base64", "content": "UklGR..."},
                            {"path": "manifest.json", "encoding": "utf-8", "content": "..."}]},
   "result": {"status": 1, "process_status": "success",
              "statistics": {"input": 2, "output": 2, "audio_standardized": 2,
                             "conservation_ok": true},
              "entrypoint": "06_audio_index.jsonl",
              "input": "input/demo.jsonl"}}}
```

- `outputFile` 是**文件内容对象而不是路径**：把 `files` 落盘到新目录后，`entrypoint` 指向的文件即可直接读；
- 内容对象必须含入口、`06_rejects.jsonl`、全部被引用的媒体（`media/**`、`06_audio/**`）与 `manifest.json`，
  少任何一个都会在打包阶段报 `code=1`，不会返回缺媒体的“成功”；
- `data.result` 是本 run 的业务摘要：`status` 为 int（1 处理成功）+ `process_status`
  （`success`/`partial`），`statistics` 字段见 §6，`entrypoint` 为主清单名，`input` 为任务相对路径；
- `isLastOperator` 只影响调用方对阶段末的语义标记，不改变本算子的响应结构。

### 2.2.1 形态一（单文件流）`POST /predict?detection=normalize_audio`

- `multipart/form-data`，表单字段 **`file`** 必传：单个音频（`wav`/`mp3`/`flac`，自动合成单记录 JSONL），
  或 JSON/JSONL/zip；`uniqueId` 可放在 query 或表单，缺省自动生成；
- 表单白名单标量 `enabled` / `sample_rate` / `channels` / `pcm_bits`，未知字段忽略；
  形态一默认 `enabled=true`（表单 `enabled=false` 可显式关闭）；
- `detection` 别名：`m07` / `07` / `7` / `normalize_audio` / `cma_normalize_audio`（大小写不敏感）；
- **成功**返回标准化后的 WAV **文件流**（`FileResponse`，`audio/wav`），不是 JSON 信封；
- **失败**返回 `{"status": 0, "msg": "<原因>"}`（如缺少 `file`、无标准化音频产出）；成功时不再回 int `status` 字段。

### 2.3 `POST /removeFile?uniqueId=<id>`：清理任务

只删除 `<output_root>/<uniqueId>/`，不触碰其它任务；任务不存在也返回成功（幂等）。

```json
{"code": 0, "msg": "success", "data": {}}
```

### 2.4 不实现的接口

`/downloadFile` **不在**本算子的三接口范围内，固定返回 `404 {"code":1,"msg":"Not Found","data":{}}`，
不返回假成功，也不返回本地路径充数。

---

## 3. 配置

配置文件 `configs/config.yaml`；默认取值与取值域：

| 键 | 默认 | 取值域 | 可否被请求覆盖 |
| --- | --- | --- | --- |
| `operator.name` / `operator.version` | `audio_standardization` / `v1.0.0` | 非空 | 否 |
| `stage_id` | `S2` | 非空 | 否 |
| `sample_rate` | `16000` | 固定 `16000`（Hz） | 是（仅允许 16000） |
| `channels` | `1` | 固定 `1`（单声道） | 是（仅允许 1） |
| `pcm_bits` | `16` | 固定 `16` | 是（仅允许 16） |
| `max_duration_seconds` | `60` | `(0, 3600]` 有限值 | 否（部署固定/锁定） |
| `ffmpeg_path` | `ffmpeg` | 非空、无 NUL | 否（部署固定/锁定） |
| `ffprobe_path` | `ffprobe` | 非空、无 NUL | 否（部署固定/锁定） |
| `process_timeout_seconds` | `30` | 有限值 `(0, 300]` | 否（部署固定/锁定） |
| `clip_start` / `clip_end` | `null` | 不开放请求下发（部署固定/锁定） | 否 |
| 输出根 | `output`（可被环境变量 `OUTPUT_DIR` 或 `--output-root` 提供） | 非空 | 否 |
| 服务端限额 `MAX_UPLOAD_BYTES` / `MAX_FILES` / `MAX_UNPACKED_BYTES` / `MAX_RESPONSE_BYTES` | `67108864` / `1000` / `67108864` / `67108864` | 定义在 `app.py`，**不接受请求放宽** | 否 |

规则：

- **部署配置不可被请求改写**：`config` 里出现 `operator` / `stage_id` 或任何未知键，直接报错，
  不静默忽略（`stage_id` 属于部署身份，被改写会使阶段边界不可追溯）；
- **请求参数强类型**：整数字段拒绝 `"16000"` 这类数字字符串与非整数，浮点字段拒绝字符串与布尔值；
- **算法不可被隐式替换**：`sample_rate` 只接受 `16000`、`channels` 只接受 `1`、`pcm_bits` 只接受 `16`，其他值直接报错而不是“就近取一个”；
- **超限不截断**：超过 `max_duration_seconds` 的记录按 `resource_limit` 拒绝，
  宁可少一条也不产出“截断后的完整音频”。

处理规则：

1. **解码后端**：`.wav` 先走标准库 `wave`（8/16/24/32 位整数 PCM）；打不开（浮点/压缩 WAV）或非 WAV
   则回退 `ffprobe` 探测 + `ffmpeg` 解为 `f32le`。缺后端时按 `dependency_missing` 拒绝该条并留痕；
2. **重采样**：目标固定 16000 Hz，`scipy.signal.resample_poly`（多相滤波，带抗混叠）；源采样率等于目标时不做滤波，直接复制；
3. **混声道**：多声道取算术平均转单声道；目标固定单声道，不再复制第二声道；
4. **量化**：浮点样本量化到 16 bit（`±32768` 满量程），写出 PCM16 WAV；
5. **裁剪**：当前不开放请求裁剪（`clip_start`/`clip_end` 锁定），输出完整音频；
6. **落盘校验**：写出的 WAV 立即用 `wave` 复读，采样率/声道/位宽/帧数任一不一致即 `audio_save_failed`。

---

## 4. 目录结构

```
06_音频标准化/
├── app.py                     # FastAPI 三接口（8106）
├── inference.py               # run / load_config / CLI
├── map.json                   # 算子映射（id/port/entrypoint）
├── requirements.txt
├── README.md                  # 本文件
├── OPERATOR_MANUAL.md               # 交付说明书（给平台/运维）
├── HANDOFF.md                 # 交接记录（真实验收输出）
├── DEVELOPMENT_PLAN.md        # 开发计划
├── TECHNICAL_DESIGN.md        # 技术设计
├── configs/config.yaml        # 配置
├── dataset/
│   ├── DATA_SCHEMA.md         # 数据契约（跨算子公共）
│   └── sample/                # 可重建样例素材（make_sample.py 生成）
├── packaging/
│   ├── Dockerfile             # 镜像构建
│   └── prepare_package.py     # 生成 packaging/audio_standardization/ 代码包
├── tests/                     # pytest：test_operator / test_api / test_transport
├── utils/                     # io / schemas / transport / lineage（v4.2 停用，存档） / logger / audio_processing
└── output/                    # 运行产物（<uniqueId>/audio_standardization/runNNN/）
```

---

## 5. 运行方式

依赖：

```powershell
python -m pip install -r requirements.txt
```

### 5.1 HTTP 服务（推荐，供平台调用）

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8106
```

```powershell
# 1) 上传上游素材包（zip 或单个 jsonl）
curl.exe -X POST "http://127.0.0.1:8106/uploadFile?uniqueId=docS2_1" -F "file=@sample.zip"

# 2) 阶段内调用（isLastOperator=false，响应与阶段末同构）
curl.exe -X POST "http://127.0.0.1:8106/predict" -H "content-type: application/json" `
  -d "{\"uniqueId\":\"docS2_1\",\"isLastOperator\":false}"

# 2') 阶段末调用（isLastOperator=true，同样返回 outputFile + result）
curl.exe -X POST "http://127.0.0.1:8106/predict" -H "content-type: application/json" `
  -d "{\"uniqueId\":\"docS2_1\",\"isLastOperator\":true}"

# 2'') 形态一单文件流（成功直接下载标准化 WAV）
curl.exe -X POST "http://127.0.0.1:8106/predict?detection=normalize_audio" -F "file=@clip.wav" -o normalized.wav

# 3) 清理任务
curl.exe -X POST "http://127.0.0.1:8106/removeFile?uniqueId=docS2_1"
```

服务端配置来源：环境变量 `OPERATOR_CONFIG` 指向的 yaml，未设置时用 `configs/config.yaml`；
输出根由 `OUTPUT_DIR` 控制。

### 5.2 命令行（本机调试/离线批处理）

```powershell
# 重建样例素材（真实 wav/jpg/mp4）
python dataset/sample/make_sample.py

# 跑样例数据
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_06 --output-root output/smoke

# 覆盖业务参数（可重复）/ 阶段末标记 / 精简摘要
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_06 --output-root output/smoke `
  --override sample_rate=16000 --override channels=1 --override pcm_bits=16 `
  --is-last --quiet
```

- 参数：`--input`、`--unique-id`、`--config`、`--output-root`、`--is-last`、
  `--override KEY=VALUE`（可重复）、`--overrides '<JSON>'`、`--quiet`；
- 日志只写 **stderr** 与运行目录的 `run.log`；**stdout 是一份干净的 JSON 信封**，可直接被脚本解析；
- 退出码：`0` = success/partial（业务处理完成），`1` = failed 或参数/配置错误；执行失败**绝不返回 0**。

### 5.3 打包镜像

```powershell
cd packaging
python prepare_package.py      # 生成 packaging/audio_standardization/ 代码包
docker build -t crossmodal-audio-standardization:v1.0.0 .
docker run --rm -p 8106:8106 -e OUTPUT_DIR=/data/output -v /host/output:/data/output `
  crossmodal-audio-standardization:v1.0.0
```

---

## 6. 统计与人工复核条件

每次运行的 `statistics` 都会带回（真值示例见 `HANDOFF.md`）：

`input`、`input_lines`、`blank_lines`、`valid_records`、`bad_lines`、`output`、`rejected`、
`not_applicable`、`success`、`duplicates`、`media_copied`、`media_bytes`、`media_missing`、
`media_generated`、`audio_bytes`、`audio_standardized`、`audio_resampled`、`audio_clipped`、
`duration_seconds_total`、`decoder_backend`、`decoder_backends`、`ffmpeg_fallback`、
`ffmpeg_encoding_delay_note`、`silent_output`、`empty_input`、`conservation_ok`、
`artifact_files`、`reject_reasons`、`requested_sample_rate`、`requested_channels`、
`requested_pcm_bits`、`max_duration_seconds`、`clip_start`、`clip_end`。

**守恒关系（硬约束）**：`input == output + rejected`，其中

- `input` = 非空行数（`input_lines = input + blank_lines`）；
- `rejected` = 坏行 + 记录级拒绝，每条非空行必须恰好有一个去向；
- 不守恒时本轮直接判失败并抛 `InputError`，主清单不提交——宁可失败也不给一份账对不上的清单。

**媒体账本**：`media_copied` 是搬运进来的上游原始媒体数（`media_bytes` 为其字节数），
`media_generated` 是本算子生成的 WAV 数（`audio_bytes` 为其字节数）；
`artifact_files = 2 (主清单 + 异常清单) + media_copied + media_generated`。

运行状态：

| status | 触发条件 | CLI 退出码 |
| --- | --- | --- |
| `success` | 无拒绝、无坏行（含空输入） | 0 |
| `partial` | 处理完成但有拒绝/坏行 | 0 |
| `failed` | 全部音频记录都因解码后端缺失而失败（`dependency_missing`） | 1 |

**出现以下任一情况需要人工复核**：

1. `status != "success"`；
2. `rejected > 0`：逐条看 `06_rejects.jsonl` 的 `reason`；
3. `bad_lines > 0`：上游清单本身有脏行，应回溯上游；
4. `decoder_backend == "unavailable"` 但 `input > 0`：说明没有任何音频被处理过；
5. `ffmpeg_fallback > 0`：非 PCM WAV 走了 ffmpeg，存在编码延迟导致的一帧级偏差（`ffmpeg_encoding_delay_note`）；
6. `silent_output > 0`：标准化后 RMS 为 0，源音频可能是静音或量化异常；
7. `media_missing > 0`：上游清单引用了没上传的媒体。

---

## 7. 测试

```powershell
python -m pytest tests/ -q
```

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_operator.py` | 纯函数（重采样/混声道/量化/RMS）、真实 WAV 解码与标准化、8/16/24/32 位与 1/2 声道、裁剪与越界、空音频、`resource_limit`、损坏文件显式降级、配置与请求覆盖校验、`run()` 统计守恒/异常清单/媒体无孤儿、CLI 退出码与摘要形状 |
| `tests/test_transport.py` | `uniqueId` 与路径安全、任务锁互斥与重入、`removeFile` 隔离、zip 安全解压（穿越/绝对路径/软链接/限额/非空目标）、manifest 与入口定位（含点目录与隐藏文件）、文件内容对象打包/还原/坏载荷 |
| `tests/test_api.py` | 三接口端到端（上传→中间→阶段末→删除）、`isLastOperator` 两种取值的同构响应与 data 键集合、形态一单文件流（WAV）与 detection 别名、统一失败信封、严格 bool、跨任务 `inputFile` 拒绝、上传安全、配置拒绝、负载还原后媒体可解码、环境路径不外泄 |

---

## 8. 外部依赖与假设

- **NumPy / SciPy**：重采样与 PCM 运算的必需依赖，缺失则无法工作（不降级为“原样输出”）；
- **ffmpeg / ffprobe 可选**：只用于非 WAV 与压缩格式；缺失时按 `dependency_missing` 逐条拒绝并留痕，
  PCM WAV 主路径完全不受影响；
- 媒体路径**只接受相对路径**（相对入口清单所在目录），绝对路径/`..`/反斜杠/`~`/URL 一律拒绝；
- 记录要求 `sample_id` 唯一且非空，本算子不做 ID 生成或改写；
- 本算子**不修改**上游清单里除 `audio`（标准化结果）与 `image`/`video`/`audio` 的
  `path`+`original_path`（搬运重写）以及 `processing["06"]` 之外的内容；
- v4.2 起不再读取 `lineage_history.json` 等历史血缘文件（该输入已随血缘输出一并停用）。
