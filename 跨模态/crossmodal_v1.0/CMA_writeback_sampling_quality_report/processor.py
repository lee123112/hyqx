"""算子 24 命令行入口（``inference.main`` 的薄封装）。

用法::

    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \
        --output-root output_smoke

    # 阶段末形态
    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \
        --output-root output_smoke --is-last-operator

    # 业务覆盖（仅允许 sample_rate / sampling_salt / report_source_names /
    # allow_degraded / require_reviewer_identity）
    python processor.py --input ... --unique-id ... --override sample_rate=0.20
"""
from __future__ import annotations

import sys

from inference import main

if __name__ == "__main__":
    sys.exit(main())
