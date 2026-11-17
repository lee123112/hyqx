"""算子 23 CLI 入口（与 HTTP 层共用同一套业务实现）。

用法::

    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \\
        --output-root output_smoke
    python processor.py --input <包目录>/samples.jsonl --unique-id t1 \\
        --is-last-operator            # 按阶段末形态输出（文件对象 + 完整阶段血缘）

退出码：0 = success/degraded（可解释完成），1 = failed（执行失败），2 = 输入或配置非法。
"""
from __future__ import annotations

import sys

from inference import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
