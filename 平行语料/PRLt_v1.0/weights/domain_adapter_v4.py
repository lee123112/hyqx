"""Two-stage calibrated BGE-M3 domain adapter for operator 16.

The BGE-M3 encoder is frozen.  A scope classifier first predicts allowed,
out_of_scope, or unknown.  Allowed records are then classified into one of
five supported domains.  Both classifiers are multinomial logistic
regressions trained on Domain-Eval-v3 Development embeddings and temperature
scaled on its document-disjoint Calibration partition.

The probabilities are calibrated against silver proxy labels.  They are
model evidence for engineering evaluation, not human-Gold business accuracy.
"""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer


HERE = Path(__file__).resolve().parent
MODEL_PATH = Path(os.environ.get("BGE_M3_MODEL_PATH", str(HERE.parent / "models" / "bge-m3-safetensors")))
CLASSIFIER_PATH = Path(os.environ.get(
    "BGE_DOMAIN_CLASSIFIER_PATH",
    str(HERE.parent / "models" / "bge-domain-prototype" / "two_stage_domain_classifier_v2.npz"),
))
DEVICE = os.environ.get("BGE_M3_DEVICE", "cuda")
MODEL_VERSION = "BGE-M3-frozen-two-stage-logistic-v2.0"
EXPECTED_CLASSIFIER_SHA256 = "e424898e4d526b2bf1ce7c5c286523341eb1e63facb46b4cdd34d8baee8f7fb3"
BGE_WEIGHT_SHA256 = "9797a236553ed55f2b6d9a7579cf343697041fe16184596d7e7726890bd1ec95"
EXPECTED_THRESHOLD = 0.8
EXPECTED_DIMENSION = 1024
SCOPE_CLASSES = ("allowed", "out_of_scope", "unknown")
DOMAIN_CLASSES = ("medical", "legal", "finance", "technology", "education")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _softmax(logits: np.ndarray, temperature: float) -> np.ndarray:
    shifted = logits / temperature
    shifted -= float(np.max(shifted))
    weights = np.exp(shifted)
    return weights / float(np.sum(weights))


def _linear(vector: np.ndarray, coefficients: np.ndarray,
            intercept: np.ndarray) -> np.ndarray:
    return coefficients @ vector + intercept


@lru_cache(maxsize=1)
def _classifier() -> dict:
    if not CLASSIFIER_PATH.is_file():
        raise FileNotFoundError(f"Domain classifier not found: {CLASSIFIER_PATH}")
    artifact_sha256 = _sha256(CLASSIFIER_PATH)
    if artifact_sha256 != EXPECTED_CLASSIFIER_SHA256:
        raise ValueError("Domain classifier SHA256 does not match the frozen artifact")

    with np.load(CLASSIFIER_PATH, allow_pickle=False) as artifact:
        payload = {key: artifact[key] for key in artifact.files}
    if tuple(payload["scope_classes"].tolist()) != SCOPE_CLASSES:
        raise ValueError("Unexpected scope class order")
    if tuple(payload["domain_classes"].tolist()) != DOMAIN_CLASSES:
        raise ValueError("Unexpected domain class order")
    if payload["scope_coef"].shape != (len(SCOPE_CLASSES), EXPECTED_DIMENSION):
        raise ValueError("Unexpected scope classifier dimensions")
    if payload["domain_coef"].shape != (len(DOMAIN_CLASSES), EXPECTED_DIMENSION):
        raise ValueError("Unexpected domain classifier dimensions")
    threshold = float(payload["threshold"][0])
    if not np.isclose(threshold, EXPECTED_THRESHOLD):
        raise ValueError("Frozen classifier threshold is not 0.8")
    payload["artifact_sha256"] = artifact_sha256
    payload["threshold_value"] = threshold
    return payload


