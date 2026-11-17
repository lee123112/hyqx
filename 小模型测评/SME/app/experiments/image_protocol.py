"""Image quality protocol modelled on the MLPerf Tiny classification recipe.

The protocol is deliberately dataset-level rather than per-image.  A caller
supplies three class-folder trees:

``D_ours``
    The candidate image dataset.
``D_ref``
    A reference dataset with the same classes and the same number of images.
``E``
    A fixed, held-out evaluation set that is never used for training or
    hyper-parameter selection.

The same proxy model, image-size, seed and training budget are used for both
sides.  The reported ``top1`` and ``macroF1`` are therefore model measurements,
but only under this proxy protocol; they are not paper values for DataComp,
CIFAR-10 or MLPerf Tiny.

Two guards run before training:

* leakage: exact SHA256 duplicates and average-hash near duplicates between
  the training splits and ``E`` are rejected;
* OOD: class-distribution Jensen-Shannon divergence and an RBF-kernel MMD of
  frozen backbone features are checked against configurable limits.

The image model/weights are optional dependencies and are never downloaded by
this module.  When ``torch``/``torchvision`` is absent, the audit helpers remain
importable and the training entry point returns an explicit 422.
"""
from __future__ import annotations

from .training_lock import training_locked

import hashlib
import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from ..core.envelope import ApiError
from ..core.ids import now

try:  # optional dependency
    import torch
    from torch import nn
    from torch.utils.data import DataLoader
    from torchvision import models as tv_models
    from torchvision import transforms as tv_transforms
except Exception:  # pragma: no cover - exercised when optional deps are absent
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    DataLoader = None  # type: ignore[assignment]
    tv_models = None  # type: ignore[assignment]
    tv_transforms = None  # type: ignore[assignment]

try:  # optional DeiT support; not required for ResNet/MobileNet
    import timm  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional
    timm = None  # type: ignore[assignment]

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _no_grad_decorator(function: Any) -> Any:
    """Keep the audit helpers importable when torch is not installed."""
    if torch is None:
        return function
    return torch.no_grad()(function)

BENCHMARKS = {"mlperftiny", "cifar10", "imagenet_subset", "datacomp_clip"}
TASKS = {"classification"}
TRAINING_MODES = {"full", "linear_probe"}
TORCHVISION_MODELS = {
    "resnet18": ("resnet18", "resnet18-f37072fd.pth"),
    "resnet50": ("resnet50", "resnet50-0676ba61.pth"),
    "mobilenet_v2": ("mobilenet_v2", "mobilenet_v2-b0353104.pth"),
}
MODELS = set(TORCHVISION_MODELS) | {"deit_tiny"}
METRICS = ("top1", "macroF1")


@dataclass(frozen=True)
class ImageProtocolSpec:
    ours_dir: Path
    ref_dir: Path
    eval_dir: Path
    proxy_model: str = "resnet18"
    model_weight: Path | None = None
    benchmark: str = "mlperftiny"
    task: str = "classification"
    training_mode: str = "full"
    seed: int = 42
    epochs: int = 10
    batch_size: int = 32
    learning_rate: float = 1e-3
    image_size: int = 128
    max_per_class: int | None = None
    augment: bool = True
    near_duplicate_hamming: int = 5
    max_label_jsd: float = 0.25
    max_feature_mmd: float = 0.35
    max_ood_samples: int = 256
    max_leakage_pairs: int = 4_000_000
    strict: bool = True
    seeds: tuple[int, ...] | None = None
    purpose: str = "exploration"


@dataclass(frozen=True)
class ImageRecord:
    path: Path
    rel: str
    split: str
    label: int
    sha256: str
    ahash: int


def _require_torch() -> None:
    if torch is None or nn is None or tv_models is None or tv_transforms is None:
        raise ApiError(422, "ENGINE_UNAVAILABLE",
                       "图像协议实验需要 torch + torchvision；当前环境未安装")


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    if torch is not None:
        torch.manual_seed(seed)


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _average_hash_bits(pixels: list[int], average: float) -> int:
    value = 0
    for index, pixel in enumerate(pixels):
        if pixel >= average:
            value |= 1 << index
    return value


def _average_hash(path: Path) -> int:
    """Dihedral-invariant 256-bit average hash.

    A plain 8x8 aHash misses a horizontal flip, even though ``full`` training
    itself uses random horizontal flips.  Taking the minimum over the eight
    dihedral transforms makes a flipped or rotated copy compare as identical.
    The 16x16 grid is deliberate: CIFAR-scale thumbnails have many coarse
    8x8 collisions across different classes, so the extra detail keeps
    cross-class false positives out while still catching re-encoded copies.
    """
    from PIL import Image

    try:
        with Image.open(path) as image:
            base = image.convert("L").resize((16, 16), Image.Resampling.BILINEAR)
            transforms = (
                None,
                Image.Transpose.FLIP_LEFT_RIGHT,
                Image.Transpose.FLIP_TOP_BOTTOM,
                Image.Transpose.ROTATE_180,
                Image.Transpose.ROTATE_90,
                Image.Transpose.ROTATE_270,
                Image.Transpose.TRANSPOSE,
                Image.Transpose.TRANSVERSE,
            )
            values: list[int] = []
            for operation in transforms:
                variant = base if operation is None else base.transpose(operation)
                pixels = list(variant.getdata())
                if len(pixels) != 256:
                    continue
                values.append(_average_hash_bits(pixels, sum(pixels) / len(pixels)))
    except Exception as exc:
        raise ApiError(422, "DATASET_INVALID",
                       f"图片无法解码或计算哈希：{path.name}") from exc
    if not values:
        raise ApiError(422, "DATASET_INVALID", f"无法计算图像哈希：{path.name}")
    return min(values)


