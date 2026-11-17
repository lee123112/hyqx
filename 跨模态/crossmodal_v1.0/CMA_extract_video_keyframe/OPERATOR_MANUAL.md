# 算子 5 说明书：视频关键帧与片段处理

> 配套文件：`README.md`（使用概览）、`dataset/DATA_SCHEMA.md`（数据契约）、`HANDOFF.md`（验收证据）。
> 版本：v1.0.0（demo）；阶段：S2；正式算子编号：5；映射 ID：`video_keyframes`。

## 1. 概述

| 项 | 内容 |
|---|---|
| 职责 | 对上游清单中 `video` 引用的视频按配置采样率抽关键帧；把清单里的 `image`/`video`/`audio` 媒体按样本搬运进本次运行目录；保留上游记录结构做增量更新 |
| 输入 | 同阶段上游主清单 `04_image_index.jsonl`（入口名推荐 `demo.jsonl`，也接受 `samples.jsonl`）+ 清单引用的媒体文件 |
| 输出 | `05_frame_index.jsonl`（主清单）、`05_rejects.jsonl`、`05_frames/<sample_id>/`、`media/<sample_id>/`、`manifest.json` |
| 算法 | CPU 规则：OpenCV 解码 + 索引采样 + JPEG 落盘；**无模型、无需训练、不联网**；黑边裁剪锁定关闭 |
| 依赖 | FastAPI / Uvicorn / Pydantic / PyYAML / python-multipart / OpenCV(headless) / NumPy（必需）；`ffprobe`（可选）；pytest+httpx（仅测试） |
| 端口 | 8105 |

## 2. 部署

### 2.1 直接运行（开发/联调）

```bash
cd G:/HYQX/算子/跨模态/demo/05_视频关键帧与片段处理
python -m pip install -r requirements.txt
python dataset/sample/make_sample.py          # 生成可重建样例素材
export OUTPUT_DIR=/data/output                # PowerShell: $env:OUTPUT_DIR="D:/data/output"
python -m uvicorn app:app --host 0.0.0.0 --port 8105
```

### 2.2 容器

```bash
cd packaging
python prepare_package.py
docker build -t crossmodal-video-keyframes:v1.0.0 .
docker run --rm -p 8105:8105 -v /host/output:/data/output \
  -e OUTPUT_DIR=/data/output crossmodal-video-keyframes:v1.0.0
```

- 同阶段服务须挂载同一共享 `OUTPUT_DIR` 且容器内路径一致；否则由工程侧搬运，不得假定 A 容器路径在 B 容器存在。
- 环境变量：`OUTPUT_DIR`（输出根）、`VIDEO_KEYFRAMES_CONFIG`（自定义配置路径，默认 `configs/config.yaml`）。
- 镜像内**不含 ffmpeg/ffprobe 也能工作**：VFR 预判退回 OpenCV（`statistics.probe_backend=opencv`）。

## 3. 接口

| 方法 | 路径 | 请求 | 成功返回 |
|---|---|---|---|
| POST | `/uploadFile?uniqueId=<id>` | `multipart/form-data`，字段 `file`（zip 或单个 jsonl） | `{"code":0,"msg":"success","data":{"entrypoint":"demo.jsonl"}}` |
| POST | `/predict?operatorId=M06` | JSON `{"uniqueId","isLastOperator","inputFile?","config?"}` | 统一响应（中间/阶段末同构，见下方） |
| POST | `/predict?detection=extract_video_keyframe` | `multipart/form-data`，字段 `file`（单视频 mp4/mov/avi/mkv/m4v/webm/flv/wmv/mpeg/mpg/ts/3gp/ogv 或 JSON/JSONL/zip）+ 可选 `uniqueId`、`enabled`/`sample_fps`/`max_frames`/`fps` | 成功：帧 ZIP 文件流（`<uniqueId>_frames.zip`）；失败：`{"status":0,"msg":"..."}` |
| POST | `/removeFile?uniqueId=<id>` | 无体（幂等） | `{"code":0,"msg":"success","data":{}}` |
| * | `/downloadFile` | — | 契约要求不实现，固定 404 信封 |

`/predict` 请求字段：`uniqueId`（string，必传）、`isLastOperator`（**严格** bool，必传）、`inputFile`（可选，必须在任务根内）、`config`（可选，业务参数对象）。

- **统一响应（v4.2）**：`isLastOperator` 仍必传（阶段末标记），但**中间与阶段末响应同构**，
  均返回 `data{operator,status,outputFile,result}`：`outputFile` 为 `crossmodal-files-v1`
  文件内容对象（含入口与全部媒体/帧，**`manifest.json` 不入包**）；`result` 为业务摘要
  （int `status` + `process_status` + `statistics` + `entrypoint` + `input`），
  不再用 `isLastOperator=false` 只回血缘对象；
- 失败信封统一为 `{"code":1,"msg":"<kind>: <detail>","data":{...}}`：运行失败为
  `data={"status":"failed"}`，参数/配置等请求错误（422）为 `data={}`；
  `partial`（有拒绝但处理完成）仍为 `code=0`；
- 框架自身产生的错误也经全局处理器转成同一信封：未知路径 404、方法不符 405、请求校验 422，
  不会漏出 `{"detail": ...}`。

