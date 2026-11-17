"""算子 22「生成跨模态Token」工具层。

模块划分：
- ``names``      产物名与业务常量（唯一来源）
- ``io``         路径安全、流式 UTF-8 读取、原子写入、任务锁、安全解压
- ``logger``     逐行运行日志（时间 / 动作 / 真实数量）
- ``lineage``    血缘、阶段上下文、配置快照
- ``transport``  ``crossmodal-files-v1`` 文件内容对象与运行清单
- ``schemas``    严格配置模型与请求模型
- ``upstream``   上游 #19/#20/#21/#16/#17/#18 结论的取值与归一化（只读）
- ``tokens``     ★业务规则：表1 #22 模态标记/路线分文件/台账判定
"""
from __future__ import annotations

__all__ = ["io", "lineage", "logger", "names", "schemas", "tokens", "transport",
           "upstream"]
