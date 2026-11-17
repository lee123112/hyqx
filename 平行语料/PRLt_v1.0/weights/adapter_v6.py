"""Primary engineering baseline for operator 15 (v6, evaluation-only).

This explicit entry point prevents process environment or directory-level
``adapter.py`` loading from selecting an older calibration by accident.
The v6 artifact is frozen and remains unapproved for formal business release.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path


HERE = Path(__file__).resolve().parent
CALIBRATION_PATH = (
    HERE.parent / "runs" / "run_20261010_wmt_mqm_qwen_calibration_v6" /
    "fluency_calibration_v2.json"
)
if not CALIBRATION_PATH.is_file():
    raise RuntimeError("missing_frozen_v6_fluency_calibration")
_calibration = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
if _calibration.get("business_gate_approved") is not False:
    raise RuntimeError("v6_business_gate_contract_changed")
os.environ["QWEN_FLUENCY_CALIBRATION"] = str(CALIBRATION_PATH)

_spec = importlib.util.spec_from_file_location("qwen_fluency_v6_engine", HERE / "adapter.py")
if _spec is None or _spec.loader is None:
    raise RuntimeError("cannot_load_qwen_fluency_v6_engine")
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)

MODEL_VERSION = (
    "Qwen2.5-1.5B-Instruct-local-fluency-rubric-v2+"
    "qwen2.5-1.5b-wmt-mqm-evidence-logistic-isotonic-v1[v6-baseline]"
)
RUBRIC_VERSION = _engine.RUBRIC_VERSION
DEVICE = _engine.DEVICE
STATUS = "evaluation_only"
BUSINESS_GATE_APPROVED = False


def score_fluency_batch(texts, target_lang: str = "en") -> list:
    outputs = _engine.score_fluency_batch(texts, target_lang)
    for payload in outputs:
        payload["model_version"] = MODEL_VERSION
        payload["business_gate_approved"] = False
    return outputs


def score_fluency(text: str, target_lang: str = "en") -> dict:
    payload = _engine.score_fluency(text, target_lang)
    payload["model_version"] = MODEL_VERSION
    payload["business_gate_approved"] = False
    return payload
