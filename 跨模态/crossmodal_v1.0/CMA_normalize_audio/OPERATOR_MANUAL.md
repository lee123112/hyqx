# 算子 6 说明书：音频标准化

> 配套文件：`README.md`（使用概览）、`dataset/DATA_SCHEMA.md`（数据契约）、`HANDOFF.md`（验收证据）。
> 版本：v1.0.0（demo）；阶段：S2；正式算子编号：6；映射 ID：`audio_standardization`；端口 **8106**。

## 1. 概述

| 项 | 内容 |
|---|---|
| 职责 | 把上游清单中 `audio` 引用的音频统一为指定采样率/声道数/位宽的 PCM WAV，记录真实时长与解码后端；把清单里的 `image`/`video`/`audio` 媒体按样本搬进本次运行目录；保留上游记录结构做增量更新 |
| 输入 | 同阶段上游主清单 `05_frame_index.jsonl`（入口名推荐 `demo.jsonl`，也接受 `samples.jsonl`）+ 清单引用的媒体文件 |
| 输出 | `06_audio_index.jsonl`（主清单）、`06_rejects.jsonl`、`06_audio/<sample_id>.wav`、`media/<sample_id>/`、`manifest.json` |
| 算法 | CPU 规则：标准库 `wave` / `ffmpeg` 解码 → `scipy.signal.resample_poly` 抗混叠重采样 → 混声道 → 定点量化 → `wave` 复读校验；**无模型、无需训练、不联网** |
| 依赖 | FastAPI / Uvicorn / Pydantic / PyYAML / python-multipart / NumPy / SciPy（必需）；系统级 `ffmpeg` + `ffprobe`（可选，仅非 WAV 与压缩格式）；pytest+httpx（仅测试） |
| 资源建议 | 纯 CPU；见 §6 |

## 2. 部署

### 2.1 直接运行（开发/联调）

```bash
cd G:/HYQX/算子/跨模态/demo/06_音频标准化
python -m pip install -r requirements.txt
python dataset/sample/make_sample.py          # 生成可重建样例素材
export OUTPUT_DIR=/data/output                # PowerShell: $env:OUTPUT_DIR="D:/data/output"
python -m uvicorn app:app --host 0.0.0.0 --port 8106
```

### 2.2 容器

```bash
cd packaging
python prepare_package.py
docker build -t crossmodal-audio-standardization:v1.0.0 .
docker run --rm -p 8106:8106 -v /host/output:/data/output \
  -e OUTPUT_DIR=/data/output crossmodal-audio-standardization:v1.0.0
```

- 同阶段服务须挂载同一共享 `OUTPUT_DIR` 且容器内路径一致；否则由工程侧搬运，不得假定 A 容器路径在 B 容器存在；
- 环境变量：`OUTPUT_DIR`（输出根）、`OPERATOR_CONFIG`（自定义配置路径，默认 `configs/config.yaml`）；
- 镜像内**不含 ffmpeg** 也能工作：PCM WAV 主路径完全不受影响，非 WAV 输入按 `dependency_missing` 逐条拒绝并留痕。
  需要处理 MP3/FLAC/AIFF 时取消 `packaging/Dockerfile` 中 `apt-get install ffmpeg` 的注释后重建镜像。

## 3. 接口

| 方法 | 路径 | 请求 | 成功返回 |
|---|---|---|---|
| POST | `/uploadFile?uniqueId=<id>` | `multipart/form-data`，字段 `file`（zip 或单个 jsonl/json） | `{"code":0,"msg":"success","data":{"entrypoint":"demo.jsonl"}}` |
| POST | `/predict?operatorId=M07` | JSON `{"uniqueId","isLastOperator","inputFile?","config?"}` | 统一响应（中间/阶段末同构，见下方） |
| POST | `/predict?detection=normalize_audio` | `multipart/form-data`，字段 `file`（单音频 wav/mp3/flac 或 JSON/JSONL/zip）+ 可选 `uniqueId`、`enabled`/`sample_rate`/`channels`/`pcm_bits` | 成功：标准化 WAV 文件流；失败：`{"status":0,"msg":"..."}` |
| POST | `/removeFile?uniqueId=<id>` | 无体（幂等） | `{"code":0,"msg":"success","data":{}}` |
| * | `/downloadFile` | — | 契约要求不实现，固定 404 信封 |

`/predict` 请求字段：`uniqueId`（string，必传）、`isLastOperator`（**严格** bool，必传）、`inputFile`（可选，必须在任务根内）、`config`（可选，业务参数对象）。