def _records_for_split(root: Path, split: str,
                       max_per_class: int | None) -> tuple[list[ImageRecord], list[str]]:
    if not root.is_dir():
        raise ApiError(422, "DATASET_INVALID", f"{split} 目录不存在：{root}")
    classes = sorted(
        child.name for child in root.iterdir()
        if child.is_dir() and any(
            item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
            for item in child.iterdir())
    )
    if len(classes) < 2:
        raise ApiError(422, "DATASET_INVALID", f"{split} 至少需要两个含图片的类别目录")
    records: list[ImageRecord] = []
    for label, name in enumerate(classes):
        images = sorted(
            item for item in (root / name).iterdir()
            if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES)
        if max_per_class:
            images = images[:max_per_class]
        if not images:
            raise ApiError(422, "DATASET_INVALID", f"{split}/{name} 没有可用图片")
        for image in images:
            records.append(ImageRecord(
                path=image,
                rel=image.relative_to(root).as_posix(),
                split=split,
                label=label,
                sha256=_sha256(image),
                ahash=_average_hash(image),
            ))
    return records, classes


def _counts(records: list[ImageRecord], classes: list[str]) -> dict[str, int]:
    return {name: sum(1 for item in records if item.label == index)
            for index, name in enumerate(classes)}


def _digest(records: list[ImageRecord]) -> str:
    """Content-only digest; split name is intentionally excluded."""
    value = hashlib.sha256()
    for item in sorted(records, key=lambda row: (row.rel, row.label)):
        value.update(f"{item.rel}\0{item.label}\0{item.sha256}\n".encode("utf-8"))
    return value.hexdigest()


def _hamming(left: int, right: int) -> int:
    return (int(left) ^ int(right)).bit_count()


def audit_leakage(ours: list[ImageRecord], ref: list[ImageRecord],
                  eval_rows: list[ImageRecord], *,
                  near_duplicate_hamming: int = 5,
                  max_pairs: int = 4_000_000) -> dict[str, Any]:
    """Check exact and perceptual-hash leakage against the held-out set.

    ``E`` must not share an exact or near-identical image with either training
    split.  Duplicates inside one training split are reported separately; they
    are a data-quality warning, not automatically a test leak.
    """
    by_sha: dict[str, list[ImageRecord]] = {}
    for item in [*ours, *ref, *eval_rows]:
        by_sha.setdefault(item.sha256, []).append(item)

    cross_exact: list[dict[str, str]] = []
    within_exact: list[dict[str, str]] = []
    cross_side_exact: list[dict[str, str]] = []
    for group in by_sha.values():
        if len(group) < 2:
            continue
        splits = {item.split for item in group}
        if "eval" in splits and ({"ours", "ref"} & splits):
            cross_exact.append({
                "sha256": group[0].sha256[:16],
                "paths": ", ".join(f"{item.split}:{item.rel}" for item in group[:4]),
            })
        elif splits == {"ours", "ref"}:
            cross_side_exact.append({
                "sha256": group[0].sha256[:16],
                "paths": ", ".join(f"{item.split}:{item.rel}" for item in group[:4]),
            })
        elif len(splits) == 1 and next(iter(splits)) in {"ours", "ref"}:
            within_exact.append({
                "sha256": group[0].sha256[:16],
                "paths": ", ".join(f"{item.split}:{item.rel}" for item in group[:4]),
            })

    train = [*ours, *ref]
    cross_near: list[dict[str, Any]] = []
    checked_pairs = 0
    truncated = False
    for left in eval_rows:
        for right in train:
            if checked_pairs >= max_pairs:
                truncated = True
                break
            checked_pairs += 1
            if left.sha256 == right.sha256:
                continue
            distance = _hamming(left.ahash, right.ahash)
            if distance <= near_duplicate_hamming:
                cross_near.append({
                    "distance": distance,
                    "eval": left.rel,
                    "train": f"{right.split}:{right.rel}",
                })
        if truncated:
            break

    cross_side_near: list[dict[str, Any]] = []
    cross_side_near_checked = len(ours) * len(ref) <= 250_000
    if cross_side_near_checked:
        for left in ours:
            for right in ref:
                if left.sha256 == right.sha256:
                    continue
                distance = _hamming(left.ahash, right.ahash)
                if distance <= near_duplicate_hamming:
                    cross_side_near.append({
                        "distance": distance,
                        "ours": left.rel,
                        "ref": right.rel,
                    })

    warnings: list[str] = []
    if within_exact:
        warnings.append(f"训练集内部有 {len(within_exact)} 组精确重复，统计有效样本量时应聚合。")
    if truncated:
        warnings.append("近重复审计达到 pair 上限，未完成全量核对，不能判定为无泄漏。")
    if cross_exact:
        warnings.append(f"E 与训练集存在 {len(cross_exact)} 组精确重复。")
    if cross_near:
        warnings.append(f"E 与训练集存在 {len(cross_near)} 组近重复（Hamming ≤ "
                        f"{near_duplicate_hamming}）。")
    if cross_side_exact:
        warnings.append(f"D_ours 与 D_ref 存在 {len(cross_side_exact)} 组精确重复；"
                        "两侧共享图像时差值可能由构造性重合导致。")
    if cross_side_near:
        warnings.append(f"D_ours 与 D_ref 存在 {len(cross_side_near)} 组近重复。")
    if not cross_side_near_checked:
        warnings.append("D_ours 与 D_ref 的跨侧近重复未全量核对（规模超过 250,000 对）。")

    passed = not cross_exact and not cross_near and not truncated
    return {
        "policy": "exact_sha256 + dihedral_average_hash_holdout",
        "passed": passed,
        "nearDuplicateHamming": near_duplicate_hamming,
        "checkedPairs": checked_pairs,
        "truncated": truncated,
        "crossSplitExactDuplicates": len(cross_exact),
        "crossSplitNearDuplicates": len(cross_near),
        "withinSplitExactDuplicates": len(within_exact),
        "crossSideExactDuplicates": len(cross_side_exact),
        "crossSideNearDuplicates": len(cross_side_near),
        "crossSideNearChecked": cross_side_near_checked,
        "examples": (cross_exact + cross_near + cross_side_exact + cross_side_near)[:20],
        "warnings": warnings,
    }


