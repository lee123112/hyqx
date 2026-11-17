"""CLIP-ViT/B-32 后端适配器（表1 #14「复用图文评分的 CLIP-ViT/B-32」）。

职责与边界：
- **只从本地路径加载权重**，运行期绝不联网下载（``pretrained=None`` 只构建架构）；
- 文本与图像都经 L2 归一化，分数语义统一为余弦相似度；
- 逐帧容错：帧缺失/损坏返回 ``None`` 向量 + 可解释原因，不伪造向量、不中断整流；
- **实例复用**：``cached_encoder`` 按签名做进程级缓存，保证「不重复加载 CLIP」；
  ``register_encoder`` 允许编排侧把图文评分已加载的实例登记进来直接复用。

复用说明（诚实边界）：兄弟算子目录之间不得互相 import（各算子自带实现、独立打包），
因此跨算子复用有两条真实路径——①同进程部署时由编排侧把 #12 的实例
``register_encoder`` 登记进来；②两个算子指向同一份只读权重，
各自进程内只加载一次。若既未登记、签名也不同，则各进程各持一份实例。
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.schemas import DEFAULT_ARCH, normalize_arch

#: 对外固定的模型名（表1）
CLIP_NAME = "CLIP-ViT/B-32"
#: CLIP-ViT/B-32 图像/文本嵌入维度
CLIP_EMBED_DIM = 512

REASON_DISABLED = "library_disabled"
REASON_NO_WEIGHTS = "model_weights_not_configured"
REASON_WEIGHTS_MISSING = "weights_not_found"
REASON_LIBRARY_MISSING = "clip_library_not_installed"
REASON_LOAD_FAILED = "clip_load_failed"
REASON_DEVICE_UNAVAILABLE = "device_unavailable"
REASON_ARCH_BUILD_FAILED = "clip_arch_build_failed"

#: 权重文件候选名（目录形态）
WEIGHT_FILENAMES = (
    "open_clip_model.safetensors", "open_clip_pytorch_model.bin",
    "model.safetensors", "pytorch_model.bin", "open_clip_model.pt",
)


class BackendUnavailable(RuntimeError):
    """CLIP 后端不可用（权重缺失、库缺失、加载失败）。"""


# --------------------------------------------------------------------------- #
# 实例缓存（进程级，签名一致即复用）
# --------------------------------------------------------------------------- #

_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def model_signature(clip_section: Dict[str, Any]) -> str:
    """模型实例缓存键：库 + 架构 + 权重 + 设备 + 批大小。"""
    section = clip_section or {}
    weights = section.get("weights")
    weights_key = str(Path(str(weights)).resolve()) if weights else "none"
    return "|".join([
        str(section.get("library") or "auto"),
        normalize_arch(section.get("model_name")),
        weights_key,
        str(section.get("device") or "cpu"),
        str(section.get("batch_size") or 8),
    ])


def cached_encoder(signature: str, factory) -> Any:
    """按签名复用编码器实例；同一签名只调用一次 ``factory``。"""
    with _CACHE_LOCK:
        if signature in _CACHE:
            return _CACHE[signature]
    encoder = factory()
    with _CACHE_LOCK:
        _CACHE.setdefault(signature, encoder)
        return _CACHE[signature]


def register_encoder(signature: str, encoder: Any) -> None:
    """登记外部已加载的编码器（复用图文评分实例的唯一入口）。"""
    with _CACHE_LOCK:
        _CACHE[signature] = encoder


def get_registered_encoder(signature: str) -> Any:
    with _CACHE_LOCK:
        return _CACHE.get(signature)


def clear_encoder_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------- #
# 设备与权重解析
# --------------------------------------------------------------------------- #

def resolve_device(requested: str) -> str:
    """解析 ``cpu`` / ``cuda`` / ``auto``；``auto`` 在无 GPU 时退回 cpu。"""
    value = (requested or "cpu").strip().lower()
    if value == "cpu":
        return "cpu"
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - 环境缺 torch
        if value == "auto":
            return "cpu"
        raise BackendUnavailable("%s：torch 未安装" % REASON_DEVICE_UNAVAILABLE) from exc
    if value == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if value.startswith("cuda") and not torch.cuda.is_available():
        raise BackendUnavailable("%s：请求 %s 但 CUDA 不可用" % (REASON_DEVICE_UNAVAILABLE, value))
    return value


def model_root() -> Path:
    """权重根目录：``$MODEL_ROOT`` 优先，否则 ``<算子目录>/weight``。

    模型权重随本算子目录一起交付（``weight/clip/ViT-B-32.pt``）；未设
    ``MODEL_ROOT`` 时默认取本算子目录下的 ``weight/``，因此把单个算子目录
    原样拷到包外也能独立加载。``MODEL_ROOT`` 只作覆盖（如指向外部只读模型库）。
    """
    override = os.environ.get("MODEL_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "weight"


def resolve_model_path(value: Optional[str]) -> Optional[str]:
    """把配置中的权重/模型路径解析为绝对路径；``None``/空串原样返回。

    相对路径按 :func:`model_root` 解析（默认 ``<算子目录>/weight/``），
    与 ``configs/config.yaml`` 中的 ``clip.weights: clip/ViT-B-32.pt`` 约定配套。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    path = Path(text).expanduser()
    return str(path) if path.is_absolute() else str(model_root() / path)


