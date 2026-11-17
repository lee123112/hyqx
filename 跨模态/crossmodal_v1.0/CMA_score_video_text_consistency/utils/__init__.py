"""算子 14「评分视文语义一致性」内部工具包。

本包**只**服务于本算子目录，不跨目录 import 兄弟算子（各算子自带一份实现，
便于独立打包与版本冻结）。模块划分：

- ``schemas``：配置与请求模型（Pydantic v2，严格布尔 + 定义域校验）；
- ``frames``：视频帧/语义段的纯逻辑（选帧上限、分段、均值聚合），不依赖模型；
- ``scoring``：余弦相似度与阈值判定等纯算法；
- ``backends``：真实 CLIP-ViT/B-32 适配器（只加载本地权重，不联网）；
- ``video_text``：表1 #14 业务模块（解析 → 分段 → 逐帧评分 → 段均值判定）；
- ``io``：流式 JSONL、任务目录、路径安全、任务锁、安全解压；
- ``lineage``：血缘记录与阶段上下文；
- ``logger``：规范日志；
- ``transport``：``crossmodal-files-v1`` 文件内容对象与运行清单。
"""
from __future__ import annotations

__all__ = [
    "schemas", "frames", "scoring", "backends", "video_text",
    "io", "lineage", "logger", "transport",
]
