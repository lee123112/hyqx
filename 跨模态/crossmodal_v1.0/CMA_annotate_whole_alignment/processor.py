"""算子 #16 CLI 入口（与 DEVELOPMENT_PLAN.md §3 一致，业务实现见 inference.py）。

用法：
    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import inference  # noqa: E402

if __name__ == "__main__":
    sys.exit(inference.main())
