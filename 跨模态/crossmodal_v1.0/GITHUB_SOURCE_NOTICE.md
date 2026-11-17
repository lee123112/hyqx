# GitHub 源码副本说明

本目录于 2026-11-17 从指定部署包整理。源码、配置、依赖、文档、测试与样例保持原样；模型权重、真实 `.env`、缓存和日志未上传。

原 README 与交付验收/文件清单描述的是完整部署包，可能包含“权重已内置”等表述。GitHub 副本以本说明和仓库根目录清单为准；历史验收不代表缺少权重的副本可以直接运行。

## 部署前恢复

1. 如存在 `.env.example`，复制为 `.env` 并配置部署参数。
2. 将以下权重从原部署包或可信模型源恢复到同名相对路径；保留的分词器、词表与模型配置仍需匹配原权重。
3. 权重补齐后，按原 README 执行包校验、启动和健康检查。

| 未上传权重相对路径 | 大小 MiB |
|---|---:|
| `CMA_align_semantic_unit/weight/whisper-base/model.safetensors` | 276.95 |
| `CMA_annotate_local_to_local/weight/clip/ViT-B-32.pt` | 337.58 |
| `CMA_annotate_local_to_whole/weight/YOLOv8n/yolov8n.pt` | 6.23 |
| `CMA_annotate_whole_alignment/weight/clip/ViT-B-32.pt` | 337.58 |
| `CMA_check_alignment_quality/weight/clip/ViT-B-32.pt` | 337.58 |
| `CMA_score_image_text_consistency/weight/YOLOv8n/yolov8n.pt` | 6.23 |
| `CMA_score_image_text_consistency/weight/clip/ViT-B-32.pt` | 337.58 |
| `CMA_score_video_text_consistency/weight/clip/ViT-B-32.pt` | 337.58 |
