# 算子 #12 评分图文语义一致性 · 内置权重

本目录随算子目录（`CMA_score_image_text_consistency/weight/`）一起交付，运行时**只从本地加载**，
绝不联网下载。权重文件请按只读使用，不要在本目录写入运行产物（产物写到 `output/`）。

## 1. 文件清单

| 模型 | 相对路径 | 字节数 | SHA-256 |
| --- | --- | --- | --- |
| CLIP-ViT/B-32（正式图文评分模型） | `clip/ViT-B-32.pt` | 353,976,522 | `40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af` |
| YOLOv8n（可选辅助检测，默认关闭） | `YOLOv8n/yolov8n.pt` | 6,534,387 | `31e20dde3def09e2cf938c7be6fe23d9150bbe503982af13345706515f2ef95` |

- 来源包：`G:\HYQX\算子\跨模态\crossmodal_package_20261026\weight`
  （对应 `weight/clip/ViT-B-32.pt` 与 `weight/YOLOv8n/yolov8n.pt`）。
- 两个文件均为来源包文件的原样复制；字节数与 SHA-256 与来源一致（已用 `Get-FileHash -Algorithm SHA256` 核对）。

## 2. 默认加载路径

- 配置项：`model.weights: clip/ViT-B-32.pt`（字段名与值均保持不变）。
- 相对路径由 `utils/backends.py::resolve_model_path` 解析：
  - **未设 `MODEL_ROOT`** → `<本算子目录>/weight/clip/ViT-B-32.pt`，即本目录（默认）；
  - **设了 `MODEL_ROOT`** → `<MODEL_ROOT>/clip/ViT-B-32.pt`，仅作覆盖，仍按相对路径拼接；
  - 绝对路径原样使用。
- 本算子目录可以单独复制到包外运行：不依赖包根 `weight/`，也不要求存在 `shared_clip/`。
  `shared_clip.runtime` 为可选导入，包根不存在或未设 `MULTIMODAL_CLIP_SESSION` 时
  静默走本地权重；只有共享实例真实存在时才走远程复用（行为与改造前一致）。

## 3. 可选 YOLOv8 辅助检测

- `YOLOv8n/yolov8n.pt` 只在 CLI `--enable-detector` 且配置 `detector.weights` 显式指向本文件时加载；
  默认 `detector.weights: null`，检测器默认不启用，行为与改造前一致。
- 检测器只补辅助证据，**不改变** CLIP 分档；不可用只记 `unavailable`。

## 4. 完整性校验（PowerShell）

```powershell
Get-FileHash -Algorithm SHA256 .\clip\ViT-B-32.pt
Get-FileHash -Algorithm SHA256 .\YOLOv8n\yolov8n.pt
```

期望值见上表；不一致说明文件在复制/传输中损坏，请从来源包重新复制。
