"""算子 #12 命令行入口（与 DEVELOPMENT_PLAN.md 约定一致）。

用法：
    python processor.py --input dataset/sample/pairs.jsonl --unique-id smoke_cli --output-root output_smoke

实际逻辑在 ``inference.main``，本文件只作为稳定 CLI 入口，便于工程侧与
其他算子保持一致的调用习惯。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from inference import build_parser, main  # noqa: E402

__all__ = ["main", "build_parser"]

if __name__ == "__main__":
    sys.exit(main())
