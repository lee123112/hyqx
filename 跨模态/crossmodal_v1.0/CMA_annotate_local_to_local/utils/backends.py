"""CLIP-ViT/B-32 编码后端（复用 #12/#14 的同一实例与配方）。

设计要点：
- **不下载任何权重**。``clip.weights`` 默认指向本算子自带的
  ``weight/clip/ViT-B-32.pt``（相对路径按 ``MODEL_ROOT`` 解析，未设时取本算子 ``weight/``）；
  为空时视为「共享实例未加载」→ 严格模式显式失败，或按配置显式降级（不产生任何分数）；
- ``shared_clip`` 任务服务是**可选依赖**：整包编排环境存在共享实例时行为不变；
  单算子目录被复制到包外独立部署（没有 ``shared_clip``）时静默回落本地权重加载；
- 进程内按 ``model_signature`` 缓存实例，等价于复用已加载的同一 CLIP；
- ``open_clip_encoder(weights=None)`` 只用于**管线连通性测试**（随机初始化架构），
  其输出没有语义，实例会被显式标记 ``untrained_architecture_only=True``。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .schemas import CLIP_MODEL_NAME, normalize_arch

__all__ = [
    "BackendUnavailable",
    "EncoderUnavailable",
    "OpenClipEncoder",
    "build_clip_backend",
    "clear_encoder_cache",
    "get_registered_encoder",
    "installed_libraries",
    "model_signature",
    "open_clip_encoder",
    "register_encoder",
]

DEFAULT_ARCH = "ViT-B-32"


class BackendUnavailable(RuntimeError):
    """编码后端不可用（缺库、缺权重、加载失败）。"""


EncoderUnavailable = BackendUnavailable

_ENCODER_CACHE: Dict[Tuple[Any, ...], Any] = {}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):        # pragma: no cover - 环境异常
        return False


def installed_libraries() -> Dict[str, bool]:
    return {"open_clip": _module_available("open_clip"),
            "torch": _module_available("torch"),
            "torchvision": _module_available("torchvision"),
            "transformers": _module_available("transformers"),
            "PIL": _module_available("PIL"),
            "numpy": _module_available("numpy"),
            "jieba": _module_available("jieba")}


# ---------------------------------------------------------------- 权重路径解析
# 正式版每个算子自带 `weight/`（见 `weight/README.md`），默认只从本算子目录加载：
#   <算子目录>/weight/clip/ViT-B-32.pt
# 配置里的**相对**路径按 `MODEL_ROOT` 解析；未设该环境变量时取**本算子** `weight/`。
# 容器部署：把 weight/ 挂到 /models 并设 `-e MODEL_ROOT=/models`（覆盖优先，不会回退本地）。
def model_root() -> Path:
    """权重根目录：``$MODEL_ROOT`` 优先，否则本算子目录下的 ``weight/``。"""
    override = os.environ.get("MODEL_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "weight"


def resolve_model_path(value: Optional[str]) -> Optional[str]:
    """把配置中的权重/模型路径解析为绝对路径；``None``/空串原样返回。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    path = Path(text).expanduser()
    return str(path) if path.is_absolute() else str(model_root() / path)


def model_signature(payload: Optional[Dict[str, Any]]
                    ) -> Tuple[str, str, Optional[str], str, int]:
    """(library, arch, weights, device, batch_size)——用于复用同一 CLIP 实例。"""
    data = payload or {}
    library = str(data.get("library") or "auto").strip().lower()
    arch = normalize_arch(data.get("model_name")) or DEFAULT_ARCH
    weights = resolve_model_path(data.get("weights")) or None
    device = str(data.get("device") or "cpu")
    batch = int(data.get("batch_size") or 8)
    return (library, arch, weights, device, batch)


def get_registered_encoder(signature: Tuple[Any, ...]) -> Any:
    return _ENCODER_CACHE.get(signature)


def register_encoder(signature: Tuple[Any, ...], encoder: Any) -> None:
    _ENCODER_CACHE[signature] = encoder


def clear_encoder_cache() -> None:
    _ENCODER_CACHE.clear()