def resolve_weights_path(weights: Optional[str]) -> Optional[Path]:
    """把权重配置解析为具体文件；目录形态时按候选名查找。"""
    if not weights:
        return None
    path = Path(resolve_model_path(weights)).expanduser()
    if not path.exists():
        return None
    if path.is_dir():
        for name in WEIGHT_FILENAMES:
            candidate = path / name
            if candidate.is_file():
                return candidate
        return None
    return path


def sha256_of_file(path, limit_bytes: int = 64 * 1024 * 1024) -> Optional[str]:
    """可选权重指纹；过大文件跳过（避免拖慢启动）。"""
    try:
        if Path(path).stat().st_size > limit_bytes:
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()[:16]
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# open_clip 后端
# --------------------------------------------------------------------------- #

class ClipViTB32Backend:
    """基于 ``open_clip`` 的 CLIP-ViT/B-32 编码器（真实前向，本地权重）。"""

    def __init__(self, model, preprocess, tokenizer, arch: str, weights_path: Optional[Path],
                 device: str, batch_size: int, library: str = "open_clip",
                 untrained_architecture_only: bool = False) -> None:
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer
        self.arch = arch
        self.weights_path = weights_path
        self.device = device
        self.batch_size = max(1, int(batch_size))
        self.library = library
        self.untrained_architecture_only = bool(untrained_architecture_only)
        try:
            self.embed_dim = int(model.text_projection.shape[-1])
        except Exception:  # noqa: BLE001 - 取不到投影维度时退回表1 约定维度
            self.embed_dim = CLIP_EMBED_DIM

    # -- 文本 ------------------------------------------------------------- #
    def encode_texts(self, texts: Sequence[str]) -> List[List[float]]:
        import torch

        prepared = [text if isinstance(text, str) and text.strip() else "空文本"
                    for text in texts]
        vectors: List[List[float]] = []
        with torch.no_grad():
            for start in range(0, len(prepared), self.batch_size):
                batch = prepared[start:start + self.batch_size]
                tokens = self._tokenizer(batch).to(self.device)
                features = self._model.encode_text(tokens)
                features = features / features.norm(dim=-1, keepdim=True).clamp(min=1e-12)
                vectors.extend(row.tolist() for row in features.float().cpu())
        return vectors

    # -- 图像 ------------------------------------------------------------- #
    def encode_images(self, paths: Sequence[Optional[Path]]):
        """逐帧容错编码；返回 ``(vectors, errors)``，失败项向量为 ``None``。"""
        import torch
        from PIL import Image

        batch, errors, positions = [], [], []
        vectors: List[Optional[List[float]]] = [None] * len(paths)
        for index, path in enumerate(paths):
            if path is None:
                errors.append("media_missing")
                continue
            target = Path(path)
            if not target.is_file():
                errors.append("media_missing")
                continue
            try:
                with Image.open(target) as image:
                    tensor = self._preprocess(image.convert("RGB"))
            except Exception:  # noqa: BLE001 - 解码失败必须进异常清单而非伪造
                errors.append("media_unreadable")
                continue
            batch.append(tensor)
            positions.append(index)
            errors.append(None)

        with torch.no_grad():
            for start in range(0, len(batch), self.batch_size):
                chunk = batch[start:start + self.batch_size]
                chunk_positions = positions[start:start + self.batch_size]
                stacked = torch.stack(chunk).to(self.device)
                features = self._model.encode_image(stacked)
                features = features / features.norm(dim=-1, keepdim=True).clamp(min=1e-12)
                for offset, row in enumerate(features.float().cpu()):
                    vectors[chunk_positions[offset]] = row.tolist()
        return vectors, errors

    # -- 身份 ------------------------------------------------------------- #
    def model_identity(self) -> Dict[str, Any]:
        return {
            "name": CLIP_NAME,
            "arch": self.arch,
            "version": (self.weights_path.name if self.weights_path
                        else "uninitialized-architecture"),
            "library": self.library,
            "weights": str(self.weights_path) if self.weights_path else None,
            "weights_sha256": sha256_of_file(self.weights_path) if self.weights_path else None,
            "device": self.device,
            "batch_size": self.batch_size,
            "embed_dim": self.embed_dim,
            "degraded": False,
            "real_backend": True,
            "untrained_architecture_only": self.untrained_architecture_only,
            "score_semantics": "cosine",
            "test_double": False,
        }


