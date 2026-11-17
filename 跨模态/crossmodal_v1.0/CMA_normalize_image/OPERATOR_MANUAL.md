# 算子 4 说明书：图像标准化处理

> 配套文件：`README.md`（使用概览）、`dataset/DATA_SCHEMA.md`（数据契约）、`HANDOFF.md`（测试证据）。
> 版本：v1.0.0（demo）；阶段：S2；正式算子编号：4。

## 1. 概述

| 项 | 内容 |
|---|---|
| 职责 | 对图文样本中的图像做解码校验、EXIF 方向归一、等比尺寸/格式标准化（清晰度评分锁定关闭） |
| 输入 | 同阶段上游主清单 `03_text_cleaned.jsonl`（每行一条完整记录） |
| 输出 | `04_image_index.jsonl`（主清单）、`04_images/`、`media/original/`、`04_rejects.jsonl` |
| 算法 | CPU 规则：Pillow；**无模型、无需训练、不联网**；清晰度评分锁定关闭 |
| 依赖 | FastAPI / Uvicorn / Pydantic / PyYAML / python-multipart / Pillow / NumPy（必需） |
| 端口 | 8104 |

## 2. 部署

### 2.1 直接运行（开发/联调）

```bash
cd G:/HYQX/算子/跨模态/demo/04_图像标准化处理
python -m pip install -r requirements.txt
python dataset/sample/make_sample.py
export OUTPUT_DIR=/data/output          # PowerShell: $env:OUTPUT_DIR="D:/data/output"
python -m uvicorn app:app --host 0.0.0.0 --port 8104
```

### 2.2 容器

```bash
cd packaging
python prepare_package.py
docker build -t crossmodal-image-standardization:v1.0.0 .
docker run --rm -p 8104:8104 -v /host/output:/data/output \
  -e OUTPUT_DIR=/data/output crossmodal-image-standardization:v1.0.0
```

- 同阶段服务须挂载同一共享 `OUTPUT_DIR` 且容器内路径一致；否则由工程侧搬运，不得假定 A 容器路径在 B 容器存在。
- 环境变量：`OUTPUT_DIR`（输出根）、`OPERATOR_CONFIG`（自定义配置路径，默认 `configs/config.yaml`）。

## 3. 接口

| 方法 | 路径 | 请求 | 成功返回 |
|---|---|---|---|
| GET | `/health` | — | `{"status":"ok","operator":"image_standardization"}` |
| POST | `/predict` | `multipart`（`request`=JSON 字符串、`file`=zip）或 JSON 体；[v4.2] 形态一为不带 `request` 的 multipart（`?detection=M05`，`file` 必传，成功返回图像文件流、失败返回 `{"status":0,"msg":...}`） | 见下（v4.2 统一响应） |
| POST | `/removeFile` | JSON `{"uniqueId":"..."}` | `{"code":0,"msg":"任务已删除","data":{"deleted":true}}` |
| * | `/downloadFile` | — | 契约要求不实现，明确 404 |

`/predict` 请求字段：`uniqueId`（string，必传）、`isLastOperator`（**严格** bool，必传）、`inputFile`（可选，任务根内相对/绝对路径）、`config`（可选，业务参数对象）。

- [v4.2] `isLastOperator` 仍必传（严格 bool），用于标记阶段末调用；中间与阶段末响应同构，形式二均返回 `data={operator, status, outputFile, result}`：
  - `outputFile` 为 `crossmodal-files-v1` 内容对象（含入口、全部媒体、旁路清单、`manifest.json`），[v4.2] 不再返回 `lineageFile`、运行目录也不再写 `lineage.json`/`stage_context.json`；
  - `result` 为业务摘要：`{process_status, image, format, max_side, statistics}`，统计/配置快照/模型身份等信息由此承载。

失败信封统一 `{"code":1,"msg":"...","success":false,"data":{"error":"..."}}`；`partial` 仍为 `code=0`。

## 4. 配置说明

见 `configs/config.yaml` 与 README §5。要点：

- 业务参数（`enabled`、`max_side`、`output_format`）可被请求 `config` 覆盖；`max_side` 取值域 `512`—`2048`（默认 `1024`，只缩不放，`allow_upscale` 固定 `false`）；
- 部署固定/锁定项（`operator`、`stage_id`、`io`、`server`、`allow_upscale`、`alpha_background`、`blur_threshold`、`blur_action`、`max_pixels`、`jpeg_quality`）**不接受 `/predict` 覆盖**：请求白名单会拒绝，部署身份不可篡改，算法/质量参数固定，限额不可放宽；
- 未知字段/枚举、非有限数值 → HTTP 422、CLI 退出码 1。

## 5. 运行与运维

- 新 run 使用递增 `run_id`（`run001`…），**不覆盖**上游或历史 run；`input/manifest.json` 为入口定位依据。
- 同一 `uniqueId` 同时只允许一个写操作，锁冲突返回 409 `task_busy`；不同 `uniqueId` 可并发。
- `/removeFile` 取任务锁，任务不存在时返回 `deleted=false`（幂等），锁文件放任务目录之外。
- 日志 `run.log` 只记录动作、样本标识与必要统计；不打印完整输入原文、密钥、token。

## 6. 故障排查

| 现象 | 原因 | 处置 |
|---|---|---|
| `code=1, error=invalid_config` | 业务参数非法 | 按 README §5 取值域修正 |
| `error=input_file_not_found` / `input_missing` | 未上传素材或 `inputFile` 错误 | 先上传 zip 或传正确 `inputFile` |
| `error=invalid_input_encoding` | 输入非 UTF-8 | 重新导出为 UTF-8 |
| `error=empty_input` | 输入为空 | 检查上游是否产出记录 |
| `error=output_too_large` | 响应文件对象超 `max_response_bytes` | 提高限额或减小媒体 |
| `error=media_unreadable` | 阶段末门禁：主清单媒体引用不可读 | 检查录像/搬运，勿手工删媒体 |
| 记录 `reason=image_decode_failed` | 图片损坏/像素超限/后缀不匹配 | 属业务拒绝，检查 `04_rejects.jsonl` |
| `reason=dependency_missing:pillow|numpy` | 依赖缺失 | 安装 requirements |

## 7. 验收（本目录内）

```bash
python -m pytest tests/ -q
python dataset/sample/make_sample.py
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_04 --output-root output/smoke
python -m json.tool map.json
python -m compileall -q inference.py app.py utils
python -m uvicorn app:app --host 127.0.0.1 --port 8104
```

验收关注：记录/字节/计数一致、守恒、媒体可恢复可读、中间/阶段末响应同构。实际结果见 `HANDOFF.md`。

## 8. 边界与已知限制

- 只处理图像模态；音视频/纯文本透传并标 `not_applicable`，不做语义对齐。
- 清晰度评分锁定关闭（不产出 `blur_score`）；最终质检与分流属算子 21。
- 无模型与权重，不做训练/微调，不提供虚假训练命令。
- demo 传输格式与 64MiB 限额为约定值，收敛于 `utils/transport.py` 单点适配。
