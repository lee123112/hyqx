"""算子 #15 评分时空一致性 命令行入口（与项目统一 CLI 约定一致）。

用法（示例，参数以本算子 ``inference.py`` 的 parser 为准）：
    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke

实际逻辑在 ``inference.main``；本文件只是稳定 CLI 入口，便于工程侧批量脚本
与其他算子保持一致的调用习惯（HTTP 入口仍为 ``app.py``）。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from inference import build_parser, default_config_path, main  # noqa: E402

__all__ = ["build_parser", "default_config_path", "main"]

if __name__ == "__main__":
    sys.exit(main())
