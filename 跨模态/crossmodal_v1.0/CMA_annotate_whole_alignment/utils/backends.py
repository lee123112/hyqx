"""模型后端：真实 CLIP-ViT/B-32 适配器与"明确降级"表态。

表1 #16 冻结口径（`跨模态算子开发背景与规则.md` §4 #16）：
- 正式模型是 **CLIP-ViT/B-32**，复用 #12 已加载实例；**人工改写描述后必须用同一实例重算**；
- #16 的常规分档消费 #12 的真实分数，本算子只在"人工改描述"时才需要 CLIP；
- 权重/依赖/设备不可用时只能显式 ``degraded``（把重评分延后为待人工）或 ``failed``，
  **绝不用固定分数、随机分数或规则分冒充模型分数**；
- 默认加载**本算子目录**自带的 ``weight/clip/ViT-B-32.pt``（见 ``weight/README.md``），
  不自动联网下载；``$MODEL_ROOT`` 存在时优先按它解析（显式覆盖，不到位也不回退本地）；
- 同签名复用同一实例（``cached_encoder``），编排侧可用 :func:`register_encoder`
  注入 #12 已加载的实例；``shared_clip`` 任务服务是**可选依赖**，整包环境不存在该模块时
  静默回落本算子本地权重，共享实例存在（``MULTIMODAL_CLIP_SESSION`` 已设）时行为不变。
"""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: 表1 正式模型标识
CLIP_NAME = "CLIP-ViT/B-32"
DEFAULT_ARCH = "ViT-B-32"
SUPPORTED_LIBRARIES = ("auto", "open_clip", "clip", "transformers", "none")
SUPPORTED_DEVICES = ("auto", "cpu", "cuda")

_ARCH_ALIASES = {
    "vit-b/32": DEFAULT_ARCH,
    "vit-b-32": DEFAULT_ARCH,
    "clip-vit/b-32": DEFAULT_ARCH,
    "clip-vit-b-32": DEFAULT_ARCH,
    "clip-vit-base-patch32": DEFAULT_ARCH,
    "openai/clip-vit-base-patch32": DEFAULT_ARCH,
    "b-32": DEFAULT_ARCH,
}

REASON_DISABLED = "backend_disabled_by_config"
REASON_NO_WEIGHTS = "model_weights_not_configured"


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


class BackendUnavailable(RuntimeError):
    """请求的后端在当前环境不可用（依赖缺失、权重缺失、设备不可用、推理失败等）。"""


def _section(cfg: dict, name: str) -> Dict[str, Any]:
    value = (cfg or {}).get(name)
    return value if isinstance(value, dict) else {}


def normalize_arch(model_name: Optional[str]) -> str:
    """把常见 CLIP 名称写法统一为 open_clip 架构名（默认 ViT-B-32）。"""
    if not isinstance(model_name, str) or not model_name.strip():
        return DEFAULT_ARCH
    key = model_name.strip().lower()
    return _ARCH_ALIASES.get(key, model_name.strip())


def resolve_device(name: Optional[str]) -> str:
    """解析设备；``cuda`` 在无可用 CUDA 时退回 ``cpu``（降级原因由调用方记录）。"""
    requested = (name or "auto").strip().lower()
    if requested == "cpu":
        return "cpu"
    try:
        torch = importlib.import_module("torch")
        available = bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        available = False
    if requested == "cuda":
        return "cuda" if available else "cpu"
    return "cuda" if available else "cpu"


def model_signature(model_cfg: Dict[str, Any]) -> str:
    """模型实例签名：同签名复用同一实例（支持全产线复用一份 CLIP）。"""
    return "|".join(str(model_cfg.get(key)) for key in
                    ("library", "model_name", "weights", "device", "batch_size"))


_ENCODER_CACHE: Dict[str, Any] = {}


def register_encoder(signature: str, encoder: Any) -> None:
    """注入外部已加载的编码器实例（编排侧复用 #12 实例的唯一入口，不做目录间 import）。"""
    _ENCODER_CACHE[signature] = encoder


