# 模型与适配器（正式版 · 2026-10-31）

模型权重统一放在**模型根目录**，代码按以下顺序解析：环境变量 `MODEL_ROOT` → 包内
`models/`（交付机只读挂载点）→ 开发验证机共享目录 `G:\HYQX\算子\model`。

> **2026-10-31 正式版模型升级**：09/14 改用 Qwen3-Embedding-0.6B，10 改用 mDeBERTa-v3-base，
> 11 改用 Qwen3-Embedding-0.6B zero-shot 原型分类，15/22 改用 Qwen3.5-2B（本地），
> 20/23 改用硅基流动 Qwen3.5-9B（远程 API）。旧版模型（LaBSE/XLM-R/BGE-M3/Qwen2.5/NLLB）
> 保留用于追溯，默认链路不再加载；`weights/adapter_v6.py`、`weights/domain_adapter_v4.py`
> 为旧冻结契约副本，仅作历史参考。

## models/ 目录（正式版交付布局）

- `models/Qwen3-Embedding-0.6B`（API09 句子级对齐、API11 领域识别、API14 语义等价；1024 维）
- `models/mdeberta-v3-base`（API10 子句/短语级对齐；278M 参数，768 维，含本地 fast tokenizer）
- `models/Qwen3.5-2B`（API15 译文流畅度、API22 句式变换；含开发侧适配器 `adapter.py`）
- API20/API23：硅基流动 `Qwen/Qwen3.5-9B`，不落盘权重

旧版目录（已退役，仅追溯）：`models/labse`、`models/xlm-roberta-base`、`models/bge-m3`、
`models/bge-m3-safetensors`、`models/bge-domain-prototype`、`models/Qwen2.5-1.5B-Instruct`、
`models/nllb-200-distilled-600M`。

交付/部署前必须校验：`python verify_models.py`。
`[OK]` 为与 `models/NEW_MODELS_PROVENANCE.json` 实测 SHA256 一致；`[LOCAL]` 为开发侧生成的
适配器/原型文件（登记在 `models/LOCAL_RECONSTRUCTION.json`，不能冒充原厂冻结件）；
`[LEGACY]` 为旧模型，仅提示。

## 运行要求

- 向量/编码器：`sentence-transformers>=6.2`（Qwen3-Embedding）、`transformers>=5.22`（mDeBERTa/Qwen3.5）；
- 生成模型：Qwen3.5-2B 为多模态后训练架构（`Qwen3_5ForConditionalGeneration`），仅使用文本通道，
  生产建议 GPU；CPU 可运行但较慢；
- 20/23 需要环境变量 `SILICONFLOW_API_KEY`，无 Key 时结构化失败，不伪造译文；
- `models/` 缺失或校验失败时，09/10/11/14/15 按各自 `fallback_policy` 处理
  （本交付默认模型算子为 `fail`：结构化失败，不静默降级、不伪造分数）；
- 01–08、12–13、16–19、21 不依赖模型，可独立运行。
