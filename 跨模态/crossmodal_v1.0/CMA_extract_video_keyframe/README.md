# 算子 5：视频关键帧与片段处理（video_keyframes）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传；本算子成功返回帧 ZIP 文件流，失败返回 JSON `{"status":0,"msg":"..."}`，详见 §2.2.1）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

把上游（S2）清单里 `video` 字段引用的视频，按配置采样率抽成 JPEG 关键帧，并把清单中出现的
`image` / `video` / `audio` 媒体文件按样本搬运到本次运行的目录下；输出一份**保留上游原始记录结构**
的主清单 `05_frame_index.jsonl` 与可解释的拒绝清单 `05_rejects.jsonl`。

- 映射 ID：`video_keyframes`，服务端口 **8105**（见 `map.json`）
- 正式算子编号：5
- 当前位置：`G:\HYQX\算子\跨模态\demo\05_视频关键帧与片段处理\`

本算子**不做**：语音识别、文本改写、图像超分、模型推理（不加载任何权重）、
视频转码、片段剪辑、媒体去重（重复样本按 `sample_id` 拒绝并留痕）。

---

## 1. 输入与输出

### 1.1 输入

| 来源 | 说明 |
| --- | --- |
| `input/demo.jsonl`（推荐名，也接受 `samples.jsonl`） | 上游 04 输出的 `04_image_index.jsonl` 改名/原名上传；每行一个 JSON 对象 |
| `input/media/...` | 清单里 `image` / `video` / `audio` 的 `path` 指向的媒体文件，相对入口清单所在目录解析 |
| `input/manifest.json`（可选） | 声明入口与产物，避免多入口歧义 |

单独上传一个 `.jsonl` 也可以，但**只适合不引用媒体的轻量输入**：此时清单里的
`image` / `video` / `audio` 引用的文件不存在，会被逐条拒绝为 `media_missing`；
`video` 为 `null` / `""` 的记录不抽帧，标 `not_applicable` 后正常输出。

### 1.2 输出（单次运行目录 `output/<uniqueId>/video_keyframes/runNNN/`）

| 产物 | 说明 |
| --- | --- |
| `05_frame_index.jsonl` | **主清单**，下一节点的 `inputFile` 目标。逐条保留上游记录的所有字段，只做增量更新 |
| `05_rejects.jsonl` | 拒绝清单：缺失 `sample_id`、重复样本、坏 JSON、媒体缺失/不可读等，逐条可解释 |
| `05_frames/<sample_id>/frame_<帧号6位>_<毫秒9位>.jpg` | 抽出的关键帧 |
| `media/<sample_id>/<原文件名>` | 从上游搬进来的 `image` / `video` / `audio` 文件 |
| `manifest.json` | 本次运行产物清单（`entrypoint` + `artifacts`），供打包与完整性校验 |
| `run.log` | 本次运行的动作日志（旁路排障文件，**不属于**交付产物） |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘构造与阶段归档调用；`utils/lineage.py` 仅保留 `now_iso` 等时间辅助函数、`utils/mty_compliance.py` 保留存档，`utils/transport.py` 仅保留文件内容对象打包与入口定位等公共函数）；原血缘中的统计/配置快照等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

> 运行目录分两类文件：**入库产物**（上面 `manifest.json` 里 `artifacts` 列出的，含主清单、
> 拒绝清单、`media/`、`05_frames/`）与**旁路文件**（`manifest.json`、`run.log`）。被拒绝样本的媒体/帧会被**回收**，
> 因此运行目录里不会留下既不在主清单、也不在 manifest 里的死文件。

主清单记录里被本算子改动的字段（其余原样透传）：

```json
{
  "sample_id": "vk_001",
  "video": {
    "path": "media/vk_001/vid_cfr_2s.mp4",
    "original_path": "media/vid_cfr_2s.mp4",
    "duration": 2.0, "fps": 10.0, "frame_count": 20,
    "width": 100, "height": 80,
    "frames": [
      {"path": "05_frames/vk_001/frame_000000_000000000.jpg",
       "timestamp": 0.0, "frame_index": 0, "width": 100, "height": 80},
      {"path": "05_frames/vk_001/frame_000010_000001000.jpg",
       "timestamp": 1.0, "frame_index": 10, "width": 100, "height": 80}
    ],
    "extraction": {
      "operator": "video_keyframes", "operator_version": "v1.0.0", "run_id": "run001",
      "sample_fps": 1.0, "planned_frames": 2, "decoded_frames": 2,
      "timestamp_source": "cfr_index_over_fps", "vfr_detected": false,
      "max_pts_deviation": 0.0,
      "probe": {"backend": "unavailable", "reason": "ffprobe_not_found"},
      "warnings": []
    }
  },
  "processing": {
    "01": {"...": "上游块原样保留"},
    "04": {"...": "上游块原样保留"},
    "05": {"status": 1, "process_status": "success", "reason": null,
           "metrics": {"frames": 2, "planned_frames": 2, "decoded_frames": 2,
                       "sample_fps": 1.0, "duration": 2.0, "fps": 10.0,
                       "timestamp_source": "cfr_index_over_fps",
                       "vfr_detected": false, "crop_applied": false}}
  }
}
```

- 媒体路径一律写成**本次运行目录内的相对路径**（`media/...`、`05_frames/...`），
  同时保留 `original_path` 记录上游原值，便于回溯；
- `processing["05"]` 只写本算子这一段（int `status` + `process_status` / `reason` / `metrics`），
  `process_status` 取值 `success` / `not_applicable`，不覆盖上游写入的 `processing["04"]` 等块；
  拒绝记录只进 `05_rejects.jsonl`，不写 `processing`；
- 抽帧细节集中在 `video.extraction`：`timestamp_source` 为 `cfr_index_over_fps`
  （时间戳 = 帧号 / fps）或 `decoded_pts`（按解码 PTS 取时），
  `probe.backend` 反映 VFR 探测后端（`ffprobe` / `opencv` / `unavailable`），
  `warnings` 记录抽帧过程中的非致命提示。

### 1.3 拒绝清单条目

```json
{"sample_id": "vk_005", "line": 5, "reason": "media_missing",
 "source_reference": {"video": "media/missing_video.mp4"},
 "evidence": {"field": "video"}}