def open_clip_encoder(model_name: str = DEFAULT_ARCH, weights: Optional[str] = None,
                      device: str = "cpu", batch_size: int = 8) -> ClipViTB32Backend:
    """构建 ``open_clip`` CLIP-ViT/B-32 编码器。

    ``weights=None`` 表示只构建架构（随机初始化，可用于验证管线，**不可用于评分**）。
    """
    try:
        import open_clip
    except ImportError as exc:
        raise BackendUnavailable("%s：未安装 open_clip" % REASON_LIBRARY_MISSING) from exc

    arch = normalize_arch(model_name)
    resolved_device = resolve_device(device)
    weights_path = resolve_weights_path(weights)

    try:
        model, _train_transform, preprocess = open_clip.create_model_and_transforms(
            arch, pretrained=None, precision="fp32")
    except Exception as exc:  # noqa: BLE001 - 架构构建失败必须显式可见
        raise BackendUnavailable("%s：%s" % (REASON_ARCH_BUILD_FAILED, type(exc).__name__)) from exc

    untrained = weights_path is None
    if weights_path is not None:
        _load_state_dict_into(model, weights_path)

    model = model.to(resolved_device)
    model.eval()
    try:
        tokenizer = open_clip.get_tokenizer(arch)
    except Exception as exc:  # noqa: BLE001
        raise BackendUnavailable("%s：tokenizer 构建失败" % REASON_ARCH_BUILD_FAILED) from exc

    return ClipViTB32Backend(model, preprocess, tokenizer, arch, weights_path,
                             resolved_device, batch_size, library="open_clip",
                             untrained_architecture_only=untrained)


def _load_state_dict_into(model, weights_path: Path) -> None:
    """把本地权重载入模型；只读本地文件，绝不触发下载。"""
    suffix = weights_path.suffix.lower()
    try:
        if suffix == ".safetensors":
            from safetensors.torch import load_file

            state = load_file(str(weights_path))
        else:
            import torch

            try:
                state = torch.load(str(weights_path), map_location="cpu")
            except Exception:
                # torch<2.4 的 torch.load 不会把 TorchScript 归档分派给 jit.load，
                # 会直接抛错。这里显式兜底：先由 Python 以宽字符打开文件，再把
                # 文件对象交给 jit.load（避免 Windows 下窄字符 fopen 打不开非
                # ASCII 路径）。
                with open(weights_path, "rb") as handle:
                    state = torch.jit.load(handle, map_location="cpu")
        if isinstance(state, dict) and "state_dict" in state:
            state = state["state_dict"]
        if not isinstance(state, dict) and hasattr(state, "state_dict"):
            # [11.2] torch.load 对 TorchScript 归档会返回 ScriptModule（torch>=2.4 起直接
            # 分派 jit.load）。内置 weight/clip/ViT-B-32.pt 即 openai CLIP 的 JIT 归档，
            # 必须先取 state_dict 再按普通权重载入；否则 load_state_dict 抛 TypeError，
            # 整条 open_clip 路线在 torch>=2.4 上必然失败。
            state = state.state_dict()
        if isinstance(state, dict):
            state = {key.replace("module.", "", 1): value for key, value in state.items()}
        model.load_state_dict(state, strict=False)
    except Exception as exc:  # noqa: BLE001 - 载入失败必须显式失败，不能静默随机权重
        raise BackendUnavailable("%s：%s" % (REASON_LOAD_FAILED, type(exc).__name__)) from exc


# --------------------------------------------------------------------------- #
# transformers 后端（次选，需本地 config.json + 权重）
# --------------------------------------------------------------------------- #