def _label_distribution(records: list[ImageRecord], classes: int) -> np.ndarray:
    counts = np.zeros(classes, dtype=np.float64)
    for item in records:
        if 0 <= item.label < classes:
            counts[item.label] += 1.0
    total = counts.sum()
    return counts / total if total else counts


def _jsd(left: np.ndarray, right: np.ndarray) -> float:
    if left.size != right.size or left.sum() <= 0 or right.sum() <= 0:
        return 1.0
    middle = (left + right) / 2.0
    def divergence(first: np.ndarray, second: np.ndarray) -> float:
        mask = first > 0
        return float(np.sum(first[mask] * np.log2(first[mask] / second[mask])))
    return max(0.0, 0.5 * divergence(left, middle) + 0.5 * divergence(right, middle))


def _rbf_mmd(left: np.ndarray, right: np.ndarray, *, seed: int,
             max_samples: int) -> tuple[float, float, int, int]:
    """RBF-kernel MMD² with a permutation p-value.

    The old implementation broadcasted a ``(2n, 2n, 2n)`` tensor, which needed
    more than 2 GB at 256 samples.  This version uses only n×n kernel matrices
    and calibrates the threshold with a permutation test, so small samples do
    not automatically cross a fixed threshold.
    """
    if left.ndim != 2 or right.ndim != 2 or left.shape[1] != right.shape[1]:
        raise ValueError("feature matrices must be 2-D with the same width")
    if max_samples < 2:
        raise ValueError("max_ood_samples must be at least 2")
    rng = np.random.default_rng(seed)
    def sample(value: np.ndarray) -> np.ndarray:
        if value.shape[0] <= max_samples:
            return value
        indices = rng.choice(value.shape[0], size=max_samples, replace=False)
        return value[np.sort(indices)]
    left = sample(np.asarray(left, dtype=np.float64))
    right = sample(np.asarray(right, dtype=np.float64))
    if left.shape[0] < 2 or right.shape[0] < 2:
        raise ValueError("each OOD group needs at least 2 feature vectors")
    pooled = np.concatenate([left, right], axis=0)
    mean = pooled.mean(axis=0, keepdims=True)
    std = pooled.std(axis=0, keepdims=True)
    std[std < 1e-6] = 1.0
    left = (left - mean) / std
    right = (right - mean) / std
    pooled = np.concatenate([left, right], axis=0)

    norms = np.sum(pooled * pooled, axis=1)
    distances = norms[:, None] + norms[None, :] - 2.0 * (pooled @ pooled.T)
    distances = np.maximum(distances, 0.0)
    positive = distances[distances > 0]
    gamma = 1.0 / max(float(np.median(positive)) if positive.size else 1.0, 1e-6)

    def kernel(first: np.ndarray, second: np.ndarray) -> np.ndarray:
        first_norm = np.sum(first * first, axis=1)
        second_norm = np.sum(second * second, axis=1)
        squared = first_norm[:, None] + second_norm[None, :] - 2.0 * (first @ second.T)
        return np.exp(-gamma * np.maximum(squared, 0.0))

    def statistic(first: np.ndarray, second: np.ndarray) -> float:
        k_xx = kernel(first, first).mean()
        k_yy = kernel(second, second).mean()
        k_xy = kernel(first, second).mean()
        return max(0.0, float(k_xx + k_yy - 2.0 * k_xy))

    observed = statistic(left, right)
    n_left = left.shape[0]
    count = 0
    permutations = 64
    for _ in range(permutations):
        order = rng.permutation(pooled.shape[0])
        permuted = pooled[order]
        if statistic(permuted[:n_left], permuted[n_left:]) >= observed - 1e-12:
            count += 1
    p_value = (count + 1.0) / (permutations + 1.0)
    return observed, p_value, int(left.shape[0]), int(right.shape[0])