def cached_encoder(signature: str, factory: Callable[[], Any]) -> Any:
    """按签名缓存编码器实例，避免同一进程重复加载权重。"""
    if signature not in _ENCODER_CACHE:
        _ENCODER_CACHE[signature] = factory()
    return _ENCODER_CACHE[signature]


def clear_encoder_cache() -> None:
    _ENCODER_CACHE.clear()


def _require_library(module_name: str, reason: str) -> Any:
    if importlib.util.find_spec(module_name) is None:
        raise BackendUnavailable(reason)
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        raise BackendUnavailable("%s 不可用：%s" % (module_name, type(exc).__name__)) from exc


def load_image(path: Optional[Path]) -> Tuple[Any, Optional[str]]:
    """解码图像；缺失/损坏返回可解释原因，绝不伪造向量。"""
    if path is None:
        return None, "media_missing"
    try:
        from PIL import Image

        with Image.open(str(path)) as handle:
            return handle.convert("RGB"), None
    except FileNotFoundError:
        return None, "media_missing"
    except Exception:  # noqa: BLE001 - 损坏文件按可解释原因记录，不抛栈
        return None, "media_unreadable"


class ClipViTB32Backend:
    """CLIP-ViT/B-32 真实后端：文本与图像分别编码后做单位归一化，余弦为真实值。

    ``weights`` 必须是**本地**权重路径；``weights=None`` 只用于适配器连通性测试
    （此时 ``untrained_architecture_only=True``，公共入口不允许这种配置）。
    """

    name = CLIP_NAME
    degraded = False

    def __init__(self, model_name: str = DEFAULT_ARCH, weights: Optional[str] = None,
                 device: str = "cpu", batch_size: int = 8, library: str = "auto",
                 torch_module: Any = None) -> None:
        self.model_name = normalize_arch(model_name)
        self.weights = weights
        self.library = library
        self.device = device
        self.batch_size = max(1, int(batch_size))
        self.untrained_architecture_only = weights is None
        self._torch = torch_module or _require_library(
            "torch", "torch 不可用，无法加载 CLIP 权重")
        self._impl = self._load()
        self.library = self._impl.get("library", library)

    # -- 加载 --
    def _load(self) -> Dict[str, Any]:
        candidates = ["open_clip", "clip", "transformers"] \
            if self.library == "auto" else [self.library]
        errors: List[str] = []
        for library in candidates:
            try:
                loader = {"open_clip": self._load_open_clip, "clip": self._load_clip,
                          "transformers": self._load_transformers}[library]
                return loader()
            except BackendUnavailable as exc:
                errors.append("%s: %s" % (library, exc))
        raise BackendUnavailable("；".join(errors) or "没有可用的 CLIP 后端")

    def _load_open_clip(self) -> Dict[str, Any]:
        module = _require_library("open_clip", "open_clip 不可用")
        if self.weights is not None:
            weights_path = Path(self.weights)
            if not weights_path.exists():
                raise BackendUnavailable("weights_not_found: %s" % weights_path.name)
            if weights_path.is_dir():
                local = _find_open_clip_weights(weights_path)
                if local is None:
                    raise BackendUnavailable(
                        "weights_not_found: 目录中没有 open_clip 权重文件")
                weights_path = local
            pretrained: Any = str(weights_path)
        else:
            pretrained = None
        try:
            model, _train_preprocess, preprocess = module.create_model_and_transforms(
                self.model_name, pretrained=pretrained, device=self.device)
            tokenizer = module.get_tokenizer(self.model_name)
        except Exception as exc:  # noqa: BLE001 - 权重不匹配等一律视为后端不可用
            raise BackendUnavailable("加载本地权重失败：%s" % type(exc).__name__) from exc
        model.eval()
        return {"library": "open_clip", "module": module, "model": model,
                "preprocess": preprocess, "tokenizer": tokenizer}

    def _load_clip(self) -> Dict[str, Any]:
        module = _require_library("clip", "openai clip 包不可用")
        if self.weights is None:
            raise BackendUnavailable(REASON_NO_WEIGHTS)
        weights_path = Path(self.weights)
        if not weights_path.exists():
            raise BackendUnavailable("weights_not_found: %s" % weights_path.name)
        try:
            model, preprocess = module.load(str(weights_path), device=self.device,
                                            jit=False)
            tokenizer = module.tokenize
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailable("加载本地权重失败：%s" % type(exc).__name__) from exc
        model.eval()
        return {"library": "clip", "module": module, "model": model,
                "preprocess": preprocess, "tokenizer": tokenizer}

    def _load_transformers(self) -> Dict[str, Any]:
        module = _require_library("transformers", "transformers 不可用")
        if self.weights is None:
            raise BackendUnavailable(REASON_NO_WEIGHTS)
        weights_path = Path(self.weights)
        if not weights_path.exists():
            raise BackendUnavailable("weights_not_found: %s" % weights_path.name)
        try:
            model = module.CLIPModel.from_pretrained(
                str(weights_path), local_files_only=True)
            processor = module.CLIPProcessor.from_pretrained(
                str(weights_path), local_files_only=True)
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailable("加载本地权重失败：%s" % type(exc).__name__) from exc
        model.eval()
        model.to(self.device)
        return {"library": "transformers", "module": module, "model": model,
                "processor": processor}

    # -- 血缘/业务记录用 --
    def model_identity(self) -> Dict[str, Any]:
        return {
            "name": CLIP_NAME,
            "version": self.model_name,
            "library": self.library,
            "weights": Path(self.weights).name if self.weights else None,
            "device": self.device,
            "degraded": False,
            "real_backend": not self.untrained_architecture_only,
            "untrained_architecture_only": self.untrained_architecture_only,
            "score_semantics": "cosine",
        }

    # -- 推理 --
    def encode_texts(self, texts: Sequence[str]) -> List[List[float]]:
        torch = self._torch
        vectors: List[List[float]] = []
        try:
            with torch.no_grad():
                for start in range(0, len(texts), self.batch_size):
                    chunk = [text or "" for text in texts[start:start + self.batch_size]]
                    features = self._text_features(chunk)
                    features = features / features.norm(dim=-1, keepdim=True)
                    vectors.extend([[float(value) for value in row]
                                    for row in features.detach().cpu().tolist()])
        except BackendUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 整批推理失败按后端不可用上报
            raise BackendUnavailable("模型推理失败：%s" % type(exc).__name__) from exc
        return vectors

    def encode_images(self, paths: Sequence[Optional[Path]]):
        torch = self._torch
        vectors: List[Optional[List[float]]] = [None] * len(paths)
        errors: List[Optional[str]] = [None] * len(paths)
        decoded: List[Tuple[int, Any]] = []
        for index, path in enumerate(paths):
            image, error = load_image(path)
            if error is not None:
                errors[index] = error
                continue
            decoded.append((index, image))
        try:
            with torch.no_grad():
                for start in range(0, len(decoded), self.batch_size):
                    chunk = decoded[start:start + self.batch_size]
                    tensors = [self._image_tensor(image) for _index, image in chunk]
                    batch = torch.stack(tensors).to(self.device)
                    features = self._image_features(batch)
                    features = features / features.norm(dim=-1, keepdim=True)
                    for (position, _image), row in zip(
                            chunk, features.detach().cpu().tolist()):
                        vectors[position] = [float(value) for value in row]
        except BackendUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailable("模型推理失败：%s" % type(exc).__name__) from exc
        return vectors, errors

    # -- 各库的前向细节 --
    def _text_features(self, chunk: Sequence[str]):
        impl = self._impl
        if impl.get("library") == "transformers":
            inputs = impl["processor"](text=list(chunk), return_tensors="pt",
                                       padding=True, truncation=True)
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            return impl["model"].get_text_features(**inputs)
        if impl.get("library") == "clip":
            tokens = impl["tokenizer"](list(chunk), truncate=True).to(self.device)
            return impl["model"].encode_text(tokens)
        tokens = impl["tokenizer"](list(chunk)).to(self.device)
        return impl["model"].encode_text(tokens)

    def _image_tensor(self, image: Any):
        impl = self._impl
        if impl.get("library") == "transformers":
            inputs = impl["processor"](images=image, return_tensors="pt")
            return inputs["pixel_values"][0]
        return impl["preprocess"](image)

    def _image_features(self, batch: Any):
        impl = self._impl
        if impl.get("library") == "transformers":
            return impl["model"].get_image_features(pixel_values=batch)
        return impl["model"].encode_image(batch)