class TransformersClipBackend:
    """基于 ``transformers.CLIPModel`` 的等价实现（同样只用本地权重）。"""

    def __init__(self, model, processor, arch: str, weights_path: Path, device: str,
                 batch_size: int) -> None:
        self._model = model
        self._processor = processor
        self.arch = arch
        self.weights_path = weights_path
        self.device = device
        self.batch_size = max(1, int(batch_size))
        self.library = "transformers"
        self.untrained_architecture_only = False
        self.embed_dim = int(model.config.projection_dim)

    def encode_texts(self, texts: Sequence[str]) -> List[List[float]]:
        import torch

        prepared = [text if isinstance(text, str) and text.strip() else "空文本"
                    for text in texts]
        vectors: List[List[float]] = []
        with torch.no_grad():
            for start in range(0, len(prepared), self.batch_size):
                batch = prepared[start:start + self.batch_size]
                inputs = self._processor(text=batch, return_tensors="pt", padding=True,
                                         truncation=True, max_length=77)
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                features = self._model.get_text_features(**inputs)
                features = features / features.norm(dim=-1, keepdim=True).clamp(min=1e-12)
                vectors.extend(row.tolist() for row in features.float().cpu())
        return vectors

    def encode_images(self, paths: Sequence[Optional[Path]]):
        import torch
        from PIL import Image

        batch, positions = [], []
        vectors: List[Optional[List[float]]] = [None] * len(paths)
        errors: List[Optional[str]] = []
        for index, path in enumerate(paths):
            if path is None or not Path(path).is_file():
                errors.append("media_missing")
                continue
            try:
                with Image.open(Path(path)) as image:
                    rgb = image.convert("RGB")
                    inputs = self._processor(images=rgb, return_tensors="pt")
            except Exception:  # noqa: BLE001
                errors.append("media_unreadable")
                continue
            batch.append(inputs["pixel_values"][0])
            positions.append(index)
            errors.append(None)

        with torch.no_grad():
            for start in range(0, len(batch), self.batch_size):
                chunk = torch.stack(batch[start:start + self.batch_size]).to(self.device)
                chunk_positions = positions[start:start + self.batch_size]
                features = self._model.get_image_features(pixel_values=chunk)
                features = features / features.norm(dim=-1, keepdim=True).clamp(min=1e-12)
                for offset, row in enumerate(features.float().cpu()):
                    vectors[chunk_positions[offset]] = row.tolist()
        return vectors, errors

    def model_identity(self) -> Dict[str, Any]:
        return {
            "name": CLIP_NAME,
            "arch": self.arch,
            "version": self.weights_path.name if self.weights_path else None,
            "library": self.library,
            "weights": str(self.weights_path) if self.weights_path else None,
            "weights_sha256": sha256_of_file(self.weights_path) if self.weights_path else None,
            "device": self.device,
            "batch_size": self.batch_size,
            "embed_dim": self.embed_dim,
            "degraded": False,
            "real_backend": True,
            "untrained_architecture_only": self.untrained_architecture_only,
            "score_semantics": "cosine",
            "test_double": False,
        }


def transformers_encoder(model_name: str, weights: str, device: str,
                         batch_size: int) -> TransformersClipBackend:
    try:
        from transformers import CLIPModel, CLIPProcessor
    except ImportError as exc:
        raise BackendUnavailable("%s：未安装 transformers" % REASON_LIBRARY_MISSING) from exc

    resolved_device = resolve_device(device)
    source = str(Path(weights).expanduser())
    try:
        model = CLIPModel.from_pretrained(source, local_files_only=True)
        processor = CLIPProcessor.from_pretrained(source, local_files_only=True)
    except Exception as exc:  # noqa: BLE001
        raise BackendUnavailable("%s：%s" % (REASON_LOAD_FAILED, type(exc).__name__)) from exc
    model = model.to(resolved_device)
    model.eval()
    return TransformersClipBackend(model, processor, normalize_arch(model_name),
                                   Path(source), resolved_device, batch_size)


# --------------------------------------------------------------------------- #
# 对外构建入口
# --------------------------------------------------------------------------- #

def _clip_section(config: Any) -> Dict[str, Any]:
    """从 dict 或 ``OperatorConfig`` 中取出 clip 段（含 allow_degraded 回退）。"""
    if config is None:
        return {}
    if isinstance(config, dict):
        section = dict(config.get("clip") or {})
        if "allow_degraded" not in section and "allow_degraded" in config:
            section["allow_degraded"] = config.get("allow_degraded")
        return section
    clip = getattr(config, "clip", None)
    section = clip.model_dump() if hasattr(clip, "model_dump") else dict(clip or {})
    section["allow_degraded"] = bool(getattr(config, "allow_degraded", False))
    return section


