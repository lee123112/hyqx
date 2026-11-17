"""Image measurement engine (``type = "image"``).

The four image metrics are computed on the real pixels of each sample.  A
sample is expected to carry a local path, a business asset URL
(``/api/v1/datasets/.../assets/...``) or a public ``http(s)`` URL in
``uri``/``input``; missing or undecodable assets are an explicit
``ASSET_MISSING`` error in strict mode instead of a silent zero.

Because no labelled masks or supervised image model are bundled, the two
comparison metrics are deliberately documented as **proxies**:

``clarity`` (measured_rule)
    Grayscale Laplacian variance on the image resized so its longest side is
    ``ANALYSIS_MAX_SIDE`` pixels (reference scale), then
    ``clarity = 100 * var / (var + 500)`` clipped to 0–100.

``semantic`` (measured_rule)
    ``edge_density`` is the share of pixels whose Sobel magnitude exceeds
    ``EDGE_GRADIENT_THRESHOLD``; ``edge_norm = min(1, density / 0.15)`` and
    ``entropy_norm = H(gray) / 8``; finally
    ``semantic = 100 * (0.55 * edge_norm + 0.45 * entropy_norm)``.

``iou`` (proxy)
    The original image is compared with a deterministic JPEG re-encode
    (quality ``30 + seed % 11`` plus a light blur).  Both images are reduced to
    an Otsu foreground mask (the minority side is treated as the salient
    region); the score is the mask intersection-over-union in 0–1.  This is a
    *proxy for segmentation IoU*, not an annotated-mask IoU.

``accuracy`` (proxy)
    The original is compared with a lightly enhanced copy (autocontrast +
    unsharp mask).  A 64-dimensional colour/edge histogram feature vector is
    cosine-compared; the score is ``100 * cosine`` clipped to 0–100.  This is a
    *consistency proxy for scene-recognition accuracy*, not a classifier score.

``baseline`` measurements describe the untouched asset; ``candidate``
measurements describe the deterministically enhanced copy.  ``iou`` and
``accuracy`` measure process fidelity and are therefore identical on both
sides.
"""
from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from ..core.envelope import ApiError
from .base import BaseEngine, EngineResult
from .media import (
    AssetUnavailable,
    clamp,
    load_asset_bytes,
    missing_rows,
    round_metric,
    sample_asset_value,
    sample_dataset_id,
)

ANALYSIS_MAX_SIDE = 512
MASK_MAX_SIDE = 256
CLARITY_VAR_SCALE = 500.0
EDGE_GRADIENT_THRESHOLD = 24.0
EDGE_DENSITY_REFERENCE = 0.15
ENTROPY_MAX = 8.0
METRICS = ("iou", "clarity", "semantic", "accuracy")


def _analysis_image(image: Image.Image, max_side: int = ANALYSIS_MAX_SIDE) -> Image.Image:
    copy = image.copy()
    width, height = copy.size
    if max(width, height) > max_side:
        copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return copy


def _gray_float(image: Image.Image, max_side: int = ANALYSIS_MAX_SIDE) -> np.ndarray:
    return np.asarray(_analysis_image(image, max_side).convert("L"), dtype=np.float32)


def _laplacian_variance(gray: np.ndarray) -> float:
    if gray.ndim != 2 or gray.size == 0:
        return 0.0
    padded = np.pad(gray, 1, mode="edge")
    laplacian = (padded[:-2, 1:-1] + padded[2:, 1:-1]
                 + padded[1:-1, :-2] + padded[1:-1, 2:]
                 - 4.0 * padded[1:-1, 1:-1])
    return float(laplacian.var())


def clarity_score(image: Image.Image) -> float:
    """``100 * var / (var + 500)`` on the reference-scale Laplacian variance."""
    variance = _laplacian_variance(_gray_float(image))
    return round_metric(clamp(100.0 * variance / (variance + CLARITY_VAR_SCALE)))