def audit_ood(ours: list[ImageRecord], ref: list[ImageRecord],
              eval_rows: list[ImageRecord], classes: list[str], *,
              features: dict[str, np.ndarray] | None = None,
              max_label_jsd: float = 0.25,
              max_feature_mmd: float = 0.35,
              max_ood_samples: int = 256,
              enforce_feature_mmd: bool = False,
              seed: int = 42) -> dict[str, Any]:
    """Check label and feature-distribution matching against fixed ``E``."""
    class_count = len(classes)
    ours_dist = _label_distribution(ours, class_count)
    ref_dist = _label_distribution(ref, class_count)
    eval_dist = _label_distribution(eval_rows, class_count)
    ours_jsd = _jsd(ours_dist, eval_dist)
    ref_jsd = _jsd(ref_dist, eval_dist)
    label_jsd = max(ours_jsd, ref_jsd)

    feature_mmd: float | None = None
    feature_pvalue: float | None = None
    feature_counts: dict[str, int] = {}
    feature_error: str | None = None
    warnings: list[str] = []
    if features is not None and {"ours", "ref", "eval"} <= set(features):
        try:
            ours_mmd, ours_p, ours_n, eval_n = _rbf_mmd(
                features["ours"], features["eval"], seed=seed, max_samples=max_ood_samples)
            ref_mmd, ref_p, ref_n, _ = _rbf_mmd(
                features["ref"], features["eval"], seed=seed, max_samples=max_ood_samples)
            feature_mmd = max(ours_mmd, ref_mmd)
            feature_pvalue = min(ours_p, ref_p)
            feature_counts = {"ours": ours_n, "ref": ref_n, "eval": eval_n}
        except ValueError as exc:
            feature_error = str(exc)
            warnings.append(f"特征 MMD 未计算：{exc}")
    else:
        warnings.append("未提供冻结 backbone 特征，featureMMD 不计算；类别分布检查仍然生效。")

    alpha = 0.05
    feature_significant = bool(
        feature_mmd is not None and feature_pvalue is not None and feature_pvalue <= alpha)
    feature_overt = bool(feature_mmd is not None and feature_mmd > max_feature_mmd)
    feature_failed = bool(enforce_feature_mmd and (feature_mmd is None or (
        feature_significant and feature_overt)))
    passed = label_jsd <= max_label_jsd and not feature_failed
    if label_jsd > max_label_jsd:
        warnings.append(f"类别分布 JSD={label_jsd:.4f} 超过阈值 {max_label_jsd:.4f}。")
    if feature_mmd is not None and feature_overt:
        if feature_significant:
            warnings.append(f"特征 MMD={feature_mmd:.4f} 超过阈值 {max_feature_mmd:.4f}，"
                            f"置换检验 p={feature_pvalue:.3f}。")
        else:
            warnings.append(f"特征 MMD={feature_mmd:.4f} 超过阈值，但置换检验 p="
                            f"{feature_pvalue:.3f} 不显著；小样本下只作告警。")
    if not enforce_feature_mmd:
        warnings.append("随机初始化 backbone 的 featureMMD 仅作诊断，不作为 OOD 门槛；"
                        "配置本地预训练权重后才会强制阈值。")
    if enforce_feature_mmd and feature_mmd is None:
        warnings.append("已要求强制 featureMMD，但特征审计未完成；按未通过处理。")
    return {
        "policy": "label_distribution + frozen_feature_mmd_permutation",
        "passed": passed,
        "labelJSD": round(label_jsd, 6),
        "oursLabelJSD": round(ours_jsd, 6),
        "refLabelJSD": round(ref_jsd, 6),
        "featureMMD": None if feature_mmd is None else round(feature_mmd, 6),
        "featureMMDPValue": None if feature_pvalue is None else round(feature_pvalue, 6),
        "featureMMDAlpha": alpha,
        "featureSampleCounts": feature_counts,
        "featureMMDError": feature_error,
        "featureMMDEnforced": bool(enforce_feature_mmd and feature_mmd is not None),
        "thresholds": {"labelJSD": max_label_jsd, "featureMMD": max_feature_mmd},
        "classDistribution": {
            "classes": classes,
            "ours": {name: int(ours_dist[index] * len(ours)) for index, name in enumerate(classes)},
            "ref": {name: int(ref_dist[index] * len(ref)) for index, name in enumerate(classes)},
            "eval": {name: int(eval_dist[index] * len(eval_rows)) for index, name in enumerate(classes)},
        },
        "warnings": warnings,
    }


def _model_builder(model_name: str) -> tuple[Any, str]:
    if model_name in TORCHVISION_MODELS:
        builder_name, filename = TORCHVISION_MODELS[model_name]
        builder = getattr(tv_models, builder_name)
        return builder, filename
    if model_name == "deit_tiny":
        if timm is None:
            raise ApiError(422, "ENGINE_UNAVAILABLE",
                           "deit_tiny 需要 timm；当前环境未安装，请使用 resnet18/mobilenet_v2")
        return (lambda **kwargs: timm.create_model(
            "deit_tiny_patch16_224", pretrained=False, **kwargs)), "deit_tiny.pth"
    raise ApiError(422, "CONFIG_INVALID", f"不支持的图像模型：{model_name}")


def _load_state(model: Any, weight_path: Path | None) -> bool:
    if weight_path is None:
        return False
    if not weight_path.is_file():
        raise ApiError(422, "ASSET_MISSING", f"模型权重不存在：{weight_path}")
    try:
        state = torch.load(weight_path, map_location="cpu", weights_only=True)
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        state = {str(key).removeprefix("module."): value for key, value in state.items()}
        incompatible = model.load_state_dict(state, strict=False)
        if incompatible.missing_keys or incompatible.unexpected_keys:
            raise ApiError(
                422, "ASSET_INVALID",
                "模型权重与所选骨干不匹配："
                f"missing={incompatible.missing_keys[:5]} "
                f"unexpected={incompatible.unexpected_keys[:5]}")
    except ApiError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ApiError(422, "ASSET_INVALID", f"无法加载模型权重：{exc}") from exc
    return True


def _build_model(model_name: str, weight_path: Path | None, classes: int,
                 *, feature_only: bool) -> tuple[Any, bool]:
    _require_torch()
    builder, _filename = _model_builder(model_name)
    if model_name == "deit_tiny":
        model = builder(num_classes=0 if feature_only else classes)
        loaded = _load_state(model, weight_path)
        if not feature_only:
            # timm with num_classes already provides a head; loading a 1000-class
            # state would not match, so custom weights are not silently accepted.
            if loaded:
                raise ApiError(422, "ASSET_INVALID",
                               "deit_tiny 的自定义权重需要与类别头匹配，当前不自动改写")
        model.eval()
        for parameter in model.parameters():
            parameter.requires_grad_(not feature_only)
        return model, loaded

    model = builder(weights=None)
    loaded = _load_state(model, weight_path)
    if hasattr(model, "fc"):
        in_features = model.fc.in_features
        model.fc = nn.Identity() if feature_only else nn.Linear(in_features, classes)
    elif hasattr(model, "classifier"):
        classifier = model.classifier
        if isinstance(classifier, nn.Sequential) and len(classifier) >= 1 \
                and isinstance(classifier[-1], nn.Linear):
            in_features = classifier[-1].in_features
        elif isinstance(classifier, nn.Linear):
            in_features = classifier.in_features
        else:  # pragma: no cover - supported builders use Sequential/Linear
            raise ApiError(422, "CONFIG_INVALID", f"{model_name} 的分类头不可识别")
        model.classifier = nn.Identity() if feature_only else nn.Linear(in_features, classes)
    else:  # pragma: no cover
        raise ApiError(422, "CONFIG_INVALID", f"{model_name} 没有可替换的分类头")
    if feature_only:
        model.eval()
        for parameter in model.parameters():
            parameter.requires_grad_(False)
    return model, loaded