def build_clip_backend(config: Any) -> Tuple[Optional[Any], Optional[str], Optional[Dict[str, Any]]]:
    """按配置构建编码器。

    返回 ``(encoder, reason, identity)``：
    - 可用 → ``(encoder, None, identity)``；
    - 显式关闭（``library=none``）→ ``(None, REASON_DISABLED, None)``，不算失败；
    - 不可用且 ``allow_degraded=True`` → ``(None, reason, None)``；
    - 不可用且 ``allow_degraded=False`` → 抛 ``BackendUnavailable``（绝不伪造分数）。
    """
    # 产线任务服务优先；连接失败显式失败，禁止另加载第二份权重。
    # ``shared_clip`` 是**可选**依赖：单算子目录独立部署时包根不存在，导入失败必须静默
    # 走本地权重（未设 MULTIMODAL_CLIP_SESSION 时远程实例本就为 None）。
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from shared_clip.runtime import RemoteClip
    except ImportError:  # 单算子独立部署：不带 shared_clip，直接本地加载
        RemoteClip = None  # type: ignore[assignment,misc]
    section = _clip_section(config)
    if RemoteClip is not None and section.get("library") != "none":
        try:
            remote = RemoteClip.from_env(
                {**section, "weights": resolve_model_path(section.get("weights"))}, "14")
            if remote is not None:
                return remote, None, remote.model_identity()
        except (OSError, RuntimeError, ValueError) as exc:
            raise BackendUnavailable("共享CLIP不可用，不重复加载：%s" % exc) from exc
    section = _clip_section(config)
    allow_degraded = bool(section.get("allow_degraded"))

    library = str(section.get("library") or "auto")
    if library == "none":
        return None, REASON_DISABLED, None

    weights = section.get("weights")
    if not weights:
        if allow_degraded:
            return None, REASON_NO_WEIGHTS, None
        raise BackendUnavailable(
            "%s：表1 要求复用 CLIP-ViT/B-32，但未配置本地权重路径" % REASON_NO_WEIGHTS)

    weights_path = resolve_weights_path(weights)
    if weights_path is None:
        reason = "%s:%s" % (REASON_WEIGHTS_MISSING, Path(str(weights)).name)
        if allow_degraded:
            return None, reason, None
        raise BackendUnavailable("%s：本地权重不存在（%s）" % (REASON_WEIGHTS_MISSING, reason))

    failure: Optional[str] = None
    for candidate in _library_order(library, weights_path):
        try:
            encoder = _build_with(candidate, section, str(weights_path))
            return encoder, None, encoder.model_identity()
        except BackendUnavailable as exc:
            failure = failure or str(exc)
        except Exception as exc:  # noqa: BLE001 - 统一转成显式失败
            failure = failure or "%s：%s" % (REASON_LOAD_FAILED, type(exc).__name__)

    reason = failure or REASON_LOAD_FAILED
    if allow_degraded:
        return None, reason, None
    raise BackendUnavailable(reason)


def _library_order(library: str, weights_path: Path) -> List[str]:
    if library != "auto":
        return [library]
    order = ["open_clip", "transformers", "clip"]
    return order


def _build_with(library: str, section: Dict[str, Any], weights: str):
    model_name = normalize_arch(section.get("model_name"))
    device = section.get("device") or "cpu"
    batch_size = int(section.get("batch_size") or 8)
    signature = "|".join([library, model_name, str(Path(weights).resolve()),
                          str(device), str(batch_size)])

    def factory():
        if library == "open_clip":
            return open_clip_encoder(model_name, weights, device, batch_size)
        if library == "transformers":
            return transformers_encoder(model_name, weights, device, batch_size)
        if library == "clip":
            return _openai_clip_encoder(weights, device, batch_size)
        raise BackendUnavailable("未知 clip.library：%s" % library)

    return cached_encoder(signature, factory)


