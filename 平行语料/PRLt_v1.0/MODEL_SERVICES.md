# 当前模型与调用方式（保持原样）

| API | 用途 | 现有位置/调用方式 |
|---|---|---|
| 09 | 句对对齐 | models/Qwen3-Embedding-0.6B，本地加载 |
| 10 | 可选子句对齐 | models/mdeberta-v3-base，本地加载 |
| 11 | 领域识别/相关性 | models/Qwen3-Embedding-0.6B/domain_adapter.py，原原型和评分 |
| 14 | 语义等价/实体复核 | 本地Qwen3-Embedding-0.6B及原Qwen3.5-2B判别器 |
| 15 | 流畅度 | models/Qwen3.5-2B/adapter.py，本地加载 |
| 22 | 可选句式变换 | models/Qwen3.5-2B，本地加载 |
| 20、23 | 可选回译/跨语言增强 | 保留原硅基流动Qwen3.5-9B API配置 |
| 21 | 同义词替换 | PRLt_synonym_substitution/configs/general_synonyms.json，原词库保留 |

本次整理未更换模型、模型调用后端、提示词、适配器、词库或阈值；不引入IDG网关或入站X-Token认证。`weights/`内的原兼容适配器也保留。

模型根目录继续支持原 `MODEL_ROOT` 环境变量；正常运行默认使用本包 `models/`。Docker继续只读挂载 `./models:/opt/pretrain-parallel-corpus/models`。

模型来源与原有逐文件溯源记录仍在 `models/NEW_MODELS_PROVENANCE.json`、`models/LOCAL_RECONSTRUCTION.json` 和各模型目录，内容保持不变。本次整理另外对全部模型文件做整理前后SHA256比对，结果保存在包外整理验证记录中。

默认最小联调链只使用本地模型，无需调用20/23增强服务。模型依赖沿用原requirements，当前本地验证使用外置Python3.12.14环境和原有库。