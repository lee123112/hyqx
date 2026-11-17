"""CLI 入口（与 ``inference.py`` 同源）。

用法示例::

    python processor.py --input dataset/sample/demo.jsonl \
        --unique-id smoke_cli --output-root output_smoke

退出码：``0`` 成功/待人工，``1`` 运行状态为 failed，``2`` 输入或配置错误。
"""
from __future__ import annotations

import sys

from inference import main

__all__ = ["main"]

if __name__ == "__main__":                     # pragma: no cover
    sys.exit(main())
