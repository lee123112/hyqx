# weight/ · 跨模态算子包根权重说明（v1.0 正式版）

> **v1.0 正式版（2026-10-31）起，运行所需模型权重不再放在本目录，而是逐算子内置到
> `<算子目录>/weight/`。** 这是为了满足“后续会单个算子调用”的部署形态：单个算子目录
> 复制到任何位置都能独立加载自己的小模型，不依赖包根共享模型目录。

## 1. 运行权重实际位置

| 模型 | 内置算子 | 算子内相对路径 | 字节数 | SHA-256 |
|---|---|---|---:|---|
| CLIP-ViT/B-32 | #12、#14、#16、#18、#20 | `weight/clip/ViT-B-32.pt` | 353,976,522 | `40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af` |
| whisper-base（11 文件） | #08 | `weight/whisper-base/` | 290,403,936（主权重 `model.safetensors`） | `07cadb9f25677c8d50df603e66a98fbd842cce45047139baeb16e6219a1e807b` |
| YOLOv8n | #12（可选）、#17（必需） | `weight/YOLOv8n/yolov8n.pt` | 6,534,387 | `31e20dde3def09e2cf938c7be6fe23d9150bbbe503982af13345706515f2ef95` |

- 各算子目录下的 `weight/README.md` 记录了该算子的完整文件清单、字节数与 SHA-256。
- `#13 评分音文语义一致性` 为 v2 规则路线（复用上游 whisper 转写），生产**不加载**
  `wav2vec2-base` / `bert-base-chinese`，因此正式包不内置这两份诊断权重。

## 2. 加载规则

1. 各算子配置里的权重仍是**相对路径**（如 `clip/ViT-B-32.pt`、`whisper-base`）。
2. 默认解析根为**该算子目录下的 `weight/`**（例如
   `CMA_score_image_text_consistency/weight/clip/ViT-B-32.pt`）。
3. 仅当显式设置环境变量 `MODEL_ROOT` 时，相对路径改为按该目录解析（用于外挂统一模型盘）；
   **单算子独立运行不需要设置 `MODEL_ROOT`**。
4. 全程 `local_files_only=True`，运行期**不联网、不下载、不调用大模型/公网 API**；
   缺权重时按各算子 `allow_degraded` 显式失败或降级，绝不伪造分数。

## 3. 校验方式

```powershell
cd G:\HYQX\算子\跨模态\crossmodal_v1.0
python -X utf8 -B inference.py --preflight
# 期望：operatorCount=26、okCount=26，weightBundles.checked=8、weightBundles.missing=[]

# 单算子自检示例（复制算子目录到包外仍可用）：
python -X utf8 -B CMA_score_image_text_consistency\inference.py --help
```

## 4. 包根 weight/ 的定位

本目录在正式交付中只保留本说明（以及 `map.json` 中的权重清单），**不再放模型大文件**。
这是有意的结构调整，不是漏交付；`#3–#22` 的所有模型依赖都已在对应算子目录内闭环。
