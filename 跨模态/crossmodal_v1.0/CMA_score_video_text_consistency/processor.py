#!/usr/bin/env python
"""算子 14 命令行入口（表1 #14 评分视文语义一致性）。

与 HTTP 完全同一套实现：``processor.py`` 只是 ``inference.main`` 的薄壳，
保证 CLI 与 API 的返回口径、统计口径、血缘结构一致。

示例：

    # 默认关闭路线：原样透传，不加载模型、不生成视文产物
    python processor.py --input dataset/sample/demo.jsonl \
        --unique-id smoke_cli --output-root output_smoke

    # 启用路线（需要本地 CLIP-ViT/B-32 权重）
    python processor.py --input dataset/sample/demo.jsonl \
        --unique-id smoke_enabled --output-root output_smoke \
        --enabled --clip-weights /workdir/weight/clip-vit-b-32

    # 阶段末算子：返回最终文件内容对象 + 完整阶段血缘
    python processor.py --input <上一节点 outputFile> --unique-id smoke_cli \
        --is-last-operator
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import inference  # noqa: E402


def main(argv=None) -> int:
    return inference.main(argv)


if __name__ == "__main__":
    sys.exit(main())
