# M18 联调兼容修复（2026-11-09）

补读 `image_reference.path`。M11 配对记录改为非空深合并，避免左右侧的 `null`、空字符串、空列表、空字典或空 `processing` 遮住真实媒体及根级 M17 候选框；保留原配对标识选择方式。

优先使用明确的图像引用与 M17 `paths.image`。M17 通用 `path` 的已知音视频扩展名不能作为图片；无 typed paths 的旧版未知扩展图像引用继续兼容。有 typed paths 时未知扩展不猜类型。无图视频仍可从 M17 关键帧取得裁图源。

保留原优化：分词停用词/数词/量词/词性过滤，词与短语变体，裁图扩边和背景抑制，类别 `prior` 校准及 `assignment_score`；`clip_score` 和阈值仍按原始余弦，校准只影响一对一分配。相关打分、分词、裁图函数与配置均未改动。

验证：新增 14 条无权重回归；`python -X utf8 -m pytest tests -q -ra --ignore=tests/test_clip_plumbing.py` 为 **181 passed / 62 skipped**。跳过项为已停用的 v4.2 血缘/运输测试；本轮按要求不跑 CLIP architecture-only 前向文件，不加载真实 CLIP 权重。
