# 算子 #14 评分视文语义一致性 · 内置权重

本目录随算子目录（`CMA_score_video_text_consistency/weight/`）一起交付，运行时**只从本地加载**，
绝不联网下载。权重文件请按只读使用，不要在本目录写入运行产物（产物写到 `output/`）。

## 1. 文件清单

| 模型 | 相对路径 | 字节数 | SHA-256 |
| --- | --- | --- | --- |
| CLIP-ViT/B-32（正式视文评分模型，复用图文评分口径） | `clip/ViT-B-32.pt` | 353,976,522 | `40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af` |

- 来源包：`G:\HYQX\算子\跨模态\crossmodal_package_20261026\weight\clip\ViT-B-32.pt`。
- 文件为来源包的原样复制；字节数与 SHA-256 与来源一致（已用 `Get-FileHash -Algorithm SHA256` 核对）。
- 与 #12 图文评分使用同一份 ViT-B-32 权重（同一 sha256），各算子目录各自内置一份，
  互不 import、可独立打包。

## 2. 默认加载路径

- 配置项：`clip.weights: clip/ViT-B-32.pt`（字段名与值均保持不变）。
- 相对路径由 `utils/backends.py::resolve_model_path` 解析：
  - **未设 `MODEL_ROOT`** → `<本算子目录>/weight/clip/ViT-B-32.pt`，即本目录（默认）；
  - **设了 `MODEL_ROOT`** → `<MODEL_ROOT>/clip/ViT-B-32.pt`，仅作覆盖，仍按相对路径拼接；
  - 绝对路径原样使用。
- 本算子目录可以单独复制到包外运行：不依赖包根 `weight/`，也不要求存在 `shared_clip/`。
  `shared_clip.runtime` 为可选导入，包根不存在或未设 `MULTIMODAL_CLIP_SESSION` 时
  静默走本地权重；只有共享实例真实存在时才走远程复用（行为与改造前一致）。

## 3. 完整性校验（PowerShell）

```powershell
Get-FileHash -Algorithm SHA256 .\clip\ViT-B-32.pt
```

期望值见上表；不一致说明文件在复制/传输中损坏，请从来源包重新复制。
