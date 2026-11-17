"""算子 25「管理人工复核任务」工具包。

模块划分
--------

- ``io``：流式 JSONL 读写、任务/运行目录、路径安全、任务锁与安全解压；
- ``logger``：规范日志（每行一条动作 + 真实数量）；
- ``lineage``：血缘记录、阶段上下文与阶段末血缘视图；
- ``transport``：``crossmodal-files-v1`` 文件内容对象与运行清单；
- ``schemas``：部署配置、业务覆盖与 ``/predict`` 严格请求模型；
- ``review``：人工复核结论解析（只来自输入证据，绝不自行生成结论）；
- ``dispatch``：本算子业务模块（类别判定、派单预算、结论回写、隔离与守恒）。

本包由算子 25 在本目录内自带，不跨目录 import 兄弟算子。
"""

from . import dispatch, io, lineage, logger, review, schemas, transport  # noqa: F401

__all__ = ["dispatch", "io", "lineage", "logger", "review", "schemas", "transport"]
