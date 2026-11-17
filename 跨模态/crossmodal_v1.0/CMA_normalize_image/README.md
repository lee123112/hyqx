# 算子 4：图像标准化处理（crossmodal / S2 阶段）

> **v4.2 接口变更（2026-10-19）**：`/predict` 双协议——形态一 `POST /predict?detection=<算子 id>`（`multipart/form-data`，`file` 必传，返回 JSON 首字段 int `status`：判定型 1 通过/0 不通过，处理型 1 成功/0 失败）；形态二 `POST /predict?operatorId=Mxx`（`application/json`，返回 `{code,msg,data{operator,status,outputFile,result}}`，业务失败 `data={}`）。**血缘/阶段归档输出已停用**：不再返回 `lineageFile`，不再写 `lineage.json`/`stage_context.json`/`stage_lineage.jsonl`/`stage_artifact.zip`；记录级 `status` 为 int（1/0）+ `process_status`。

> demo 目录：`G:/HYQX/算子/跨模态/demo/04_图像标准化处理`
> 阶段：S2（跨模态数据处理）；默认 `isLastOperator=false`，阶段末节点为 07，[v4.2] 中间/阶段末响应同构。
> 传输格式 `crossmodal-files-v1` / 清单 `crossmodal-manifest-v1` 与 64MiB 限额是**并行开发选定的 demo 约定**，非甲方正式协议；正式二进制承载协议到位时只替换 `utils/transport.py` 适配层。

## 1. 用途

读取同阶段上游（03 文本清洗）的 `03_text_cleaned.jsonl`，对该节点拥有的图像子对象做：

1. **解码校验**（Pillow 打开并 `load()`，失败明确拒绝，不遗留半文件）；
2. **EXIF 方向归一**（`ImageOps.exif_transpose`，记录原始尺寸与方向）；
3. **等比尺寸标准化**（`ratio=min(1, max_side/max(w,h))`，至少 1 像素；默认不放大小图）；
4. **格式与色彩标准化**（统一转 RGB，RGBA/LA 按 `alpha_background` 合成到不透明底色；输出 PNG/JPEG 且扩展名与格式匹配）；
5. **清晰度评分**：表1 口径下锁定/禁用，不计算 `blur_score`、不按阈值标记/剔除；
6. **原图另存** `media/original/<sample_id>.<ext>` 供质检回溯。

主清单每行仍是**完整样本记录**：保留 `sample_id`、`text`、`video`、`audio`、`processing`、`alignment`、`qc`、`metadata` 以及未知字段；本算子只改写自己负责的 `image` 子对象并追加 `processing["04"]`。

## 2. 业务边界（明确不做）

- 不做文本/视频/音频的业务判定；音视频组合（video+audio）与纯文本记录**透传**并标记 `processing["04"] = {"status": 1, "process_status": "not_applicable"}`；
- 不做语义清晰度/质量判定；最终质检与 accepted/review/rejected 分流是算子 21 的职责；
- 不改写上游失败原因、模型、路径映射；
- 不 import 兄弟算子的 `utils`/`inference`，不修改根目录共享文件。

## 3. 输入 / 输出

| 类型 | 文件 | 说明 |
|---|---|---|
| 输入 | `03_text_cleaned.jsonl` | 上游主清单，每行一条完整记录；HTTP 用上游响应 `outputFile` 恢复出的任务内路径 |
| 输出（主清单） | `04_image_index.jsonl` | 完整记录 + `image` 对象 + `processing["04"]`；下一节点 inputFile 目标 |
| 输出（图像） | `04_images/<sample_id>.<png|jpg>` | 标准化后的图像 |
| 输出（原图） | `media/original/<sample_id>.<ext>` | 另存的可解码原图，字节不变 |
| 输出（异常） | `04_rejects.jsonl` | 异常/剔除记录：`sample_id`、`line`、`reason`、`record`、`evidence` |
| 搬运 | `manifest.json` + 被引用媒体 | `crossmodal-manifest-v1`，随 `outputFile` 内容对象返回（[v4.2] 中间/阶段末同构） |
| 控制文件 | `run.log` | 本地控制文件，不打进 files |

> 注：v4.2 起 `lineage.json`、`stage_context.json`、`stage_lineage.jsonl`、`stage_artifact.zip` 等血缘/阶段归档文件不再产出（`app.py`/`inference.py` 已停止血缘/阶段归档与 MTY 归档调用，`utils/lineage.py` 仅保留 `now_iso` 时间工具、`utils/mty_compliance.py` 代码保留存档；`utils/transport.py` 仍用于 `crossmodal-files-v1` 内容对象）；原血缘中的统计/配置快照/模型身份等信息改由 `data.result`、`manifest.json`、`run.log` 承载。

输出目录布局（`OUTPUT_DIR` 为根）：