- **统一响应（v4.2）**：`isLastOperator` 仍必传（阶段末标记），但**中间与阶段末响应同构**，
  均返回 `data{operator,status,outputFile,result}`：`outputFile` 为 `crossmodal-files-v1`
  文件内容对象（含入口、`06_rejects.jsonl`、全部被引用媒体与 `manifest.json`）；
  `result` 为业务摘要（int `status` + `process_status` + `statistics` + `entrypoint` + `input`），
  不再用 `isLastOperator=false` 只回血缘对象；
- 失败信封统一 `{"code":1,"msg":"...","data":{...}}`：运行失败为 `data={"status":"failed"}`，
  参数/配置等请求错误为 `data={}`；`partial`（有拒绝但处理完成）仍为 `code=0`。

## 4. 配置说明

见 `configs/config.yaml` 与 README §3。要点：

- 业务参数（`enabled`、`sample_rate`、`channels`、`pcm_bits`）可被请求 `config` 覆盖，但取值固定为 16000 Hz、单声道、16 bit；
- 部署固定/锁定项（`operator`、`stage_id`、`max_duration_seconds`、`ffmpeg_path`、`ffprobe_path`、`process_timeout_seconds`、`clip_start`、`clip_end`）**不接受 `/predict` 覆盖**：请求白名单会拒绝，部署身份、解码后端路径、时长上限与裁剪区间均随镜像锁定；
- 服务端限额（上传字节/文件数/解压总大小/响应字节）定义在 `app.py` 常量中，**只随镜像发布，不接受请求放宽**；
- 请求参数强类型：整数字段拒绝 `"16000"` 等数字字符串，浮点字段拒绝字符串与布尔值；
- 未知字段/非法取值 → HTTP 400/422、CLI 退出码 1。

## 5. 运行与运维

- 新 run 使用递增 `run_id`（`run001`…），**不覆盖**上游或历史 run；
- 上游素材落点 `input/`，上传为**原子提交**（解压到 `<root>/.tmp/upload_*/`、校验入口唯一后才替换），失败不留半成品；
- 同一 `uniqueId` 同时只允许一个写操作，锁冲突返回 HTTP 409 `task_busy`；不同 `uniqueId` 可并发；
- `/removeFile` 只删本任务目录，任务不存在也返回 `code=0`（幂等），锁文件放任务目录之外；
- 日志只写 **stderr** 与 run 目录的 `run.log`，只记录动作、样本标识与必要统计，不打印完整输入原文、密钥、token、堆栈；
- 优雅降级：缺 `ffmpeg`/`ffprobe` 时非 WAV 输入**显式拒绝**（`dependency_missing`）并写入异常清单，
  不会静默把源文件当成功产物；全量 `dependency_missing` 时本轮 `status=failed`、CLI 退出码 1。

## 6. 资源建议与实测

| 项 | 建议 | 本次实测 |
|---|---|---|
| CPU | 1 核起；重采样为单线程 NumPy/SciPy，`resample_poly` 峰值内存与单条音频时长成正比 | 见 `HANDOFF.md`（本机 CPU，纯规则，无 GPU） |
| 内存 | 单条音频按「目标采样率 × 时长 × 4 字节（f32）」估算，`max_duration_seconds` 是硬上限 | 60s/16kHz 单声道约 3.8 MB 浮点 + 1.9 MB 输出 |
| GPU | **不需要**，本算子无模型推理 | — |
| 磁盘 | 每个 run 目录 = 标准化 WAV + 被引用媒体副本，二者都会随 `crossmodal-files-v1` 打包 | 见 `HANDOFF.md` 的 `media_bytes` / `audio_bytes` |
| 网络 | **不需要**（不下载模型、不抓取 URL 媒体） | — |

`process_timeout_seconds` 只约束 ffmpeg/ffprobe 子进程；纯 `wave` 路径没有子进程。

## 7. 评价指标

本算子为确定性信号处理，指标是**过程正确性**而非模型精度：

| 指标 | 定义 | 期望 |
|---|---|---|
| 输出参数一致率 | 写出 WAV 复读的采样率/声道/位宽与配置一致的比例 | 100%（不一致即 `audio_save_failed`，本轮不提交） |
| 时长保真 | 无裁剪时 `|输出时长 − 源时长|` | ≤ 1 个目标采样帧（见 `utils/audio_processing.FRAME_TOLERANCE`） |
| 统计守恒 | `input == output + rejected` | 恒成立，否则本轮判失败 |
| 媒体完整性 | 有效记录引用的媒体均存在于 run 目录 | 100%（缺失即拒绝该记录，不留孤儿文件） |
| 非静音率 | `rms > 0` 的标准化结果占比 | 源音频非静音时应为 100%，否则计 `silent_output` 供复核 |

