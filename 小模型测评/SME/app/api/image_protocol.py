"""Dataset-level image quality protocol endpoint.

The per-image ``/predict`` path remains the compatibility proxy path.  This
endpoint runs the audited MLPerf-Tiny-style comparison over three class-folder
directories and returns the protocol record, including OOD and leakage audits.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body

from ..core.config import settings
from ..core.envelope import ApiError, ok
from ..experiments.image_protocol import (
    BENCHMARKS,
    MODELS,
    TRAINING_MODES,
    ImageProtocolSpec,
    TORCHVISION_MODELS,
    run_image_protocol,
)

router = APIRouter(prefix="/api/v1/eval", tags=["image-protocol"])


def _bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(422, "REQUEST_INVALID", f"{key} 必须为布尔值")
    return value


def _int(payload: dict[str, Any], key: str, default: int | None) -> int | None:
    value = payload.get(key, default)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ApiError(422, "REQUEST_INVALID", f"{key} 必须为整数")
    return value


def _float(payload: dict[str, Any], key: str, default: float) -> float:
    value = payload.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ApiError(422, "REQUEST_INVALID", f"{key} 必须为数值")
    return float(value)


def _allowed_roots() -> list[Path]:
    roots = {
        settings.project_root.resolve(),
        settings.project_root.parent.resolve(),
        settings.dataset_dir.resolve(),
        settings.data_dir.resolve(),
    }
    return sorted(roots, key=lambda item: len(str(item)))


def _inside_allowed(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in _allowed_roots())


def _resolve_dir(payload: dict[str, Any], key: str) -> Path:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(422, "REQUEST_INVALID", f"{key} 必须是非空路径")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = settings.project_root.parent / path
    path = path.resolve()
    if not _inside_allowed(path):
        raise ApiError(422, "REQUEST_INVALID", f"{key} 超出允许的本地数据目录")
    if not path.is_dir():
        raise ApiError(422, "DATASET_INVALID", f"{key} 目录不存在")
    return path


def _resolve_weight(payload: dict[str, Any], model: str) -> Path | None:
    raw = payload.get("modelWeight")
    if raw is not None and not isinstance(raw, str):
        raise ApiError(422, "REQUEST_INVALID", "modelWeight 必须是路径字符串或 null")
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = settings.project_root / path
        path = path.resolve()
        if not _inside_allowed(path):
            raise ApiError(422, "REQUEST_INVALID", "modelWeight 超出允许的本地目录")
        if not path.is_file():
            raise ApiError(422, "ASSET_MISSING", "modelWeight 文件不存在")
        return path
    from ..core.model_assets import image_weight
    return image_weight(model, required='modelWeight' not in payload)


@router.post("/image/protocol")
def run_image_quality_protocol(payload: dict[str, Any] = Body(...)) -> dict:
    model = payload.get("proxyModel", "resnet18")
    benchmark = payload.get("benchmark", "mlperftiny")
    training_mode = payload.get("trainingMode", "full")
    if model not in MODELS:
        raise ApiError(422, "CONFIG_INVALID", f"不支持的 proxyModel：{model}")
    if benchmark not in BENCHMARKS:
        raise ApiError(422, "CONFIG_INVALID", f"不支持的 benchmark：{benchmark}")
    if training_mode not in TRAINING_MODES:
        raise ApiError(422, "CONFIG_INVALID", f"不支持的 trainingMode：{training_mode}")

    seed = _int(payload, "seed", 42)
    epochs = _int(payload, "epochs", 10)
    batch_size = _int(payload, "batchSize", 32)
    image_size = _int(payload, "imageSize", 128)
    near_hamming = _int(payload, "nearDuplicateHamming", 5)
    max_ood = _int(payload, "maxOodSamples", 256)
    max_pairs = _int(payload, "maxLeakagePairs", 4_000_000)
    spec = ImageProtocolSpec(
        ours_dir=_resolve_dir(payload, "oursDir"),
        ref_dir=_resolve_dir(payload, "refDir"),
        eval_dir=_resolve_dir(payload, "evalDir"),
        proxy_model=model,
        model_weight=_resolve_weight(payload, model),
        benchmark=benchmark,
        task=payload.get("task", "classification"),
        training_mode=training_mode,
        seed=42 if seed is None else seed,
        epochs=10 if epochs is None else epochs,
        batch_size=32 if batch_size is None else batch_size,
        learning_rate=_float(payload, "learningRate", 1e-3),
        image_size=128 if image_size is None else image_size,
        max_per_class=_int(payload, "maxPerClass", None),
        augment=_bool(payload, "augment", True),
        near_duplicate_hamming=5 if near_hamming is None else near_hamming,
        max_label_jsd=_float(payload, "maxLabelJSD", 0.25),
        max_feature_mmd=_float(payload, "maxFeatureMMD", 0.35),
        max_ood_samples=256 if max_ood is None else max_ood,
        max_leakage_pairs=4_000_000 if max_pairs is None else max_pairs,
        strict=_bool(payload, "strict", True),
        seeds=tuple(payload["seeds"]) if isinstance(payload.get("seeds"), list) else None,
        purpose=payload.get("purpose", "exploration"),
    )
    if "seeds" in payload and not isinstance(payload["seeds"], list):
        raise ApiError(422, "REQUEST_INVALID", "seeds 必须为整数数组")
    return ok(run_image_protocol(spec))
