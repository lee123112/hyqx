"""Platform processor for SME_EVAL_AUDIO_001.

The class is loaded by the root service through ``map.json``.  It deliberately
delegates to the shared FastAPI implementation so the package and the pooled
service always use the same evaluation rules.
"""
from __future__ import annotations

from common.eval_adapter import EvaluationOperator


class AudioEvaluationOperator(EvaluationOperator):
    operator_id = "SME_eval_audio"
    package = "SME_EVAL_AUDIO_001"
    eval_type = "audio"

    def run_protocol(self, spec):
        """Dataset-level training comparison, separate from frozen /predict."""
        if isinstance(spec, dict):
            return super().run_protocol(spec)
        from app.experiments.audio_protocol import run_audio_protocol
        return run_audio_protocol(spec)
