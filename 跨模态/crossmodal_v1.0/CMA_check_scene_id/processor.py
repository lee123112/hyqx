"""CLI 入口薄封装：业务编排在 ``inference.py`` / ``utils.scene``。

``DEVELOPMENT_PLAN.md`` §3 约定的入口是 ``inference.py``（内部含 ``run`` 与 CLI）；
本文件按项目通用提示词的统一 CLI 约定（``python processor.py --input ...``）转发一层，
便于批量脚本与并行开发模板复用同一命令。
"""
from __future__ import annotations

import sys

from inference import main

if __name__ == "__main__":
    sys.exit(main())