def _openai_clip_encoder(weights: str, device: str, batch_size: int):
    """``clip``（OpenAI）包后端，best-effort；不可用时报显式原因。"""
    try:
        import clip  # type: ignore
        import torch
    except ImportError as exc:
        raise BackendUnavailable("%s：未安装 clip" % REASON_LIBRARY_MISSING) from exc

    resolved_device = resolve_device(device)
    try:
        model, preprocess = clip.load(weights, device=resolved_device, jit=False)
    except Exception as exc:  # noqa: BLE001
        raise BackendUnavailable("%s：%s" % (REASON_LOAD_FAILED, type(exc).__name__)) from exc
    model.eval()

    class _OpenAIClipBackend:
        library = "clip"
        arch = "ViT-B/32"
        embed_dim = CLIP_EMBED_DIM
        untrained_architecture_only = False

        def __init__(self) -> None:
            self.weights_path = Path(weights)
            self.device = resolved_device
            self.batch_size = max(1, int(batch_size))

        def encode_texts(self, texts):
            prepared = [text if isinstance(text, str) and text.strip() else "空文本"
                        for text in texts]
            vectors = []
            with torch.no_grad():
                for start in range(0, len(prepared), self.batch_size):
                    # 回归 #14 长字幕：OpenAI clip.tokenize 默认 truncate=False，>77 token 会抛
                    # RuntimeError（被算子记成 pending/score_error）；显式 truncate=True 截断到
                    # context length。短文本 token 序列不变，分数口径不变。
                    tokens = clip.tokenize(prepared[start:start + self.batch_size],
                                           truncate=True).to(resolved_device)
                    features = model.encode_text(tokens)
                    features = features / features.norm(dim=-1, keepdim=True).clamp(min=1e-12)
                    vectors.extend(row.tolist() for row in features.float().cpu())
            return vectors

        def encode_images(self, paths):
            from PIL import Image

            vectors: List[Optional[List[float]]] = [None] * len(paths)
            errors: List[Optional[str]] = []
            batch, positions = [], []
            for index, path in enumerate(paths):
                if path is None or not Path(path).is_file():
                    errors.append("media_missing")
                    continue
                try:
                    with Image.open(Path(path)) as image:
                        batch.append(preprocess(image.convert("RGB")))
                except Exception:  # noqa: BLE001
                    errors.append("media_unreadable")
                    continue
                positions.append(index)
                errors.append(None)
            with torch.no_grad():
                for start in range(0, len(batch), self.batch_size):
                    chunk = torch.stack(batch[start:start + self.batch_size]).to(resolved_device)
                    chunk_positions = positions[start:start + self.batch_size]
                    features = model.encode_image(chunk)
                    features = features / features.norm(dim=-1, keepdim=True).clamp(min=1e-12)
                    for offset, row in enumerate(features.float().cpu()):
                        vectors[chunk_positions[offset]] = row.tolist()
            return vectors, errors

        def model_identity(self) -> Dict[str, Any]:
            return {
                "name": CLIP_NAME,
                "arch": self.arch,
                "version": self.weights_path.name,
                "library": "clip",
                "weights": str(self.weights_path),
                "weights_sha256": sha256_of_file(self.weights_path),
                "device": self.device,
                "batch_size": self.batch_size,
                "embed_dim": self.embed_dim,
                "degraded": False,
                "real_backend": True,
                "untrained_architecture_only": False,
                "score_semantics": "cosine",
                "test_double": False,
            }

    return _OpenAIClipBackend()


def clip_availability(config: Any) -> Dict[str, Any]:
    """只做「能否加载」探测，供 ``/health`` 使用；不产生任何分数。"""
    section = _clip_section(config)
    report: Dict[str, Any] = {
        "clip_model": CLIP_NAME,
        "library": section.get("library") or "auto",
        "weights": section.get("weights"),
        "weights_found": bool(resolve_weights_path(section.get("weights"))),
        "libraries_installed": installed_libraries(),
        "identity": None,
        "reason": None,
    }
    probe = dict(section)
    probe["allow_degraded"] = True  # 探测不改写真实任务行为
    try:
        encoder, reason, identity = build_clip_backend({"clip": probe})
    except BackendUnavailable as exc:
        encoder, reason, identity = None, str(exc), None
    report["available"] = encoder is not None
    report["reason"] = reason
    report["identity"] = identity if isinstance(identity, dict) else None
    return report


def installed_libraries() -> List[str]:
    """已安装的 CLIP 相关库（供 ``/health`` 展示，不加载任何模型）。"""
    import importlib.util

    return [name for name in ("open_clip", "clip", "transformers")
            if importlib.util.find_spec(name) is not None]


def warmup(config: Any) -> Optional[Dict[str, Any]]:
    """预热：加载编码器并返回身份（供服务启动时一次性加载，避免首请求抖动）。"""
    _encoder, _reason, identity = build_clip_backend(config)
    return identity
