"""算子 18「标注局部对局部」共享契约层。

模块划分：
- ``schemas``      配置与请求模型（严格校验，越界即失败）
- ``io``           路径安全、流式读取、原子写入、任务锁、安全解压
- ``transport``    接口文件内容对象与清单（跨模态共享格式）
- ``logger``       逐行运行日志（时间 / 动作 / 真实数量）
- ``lineage``      血缘与阶段上下文
- ``scoring``      余弦相似度、阈值判定、一对一贪心匹配
- ``tokenization`` 中文分词与「文件名不能当词」过滤
- ``phrase_region``该算子的业务规则（框规范化、词-区域关系、台账）
- ``backends``     CLIP-ViT/B-32 编码后端（复用 #12/#14 的同一实例与配方）
"""
from __future__ import annotations

__all__ = [
    "backends",
    "io",
    "lineage",
    "logger",
    "phrase_region",
    "schemas",
    "scoring",
    "tokenization",
    "transport",
]