```text
OUTPUT_DIR/<uniqueId>/
  input/                                  # 上传素材：manifest.json + 入口JSONL + media/
  image_standardization/<run_id>/
    04_image_index.jsonl  04_rejects.jsonl  manifest.json  run.log
    04_images/*.jpg         media/original/*.<ext>   media/<pass-through>
```

> 注：v4.2 起任务目录与运行目录不再写 `lineage.json` / `stage_context.json`；`input/lineage_history.json` 仅为历史样例存档，不再读取。

`image` 对象字段（示例见 `dataset/sample` 运行结果）：

```json
{
  "path": "04_images/s1.jpg",
  "original_path": "media/original/s1.png",
  "width": 1024, "height": 512, "format": "JPEG",
  "original_width": 1600, "original_height": 800, "original_format": "PNG",
  "transform": {"original_width":1600,"original_height":800,"original_mode":"RGB",
    "original_format":"PNG","exif_orientation":null,"orientation_applied":false,
    "oriented_width":1600,"oriented_height":800,"scale":0.64,"resized":true,"upscaled":false,
    "background":[255,255,255],"output_format":"JPEG","jpeg_quality":95,
    "original_pixel_count":1280000,"output_pixel_count":524288}
}
```

尺寸、格式与缩放信息见 `transform`；清晰度评分锁定关闭，不产出 `blur_score`。

> 上例按默认 `output_format=JPEG`；若配置为 `PNG`，则路径为 `.png`、`format` 为 `PNG`、`jpeg_quality` 为 `null`。

## 4. HTTP 接口

三个服务接口与共享契约一致：`POST /predict`、`POST /removeFile`、以及按契约保留路径但不实现的 `/downloadFile`（明确 404）。阶段传递通过 `/predict` 的 `multipart/form-data`（字段 `request`=JSON 字符串、`file`=zip）或 JSON 体承载；[v4.2] 另支持不带 `request` 字段的单文件流（`?detection=M05`，`file` 必传，成功返回标准化图像文件流）。`/health` 为存活探针（框架层，不算业务接口）。

```bash
# 存活
curl http://127.0.0.1:8104/health

# 阶段内（isLastOperator=false）：与阶段末同形，返回 outputFile + result
curl -X POST http://127.0.0.1:8104/predict \
  -F 'request={"uniqueId":"demoS2_f1","isLastOperator":false}' \
  -F 'file=@dataset/sample/demo.zip'

# 阶段末标记（isLastOperator=true）：响应同形，均返回 outputFile 内容对象 + result
curl -X POST http://127.0.0.1:8104/predict \
  -H 'content-type: application/json' \
  -d '{"uniqueId":"demoS2_f1","isLastOperator":true,"config":{"max_side":512}}'

# 形态一（单文件流）：成功返回标准化图像（image/png 或 image/jpeg）文件流；失败返回 {"status":0,"msg":...}
curl -X POST 'http://127.0.0.1:8104/predict?detection=M05' \
  -F 'fileType=png' -F 'file=@dataset/sample/media/wide_1600x800.png'

# 清理任务
curl -X POST 'http://127.0.0.1:8104/removeFile?uniqueId=demoS2_f1'
```

形态二响应示例（[v4.2] `isLastOperator` 仍必传，中间/阶段末响应同构）：

```json
{"code":0,"msg":"处理 14 条：输出 12（含不适用 2）、拒绝 2、坏行 0 行","success":true,
 "message":"处理 14 条：输出 12（含不适用 2）、拒绝 2、坏行 0 行",
 "data":{"operator":"normalize_image","status":"success",
  "outputFile":{"format":"crossmodal-files-v1","entrypoint":"04_image_index.jsonl",
    "files":[{"path":"04_image_index.jsonl","encoding":"utf-8","content":"..."},
             {"path":"manifest.json","encoding":"utf-8","content":"..."}]},
  "result":{"process_status":"success","image":"04_image_index.jsonl",
            "format":"JPEG","max_side":1024,
            "statistics":{"total_lines":14,"valid_records":14,"output":12,
                          "rejected":2,"not_applicable":2,"images_standardized":10}}}}
```

统一信封：成功 `{"code":0,"msg":"success","data":{...}}`；失败 `code=1` 且 `success=false`，参数错误 HTTP 422、输入/请求错误 400、输出超限 413、任务占用 409。`partial` 仍为 `code=0`，细节进 `data.result`。本 demo 的 `/removeFile` 使用 JSON 体 `{"uniqueId": "..."}`（与旧 02 的 query 形式不同，见 `HANDOFF.md` 共享协议问题）。

## 5. 配置

默认见 `configs/config.yaml`；仅 `enabled`、`max_side`、`output_format` 可由请求 `config` 对象覆盖，CLI 用 `--override KEY=VALUE`（值为 JSON 字面量）。**部署固定/锁定项不接受请求覆盖**：`operator`、`stage_id`、`io`、`server`、`allow_upscale`、`alpha_background`、`blur_threshold`、`blur_action`、`max_pixels`、`jpeg_quality`；请求里出现这些键会被白名单拒绝（HTTP 422）。