def _eval_transform(image_size: int) -> Any:
    return tv_transforms.Compose([
        tv_transforms.Resize((image_size, image_size)),
        tv_transforms.ToTensor(),
        tv_transforms.Normalize(mean=(0.485, 0.456, 0.406),
                                std=(0.229, 0.224, 0.225)),
    ])


def _train_transform(image_size: int, augment: bool) -> Any:
    if not augment:
        return _eval_transform(image_size)
    return tv_transforms.Compose([
        tv_transforms.RandomResizedCrop(image_size, scale=(0.7, 1.0), ratio=(0.8, 1.25)),
        tv_transforms.RandomHorizontalFlip(),
        tv_transforms.ToTensor(),
        tv_transforms.Normalize(mean=(0.485, 0.456, 0.406),
                                std=(0.229, 0.224, 0.225)),
    ])


@_no_grad_decorator
def _extract_features(model: Any, paths: list[Path], transform: Any,
                      batch_size: int) -> np.ndarray:
    from PIL import Image

    batches = []
    for start in range(0, len(paths), batch_size):
        tensors = []
        for path in paths[start:start + batch_size]:
            with Image.open(path) as image:
                tensors.append(transform(image.convert("RGB")))
        batches.append(model(torch.stack(tensors)).detach().cpu())
    if not batches:
        return np.empty((0, 0), dtype=np.float32)
    return torch.cat(batches, dim=0).numpy()


def _macro_f1(y_true: list[int], y_pred: list[int], classes: int) -> float:
    scores = []
    for label in range(classes):
        tp = sum(1 for truth, pred in zip(y_true, y_pred) if truth == label and pred == label)
        fp = sum(1 for truth, pred in zip(y_true, y_pred) if truth != label and pred == label)
        fn = sum(1 for truth, pred in zip(y_true, y_pred) if truth == label and pred != label)
        if tp + fp + fn == 0:
            scores.append(0.0)
            continue
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall)
                      if precision + recall else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


def _metrics(y_true: list[int], y_pred: list[int], classes: int) -> dict[str, float]:
    top1 = 100.0 * sum(int(pred == truth) for truth, pred in zip(y_true, y_pred)) / max(1, len(y_true))
    return {"top1": round(top1, 2), "macroF1": round(100.0 * _macro_f1(y_true, y_pred, classes), 2)}


class _ImageDataset:
    def __init__(self, records: list[ImageRecord], transform: Any) -> None:
        self.records = records
        self.transform = transform
        from PIL import Image
        self._image = Image

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> tuple[Any, int]:
        item = self.records[index]
        with self._image.open(item.path) as image:
            return self.transform(image.convert("RGB")), item.label


def _train_full(model: Any, records: list[ImageRecord], classes: int, *,
                spec: ImageProtocolSpec) -> dict[str, Any]:
    _seed_everything(spec.seed)
    dataset = _ImageDataset(records, _train_transform(spec.image_size, spec.augment))
    generator = torch.Generator().manual_seed(spec.seed)
    loader = DataLoader(dataset, batch_size=spec.batch_size, shuffle=True,
                        generator=generator, num_workers=0)
    optimizer = torch.optim.AdamW(model.parameters(), lr=spec.learning_rate)
    criterion = nn.CrossEntropyLoss()
    model.train()
    losses, steps = [], 0
    for epoch in range(spec.epochs):
        epoch_losses = []
        for images, labels in loader:
            optimizer.zero_grad()
            loss = criterion(model(images), labels)
            if not torch.isfinite(loss):
                raise ApiError(422, "TRAINING_FAILED", "图像训练 loss 为 NaN/Inf")
            loss.backward()
            optimizer.step()
            epoch_losses.append(float(loss.detach()))
            steps += 1
        losses.append({"epoch": epoch + 1, "meanLoss": float(np.mean(epoch_losses)),
                       "stepLosses": epoch_losses})
    model.eval()
    return {"steps": steps, "lossCurve": losses}


@_no_grad_decorator
def _predict_full(model: Any, records: list[ImageRecord], transform: Any,
                  batch_size: int) -> list[int]:
    model.eval()
    dataset = _ImageDataset(records, transform)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)
    predictions: list[int] = []
    for images, _labels in loader:
        predictions.extend(model(images).argmax(dim=1).tolist())
    return predictions