def _gradient_magnitude(gray: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    return np.hypot(gx, gy)


def _entropy(gray: np.ndarray) -> float:
    values = np.clip(gray, 0, 255).astype(np.uint8).ravel()
    counts = np.bincount(values, minlength=256).astype(np.float64)
    total = counts.sum()
    if total <= 0:
        return 0.0
    probabilities = counts[counts > 0] / total
    return float(-(probabilities * np.log2(probabilities)).sum())


def semantic_score(image: Image.Image) -> float:
    """``100 * (0.55 * edge_norm + 0.45 * entropy_norm)`` as documented above."""
    gray = _gray_float(image)
    if gray.size == 0:
        return 0.0
    density = float((_gradient_magnitude(gray) > EDGE_GRADIENT_THRESHOLD).mean())
    edge_norm = min(1.0, density / EDGE_DENSITY_REFERENCE)
    entropy_norm = min(1.0, _entropy(gray) / ENTROPY_MAX)
    return round_metric(clamp(100.0 * (0.55 * edge_norm + 0.45 * entropy_norm)))


def _feature_vector(image: Image.Image) -> np.ndarray:
    rgb = np.asarray(_analysis_image(image).convert("RGB"), dtype=np.uint8)
    features: list[np.ndarray] = []
    for channel in range(3):
        histogram, _ = np.histogram(rgb[:, :, channel], bins=16, range=(0, 256))
        total = histogram.sum() or 1
        features.append(histogram.astype(np.float32) / total)
    gray = np.asarray(_analysis_image(image).convert("L"), dtype=np.float32)
    magnitude = np.clip(_gradient_magnitude(gray), 0, 255)
    histogram, _ = np.histogram(magnitude, bins=16, range=(0, 256))
    total = histogram.sum() or 1
    features.append(histogram.astype(np.float32) / total)
    vector = np.concatenate(features).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


def consistency_score(original: Image.Image, processed: Image.Image) -> float:
    """``100 * cosine`` between the colour/edge feature vectors."""
    cosine = float(np.dot(_feature_vector(original), _feature_vector(processed)))
    return round_metric(clamp(100.0 * cosine))


def _otsu_threshold(values: np.ndarray) -> int:
    counts = np.bincount(values.astype(np.uint8).ravel(), minlength=256).astype(np.float64)
    total = counts.sum()
    if total <= 0:
        return 128
    weighted_sum = float((np.arange(256, dtype=np.float64) * counts).sum())
    background_weight = 0.0
    background_sum = 0.0
    best_threshold = 128
    best_variance = -1.0
    for threshold in range(256):
        background_weight += counts[threshold]
        if background_weight <= 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight <= 0:
            break
        background_sum += threshold * counts[threshold]
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_sum - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (
            background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return best_threshold


def _saliency_mask(image: Image.Image) -> np.ndarray:
    """Otsu foreground mask on a small grayscale copy; minority is salient."""
    gray = np.clip(_gray_float(image, MASK_MAX_SIDE), 0, 255).astype(np.uint8)
    if gray.size == 0:
        return np.zeros((1, 1), dtype=bool)
    mask = gray > _otsu_threshold(gray)
    if mask.mean() > 0.5:
        mask = ~mask
    if mask.size and (mask.sum() < 0.005 * mask.size or mask.sum() > 0.995 * mask.size):
        magnitude = _gradient_magnitude(gray.astype(np.float32))
        cutoff = float(np.percentile(magnitude, 90))
        mask = magnitude >= cutoff
    return mask


def mask_iou(original: Image.Image, degraded: Image.Image) -> float:
    """IoU of Otsu saliency masks (0–1)."""
    first = _saliency_mask(original)
    second = _saliency_mask(degraded)
    if first.shape != second.shape:
        second_image = _analysis_image(degraded, MASK_MAX_SIDE).resize(
            (first.shape[1], first.shape[0]), Image.Resampling.BILINEAR)
        second = _saliency_mask(second_image)
    union = int(np.logical_or(first, second).sum())
    if union <= 0:
        return 0.0
    intersection = int(np.logical_and(first, second).sum())
    return round_metric(clamp(intersection / union, 0.0, 1.0))


def degraded_variant(image: Image.Image, seed: int) -> tuple[Image.Image, int]:
    """Deterministic light degradation used by the IoU proxy."""
    quality = 30 + (int(seed) % 11)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    with Image.open(buffer) as raw:
        decoded = raw.convert("RGB")
    sigma = 0.4 + (int(seed) % 3) * 0.1
    return decoded.filter(ImageFilter.GaussianBlur(radius=sigma)), quality


def enhanced_variant(image: Image.Image) -> Image.Image:
    """Deterministic light enhancement used by the accuracy proxy."""
    enhanced = ImageOps.autocontrast(image, cutoff=1)
    return enhanced.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))