| 键 | 默认 | 取值域 | 说明 |
|---|---|---|---|
| `operator.name` / `operator.version` | `image_standardization` / `v1.0.0` | 部署 | 部署身份，请求不可改 |
| `stage_id` | `S2` | 部署 | 阶段标识，请求不可改 |
| `max_side` | `1024` | `512`—`2048` | 归一后最长边像素；只缩不放（`allow_upscale` 固定 `false`） |
| `allow_upscale` | `false` | 固定 `false` | 禁止放大小图（锁定，请求不可开） |
| `output_format` | `JPEG` | `PNG`\|`JPEG` | 输出格式；扩展名必须匹配 |
| `alpha_background` | `[255,255,255]` | 3/4 通道，各 0—255 | alpha 合成底色（取前 3 通道） |
| `blur_threshold` | `—` | 锁定/禁用 | 清晰度评分不启用，不接受请求覆盖 |
| `blur_action` | `—` | 锁定/禁用 | 清晰度评分不启用，不接受请求覆盖 |
| `max_pixels` | `40000000` | >0 | 解码前像素上限，防解压炸弹 |
| `jpeg_quality` | `95` | 1—100 | JPEG 输出质量 |
| `io.output_root` | `output` | 部署 | 输出根（`OUTPUT_DIR` 优先） |
| `server.max_files` | `1000` | 1—100000 | 单素材包文件数上限（部署） |
| `server.max_unpacked_bytes` | `67108864` | >0 | 解压/上传字节上限（64MiB demo 限额） |
| `server.max_response_bytes` | `67108864` | >0 | 响应序列化上限 |

未知字段、未知枚举、非有限数值一律报错（HTTP 422 / CLI 退出 1），不会静默改用另一种算法或格式。

## 6. 安装与启动

```bash
cd G:/HYQX/算子/跨模态/demo/04_图像标准化处理
python -m pip install -r requirements.txt          # Python 3.10+（实测 3.9.7 亦可）
python dataset/sample/make_sample.py               # 生成固定样例与真实媒体
python -m pytest tests/ -q                         # 业务 / 三接口 / 可搬运
python -m uvicorn app:app --host 127.0.0.1 --port 8104
```

CLI（与 HTTP 使用同一 `load_config`）：

```bash
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_04 --output-root output/smoke
python inference.py --input dataset/sample/demo.jsonl --unique-id smoke_04 \
  --override output_format=JPEG --override max_side=512 --last-operator
```

打包：

```bash
cd packaging
python prepare_package.py            # 生成 packaging/image_standardization/
docker build -t crossmodal-image-standardization:v1.0.0 .
```

## 7. 资源与性能

CPU 规则算子，**无需训练、无需模型权重、不联网**。实测（本机 Windows / Python 3.9.7）：

- 14 条样例（含 6 张图像，最长边 1600→1024）单次运行约 1—2 秒；单张 1600×800 PNG 归合约 20—50ms。
- 建议：4 vCPU / 2GiB 内存起；峰值内存约 `max_pixels × 4B`（默认 40M 像素 ≈ 160MiB/张）。GPU 不参与。

## 8. 评价指标

本算子输出可复算的确定性指标，不声明语义精度：

- 守恒：`output + rejected == valid_records`，`total_lines == valid_records + bad_lines`，`not_applicable ⊆ output`；
- 尺寸合规率：输出 `max(width,height) <= max_side`（`allow_upscale=false` 时为 `<=` 原最长边）；
- 可解码率：`04_images/` 全部输出可被 Pillow `verify()`；原图字节与输入一致；
- 格式一致性：输出图像格式与 `output_format` 一致，扩展名与格式匹配；

## 9. 已知限制与降级

- 不做清晰度评分（`blur_score` 锁定关闭），受内容/纹理影响的启发式不参与验收；
- 明确不做：语义质检、accepted/review/rejected 分流、视频抽帧、音频标准化；
- 降级：缺 Pillow/NumPy → 记录以 `dependency_missing:*` 明确拒绝或配置失败，不伪造结果；
- 无真实模型，**不存在模型精度验收**；`README` 与 `HANDOFF.md` 均不报告模型指标。

## 10. 目录结构

```text
04_图像标准化处理/
  inference.py  app.py                     # 业务/CLI 与三接口服务
  utils/{io,schemas,transport,logger,image_processing}.py
  utils/lineage.py                      # （v4.2 停用，存档；仅保留 now_iso 时间工具）
  configs/config.yaml  map.json  requirements.txt
  dataset/sample/{make_sample.py,demo.jsonl,manifest.json,lineage_history.json,media/}   # lineage_history.json 为历史样例存档
  dataset/DATA_SCHEMA.md  README.md  OPERATOR_MANUAL.md  HANDOFF.md
  tests/{conftest.py,test_operator.py,test_api.py,test_transport.py}
  packaging/{Dockerfile,prepare_package.py}
  output/                                   # 运行产物（不进代码包）
```