def _train_linear(features: np.ndarray, labels: list[int], classes: int, *,
                  spec: ImageProtocolSpec) -> tuple[Any, dict[str, Any]]:
    _seed_everything(spec.seed)
    tensor = torch.from_numpy(np.asarray(features, dtype=np.float32))
    head = nn.Linear(tensor.shape[1], classes)
    optimizer = torch.optim.AdamW(head.parameters(), lr=spec.learning_rate)
    criterion = nn.CrossEntropyLoss()
    target = torch.tensor(labels, dtype=torch.long)
    generator = torch.Generator().manual_seed(spec.seed)
    losses, steps = [], 0
    for epoch in range(spec.epochs):
        epoch_losses = []
        order = torch.randperm(tensor.shape[0], generator=generator)
        for start in range(0, len(order), spec.batch_size):
            batch = order[start:start + spec.batch_size]
            optimizer.zero_grad()
            loss = criterion(head(tensor[batch]), target[batch])
            if not torch.isfinite(loss):
                raise ApiError(422, "TRAINING_FAILED", "图像线性头 loss 为 NaN/Inf")
            loss.backward()
            optimizer.step()
            epoch_losses.append(float(loss.detach()))
            steps += 1
        losses.append({"epoch": epoch + 1, "meanLoss": float(np.mean(epoch_losses)),
                       "stepLosses": epoch_losses})
    head.eval()
    return head, {"steps": steps, "lossCurve": losses}


@_no_grad_decorator
def _predict_linear(head: Any, features: np.ndarray) -> list[int]:
    return head(torch.from_numpy(np.asarray(features, dtype=np.float32))).argmax(dim=1).tolist()


def _check_protocol(spec: ImageProtocolSpec) -> None:
    if spec.task not in TASKS:
        raise ApiError(422, "CONFIG_INVALID",
                       "图像质量协议当前只支持 classification；检测/分割任务尚不训练")
    if spec.benchmark not in BENCHMARKS:
        raise ApiError(422, "CONFIG_INVALID", f"不支持的图像基准：{spec.benchmark}")
    if spec.proxy_model not in MODELS:
        raise ApiError(422, "CONFIG_INVALID", f"不支持的图像代理模型：{spec.proxy_model}")
    if spec.training_mode not in TRAINING_MODES:
        raise ApiError(422, "CONFIG_INVALID", f"不支持的训练模式：{spec.training_mode}")
    if spec.seed < 0:
        raise ApiError(422, "CONFIG_INVALID", "seed 不能为负数")
    if spec.epochs < 1:
        raise ApiError(422, "CONFIG_INVALID", "epochs 必须至少为 1")
    if spec.epochs > 10_000:
        raise ApiError(422, "CONFIG_INVALID", "epochs 过大")
    if spec.batch_size < 1:
        raise ApiError(422, "CONFIG_INVALID", "batch_size 必须至少为 1")
    if spec.batch_size > 4096:
        raise ApiError(422, "CONFIG_INVALID", "batch_size 过大")
    if not np.isfinite(spec.learning_rate) or spec.learning_rate <= 0:
        raise ApiError(422, "CONFIG_INVALID", "learning_rate 必须是有限正数")
    if spec.max_per_class is not None and spec.max_per_class < 1:
        raise ApiError(422, "CONFIG_INVALID", "max_per_class 必须至少为 1")
    if spec.max_per_class is not None and spec.max_per_class > 100_000:
        raise ApiError(422, "CONFIG_INVALID", "max_per_class 过大")
    if spec.image_size < 16:
        raise ApiError(422, "CONFIG_INVALID", "image_size 至少为 16")
    if spec.image_size > 2048:
        raise ApiError(422, "CONFIG_INVALID", "image_size 过大")
    if spec.near_duplicate_hamming < 0 or spec.near_duplicate_hamming > 256:
        raise ApiError(422, "CONFIG_INVALID", "near_duplicate_hamming 必须在 0–256")
    if not (0.0 <= spec.max_label_jsd <= 1.0 and 0.0 <= spec.max_feature_mmd <= 1.0):
        raise ApiError(422, "CONFIG_INVALID", "OOD 阈值必须在 0–1")
    if spec.max_ood_samples < 2:
        raise ApiError(422, "CONFIG_INVALID", "max_ood_samples 必须至少为 2")
    if spec.max_leakage_pairs < 1:
        raise ApiError(422, "CONFIG_INVALID", "max_leakage_pairs 必须至少为 1")


