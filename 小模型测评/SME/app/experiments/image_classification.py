"""Controlled image-classification experiment (D_ours vs D_ref).

This is an offline experiment helper, not the online image proxy engine.  It
implements the methodology required by the manual with a deliberately small
proxy:

* the same frozen ResNet/MobileNet backbone for both sides;
* the same linear head architecture, training volume, optimizer and seed;
* one fixed evaluation directory ``E``;
* only the training directory changes between ``D_ours`` and ``D_ref``.

The result is a linear-probe comparison, not a full ResNet fine-tune.  The
returned record says so explicitly; it must not be presented as a paper
reproduction or as an online engine metric.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.envelope import ApiError
from ..core.ids import now

try:  # optional dependency; the online service does not need it
    import torch
    from torch import nn
    from torchvision import models as tv_models
    from torchvision import transforms as tv_transforms
except Exception:  # pragma: no cover - exercised only when optional deps absent
    torch = None  # type: ignore[assignment]
    nn = None  # type: ignore[assignment]
    tv_models = None  # type: ignore[assignment]
    tv_transforms = None  # type: ignore[assignment]

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MODEL_BUILDERS = {
    "resnet18": ("resnet18", "resnet18-f37072fd.pth"),
    "resnet50": ("resnet50", "resnet50-0676ba61.pth"),
    "mobilenet_v2": ("mobilenet_v2", "mobilenet_v2-b0353104.pth"),
}


@dataclass(frozen=True)
class ImageExperimentSpec:
    ours_dir: Path
    ref_dir: Path
    eval_dir: Path
    model: str = "resnet18"
    model_weight: Path | None = None
    seed: int = 42
    epochs: int = 30
    batch_size: int = 64
    learning_rate: float = 1e-3
    image_size: int = 224
    max_per_class: int | None = None


def _require_torchvision() -> None:
    if torch is None or tv_models is None or tv_transforms is None or nn is None:
        raise ApiError(422, "ENGINE_UNAVAILABLE",
                       "图像训练式实验需要 torch + torchvision；当前环境未安装")


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(False)


def _dataset_rows(root: Path, max_per_class: int | None) -> tuple[list[Path], list[int], list[str], dict[str, int]]:
    if not root.is_dir():
        raise ApiError(422, "DATASET_INVALID", f"图像目录不存在：{root}")
    classes = sorted(
        child.name for child in root.iterdir()
        if child.is_dir() and any(
            item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
            for item in child.iterdir())
    )
    if len(classes) < 2:
        raise ApiError(422, "DATASET_INVALID", f"{root} 至少需要两个含图片的类别目录")
    class_to_idx = {name: index for index, name in enumerate(classes)}
    paths: list[Path] = []
    labels: list[int] = []
    for name in classes:
        images = sorted(
            item for item in (root / name).iterdir()
            if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES)
        if max_per_class:
            images = images[:max_per_class]
        if not images:
            raise ApiError(422, "DATASET_INVALID", f"类别 {name} 没有可用图片")
        for item in images:
            paths.append(item)
            labels.append(class_to_idx[name])
    return paths, labels, classes, class_to_idx


def _manifest_digest(paths: list[Path], labels: list[int], root: Path) -> str:
    digest = hashlib.sha256()
    for path, label in zip(paths, labels):
        digest.update(str(path.relative_to(root).as_posix()).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(label).encode("ascii"))
        digest.update(b"\0")
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(chunk)
        digest.update(file_digest.digest())
        digest.update(b"\n")
    return digest.hexdigest()


def _transform(image_size: int):
    return tv_transforms.Compose([
        tv_transforms.Resize((image_size, image_size)),
        tv_transforms.ToTensor(),
        tv_transforms.Normalize(mean=(0.485, 0.456, 0.406),
                                std=(0.229, 0.224, 0.225)),
    ])


@torch.no_grad()
def _extract_features(backbone: Any, paths: list[Path], transform: Any,
                      batch_size: int) -> Any:
    from PIL import Image

    batches = []
    for start in range(0, len(paths), batch_size):
        tensors = []
        for path in paths[start:start + batch_size]:
            with Image.open(path) as image:
                tensors.append(transform(image.convert("RGB")))
        batches.append(backbone(torch.stack(tensors)))
    return torch.cat(batches, dim=0) if batches else torch.empty(0)


def _load_backbone(model_name: str, weight_path: Path | None) -> Any:
    if model_name not in MODEL_BUILDERS:
        raise ApiError(422, "CONFIG_INVALID", f"不支持的图像模型：{model_name}")
    builder_name, default_filename = MODEL_BUILDERS[model_name]
    builder = getattr(tv_models, builder_name)
    model = builder(weights=None)
    if weight_path is not None:
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
    if hasattr(model, "fc"):
        model.fc = nn.Identity()
    elif hasattr(model, "classifier"):
        model.classifier = nn.Identity()
    else:  # pragma: no cover - all supported builders expose one of the above
        raise ApiError(422, "CONFIG_INVALID", f"{model_name} 没有可替换的分类头")
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


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


def _train_head(features: Any, labels: list[int], classes: int, *,
                seed: int, epochs: int, batch_size: int, learning_rate: float) -> Any:
    _seed_everything(seed)
    head = nn.Linear(features.shape[1], classes)
    optimizer = torch.optim.AdamW(head.parameters(), lr=learning_rate)
    criterion = nn.CrossEntropyLoss()
    target = torch.tensor(labels, dtype=torch.long)
    generator = torch.Generator().manual_seed(seed)
    for _epoch in range(max(1, epochs)):
        order = torch.randperm(features.shape[0], generator=generator)
        for start in range(0, len(order), batch_size):
            batch = order[start:start + batch_size]
            optimizer.zero_grad()
            loss = criterion(head(features[batch]), target[batch])
            loss.backward()
            optimizer.step()
    head.eval()
    return head


@torch.no_grad()
def _evaluate(head: Any, features: Any, labels: list[int], classes: int) -> dict[str, float]:
    prediction = head(features).argmax(dim=1).tolist()
    accuracy = 100.0 * sum(int(p == t) for p, t in zip(prediction, labels)) / max(1, len(labels))
    return {
        "accuracy": round(accuracy, 2),
        "macroF1": round(100.0 * _macro_f1(labels, prediction, classes), 2),
    }


def run_image_classification(spec: ImageExperimentSpec) -> dict[str, Any]:
    """Run one controlled linear-probe comparison and return its audit record."""
    _require_torchvision()
    if spec.model not in MODEL_BUILDERS:
        raise ApiError(422, "CONFIG_INVALID", f"不支持的图像模型：{spec.model}")
    if spec.seed < 0:
        raise ApiError(422, "CONFIG_INVALID", "seed 不能为负数")

    ours_paths, ours_labels, ours_classes, _ = _dataset_rows(spec.ours_dir, spec.max_per_class)
    ref_paths, ref_labels, ref_classes, _ = _dataset_rows(spec.ref_dir, spec.max_per_class)
    eval_paths, eval_labels, eval_classes, _ = _dataset_rows(spec.eval_dir, spec.max_per_class)
    if not (ours_classes == ref_classes == eval_classes):
        raise ApiError(422, "DATASET_INVALID",
                       "D_ours、D_ref 和 E 的类别目录必须完全一致且同序")
    if len(ours_paths) != len(ref_paths):
        raise ApiError(422, "PROTOCOL_MISMATCH", "D_ours 与 D_ref 的训练样本数必须相同")
    ours_counts = {name: ours_labels.count(index) for index, name in enumerate(ours_classes)}
    ref_counts = {name: ref_labels.count(index) for index, name in enumerate(ref_classes)}
    if ours_counts != ref_counts:
        raise ApiError(422, "PROTOCOL_MISMATCH",
                       "D_ours 与 D_ref 的每类样本数必须相同，避免类别不平衡混入数据质量差异")
    if not eval_paths:
        raise ApiError(422, "DATASET_INVALID", "固定评测集 E 为空")

    _seed_everything(spec.seed)
    backbone = _load_backbone(spec.model, spec.model_weight)
    transform = _transform(spec.image_size)
    ours_features = _extract_features(backbone, ours_paths, transform, spec.batch_size)
    ref_features = _extract_features(backbone, ref_paths, transform, spec.batch_size)
    eval_features = _extract_features(backbone, eval_paths, transform, spec.batch_size)

    ours_head = _train_head(
        ours_features, ours_labels, len(ours_classes), seed=spec.seed,
        epochs=spec.epochs, batch_size=spec.batch_size,
        learning_rate=spec.learning_rate)
    ref_head = _train_head(
        ref_features, ref_labels, len(ref_classes), seed=spec.seed,
        epochs=spec.epochs, batch_size=spec.batch_size,
        learning_rate=spec.learning_rate)
    ours = _evaluate(ours_head, eval_features, eval_labels, len(eval_classes))
    ref = _evaluate(ref_head, eval_features, eval_labels, len(eval_classes))
    delta = {key: round(ours[key] - ref[key], 2) for key in ("accuracy", "macroF1")}
    weight_note = (
        "使用本地 ImageNet 权重作为冻结 backbone。"
        if spec.model_weight else
        "未找到/未提供本地权重：backbone 为随机初始化，仅用于流程验证，不能与论文指标对标。")

    return {
        "schemaVersion": "1.0",
        "createdAt": now(),
        "experiment": "image-linear-probe",
        "protocol": {
            "model": spec.model,
            "backbone": ("frozen ImageNet weights" if spec.model_weight
                         else "frozen randomly-initialized weights"),
            "head": "single linear classifier",
            "epochs": spec.epochs,
            "batchSize": spec.batch_size,
            "learningRate": spec.learning_rate,
            "imageSize": spec.image_size,
            "seed": spec.seed,
            "trainVolume": len(ours_paths),
            "evalVolume": len(eval_paths),
            "classes": ours_classes,
            "perClassCounts": ours_counts,
            "evalPerClassCounts": {
                name: eval_labels.count(index) for index, name in enumerate(eval_classes)},
        },
        "data": {
            "oursDigest": _manifest_digest(ours_paths, ours_labels, spec.ours_dir),
            "refDigest": _manifest_digest(ref_paths, ref_labels, spec.ref_dir),
            "evalDigest": _manifest_digest(eval_paths, eval_labels, spec.eval_dir),
            "oursDir": str(spec.ours_dir),
            "refDir": str(spec.ref_dir),
            "evalDir": str(spec.eval_dir),
            "modelWeight": str(spec.model_weight) if spec.model_weight else None,
        },
        "S_ours": ours,
        "S_ref": ref,
        "delta": delta,
        "metricDirection": {"accuracy": "higher", "macroF1": "higher"},
        "provenance": {"accuracy": "measured_model", "macroF1": "measured_model"},
        "notes": [
            "线性探针：冻结 backbone，仅训练同一线性分类头；不是 ResNet 全量微调。",
            weight_note,
            "D_ours 与 D_ref 的训练样本数、每类条数、类别顺序、随机种子和固定评测集 E 已对齐。",
            "该结果只说明当前代理协议下的可学习性差异，不能直接写成业务数据质量结论。",
        ],
    }