def measure_image(image: Image.Image, seed: int = 42) -> dict[str, dict[str, float]]:
    """Compute baseline/candidate metrics from an already decoded RGB image."""
    degraded, _ = degraded_variant(image, seed)
    enhanced = enhanced_variant(image)
    iou = mask_iou(image, degraded)
    accuracy = consistency_score(image, enhanced)
    return {
        "baseline": {
            "clarity": clarity_score(image),
            "semantic": semantic_score(image),
            "iou": iou,
            "accuracy": accuracy,
        },
        "candidate": {
            "clarity": clarity_score(enhanced),
            "semantic": semantic_score(enhanced),
            "iou": iou,
            "accuracy": accuracy,
        },
    }


def _load_image(sample: dict[str, Any]) -> Image.Image:
    value = sample_asset_value(sample)
    payload, label = load_asset_bytes(value, dataset_id=sample_dataset_id(sample))
    try:
        with Image.open(io.BytesIO(payload)) as raw:
            raw.load()
            return ImageOps.exif_transpose(raw).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AssetUnavailable(f"图片无法解码：{label}") from exc


class ImageEngine(BaseEngine):
    type = "image"

    def measure(self, samples: list[dict[str, Any]], config: dict[str, Any], *,
                seed: int = 42, strict: bool = True) -> EngineResult:
        result = EngineResult(provenance={
            "iou": "proxy",
            "clarity": "measured_rule",
            "semantic": "measured_rule",
            "accuracy": "proxy",
        })
        result.notes.append(
            "图像 iou/accuracy 为代理测量（重编码显著性 IoU、颜色/边缘一致性），"
            "clarity/semantic 为固定规则公式；详见 ImageEngine docstring。")
        result.meta.update({
            "engine": "ImageEngine",
            "engineVersion": "image-proxy-v1",
            "analysisMaxSide": ANALYSIS_MAX_SIDE,
            "seed": seed,
            "sampleCount": len(samples),
            "degradation": {"jpegQuality": 30 + (int(seed) % 11),
                            "blurSigma": round(0.4 + (int(seed) % 3) * 0.1, 2)},
        })

        for index, sample in enumerate(samples):
            sample_id = str(sample.get("id") or f"S{index + 1:03d}")
            try:
                image = _load_image(sample)
            except AssetUnavailable as exc:
                detail = str(exc)
                rows = [
                    row
                    for metric in METRICS
                    for row in missing_rows(sample_id, metric, "ASSET_MISSING", detail)
                ]
                if strict:
                    raise ApiError(
                        422, "ASSET_MISSING",
                        f"样本 {sample_id} 的图片资产缺失或无法解码：{detail}",
                        {"sampleId": sample_id, "reason": "ASSET_MISSING"},
                    ) from exc
                result.missing.extend(rows)
                result.notes.append(f"样本 {sample_id} 跳过：{detail}")
                continue

            result.measurements[sample_id] = measure_image(image, seed=seed)

        return result