def _run_image_protocol_single(spec: ImageProtocolSpec) -> dict[str, Any]:
    """Run the audited image quality protocol and return one JSON record.

    The function raises ``ApiError`` for strict protocol failures.  With
    ``strict=False`` it still returns the audit record so a caller can inspect
    why a comparison is not usable; ``valid`` is then false.
    """
    _check_protocol(spec)
    _require_torch()

    ours, ours_classes = _records_for_split(spec.ours_dir, "ours", spec.max_per_class)
    ref, ref_classes = _records_for_split(spec.ref_dir, "ref", spec.max_per_class)
    eval_rows, eval_classes = _records_for_split(spec.eval_dir, "eval", None)
    if not (ours_classes == ref_classes == eval_classes):
        raise ApiError(422, "DATASET_INVALID",
                       "D_ours、D_ref 和 E 的类别目录必须完全一致且同序")
    ours_counts = _counts(ours, ours_classes)
    ref_counts = _counts(ref, ref_classes)
    eval_counts = _counts(eval_rows, eval_classes)
    if ours_counts != ref_counts:
        raise ApiError(422, "PROTOCOL_MISMATCH",
                       "D_ours 与 D_ref 的每类样本数必须相同，避免类别不平衡混入数据质量差异")
    if len(ours) != len(ref):
        raise ApiError(422, "PROTOCOL_MISMATCH", "D_ours 与 D_ref 的训练样本数必须相同")

    leakage = audit_leakage(
        ours, ref, eval_rows,
        near_duplicate_hamming=spec.near_duplicate_hamming,
        max_pairs=spec.max_leakage_pairs)
    if spec.strict and leakage["truncated"]:
        raise ApiError(422, "AUDIT_INCOMPLETE",
                       "近重复审计达到 pair 上限，未完成全量核对；请调大 maxLeakagePairs 或缩小数据量",
                       {"leakage": leakage})
    if spec.strict and not leakage["passed"]:
        raise ApiError(422, "LEAKAGE_DETECTED",
                       "训练集与固定评测集 E 存在重复/近重复，不能报告可对比数据质量",
                       {"leakage": leakage})

    # Label-distribution OOD is checked before any model work.
    ood = audit_ood(ours, ref, eval_rows, ours_classes,
                    max_label_jsd=spec.max_label_jsd,
                    max_feature_mmd=spec.max_feature_mmd,
                    max_ood_samples=spec.max_ood_samples,
                    enforce_feature_mmd=False, seed=spec.seed)
    if spec.strict and not ood["passed"]:
        raise ApiError(422, "OOD_MISMATCH",
                       "D_ours/D_ref 与 E 的分布不匹配，先对齐分布再比较规模",
                       {"ood": ood})

    _seed_everything(spec.seed)
    features: dict[str, np.ndarray] | None = None
    model_weight = spec.model_weight
    if spec.training_mode == "linear_probe":
        feature_model, loaded = _build_model(
            spec.proxy_model, model_weight, len(ours_classes), feature_only=True)
        transform = _eval_transform(spec.image_size)
        ours_features = _extract_features(feature_model, [r.path for r in ours], transform,
                                          spec.batch_size)
        ref_features = _extract_features(feature_model, [r.path for r in ref], transform,
                                          spec.batch_size)
        eval_features = _extract_features(feature_model, [r.path for r in eval_rows], transform,
                                          spec.batch_size)
        features = {"ours": ours_features, "ref": ref_features, "eval": eval_features}
        ood = audit_ood(ours, ref, eval_rows, ours_classes, features=features,
                        max_label_jsd=spec.max_label_jsd,
                        max_feature_mmd=spec.max_feature_mmd,
                        max_ood_samples=spec.max_ood_samples,
                        enforce_feature_mmd=loaded, seed=spec.seed)
        if spec.strict and not ood["passed"]:
            raise ApiError(422, "OOD_MISMATCH",
                           "冻结特征分布不匹配，先对齐 OOD 再比较数据量",
                           {"ood": ood})
        ours_head, ours_training = _train_linear(ours_features, [r.label for r in ours],
                                  len(ours_classes), spec=spec)
        ref_head, ref_training = _train_linear(ref_features, [r.label for r in ref],
                                 len(ref_classes), spec=spec)
        ours_pred = _predict_linear(ours_head, eval_features)
        ref_pred = _predict_linear(ref_head, eval_features)
    else:
        feature_model, loaded = _build_model(
            spec.proxy_model, model_weight, len(ours_classes), feature_only=True)
        transform = _eval_transform(spec.image_size)
        ours_features = _extract_features(feature_model, [r.path for r in ours], transform,
                                          spec.batch_size)
        ref_features = _extract_features(feature_model, [r.path for r in ref], transform,
                                          spec.batch_size)
        eval_features = _extract_features(feature_model, [r.path for r in eval_rows], transform,
                                          spec.batch_size)
        features = {"ours": ours_features, "ref": ref_features, "eval": eval_features}
        ood = audit_ood(ours, ref, eval_rows, ours_classes, features=features,
                        max_label_jsd=spec.max_label_jsd,
                        max_feature_mmd=spec.max_feature_mmd,
                        max_ood_samples=spec.max_ood_samples,
                        enforce_feature_mmd=loaded, seed=spec.seed)
        if spec.strict and not ood["passed"]:
            raise ApiError(422, "OOD_MISMATCH",
                           "冻结特征分布不匹配，先对齐 OOD 再比较数据量",
                           {"ood": ood})
        _seed_everything(spec.seed)
        train_model, loaded = _build_model(
            spec.proxy_model, model_weight, len(ours_classes), feature_only=False)
        ours_training = _train_full(train_model, ours, len(ours_classes), spec=spec)
        ours_pred = _predict_full(train_model, eval_rows,
                                  _eval_transform(spec.image_size), spec.batch_size)
        _seed_everything(spec.seed)
        ref_model, _loaded_ref = _build_model(
            spec.proxy_model, model_weight, len(ref_classes), feature_only=False)
        ref_training = _train_full(ref_model, ref, len(ref_classes), spec=spec)
        ref_pred = _predict_full(ref_model, eval_rows,
                                 _eval_transform(spec.image_size), spec.batch_size)

    eval_labels = [item.label for item in eval_rows]
    if len(ours_pred) != len(eval_labels) or len(ref_pred) != len(eval_labels):
        raise ApiError(500, "EXPERIMENT_FAILED", "预测数量与固定评测集不一致")
    ours_metrics = _metrics(eval_labels, ours_pred, len(eval_classes))
    ref_metrics = _metrics(eval_labels, ref_pred, len(ref_classes))
    delta = {key: round(ours_metrics[key] - ref_metrics[key], 2) for key in METRICS}
    if ours_training["steps"] != ref_training["steps"]:
        raise ApiError(422, "BUDGET_MISMATCH", "图像两侧实际训练步数不一致")
    valid = bool(leakage["passed"] and ood["passed"] and loaded)
    conclusion_eligible = bool(valid)
    notes = [
        "MLPerf Tiny 风格的分类质量协议（非 VWW 基准实现）：同一 proxy 模型、同一训练预算、同 seed、固定 E；只改变 D_ours/D_ref。",
        "OOD 分布匹配优先：类别 JSD 与冻结特征 MMD + 置换检验均记录；不匹配时不得用规模解释差距。",
        "泄漏安全：E 与训练集做精确 SHA256 和 8 向翻转/旋转归一化 average-hash 近重复检查；增强只作用于训练集，E 不参与训练/调参。",
        "top1/macroF1 是当前代理模型的 measured_model 结果，不是 DataComp/MLPerf 公开榜论文值。",
    ]
    if not loaded:
        notes.append("未加载本地预训练权重：随机初始化 backbone 的分数不能作为数据质量结论（valid=false）。")
    if leakage.get("crossSideExactDuplicates", 0):
        notes.append("D_ours 与 D_ref 存在精确重复；两侧差值可能由样本重合造成。")
    if not valid:
        notes.append("审计未通过：本次分数仅保留为诊断记录，不能写成数据质量结论。")
    if spec.training_mode == "linear_probe":
        notes.append("linear_probe：冻结 backbone，仅训练线性分类头；不是 ResNet/MobileNet 全量微调。")
    else:
        notes.append("full：从同一初始权重开始训练全部参数；E 使用确定性预处理，无测试时增强。")
    if spec.augment and spec.training_mode == "linear_probe":
        notes.append("linear_probe 使用确定性特征，未应用随机增强；增强仅在 full 训练路径生效。")

    return {
        "schemaVersion": "1.0",
        "createdAt": now(),
        "experiment": "image-quality-protocol",
        "benchmark": spec.benchmark,
        "benchmarkTask": "visual_wake_words" if spec.benchmark == "mlperftiny" else "classification",
        "benchmarkTaskNote": (
            "mlperftiny 仅标记 VWW 口径；任意类别不是 VWW 任务，引用时必须改写任务说明"
            if spec.benchmark == "mlperftiny"
            else "benchmark 仅标记口径；结果在同一固定 E 上按 classification 测量，不与公开榜数值对齐"
        ),
        "task": spec.task,
        "proxyModel": spec.proxy_model,
        "trainingMode": spec.training_mode,
        "valid": valid,
        "conclusionEligible": conclusion_eligible,
        "protocol": {
            "model": spec.proxy_model,
            "modelWeight": str(model_weight) if model_weight else None,
            "modelFingerprint": _sha256(model_weight) if model_weight else None,
            "pretrained": bool(loaded),
            "imageSize": spec.image_size,
            "epochs": spec.epochs,
            "batchSize": spec.batch_size,
            "learningRate": spec.learning_rate,
            "seed": spec.seed,
            "augmentation": "train-only" if spec.augment and spec.training_mode == "full" else "none",
            "testTimeAugmentation": False,
            "fixedEval": True,
            "trainVolume": len(ours),
            "evalVolume": len(eval_rows),
            "classes": ours_classes,
            "perClassCounts": ours_counts,
            "evalPerClassCounts": eval_counts,
        },
        "data": {
            "oursDigest": _digest(ours),
            "refDigest": _digest(ref),
            "evalDigest": _digest(eval_rows),
            "oursDir": str(spec.ours_dir),
            "refDir": str(spec.ref_dir),
            "evalDir": str(spec.eval_dir),
        },
        "leakage": leakage,
        "ood": ood,
        "S_ours": ours_metrics,
        "S_ref": ref_metrics,
        "delta": delta,
        "training": {"ours": ours_training, "ref": ref_training},
        "metricDirection": {key: "higher" for key in METRICS},
        "provenance": {key: "measured_model" for key in METRICS},
        "notes": notes,
    }


