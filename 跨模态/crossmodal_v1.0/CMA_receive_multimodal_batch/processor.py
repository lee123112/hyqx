"""算子 01「接入多模态批次」命令行入口（``inference.main`` 的薄封装）。

用法::

    # 阶段内（中间）形态：data 只含 lineageFile（含临时 outputFile）
    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \
        --output-root output_smoke

    # 阶段末形态：data 含 outputFile 文件对象 + 完整阶段血缘数组
    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \
        --output-root output_smoke --is-last-operator

    # 业务覆盖（仅允许 enabled / file_check_mode / dedupe / check_source_modality）
    python processor.py --input ... --unique-id ... --override file_check_mode=strict
    python processor.py --input ... --unique-id ... --override enabled=false

    # 生产口径：禁止读取任务目录之外的输入
    python processor.py --input ... --unique-id ... --strict-input
"""
from __future__ import annotations

import sys

from inference import main

if __name__ == "__main__":
    sys.exit(main())
