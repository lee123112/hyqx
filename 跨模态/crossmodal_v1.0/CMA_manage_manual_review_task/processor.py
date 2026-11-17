"""算子 25 命令行入口（``inference.main`` 的薄封装）。

用法::

    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \
        --output-root output_smoke

    # 阶段末形态
    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \
        --output-root output_smoke --is-last-operator

    # 手动触发（表1 #25 默认关闭，需显式开启）
    python processor.py --input ... --unique-id ... --override enabled=true

    # 业务覆盖（仅允许 enabled / auto_dispatch / manual_platform_available /
    # require_reviewer_identity / allow_degraded）
    python processor.py --input ... --unique-id ... --override enabled=true \
        --override manual_platform_available=true
"""
from __future__ import annotations

import sys

from inference import main

if __name__ == "__main__":
    sys.exit(main())