@training_locked
def run_image_protocol(spec: ImageProtocolSpec) -> dict[str, Any]:
    """Run paired seeds; a valid single run is still exploratory evidence."""
    seeds = spec.seeds if spec.seeds is not None else (spec.seed,)
    if (not isinstance(seeds, tuple) or not 1 <= len(seeds) <= 10
            or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds)
            or len(set(seeds)) != len(seeds)
            or spec.purpose not in {"smoke", "exploration", "quality", "formal"}):
        raise ApiError(422, "CONFIG_INVALID", "图像协议需要1–10个不同的有效种子和有效purpose")
    runs = [_run_image_protocol_single(replace(spec, seed=seed, seeds=None)) for seed in seeds]
    first = runs[0]
    if any(run["data"] != first["data"] for run in runs[1:]):
        raise ApiError(422, "DATASET_CHANGED", "多种子运行期间图像数据发生变化")
    fingerprints = [run["protocol"].get("modelFingerprint") for run in runs]
    if len(set(fingerprints)) > 1:
        raise ApiError(422, "ASSET_INVALID", "多种子运行期间图像初始化权重发生变化")
    statistics = {}
    for side in ("S_ours", "S_ref", "delta"):
        statistics[side] = {}
        for metric in METRICS:
            values = [float(run[side][metric]) for run in runs]
            statistics[side][metric] = {
                "mean": round(float(np.mean(values)), 6),
                "std": round(float(np.std(values, ddof=1)), 6) if len(values) > 1 else None,
                "n": len(values),
            }
    valid = all(run["valid"] for run in runs)
    required = 5 if spec.purpose == "formal" else 3
    eligible = bool(valid and spec.purpose in {"quality", "formal"} and len(seeds) >= required)
    result = dict(first)
    result.update({
        "engineVersion": "image-training-v2",
        "valid": valid, "conclusionEligible": eligible, "qualityConclusionEligible": eligible,
        "runs": runs, "statistics": statistics,
        **{side: {metric: stats["mean"] for metric, stats in statistics[side].items()}
           for side in ("S_ours", "S_ref", "delta")},
        "protocol": {**first["protocol"], "seeds": list(seeds), "purpose": spec.purpose},
        "notes": [*first["notes"],
                  "MVP质量比较至少3种子，正式比较至少5种子；std反映训练随机性，不是总体泛化置信区间。"],
    })
    if not eligible:
        result["notes"].append("当前仅为探索/证据不足的结果，不产生正式数据质量结论。")
    return result