```

全部拒绝原因（`inference.REJECT_REASON_NAMES`）：

| reason | 含义 |
| --- | --- |
| `invalid_json` | 该行不是合法 JSON（带行号） |
| `invalid_record` | 合法 JSON 但不是对象（如数组/标量） |
| `missing_sample_id` | 缺少或空的 `sample_id`，无法定位样本 |
| `duplicate_sample_id` | 同一次运行内 `sample_id` 重复，只保留首次出现 |
| `unsupported_media_path` | 媒体路径是绝对路径、含 `..`、反斜杠、URL 等，拒绝 |
| `invalid_media_field` | `image`/`video`/`audio` 字段类型非法（如数字） |
| `media_missing` | 清单声明的媒体文件不存在 |
| `media_unreadable` | 文件存在但无法解码（截断、伪容器、0 字节） |
| `invalid_video_metadata` | 能打开但拿不到有效 fps/帧数/时长 |
| `unsupported_vfr` | 判定为可变帧率且策略为 `reject` |
| `resource_limit` | 超 `max_duration_seconds`（或采样步进触顶），直接拒绝；`max_frames` 超限则只保留前 N 帧 |
| `frame_save_failed` | 关键帧写盘失败 |
| `internal_error` | 未预期异常（保留异常类型名，不吞错） |

> `video` 为空（`null` / `""`）**不是拒绝**，而是标记 `not_applicable` 正常输出，
> 这样纯文本/仅图像样本在本阶段不会被误杀。

---

## 2. 三个接口

统一响应信封，`code=0` 表示本算子这一环成功（业务上的 partial 也算 0），
`code=1` 表示本轮执行失败：

```json
{"code": 0, "msg": "success", "data": { ... }}
```

`/uploadFile`、`/removeFile` 失败时 `data` 为 `{}`；`/predict` 运行失败时 `data` 为
`{"status": "failed"}`，参数/配置等请求错误（HTTP 422）`data` 为 `{}`。`msg` 形如
`"<kind>: <detail>"`，其中 `<kind>` 取值
`invalid_request` / `invalid_unique_id` / `invalid_config` / `input_not_found` /
`invalid_input_file` / `invalid_upload` / `task_busy` / `upload_failed` /
`output_pack_failed` / `media_verify_failed` / `failed` / `not_found` / `internal_error`。

### 2.1 `POST /uploadFile?uniqueId=<id>`：上传阶段首素材

- `multipart/form-data`，字段名 **`file`**，内容可以是 zip 包，也可以是单个 `.jsonl`；
- zip 必须能唯一定位入口（含 `demo.jsonl` / `samples.jsonl`，或有 `manifest.json`），
  否则报 `invalid_upload`；zip 按白名单解压，拒绝目录穿越、软链接、大小写碰撞的成员名；
- 提交是**原子的**：解压到临时目录、校验通过后才替换 `input/`，失败不留半成品；
- 返回 `data.entrypoint` 回显定位到的入口，便于确认上游包被正确识别。

```json
{"code": 0, "msg": "success", "data": {"entrypoint": "demo.jsonl"}}
```

### 2.2 `POST /predict`：执行一轮

请求体（`Content-Type: application/json`）：

```json
{
  "uniqueId": "docS2_1",
  "isLastOperator": false,
  "inputFile": "C:/.../output/docS2_1/input/demo.jsonl",
  "config": {"sample_fps": 2}
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `uniqueId` | 是 | 任务 ID，必须是单个合法目录名（拒绝空串、`..`、含 `/` `\`、Windows 保留名） |
| `isLastOperator` | 是 | **严格布尔**，只接受 `true`/`false`（`"false"`/`1` 一律 422 拒绝） |
| `inputFile` | 否 | 阶段内后续调用的输入；必须在**本任务目录内**，否则 422。阶段首不传 |
| `config` | 否 | 覆盖业务参数，未知键/非法取值一律 422 |

**统一返回（v4.2：中间与阶段末响应同构）**：

`isLastOperator` 仍必传，用于标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回
`outputFile`+`result`，不再用 `false` 只回血缘对象。

```json
{"code": 0, "msg": "success: 有效 2 条，抽帧 2 帧，不适用 0 条",
 "data": {
   "operator": "extract_video_keyframe",
   "status": "success",
   "outputFile": {"format": "crossmodal-files-v1",
                  "entrypoint": "05_frame_index.jsonl",
                  "files": [{"path": "05_frame_index.jsonl", "encoding": "utf-8", "content": "..."},
                            {"path": "05_frames/vk_001/frame_000000_000000000.jpg",
                             "encoding": "base64", "content": "/9j/4AAQ..."}]},
   "response_mode": "inline",
   "result": {"status": 1, "process_status": "success",
              "statistics": {"input": 2, "output": 2, "frames_saved": 2, "conservation_ok": true},
              "entrypoint": "05_frame_index.jsonl",
              "input": "input/demo.jsonl"}}}
```

- `response_mode` 标明产物交付方式：`inline` = `outputFile` 是**文件内容对象**；
  `path-reference` = 产物超过 `server.max_response_bytes`，本次不内联，改为返回
  `<runDir>_form2.zip` 的路径引用（附带 `response_fallback=output_too_large`、
  `response_limit_bytes`），业务状态仍是 `success`（v1.1.1 起不再整单判失败）；
- `outputFile` 是**文件内容对象而不是路径**：把 `files` 落盘到新目录后，
  `entrypoint` 指向的文件即可直接读；`manifest.json` 不入 `files`（它只是本算子的包装物）；
- 内容对象里的媒体引用会在返回前**逐条校验**（`verify_media_references`），
  引用不齐时宁可返回 `code=1` 也不返回缺媒体的“成功”；
- `data.result` 是本 run 的业务摘要：`status` 为 int（1 处理成功）+ `process_status`
  （`success`/`partial`），`statistics` 字段见 §6，`entrypoint` 为主清单名，`input` 为任务相对路径；
- `isLastOperator` 只影响调用方对阶段末的语义标记，不改变本算子的响应结构。

**超限回退（v1.1.1）**：内联产物（base64 后）超过 `server.max_response_bytes`
（默认 256 MiB）时，`/predict` 不报错、不截断，而是把本次 run 目录打成
`<runDir>_form2.zip` 返回形态一同样的路径引用：

```json
{"response_mode": "path-reference", "response_fallback": "output_too_large",
 "response_limit_bytes": 268435456,
 "outputFile": {"format": "crossmodal-files-v1", "mode": "path-reference",
                "runDir": "...\\video_keyframes\\run001", "entrypoint": "05_frame_index.jsonl",
                "fileCount": 373, "totalBytes": 67150389,
                "zip": "...\\video_keyframes\\run001_form2.zip", "zipBytes": 61664609}}
```

打包过程逐文件累计大小、**在读取大文件之前**就判定超限，不会为了一次注定失败的
内联把几百 MB 帧序列读进内存；随后写 ZIP 的耗时与占用按产物实际大小计。

### 2.2.1 形态一（单文件流）`POST /predict?detection=extract_video_keyframe`

- `multipart/form-data`，表单字段 **`file`** 必传：单个视频（`mp4`/`mov`/`avi`/`mkv`/`m4v`/`webm`/`flv`/`wmv`/`mpeg`/`mpg`/`ts`/`3gp`/`ogv`，自动合成单记录 JSONL），
  或 JSON/JSONL/zip；`uniqueId` 可放在 query 或表单，缺省自动生成；
- 表单白名单标量 `enabled` / `sample_fps` / `max_frames` / `fps`（`fps` 是 `sample_fps` 别名），
  未知字段忽略；形态一默认 `enabled=true`（表单 `enabled=false` 可显式关闭）；
- `detection` 别名：`m06` / `06` / `6` / `extract_video_keyframe` / `cma_extract_video_keyframe`（大小写不敏感）；
- **成功**返回帧 ZIP **文件流**（`FileResponse`，`<uniqueId>_frames.zip`，内含 `05_frame_index.jsonl`
  与 `05_frames/**`），不是 JSON 信封；
- **失败**返回 `{"status": 0, "msg": "<原因>"}`（如缺少 `file`、无任何帧产出）；成功时不再回 int `status` 字段。

### 2.3 `POST /removeFile?uniqueId=<id>`：清理任务

只删除 `<output_root>/<uniqueId>/`，不触碰其它任务。任务不存在也返回成功（幂等）。

```json
{"code": 0, "msg": "success", "data": {}}
```

### 2.4 不实现的接口

`/downloadFile` 不在本算子的三接口范围内，固定返回
`404 {"code": 1, "msg": "not_found: ...", "data": {}}`，不返回假成功。
`POST` 接口改用 `DELETE`/`GET` 也会得到统一信封（405），不会退化成框架默认的
`{"detail": ...}` 结构。

---

## 3. 配置

配置文件 `configs/config.yaml`；默认取值与取值域：

| 键 | 默认 | 取值域 | 可否被请求覆盖 |
| --- | --- | --- | --- |
| `operator.name` / `operator.version` | `video_keyframes` / `v1.1.1` | 非空 | 否 |
| `stage_id` | `S2` | 非空 | 否 |
| `sample_fps` | `1.0` | `[1, 2]` 有限值，即每秒抽几帧 | 是 |
| `max_frames` | `8` | `[1, 8]`；超出只保留按顺序的前 N 帧（截断） | 是 |
| `max_duration_seconds` | `60.0` | `(0, 36000]` | 否（部署固定/锁定） |
| `crop_black_borders` | `false` | 锁定为 `false`（不裁边） | 否（部署固定/锁定） |
| `probe_timeout_seconds` | `15.0` | `(0, 300]` | 否（部署固定/锁定） |
| `unsupported_vfr_policy` | `reject` | `reject` / `use_pts` | 否（部署固定/锁定） |
| `io.output_root` | `output`（可被环境变量 `OUTPUT_DIR` 提供） | 非空 | 否 |
| `server.max_files` | `1000` | `(0, 100000]` | 否 |
| `server.max_unpacked_bytes` | `67108864` | `>0` | 否 |
| `server.max_response_bytes` | `268435456`（256 MiB） | `>0` | 否（v1.1.1 由 64 MiB 提到 256 MiB；超限自动改走 ZIP 路径引用，见 §2.2） |

规则：

- **部署配置不可被请求改写**：`config` 里出现 `operator` / `stage_id` / `io` / `server`，
  或 `max_duration_seconds` / `crop_black_borders` / `probe_timeout_seconds` / `unsupported_vfr_policy`
  等非业务键，直接 422，不静默忽略；
- **仅业务参数可覆盖**：`enabled`、`sample_fps`、`max_frames`（`fps` 是 `sample_fps` 的别名）；
- **算法不可被隐式替换**：`crop_black_borders` 锁定为 `false`（不裁边）；`unsupported_vfr_policy`
  为部署固定项，未知 VFR 策略直接报错；
- **`max_frames` 截断、`max_duration_seconds` 拒绝**：超过 `max_frames` 时只保留按采样顺序的前
  `max_frames` 帧（多出的帧不保留，不判为损坏）；超过 `max_duration_seconds` 的记录按
  `resource_limit` 拒绝，宁可少一条也不产出“截断后的完整帧集”。

抽帧规则（`sample_fps = F`）：

1. 从 `t = 0` 起以 `1/F` 为步长取 `t < duration` 的采样点，映射到解码帧号，去重后按帧号升序；
2. 采样帧数达到 `max_frames` 即停止（多出的帧不保留，不把长视频判为损坏）；时长超过
   `max_duration_seconds` 则直接拒绝该样本；
3. 黑边裁剪为锁定关闭项（`crop_black_borders` 固定 `false`，不裁边）；
4. 帧文件命名 `frame_<帧号6位>_<毫秒9位>.jpg`，JPEG 质量 90。

---

## 4. 目录结构

```
05_视频关键帧与片段处理/
├── app.py                     # FastAPI 三接口（8105）
├── inference.py               # 单次运行主流程 + CLI
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
│   └── prepare_package.py     # 生成 packaging/video_keyframes/ 代码包
├── tests/                     # pytest：test_operator / test_transport / test_api
├── utils/                     # io / schemas / transport / lineage（v4.2 停用，存档） / logger / video_processing
└── output/                    # 运行产物（<uniqueId>/video_keyframes/runNNN/）
```

---

## 5. 运行方式

依赖：

```powershell
python -m pip install -r requirements.txt
```

### 5.1 HTTP 服务（推荐，供平台调用）

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8105
```

```powershell
# 1) 上传上游素材包（zip 或单个 jsonl）
curl.exe -X POST "http://127.0.0.1:8105/uploadFile?uniqueId=docS2_1" -F "file=@sample.zip"

# 2) 阶段内调用（isLastOperator=false，响应与阶段末同构）
curl.exe -X POST "http://127.0.0.1:8105/predict" -H "content-type: application/json" `
  -d "{\"uniqueId\":\"docS2_1\",\"isLastOperator\":false}"

# 2') 阶段末调用（isLastOperator=true，同样返回 outputFile + result）
curl.exe -X POST "http://127.0.0.1:8105/predict" -H "content-type: application/json" `
  -d "{\"uniqueId\":\"docS2_1\",\"isLastOperator\":true}"

# 2'') 形态一单文件流（成功直接下载帧 ZIP）
curl.exe -X POST "http://127.0.0.1:8105/predict?detection=extract_video_keyframe" -F "file=@clip.mp4" -o frames.zip

# 3) 清理任务
curl.exe -X POST "http://127.0.0.1:8105/removeFile?uniqueId=docS2_1"
```

服务端配置来源：环境变量 `VIDEO_KEYFRAMES_CONFIG` 指向的 yaml，
未设置时用 `configs/config.yaml`。

### 5.2 命令行（本机调试/离线批处理）

```powershell
# 重建样例素材（真实 mp4/jpg/wav）
python dataset/sample/make_sample.py

# 跑样例数据
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_05 --output-root output/smoke

# 阶段末标记（--is-last 仅保留为语义标记，输出形状与普通调用一致）
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_05 --output-root output/smoke --is-last

# 覆盖业务参数 / 精简摘要
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_05 --output-root output/smoke `
  --override sample_fps=2 --quiet
```

- 参数：`--input`、`--unique-id`、`--config`、`--output-root`、`--is-last`、
  `--override KEY=VALUE`（可重复）、`--quiet`；
- 日志只写 **stderr**，**stdout 是干净的一行 JSON 信封**，可直接被脚本解析；
- 退出码：`0` = success/partial（业务处理完成），`1` = failed；
  执行失败**绝不返回 0**。

### 5.3 打包镜像

```powershell
cd packaging
python prepare_package.py      # 生成 packaging/video_keyframes/ 代码包
docker build -t crossmodal-video-keyframes:v1.0.0 .
docker run --rm -p 8105:8105 -e OUTPUT_DIR=/data/output -v /host/output:/data/output `
  crossmodal-video-keyframes:v1.0.0
```

镜像内没有 `ffprobe` 也能跑：VFR 预判退回 OpenCV（`statistics.probe_backend=opencv`），
行为与有 `ffprobe` 时一致，只是不读容器级 `avg_frame_rate`。

---

## 6. 统计与人工复核条件

每次运行的 `statistics` 都会带回（真值示例见 `HANDOFF.md`）：

`input`、`input_lines`、`valid_records`、`bad_lines`、`output`、`rejected`、
`not_applicable`、`success`、`duplicates`、`frames_total`、`frames_saved`、
`media_copied`、`media_bytes`、`media_missing`、`vfr_detected`、`cropped`、
`empty_input`、`probe_backend`、`requested_sample_fps`、`max_frames`、
`max_duration_seconds`、`artifact_files`（产物文件数，**含** `manifest.json`）、
`conservation_ok`；失败时额外有 `error`。

**守恒关系（硬约束）**：`output + rejected == valid_records`（坏行不算有效记录，
但会写进拒绝清单并计数 `bad_lines`）。不守恒时本轮直接判 `failed`
（`error=conservation`），主清单不提交——宁可失败也不给一份账对不上的清单。

运行状态：

| status | 触发条件 | CLI 退出码 |
| --- | --- | --- |
| `success` | 无拒绝、无坏行（含空输入） | 0 |
| `partial` | 处理完成但有拒绝/坏行 | 0 |
| `failed` | 输入不可读、全行不可解析、统计不守恒、产出缺失 | 1 |

**出现以下任一情况需要人工复核**：

1. `status != "success"`；
2. `rejected > 0`：逐条看 `05_rejects.jsonl` 的 `reason`；
3. `bad_lines > 0`：上游清单本身有脏行，应回溯上游；
4. `frames_saved != frames_total`：有帧没落盘；
5. `media_missing > 0`：上游清单引用了没上传的媒体；
6. `statistics.error` 非空。

---

## 7. 测试

```powershell
python -m pytest tests/ -q
```

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_operator.py` | 帧选择/黑边检测/ffprobe 解析等纯函数、真实媒体抽帧、`run()` 统计守恒、拒绝原因、空输入、非 UTF-8、不覆盖上一次运行、CLI 退出码 |
| `tests/test_transport.py` | `uniqueId` 与路径安全、任务锁互斥、`removeFile` 隔离、zip 安全解压（穿越/软链接/碰撞/限额）、manifest 与入口定位、文件内容对象打包/恢复/坏载荷 |
| `tests/test_api.py` | 三接口端到端（上传→中间→阶段末→删除）、`isLastOperator` 两种取值的同构响应、形态一单文件流（帧 ZIP）与 detection 别名、统一失败信封（含框架产生的 404/405）、参数与配置拒绝、上传安全、统计与状态、锁冲突、交付件一致性 |

---

## 8. 外部依赖与假设

- **OpenCV（`opencv-python-headless`）**：唯一的视频解码依赖。
  容器内无 `ffmpeg`/`ffprobe` 也能工作（`probe_backend=unavailable/opencv`）；
- `ffprobe` **可选**：有则额外读取 `avg_frame_rate` / `r_frame_rate` 做 VFR 预判；
- 媒体路径**只接受相对路径**（相对入口清单所在目录），绝对路径/`..`/反斜杠/URL 一律拒绝；
- 记录要求 `sample_id` 唯一且非空，本算子不做 ID 生成或改写；
- 本算子**不修改**上游清单里除 `video` / `image` / `audio` 的 `path`+`original_path`
  以及 `processing["05"]` 之外的内容。