def _suppress_background(crop: Any, outer: Tuple[int, int, int, int],
                         inner: Tuple[int, int, int, int], mode: str) -> Any:
    """把扩边裁图里「框外」的环形区域填成纯色，只保留框内目标。

    ``mode="mean"`` 用框内平均色填充（保留原图色调），``mode="gray"`` 用中性灰。
    该函数只改变送进 CLIP 的像素，不修改框坐标，也不产生任何分数。
    """
    from PIL import Image

    left = inner[0] - outer[0]
    top = inner[1] - outer[1]
    right = inner[2] - outer[0]
    bottom = inner[3] - outer[1]
    if right <= left or bottom <= top:
        return crop
    core = crop.crop((left, top, right, bottom))
    if mode == "mean":
        fill = tuple(int(round(value)) for value in core.resize((1, 1)).getpixel((0, 0)))
    else:
        fill = (114, 114, 114)
    canvas = Image.new("RGB", crop.size, fill)
    canvas.paste(core, (left, top))
    return canvas


class OpenClipEncoder:
    """open_clip 编码器：文本向量与「按候选框裁剪的图像块」向量。"""

    def __init__(self, model: Any, preprocess: Any, tokenizer: Any, *,
                 arch: str, weights: Optional[str], device: str = "cpu",
                 batch_size: int = 8, untrained_architecture_only: bool = False):
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer
        self.arch = arch
        self.weights = weights
        self.device = device
        self.batch_size = int(batch_size)
        self.untrained_architecture_only = bool(untrained_architecture_only)
        self.embed_dim = int(getattr(model, "embed_dim", 512) or 512)

    # -- 契约接口 ---------------------------------------------------------- #

    def encode_texts(self, texts: Sequence[str]) -> List[List[float]]:
        import torch

        items = [str(item) for item in texts]
        if not items:
            return []
        with torch.no_grad():
            tokens = self._tokenizer(items)
            if hasattr(tokens, "to"):
                tokens = tokens.to(self.device)
            features = self._model.encode_text(tokens)
            features = features / features.norm(dim=-1, keepdim=True)
        return [[float(value) for value in row] for row in features.detach().cpu()]

    def encode_crops(self, image_path: Any,
                     boxes: Sequence[Dict[str, Any]],
                     crop_options: Optional[Dict[str, Any]] = None
                     ) -> Tuple[List[Optional[List[float]]], List[Optional[str]]]:
        """把每个候选框裁出来编码；逐框返回 ``(向量, 错误原因)``。

        ``crop_options``（可选）：
        - ``padding_ratio``：框外扩边比例，0 表示紧贴检测框（默认）；
        - ``background_suppression``：``none`` / ``mean`` / ``gray``，
          扩边后把框外环形区域填成纯色（框内均值或中性灰），减少背景干扰。

        扩边不会改变框坐标本身，只影响送给 CLIP 的裁图；裁图失败仍逐框记错误。
        """
        import torch
        from PIL import Image

        from .phrase_region import BoxError, crop_rects

        count = len(boxes)
        if count == 0:
            return [], []

        options = crop_options or {}
        padding = float(options.get("padding_ratio") or 0.0)
        suppression = str(options.get("background_suppression") or "none").strip().lower()
        if padding <= 0 or suppression not in ("mean", "gray"):
            suppression = "none"

        target = Path(image_path)
        if not target.is_file():
            return [None] * count, ["media_missing"] * count
        try:
            with Image.open(target) as handle:
                image = handle.convert("RGB")
        except Exception:                          # noqa: BLE001 - 真实解码失败
            return [None] * count, ["media_unreadable"] * count

        width, height = image.size
        vectors: List[Optional[List[float]]] = []
        errors: List[Optional[str]] = []
        pending: List[Any] = []
        pending_index: List[int] = []

        for position, box in enumerate(boxes):
            try:
                outer, inner = crop_rects(box, width, height, padding)
            except (BoxError, TypeError, ValueError, KeyError):
                vectors.append(None)
                errors.append("crop_invalid")
                continue
            crop = image.crop(outer)
            if suppression != "none" and outer != inner:
                crop = _suppress_background(crop, outer, inner, suppression)
            pending.append(crop)
            pending_index.append(position)
            vectors.append(None)
            errors.append(None)

        for start in range(0, len(pending), self.batch_size):
            batch_crops = pending[start:start + self.batch_size]
            batch_indices = pending_index[start:start + self.batch_size]
            try:
                tensors = torch.stack([self._preprocess(crop) for crop in batch_crops])
                with torch.no_grad():
                    features = self._model.encode_image(tensors.to(self.device))
                    features = features / features.norm(dim=-1, keepdim=True)
                rows = [[float(value) for value in row] for row in features.detach().cpu()]
            except Exception:                      # noqa: BLE001 - 真实前向失败
                for index in batch_indices:
                    vectors[index] = None
                    errors[index] = "encode_failed"
                continue
            for offset, index in enumerate(batch_indices):
                vectors[index] = rows[offset]
                errors[index] = None
        return vectors, errors

    def model_identity(self) -> Dict[str, Any]:
        return {"name": CLIP_MODEL_NAME,
                "arch": self.arch,
                "version": "open_clip-%s%s" % (self.arch,
                                               "" if self.weights else "+random-init"),
                "library": "open_clip",
                "weights": self.weights,
                "device": self.device,
                "embed_dim": self.embed_dim,
                "batch_size": self.batch_size,
                "degraded": False,
                "real_backend": True,
                "untrained_architecture_only": self.untrained_architecture_only}


