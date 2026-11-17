# -*- coding: utf-8 -*-
"""算子运行上下文 ``OperatorContext``。

每个算子的 ``processor.py`` 只实现一个函数：

.. code-block:: python

    def run(ctx: OperatorContext) -> dict:
        ...

返回值约定（缺键即失败，服务层会校验）::

    {
      "outputs": {"main": Path, ...},   # 必填；键名即产物名，写进 lineage/statistics
      "statistics": {...},              # 必填；写进 lineage.statistics 与 data.statistics
      "result": {...},                  # 可选；原样放进 data.result 供后端读取
      "status": "success"               # 可选；success / partial / failed，默认 success
    }

``processor`` 内部**不碰 HTTP、不碰服务层**，因此可以 ``python processor.py --input`` 单跑，
也可以在测试里直接调 ``run(ctx)``。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Dict, Optional

from .logger import RunLog


@dataclasses.dataclass
class OperatorContext:
    """一次调用的全部入参。"""

    operator_id: str
    operator_name: str
    operator_version: str
    params: Dict[str, Any]
    unique_id: str
    work_dir: Path
    out_dir: Path
    log: RunLog
    input_file: Optional[Path] = None
    is_last: bool = False
    config: Dict[str, Any] = dataclasses.field(default_factory=dict)
    prior_lineage: Any = None

    # -- 便捷方法 ---------------------------------------------------------- #
    def out(self, filename: str) -> Path:
        """算子输出目录下的路径（自动建父目录）。"""
        p = Path(self.out_dir) / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def require_input(self) -> Path:
        """取必需的输入文件；缺失抛 ``missing_file``。"""
        from .schemas import ensure_exists

        return ensure_exists(self.input_file, "inputFile")

    def input_dir(self) -> Path:
        """输入文件所在目录（zip 解压场景下算子自己决定用哪个子目录）。"""
        p = self.require_input()
        return p if p.is_dir() else p.parent

    def subdir(self, name: str) -> Path:
        """工作目录下的临时子目录（放中间音频等大文件）。"""
        p = Path(self.work_dir) / "tmp" / name
        p.mkdir(parents=True, exist_ok=True)
        return p
