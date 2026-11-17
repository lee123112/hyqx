"""算子 20 业务处理模块（CLI 入口）。

本算子属跨模态规则型质检，业务规则集中在 ``utils/alignment_qc.py``；
本文件作为**业务处理模块与 CLI 入口**，同时把核心可复用函数重新导出，
便于工程侧与测试直接调用（不重复实现任何逻辑）。

用法::

    python processor.py --input dataset/sample/demo.jsonl \
        --unique-id smoke_cli --output-root output_smoke

    python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli \
        --output-root output_smoke \
        --override reverse_verify.enabled=false \
        --override quality_subset_fallback=0.2
"""
from __future__ import annotations

import sys
from typing import List, Optional

import inference
from inference import (DependencyError, InputError, build_intermediate_lineage,
                       build_stage_payload, run, stage_file_object)
from utils import alignment_qc as qc
from utils import media_probe, reverse_verify, schemas, transport
from utils import upstream_metrics as um
from utils.io import JsonlWriter, validate_unique_id
from utils.media_carry import MediaCarrier

# 业务函数重新导出（单一实现来源：utils/alignment_qc.py）
evaluate_record = qc.evaluate_record
resolve_modalities = qc.resolve_modalities
default_business = qc.default_business
build_quality_report = qc.build_quality_report
build_supplement_list = qc.build_supplement_list
RecordOutcome = qc.RecordOutcome


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 入口：与 HTTP ``/predict`` 共用 ``inference.run``。"""
    return inference.main(argv)


if __name__ == "__main__":
    sys.exit(main())
