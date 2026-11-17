# weight/ · 算子 #17（标注局部对整体）本地权重

> 本目录随**单算子**交付，不依赖包根 `weight/`，也不依赖 `shared_clip/`。
> 运行期只从本地路径加载，**绝不联网下载**（`utils/detectors.py` 不做任何 URL 下载）。

## 1. 模型

| 项 | 值 |
|---|---|
| 模型 | YOLOv8n（Ultralytics，表1 #17 固定项；不部署 Grounding DINO、不训练专用检测模型） |
| 用途 | 图像通用类别（COCO-80）候选框预填；专用设备类不预填，留给人工手画 |
| 后端 | `ultralytics.YOLO`（`utils/detectors.py::Yolov8nBackend`），CPU 可跑 |
| 类别口径 | 只保留官方通用 80 类 ∩ 与描述可对上的框（`source=yolov8n`、`human_verified=false`） |

## 2. 来源

- 交付副本复制自：`G:\HYQX\算子\跨模态\crossmodal_package_20261026\weight\YOLOv8n\yolov8n.pt`
- 上游原始目录：`G:\HYQX\算子\model\YOLOv8n`（Ultralytics 官方 `yolov8n.pt`）
- 复制方式：单文件全量复制，复制后比对 SHA-256（一致）。

## 3. 文件清单（1 个文件，6,534,387 字节）

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `YOLOv8n/yolov8n.pt` | 6,534,387 | `31E20DDE3DEF09E2CF938C7BE6FE23D9150BBBE503982AF13345706515F2EF95` |

复算命令（Windows PowerShell）：

```powershell
Get-FileHash .\weight\YOLOv8n\yolov8n.pt -Algorithm SHA256
```

## 4. 默认路径（不设 `MODEL_ROOT` 时）

解析顺序（`utils/detectors.py::model_root()`）：

1. `$MODEL_ROOT`（环境变量）最高优先；
2. 否则 `<本算子目录>/weight`（源码相对 `parents[1]/"weight"`，与包根无关）。

因此 `configs/config.yaml` 默认：

```yaml
detector:
  weights: YOLOv8n/yolov8n.pt   # 相对 <本算子目录>/weight 解析
```

即加载 `<本算子目录>/weight/YOLOv8n/yolov8n.pt`（本目录）。`weights` 也可写成目录
`YOLOv8n/`（自动取其中的 `yolov8n.pt`）或绝对路径（原样使用，不再拼接根目录）。

## 5. `MODEL_ROOT` 覆盖说明

- 设 `MODEL_ROOT=/models` 时：`weights: YOLOv8n/yolov8n.pt` → `/models/YOLOv8n/yolov8n.pt`；
  容器只需把本目录内容挂到 `/models`。
- 覆盖**优先且不回退**：`MODEL_ROOT` 下找不到权重时，按 `allow_degraded` 显式降级
  （`reason=model_weights_not_found`、零框、`pending_manual_review`），或
  `detector_backend=yolov8n` / `allow_degraded=false` 时显式失败；不会静默改用本目录副本。
- 真实加载路径可在运行日志与响应 `detector.identity.weights` 中核对（`weights_source=local-only`）。
- 本算子 `enabled=false`（默认）时完全不加载权重；本目录仅在被启用且 `detector_backend != none` 时使用。

## 6. 独立部署（包外单算子）实测

测试方式：本算子目录整体复制到包外临时目录（无包根 `weight/`、无 `shared_clip/`），
清空 `MODEL_ROOT`，用 `--enable` 开启（默认配置 `detector.weights: YOLOv8n/yolov8n.pt`）：

```powershell
D:\python\python.exe -X utf8 inference.py --input dataset/sample/demo.jsonl `
    --unique-id task017_file10 --output-root out --enable
```

| 场景 | 结果 |
|---|---|
| 自带 `dataset/sample/demo.jsonl`（默认 fixture 图为纯色小图） | 日志 `detector_ready ... source=local_weights`；`statistics.model.weights=<临时算子目录>\weight\YOLOv8n\yolov8n.pt`、`real_backend=true`、`network_access=false`、`degraded=false`；纯色图无匹配 → `boxes_prefilled=0`（零伪造） |
| 真实图片（本机 ultralytics 自带 `assets/bus.jpg`，离线文件） | 预填 5 框：`bus 0.873`、`person 0.866/0.853/0.825/0.261`，`source=yolov8n`、`human_verified=false`、`boxes_source=yolov8n_prefill`；模型身份同上 |
| `MODEL_ROOT=<空目录>` | 日志 `detector_degraded reason=model_weights_not_found: YOLOv8n/yolov8n.pt`；`statistics.model=null`、`status=degraded`、`boxes_prefilled=0`、记录 `boxes_source=none`；`detector_availability.weights=null`（**未回退**到本目录副本） |

`python -m pytest -q`：**133 passed, 51 skipped**（既有 skip：v4.2 血缘/运输输出停用等，与本改动无关）。

