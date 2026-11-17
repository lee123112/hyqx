"""Platform processor for SME_EVAL_BENCHMARK_001.

The class is loaded by the root service through ``map.json``.  It deliberately
delegates to the shared FastAPI implementation so the package and the pooled
service always use the same evaluation rules.
"""
from __future__ import annotations

from common.eval_adapter import EvaluationOperator


class BenchmarkEvaluationOperator(EvaluationOperator):
    operator_id = "SME_eval_benchmark"
    package = "SME_EVAL_BENCHMARK_001"
    eval_type = "benchmark"
