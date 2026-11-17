# GitHub 源码副本说明

本目录于 2026-11-17 从指定部署包整理。源码、配置、依赖、文档、测试与样例保持原样；模型权重、真实 `.env`、缓存和日志未上传。

原 README 与交付验收/文件清单描述的是完整部署包，可能包含“权重已内置”等表述。GitHub 副本以本说明和仓库根目录清单为准；历史验收不代表缺少权重的副本可以直接运行。

## 部署前恢复

1. 如存在 `.env.example`，复制为 `.env` 并配置部署参数。
2. 将以下权重从原部署包或可信模型源恢复到同名相对路径；保留的分词器、词表与模型配置仍需匹配原权重。
3. 权重补齐后，按原 README 执行包校验、启动和健康检查。

| 未上传权重相对路径 | 大小 MiB |
|---|---:|
| `SME_EVAL_AUDIO_001/models/paraformer-zh/model.pt` | 943.91 |
| `SME_EVAL_AUDIO_001/models/whisper-base/model.safetensors` | 276.95 |
| `SME_EVAL_AUDIO_001/models/whisper-small/model.safetensors` | 922.20 |
| `SME_EVAL_IMAGE_001/models/mobilenet_v2/mobilenet_v2-b0353104.pth` | 13.55 |
| `SME_EVAL_IMAGE_001/models/resnet18/resnet18-f37072fd.pth` | 44.66 |
| `SME_EVAL_IMAGE_001/models/resnet50/resnet50-0676ba61.pth` | 97.78 |
| `SME_EVAL_TEXT_001/models/bert-base-chinese/model.safetensors` | 392.49 |
| `SME_EVAL_VIDEO_001/models/clip/ViT-L-14.pt` | 889.56 |