def _find_open_clip_weights(directory: Path) -> Optional[Path]:
    for name in ("open_clip_model.safetensors", "open_clip_pytorch_model.bin",
                 "model.safetensors", "pytorch_model.bin"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def open_clip_encoder(model_name: str = DEFAULT_ARCH, weights: Optional[str] = None,
                      device: str = "cpu", batch_size: int = 1) -> ClipViTB32Backend:
    """构造 open_clip 版编码器。

    ``weights=None`` 表示**只构建架构**（随机初始化），仅供适配器连通性测试使用；
    生产路径必须通过 :func:`build_clip_backend` 传入真实本地权重。
    """
    return ClipViTB32Backend(model_name=model_name, weights=weights, device=device,
                             batch_size=batch_size, library="open_clip")


# ------------------------------------------------------------------ 后端选择

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


def build_clip_backend(cfg: dict, strict: Optional[bool] = None):
    """按配置选择图文评分后端。

    返回 ``(encoder|None, degrade_reason|None, model_identity|None)``。

    ``strict`` 语义：
    - ``None``（默认）：按配置的 ``model.allow_degraded`` 决定是否抛错；
    - ``False``：**永不抛错**，不可用时返回 ``(None, reason, None)``——
      #16 常规分档不需要本地 CLIP，只有"人工改描述重评分"才需要，因此默认宽松构建；
    - ``True``：不可用即抛 :class:`BackendUnavailable`。
    """
    # 产线任务服务优先；连接失败显式失败，禁止另加载第二份权重。
    # ``shared_clip`` 为可选依赖：包外独立部署没有该模块时静默回落本地权重加载。
    remote_class = _optional_remote_clip()
    section = _section(cfg, "model")
    if remote_class is not None and section.get("library") != "none":
        try:
            remote = remote_class.from_env(
                {**section, "weights": resolve_model_path(section.get("weights"))}, "16")
            if remote is not None:
                return remote, None, remote.model_identity()
        except (OSError, RuntimeError, ValueError) as exc:
            raise BackendUnavailable("共享CLIP不可用，不重复加载：%s" % exc) from exc
    model_cfg = _section(cfg, "model")
    library = str(model_cfg.get("library") or "auto").strip().lower()
    model_name = model_cfg.get("model_name") or DEFAULT_ARCH
    weights = resolve_model_path(model_cfg.get("weights"))
    device = resolve_device(model_cfg.get("device"))
    batch_size = int(model_cfg.get("batch_size") or 8)
    if strict is None:
        strict = not bool(model_cfg.get("allow_degraded", True))

    if library == "none":
        return _degrade(REASON_DISABLED, strict)

    if not weights:
        return _degrade(REASON_NO_WEIGHTS, strict)

    weights_path = Path(str(weights))
    if not weights_path.exists():
        return _degrade("weights_not_found: %s" % weights_path.name, strict)

    signature = model_signature({"library": library, "model_name": model_name,
                                 "weights": str(weights_path), "device": device,
                                 "batch_size": batch_size})

    def factory() -> Any:
        return ClipViTB32Backend(model_name=model_name, weights=str(weights_path),
                                 device=device, batch_size=batch_size, library=library)

    try:
        encoder = cached_encoder(signature, factory)
    except BackendUnavailable as exc:
        return _degrade(str(exc), strict)
    return encoder, None, encoder.model_identity()


def _degrade(reason: str, strict: bool):
    if strict:
        raise BackendUnavailable(reason)
    return None, reason, None


# ------------------------------------------------------------------ 余弦

def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """真实余弦相似度；向量必须等长，任一为零向量时返回 0.0（可解释而非报错）。"""
    import math

    left_values = [float(item) for item in left]
    right_values = [float(item) for item in right]
    if len(left_values) != len(right_values):
        raise ValueError("向量维度不一致：%d vs %d" % (len(left_values), len(right_values)))
    dot = math.fsum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(math.fsum(value * value for value in left_values))
    right_norm = math.sqrt(math.fsum(value * value for value in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))
