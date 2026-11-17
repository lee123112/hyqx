# PRLt models 权重目录说明（正式版 · 2026-10-31）

> 本目录为平行语料正式版算子的本地模型依赖。正式版模型由 **ModelScope 官方仓库**下载，
> 实测 SHA256 与用途登记在同目录 `NEW_MODELS_PROVENANCE.json`；开发侧生成的适配器/原型件登记在
> `LOCAL_RECONSTRUCTION.json`。旧版模型目录保留用于追溯，默认链路**不再加载**。
>
> 运行时解析顺序：环境变量 `MODEL_ROOT` → 包内 `models/`。正式版不依赖远程模型服务，
> 唯一例外是 API20/API23 调用硅基流动 API（不落盘权重）。

---

## 1. 正式版模型清单

| 目录 | 对应算子 | 规模 | 逻辑体积 | 加载方式 |
|---|---|---|---:|---|
| `models/Qwen3-Embedding-0.6B` | API09 句子级对齐、API11 领域识别、API14 语义等价 | 0.6B，1024 维 | 1.21 GB | `sentence_transformers.SentenceTransformer`（或 transformers AutoModel + last-token pooling） |
| `models/mdeberta-v3-base` | API10 子句/短语级对齐 | 278M（86M 骨干 + 190M 嵌入层），768 维 | 1.34 GB | `transformers.AutoModel` + `AutoTokenizer`（fast tokenizer，mean pooling） |
| `models/Qwen3.5-2B` | API15 译文流畅度、API22 句式变换 | 2B（多模态后训练模型，文本可用） | 4.57 GB | `AutoModelForImageTextToText` + `AutoTokenizer.apply_chat_template`（text-only） |

合计本地权重约 **7.1 GB**。旧版模型（LaBSE/XLM-R/BGE-M3/Qwen2.5/NLLB/领域分类器）合计约 12 GB，已退役但保留在目录中。

### 1.1 远程 API 模型

| 模型 | 对应算子 | 接入方式 |
|---|---|---|
| `Qwen/Qwen3.5-9B`（硅基流动） | API20 回译增强、API23 跨语言合成 | `https://api.siliconflow.cn/v1/chat/completions`，Key 取环境变量 `SILICONFLOW_API_KEY` |

无 Key 或网络失败时算子结构化失败，**不伪造译文**。

---

## 2. 实测 SHA256（完整清单见 `NEW_MODELS_PROVENANCE.json`）

| 文件 | 大小 (bytes) | SHA256 |
|---|---:|---|
| `Qwen3-Embedding-0.6B/model.safetensors` | 1,191,586,416 | `0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd` |
| `mdeberta-v3-base/pytorch_model.bin` | 1,332,809,049 | `6f89419baf0f1aaad5cab7d53901e36a8c1af8f6b4ab58b15db9af32df656ead` |
| `mdeberta-v3-base/tokenizer.json`（本地转换） | 16,331,374 | `5e26ec8cd2234df8edcc6e8c207b66b3162360f9fdc46ee4526a440d0d9d0bb9` |
| `Qwen3.5-2B/model.safetensors-00001-of-00001.safetensors` | 4,548,221,488 | `aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1` |

`mdeberta-v3-base/tokenizer.json` 并非官方仓库文件：Windows 中文路径下 sentencepiece 直接加载
`spm.model` 会失败（C++ 层非 ASCII 路径），因此用 transformers 4.41.2 将其转换为 fast tokenizer 后落盘。
`spm.model` 原样保留。

---

## 3. 加载与冒烟

环境要求：Python 3.11+、torch>=2.6、transformers>=5.22（Qwen3.5 的 `qwen3_5` 架构）、
sentence-transformers>=6.2、sentencepiece==0.2.1、tiktoken。

```powershell
cd G:\HYQX\算子\平行语料\PRLt_V1.0
$env:PYTHONIOENCODING='utf-8'
$env:HF_HUB_OFFLINE='1'; $env:TRANSFORMERS_OFFLINE='1'

# 存在性校验（秒级）
D:\python\python.exe -X utf8 verify_models.py --no-hash
# 完整 SHA 校验（约 7GB，1~3 分钟）
D:\python\python.exe -X utf8 verify_models.py
```

```python
# 向量模型
from sentence_transformers import SentenceTransformer
m = SentenceTransformer(r"models/Qwen3-Embedding-0.6B", device="cpu", local_files_only=True)
print(m.get_sentence_embedding_dimension())  # 1024

# 子句对齐编码器
from transformers import AutoModel, AutoTokenizer
tok = AutoTokenizer.from_pretrained(r"models/mdeberta-v3-base", local_files_only=True)
mdl = AutoModel.from_pretrained(r"models/mdeberta-v3-base", local_files_only=True).eval()

# 生成模型（CPU fp16，约 20s 加载 / 24 tokens 约 37s）
import torch
from transformers import AutoModelForImageTextToText
tk = AutoTokenizer.from_pretrained(r"models/Qwen3.5-2B", local_files_only=True)
mdl = AutoModelForImageTextToText.from_pretrained(
    r"models/Qwen3.5-2B", local_files_only=True, dtype=torch.float16, low_cpu_mem_usage=True).eval()
```

---

## 4. 已知限制与说明

1. **Qwen3.5-2B 是多模态后训练模型**（`Qwen3_5ForConditionalGeneration`），本包仅使用文本通道；
   CPU 推理可用但较慢，生产建议 GPU；未安装 `causal_conv1d` / `flash-linear-attention` 时走参考实现（本次验证即如此）。
2. **mDeBERTa 只下载推理必需文件**：未包含 `tf_model.h5`、`pytorch_model.generator.bin`、`configuration.json`（约省 2 GiB）。
3. **阈值未随模型自动重标定**：正式版沿用交付冻结阈值；模型更换后语义分分布会变化，业务侧需在验收数据上复评阈值（算子内已保留同一配置键）。
4. **旧模型不删除**：`labse`、`xlm-roberta-base`、`bge-m3*`、`Qwen2.5-1.5B-Instruct`、`nllb-200-distilled-600M`、
   `bge-domain-prototype` 保留在目录中但默认链路不再加载；`verify_models.py` 将其标为 `[LEGACY]` 仅提示。
5. **API20/API23 依赖网络与密钥**：`SILICONFLOW_API_KEY` 只能通过环境变量或部署平台注入，**不要写入仓库文件**。
6. 全程离线校验模型完整性；未使用网络下载以外的外部服务。

---

## 5. 复核命令

```powershell
cd G:\HYQX\算子\平行语料\PRLt_V1.0
D:\python\python.exe -X utf8 verify_models.py --no-hash
D:\python\python.exe -X utf8 verify_models.py
```

退出码：0 = 正式版必需项全部通过；1 = 存在缺失或 SHA 不符。
