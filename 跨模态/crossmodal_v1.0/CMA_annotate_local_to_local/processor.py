"""算子 18「标注局部对局部」命令行入口（业务处理模块）。

用法::

    python processor.py --input dataset/sample/demo.jsonl \
        --unique-id smoke_cli --output-root output_smoke

默认沿用 ``configs/config.yaml``（``enabled: false``，工具关闭 → 原样放行）。
需要开启细粒度对应时加 ``--enable``；CLIP 未部署权重时加 ``--allow-degraded``
才会显式降级为「待人工」，否则严格失败（绝不伪造分数）。

实现集中在 ``inference.py``，本模块只做薄封装并转出业务函数，便于
``from processor import evaluate_record`` 之类的直接复用。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import inference  # noqa: E402
from utils.phrase_region import (  # noqa: E402,F401
    BoxError,
    DependencyError,
    attach_node,
    crop_rect,
    evaluate_record,
    normalize_box,
    resolve_candidate_boxes,
    resolve_description,
    resolve_image_reference,
    resolve_media_file,
    resolve_record,
)

__all__ = ["BoxError", "DependencyError", "attach_node", "crop_rect",
           "evaluate_record", "main", "normalize_box", "resolve_candidate_boxes",
           "resolve_description", "resolve_image_reference", "resolve_media_file",
           "resolve_record"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    return inference.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