## 4. 配置说明

见 `configs/config.yaml` 与 README §3。要点：

- 业务参数（`enabled`、`sample_fps`、`max_frames`）可被请求 `config` 覆盖，取值域受限（`sample_fps` 1—2，默认 1.0；`max_frames` 1—8，默认 8，超出只保留前 N 帧）；
- 部署固定/锁定项（`operator`、`stage_id`、`io`、`server`、`max_duration_seconds`、`crop_black_borders`、`probe_timeout_seconds`、`unsupported_vfr_policy`）**不接受 `/predict` 覆盖**：请求里出现即 422/配置错误，防篡改部署身份、放宽限额或替换算法（`crop_black_borders` 固定 `false`，不裁边）；
- 未知字段/枚举、非有限数值 → HTTP 422、CLI 退出码 1。

## 5. 运行与运维

- 新 run 使用递增 `run_id`（`run001`…），**不覆盖**上游或历史 run；
- 上游素材落点 `input/`，上传为**原子提交**（解压到临时目录、校验入口唯一后才替换），失败不留半成品；
- 同一 `uniqueId` 同时只允许一个写操作，锁冲突返回 HTTP 400 `task_busy`；不同 `uniqueId` 可并发；
- `/removeFile` 只删本任务目录，任务不存在也返回 `code=0`（幂等），锁文件放任务目录之外；
- 日志同时写 **stderr** 与运行目录下的 `run.log`（容器内 stderr 由运行时收集），
  只记录动作、样本标识与必要统计，不打印完整输入原文、密钥、token；`run.log` 属旁路排障文件，不入交付产物；
- 被拒绝记录的媒体/帧会被回收：运行目录只会留下主清单与 `manifest.json` 描述的产物；
- 返回前对主清单的媒体引用逐条校验（`verify_media_references`），不齐则返回 `code=1`，不返回缺媒体的“成功”（中间/阶段末调用一致）。

## 6. 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| `invalid_config` | 业务参数非法/未知键 | 按 README §3 取值域修正，勿传 `stage_id` 等部署键 |
| `input_not_found` | 未上传素材且未传 `inputFile` | 先 `/uploadFile` 或传正确 `inputFile` |
| `invalid_input_file` | `inputFile` 越出任务目录 | 使用本任务目录内的路径 |
| `invalid_upload` | 包内无唯一入口 / 目录穿越 / 软链接 / 大小写碰撞 / 超限额 | 补 `manifest.json`，或按错误详情修正素材包 |
| `invalid_encoding`（CLI `error`） | 输入非 UTF-8 | 重新导出为 UTF-8 |
| `empty_input` | 入口清单为空 | 正常成功；检查上游是否产出记录 |
| `error=invalid_input_records` | 全部行不可解析 | 回溯上游清单 |
| `error=conservation` | `output+rejected != valid_records` | 属程序缺陷，请上报并保留 run 目录 |
| `response_fallback=output_too_large` | 内联产物超 `max_response_bytes`（默认 256 MiB），已落盘 `<runDir>_form2.zip` 并返回路径引用；**业务仍为成功** | 按 `outputFile.zip`/`runDir` 取产物；需要内联时上调限额或降低抽帧数/媒体体积 |
| 记录 `reason=media_unreadable` | 媒体损坏/伪容器/0 字节 | 属业务拒绝，检查 `05_rejects.jsonl` 与上游素材 |
| 记录 `reason=resource_limit` | 超 `max_duration_seconds`（或采样步进触顶） | 切片后再送；`max_frames` 超限不会报此错，而是截断 |
| 记录 `reason=unsupported_vfr` | 判定为可变帧率且策略为 `reject` | 改 `unsupported_vfr_policy=use_pts` 或先转 CFR |
| `probe_backend=unavailable/opencv` | 容器无 ffprobe | 正常降级；可装 ffmpeg 提升探测精度 |

## 7. 验收（本目录内）

```bash
python -m pytest tests/ -q
python dataset/sample/make_sample.py
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_05 --output-root output/smoke
python -m json.tool map.json
python -m compileall -q inference.py app.py utils
python -m uvicorn app:app --host 127.0.0.1 --port 8105
```

验收关注：记录/字节/计数一致、`output+rejected == valid_records` 守恒、媒体可恢复可读、`data.result` 统计完整；失败时返回统一失败信封并留 `run.log`。实际结果见 `HANDOFF.md`。

## 8. 边界与已知限制

- 只处理视频抽帧；文本/图像/音频**透传并搬运**，纯文本样本标 `not_applicable`，不做语义对齐；
- 音频仅搬运不转码，本算子不产生语音识别结果；
- `max_frames` 超限**截断**（只保留按采样顺序的前 `max_frames` 帧，多出的帧不保留，不判为损坏）；`max_duration_seconds` 超限仍拒绝，不产出“截断后的完整帧集”；黑边裁剪锁定关闭（`crop_black_borders=false`）；
- 无模型与权重，不做训练/微调，不提供虚假训练命令；
- demo 传输格式与响应限额（默认 256 MiB）为约定值，收敛于 `utils/transport.py` 单点适配；
  超限不判失败，落盘 `<runDir>_form2.zip` 走路径引用（`response_mode=path-reference`）。