@lru_cache(maxsize=1)
def _model_and_classifier():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    classifier = _classifier()
    weight_path = MODEL_PATH / "model.safetensors"
    if not weight_path.is_file() or _sha256(weight_path) != BGE_WEIGHT_SHA256:
        raise ValueError("BGE-M3 model.safetensors SHA256 does not match the frozen weight")
    model = SentenceTransformer(str(MODEL_PATH), device=DEVICE, local_files_only=True)
    dimension_getter = getattr(model, "get_embedding_dimension", None)
    if dimension_getter is None:
        dimension_getter = model.get_sentence_embedding_dimension
    dimension = int(dimension_getter())
    if dimension != EXPECTED_DIMENSION:
        raise ValueError(f"Unexpected BGE-M3 embedding dimension: {dimension}")
    return model, classifier


def classify_domain(source: str, target: str, language_pair: str = "zh-en") -> dict:
    model, classifier = _model_and_classifier()
    vector = model.encode(
        [f"{source}\n{target}"],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
        batch_size=1,
    )[0].astype(np.float32, copy=False)
    if vector.shape != (EXPECTED_DIMENSION,) or not np.isfinite(vector).all():
        raise ValueError("BGE-M3 returned an invalid embedding")

    scope_logits = _linear(
        vector, classifier["scope_coef"], classifier["scope_intercept"])
    domain_logits = _linear(
        vector, classifier["domain_coef"], classifier["domain_intercept"])
    scope_temperature = float(classifier["scope_temperature"][0])
    domain_temperature = float(classifier["domain_temperature"][0])
    scope_probabilities = _softmax(scope_logits, scope_temperature)
    domain_probabilities = _softmax(domain_logits, domain_temperature)

    scope_index = int(np.argmax(scope_probabilities))
    scope = SCOPE_CLASSES[scope_index]
    domain_index = int(np.argmax(domain_probabilities))
    predicted_domain = DOMAIN_CLASSES[domain_index]

    if scope == "allowed":
        domain = predicted_domain
        confidence = float(domain_probabilities[domain_index])
        confidence_semantics = "silver_calibrated_allowed_domain_probability"
    elif scope == "out_of_scope":
        domain = "out_of_scope"
        confidence = float(scope_probabilities[scope_index])
        confidence_semantics = "silver_calibrated_scope_probability"
    else:
        domain = "unknown"
        confidence = float(scope_probabilities[scope_index])
        confidence_semantics = "silver_calibrated_scope_probability"

    domain_order = np.argsort(-domain_probabilities)
    candidates = [
        {
            "domain": DOMAIN_CLASSES[int(index)],
            "subdomain": None,
            "confidence": float(domain_probabilities[int(index)]),
        }
        for index in domain_order
    ]
    if domain in {"out_of_scope", "unknown"}:
        candidates.insert(0, {
            "domain": domain,
            "subdomain": None,
            "confidence": confidence,
        })

    evidence = [{
        "type": "frozen_bge_m3_two_stage_logistic_decision",
        "language_pair": language_pair,
        "scope_prediction": scope,
        "scope_probabilities": {
            label: float(scope_probabilities[index])
            for index, label in enumerate(SCOPE_CLASSES)
        },
        "domain_prediction": predicted_domain,
        "domain_probabilities": {
            label: float(domain_probabilities[index])
            for index, label in enumerate(DOMAIN_CLASSES)
        },
        "domain_confidence_semantics": confidence_semantics,
        "scope_temperature": scope_temperature,
        "domain_temperature": domain_temperature,
        "acceptance_threshold": classifier["threshold_value"],
        "classifier_sha256": classifier["artifact_sha256"],
        "bge_weight_sha256": BGE_WEIGHT_SHA256,
        "training_label_quality": "silver_proxy_and_dataset_level_proxy",
        "formal_business_accuracy": "blocked_no_human_item_level_domain_gold",
        "device": DEVICE,
    }]
    version = f"{MODEL_VERSION}(classifier_sha256={classifier['artifact_sha256'][:12]})"
    return {
        "domain": domain,
        "subdomain": None,
        "domain_confidence": confidence,
        "candidates": candidates,
        "evidence": evidence,
        "model_version": version,
    }