def open_clip_encoder(weights: Optional[str] = None, device: str = "cpu",
                      batch_size: int = 8, model_name: str = DEFAULT_ARCH,
                      arch: Optional[str] = None) -> OpenClipEncoder:
    """构建 open_clip 编码器。

    ``weights=None`` 时只构建**架构**（随机初始化，仅用于管线测试，无语义）。
    """
    if not _module_available("open_clip") or not _module_available("torch"):
        raise BackendUnavailable("未安装 open_clip/torch，CLIP 后端不可用")
    import open_clip
    import torch

    resolved = normalize_arch(arch or model_name) or DEFAULT_ARCH
    try:
        model, _train_preprocess, preprocess = open_clip.create_model_and_transforms(
            resolved, pretrained=weights)
    except Exception as exc:                       # noqa: BLE001
        raise BackendUnavailable("CLIP 架构/权重加载失败：%s" % exc)
    try:
        tokenizer = open_clip.get_tokenizer(resolved)
    except Exception as exc:                       # noqa: BLE001
        raise BackendUnavailable("CLIP 分词器加载失败：%s" % exc)

    model = model.to(device)
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return OpenClipEncoder(model, preprocess, tokenizer, arch=resolved,
                           weights=weights, device=device, batch_size=batch_size,
                           untrained_architecture_only=weights is None)


def _optional_remote_clip() -> Optional[Any]:
    """可选依赖：返回 ``shared_clip.runtime.RemoteClip``，模块不存在时返回 ``None``。

    单算子目录被复制到包外独立部署时没有 ``shared_clip``，此时必须静默回落本算子
    ``weight/`` 本地加载；不得因可选依赖缺失抛 ImportError，也不改变共享实例存在时的行为。
    """
    import sys

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from shared_clip.runtime import RemoteClip  # noqa: PLC0415

        return RemoteClip
    except ImportError:
        return None


def build_clip_backend(payload: Optional[Dict[str, Any]], allow_degraded: bool = False,
                       use_cache: bool = True
                       ) -> Tuple[Optional[Any], Optional[str], Optional[Dict[str, Any]]]:
    """返回 ``(encoder, reason, model_info)``。

    ``encoder`` 为 ``None`` 时 ``reason`` 说明不可用原因；调用方**不得**据此伪造分数。
    """
    # 产线任务服务优先；连接失败显式失败，禁止另加载第二份权重。
    # ``shared_clip`` 为可选依赖：包外独立部署没有该模块时静默回落本地权重加载。
    remote_class = _optional_remote_clip()
    section = dict(payload or {})
    if remote_class is not None and section.get("library") != "none":
        try:
            remote = remote_class.from_env(
                {**section, "weights": resolve_model_path(section.get("weights"))}, "18")
            if remote is not None:
                return remote, None, remote.model_identity()
        except (OSError, RuntimeError, ValueError) as exc:
            raise BackendUnavailable("共享CLIP不可用，不重复加载：%s" % exc) from exc
    signature = model_signature(payload)
    library, arch, weights, device, batch = signature

    cached = get_registered_encoder(signature)
    if cached is not None:
        return cached, None, cached.model_identity()

    def _unavailable(reason: str) -> Tuple[None, str, None]:
        if not allow_degraded:
            raise BackendUnavailable(reason)
        return None, reason, None

    if library not in ("auto", "open_clip"):
        return _unavailable("clip_library_unsupported:%s" % library)
    if not (_module_available("open_clip") and _module_available("torch")):
        return _unavailable("clip_runtime_missing")
    if not weights:
        return _unavailable("clip_weights_missing")
    if not Path(str(weights)).exists():
        return _unavailable("clip_weights_missing:%s" % weights)

    try:
        encoder = open_clip_encoder(weights=str(weights), device=device,
                                    batch_size=batch, arch=arch)
    except BackendUnavailable as exc:
        return _unavailable(str(exc))
    except Exception as exc:                       # noqa: BLE001
        return _unavailable("clip_load_failed:%s" % exc)

    if use_cache:
        register_encoder(signature, encoder)
    return encoder, None, encoder.model_identity()