**不报告任何模型准确率/相似度**：本算子不加载模型，也不做语音识别。

## 8. 故障排查

| 现象（msg / 字段） | 原因 | 处置 |
|---|---|---|
| `配置非法——...` | 业务参数越界/未知键/类型不符 | 按 README §3 取值域修正，勿传 `operator`、`stage_id` |
| `inputFile 非法：...` | `inputFile` 越出任务目录 | 使用本任务目录内的路径（推荐用上一次运行的主清单 `06_audio_index.jsonl`） |
| `任务目录缺少 input/ 上传内容` | 未上传素材且未传 `inputFile` | 先 `/uploadFile` 或传正确 `inputFile` |
| `素材包内没有可识别的 JSONL 入口，且缺少 manifest.json` | 包内无入口或入口歧义 | 补 `manifest.json` 或改成 `demo.jsonl` |
| `zip 存在重复成员（含大小写碰撞）` / `zip 成员路径非法` / `不允许符号链接` | 素材包本身不合规 | 重新打包 |
| `上传内容超过 ... 限额` | 超过服务端上传限额 | 拆分素材包，限额不接受请求放宽 |
| `输出产物缺失：...` | 主清单引用的媒体没落盘（程序缺陷） | 上报并保留 run 目录 |
| `统计不守恒：...` | 程序缺陷 | 上报并保留 run 目录 |
| `output_too_large：...` | 文件内容对象超 `MAX_RESPONSE_BYTES` | 提高镜像内限额，或降低音频时长/位宽/声道 |
| `status=failed, dependency_missing` | 全部音频记录都缺 ffmpeg/ffprobe | 装 ffmpeg，或确认输入确实都是 PCM WAV |
| `reason=media_unreadable` | 媒体缺失/损坏/0 字节 | 业务拒绝，检查 `06_rejects.jsonl` 与上游素材 |
| `reason=invalid_audio_metadata` | 空音频、非法采样率/声道/位宽 | 属业务拒绝；0 帧文件应从上游剔除 |
| `reason=resource_limit` | 超 `max_duration_seconds` | 调高限额或先切片；**不会截断** |
| `reason=invalid_clip_range` | `clip_end` 超出源时长或区间反转 | 修正 `clip_start`/`clip_end` |
| `decoder_backend=ffmpeg` | 非 PCM WAV 走了 ffmpeg | 正常；注意 `ffmpeg_encoding_delay_note` 的一帧级偏差 |
| `empty_input` | 入口清单为空 | 正常成功；检查上游是否产出记录 |

## 9. 验收（本目录内）

```bash
python -m pytest tests/ -q
python dataset/sample/make_sample.py
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_06 --output-root output/smoke
python -m json.tool map.json
python -m compileall -q inference.py app.py utils
python -m uvicorn app:app --host 127.0.0.1 --port 8106
```

验收关注：记录/媒体/计数一致、`input == output + rejected` 守恒、输出 WAV 可被 `wave` 复读且时长正确、
负载还原后全部媒体可解码、`data.result` 统计完整。实际结果见 `HANDOFF.md`。

## 10. 边界与已知限制

- 只处理 `audio` 标准化；文本/图像/视频**透传并搬运**，无音频样本标 `not_applicable`，不做语义对齐；
- 不做语音识别、说话人分离、降噪、响度归一、静音切分；
- 超限**不截断**：宁可拒绝一条记录，也不产出“截断后的完整音频”；
- 非 PCM WAV 依赖 `ffmpeg`/`ffprobe`；缺失时显式 `dependency_missing`（见 §5），本地开发环境未安装 ffmpeg，
  该路径已实现但**未在本机真机验证**（见 `HANDOFF.md` 未验证项）；
- 音频编码延迟可能造成一帧级时长偏差；标准库 `wave` 无法读取编码延迟信息，因此仅在使用 ffmpeg 时
  在 `statistics.ffmpeg_encoding_delay_note` 中提示，不做虚构补偿；
- 不做重采样质量评估（无 PESQ/STOI 等指标），只保证参数一致、时长保真与非静音；
- 无模型与权重，不做训练/微调，不提供虚假训练命令；
- demo 传输格式 `crossmodal-files-v1` 与 64MiB 限额为约定值，收敛于 `utils/transport.py` 单点适配。
