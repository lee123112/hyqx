"""图文 CLIP 反向检索（表1 #20「图文子集用 CLIP 反向检索」）。

口径（与 DATA_SCHEMA §7.4 一致）：
- 候选集合是**本批次实际参与质检的图文样本图像**，不是历史全库；
- ``rank`` 为「严格更相似的候选数 + 1」，并列按同序处理；
- 只有 top-1 命中 **且** 自身相似度不低于 ``min_consistency`` 才算通过；
- 候选集合不足 2 个、图像无法编码时一律记 ``insufficient_evidence``
  （**不允许在只有一个候选时声称 Recall@1 = 1**）；
- 没有可用权重（或未注入已加载实例）时返回 ``model_unavailable`` 并
  保持 ``similarity = None``，**绝不使用随机初始化权重产出「分数」**。

正式运行只接受本地权重实例或工程侧注入的共享实例；默认从本算子自带的
``weight/clip/ViT-B-32.pt`` 加载（``$MODEL_ROOT`` 覆盖优先，见 ``weight/README.md``）。
``shared_clip`` 是**可选依赖**：整包编排环境存在共享实例时行为不变，单算子目录被复制到
包外独立部署（没有 ``shared_clip``）时静默回落本地权重。测试使用的
``tests/conftest.StubEncoder`` 属于明确标注的测试替身，仅验证排序与分档逻辑。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ENCODER_LIBRARY = "open_clip"

#: 工程侧注入的共享编码器（跨算子复用已加载实例，避免重复占显存）
_SHARED: Dict[str, Any] = {"encoder": None}


class EncoderError(RuntimeError):
    """编码器不可用或编码失败。"""


class Encoder:
    """编码器接口：``encode_texts`` 返回 ``(n, d)``，``encode_images`` 返回逐图向量（失败为 None）。"""

    name = "encoder"
    version = "unknown"
    library = ENCODER_LIBRARY
    is_test_double = False

    def encode_texts(self, texts: Sequence[str]):  # pragma: no cover - 接口
        raise NotImplementedError

    def encode_images(self, paths: Sequence[Path]):  # pragma: no cover - 接口
        raise NotImplementedError


def get_shared_encoder() -> Optional[Encoder]:
    return _SHARED["encoder"]


def set_shared_encoder(encoder: Optional[Encoder]) -> None:
    """注入/清除共享编码器；传 ``None`` 表示当前确实没有可用权重。"""
    _SHARED["encoder"] = encoder


def available_encoder_sources() -> Dict[str, Any]:
    try:
        import open_clip  # noqa: F401,PLC0415

        library_available = True
    except Exception:  # noqa: BLE001
        library_available = False
    shared = get_shared_encoder()
    return {
        "library": ENCODER_LIBRARY,
        "library_available": library_available,
        "shared_instance": None if shared is None else {
            "name": getattr(shared, "name", "unknown"),
            "version": getattr(shared, "version", "unknown"),
            "test_double": bool(getattr(shared, "is_test_double", False)),
        },
    }


class ClipEncoder(Encoder):
    """open_clip 本地权重编码器（CPU/GPU 由配置决定，不联网下载）。"""

    library = ENCODER_LIBRARY

    def __init__(self, model, preprocess, tokenizer, name: str, weights: str,
                 device: str) -> None:
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer
        self.name = name
        self.weights = weights
        self.device = device
        self.version = "open_clip"

    def encode_texts(self, texts: Sequence[str]):
        import torch  # noqa: PLC0415

        tokens = self._tokenizer(list(texts)).to(self.device)
        with torch.no_grad():
            features = self._model.encode_text(tokens)
        return features.detach().cpu().numpy().astype("float64")

    def encode_images(self, paths: Sequence[Path]):
        import numpy as np  # noqa: PLC0415
        import torch  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        vectors: List[Optional[Any]] = []
        for path in paths:
            try:
                with Image.open(str(path)) as image:
                    tensor = self._preprocess(image.convert("RGB")).unsqueeze(0).to(self.device)
                with torch.no_grad():
                    feature = self._model.encode_image(tensor)
                vectors.append(feature.detach().cpu().numpy().astype("float64")[0])
            except Exception:  # noqa: BLE001 - 单张图失败不影响其他候选
                vectors.append(None)
        return vectors


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


def _optional_reverse_remote_clip() -> Optional[Any]:
    """可选依赖：返回 ``shared_clip.runtime.ReverseRemoteClip``，模块不存在时返回 ``None``。

    单算子目录被复制到包外独立部署时没有 ``shared_clip``，此时必须静默回落本算子
    ``weight/`` 本地加载；不得因可选依赖缺失抛 ImportError，也不改变共享实例存在时的行为。
    """
    import sys

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from shared_clip.runtime import ReverseRemoteClip  # noqa: PLC0415

        return ReverseRemoteClip
    except ImportError:
        return None


def build_encoder(clip_config: Optional[Dict[str, Any]]) -> Tuple[Optional[Encoder], Optional[str]]:
    """按配置准备编码器。

    返回 ``(encoder, reason)``：``encoder`` 为 ``None`` 时 ``reason`` 说明降级原因。
    优先级：产线共享任务服务（可选）→ 本地权重 → 进程内注入实例 → 无（``clip_weights_missing``）。
    """
    # 产线任务服务优先；连接失败显式失败，禁止另加载第二份权重。
    # ``shared_clip`` 为可选依赖：包外独立部署没有该模块时静默回落本地权重加载。
    remote_class = _optional_reverse_remote_clip()
    section = dict(clip_config or {})
    if remote_class is not None and section.get("library") != "none":
        try:
            remote = remote_class.from_env(
                {**section, "weights": resolve_model_path(section.get("weights"))}, "20")
            if remote is not None:
                return remote, None
        except (OSError, RuntimeError, ValueError) as exc:
            raise EncoderError("共享CLIP不可用，不重复加载：%s" % exc) from exc
    config = dict(clip_config or {})
    weights = resolve_model_path(config.get("weights"))
    device = str(config.get("device") or "cpu")
    model_name = str(config.get("model_name") or "ViT-B-32")

    if weights:
        weight_path = Path(str(weights))
        if not weight_path.is_file():
            return None, "clip_weights_missing"
        try:
            import open_clip  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None, "clip_library_missing"
        try:
            model, _, preprocess = open_clip.create_model_and_transforms(
                model_name, pretrained=str(weight_path), device=device)
            tokenizer = open_clip.get_tokenizer(model_name)
            model.eval()
            return ClipEncoder(model, preprocess, tokenizer, model_name,
                               str(weight_path), device), None
        except Exception:  # noqa: BLE001 - 权重不匹配/损坏统一按不可用处理
            return None, "clip_load_failed"

    shared = get_shared_encoder()
    if shared is not None:
        return shared, None
    return None, "clip_weights_missing"


def model_payload(encoder: Optional[Encoder], reason: Optional[str] = None) -> Dict[str, Any]:
    if encoder is None:
        return {"name": None, "version": None, "library": ENCODER_LIBRARY,
                "degraded": True, "reason": reason or "model_unavailable",
                "test_double": False}
    return {
        **(encoder.model_identity() if hasattr(encoder, "model_identity") else {}),
        "name": getattr(encoder, "name", "unknown"),
        "version": getattr(encoder, "version", "unknown"),
        "library": getattr(encoder, "library", ENCODER_LIBRARY),
        "degraded": False,
        "reason": None,
        "test_double": bool(getattr(encoder, "is_test_double", False)),
    }


def _normalize(matrix):
    import numpy as np  # noqa: PLC0415

    array = np.asarray(matrix, dtype="float64")
    if array.ndim == 1:
        array = array.reshape(1, -1)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return array / norms


def _image_digest(path: Path) -> Optional[str]:
    """图像内容摘要（判重只用；读不到文件时返回 ``None``，按「无法去重」处理）。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha1(data).hexdigest()


def encode_images_deduped(encoder: Encoder, paths: Sequence[Path]) -> Tuple[List[Any], int]:
    """按文件内容去重后编码图像，返回 ``(逐条向量, 实际编码的去重图像数)``。

    本批数据常出现「同一图像被扩成多条样本」，重复图像对候选池只贡献一份证据；
    这里只对唯一图像跑一次编码器，向量按引用回填（结果与逐条编码完全一致）。
    读不到摘要的条目退回逐条编码，不静默丢弃。
    """
    digests = [_image_digest(path) for path in paths]
    first_seen: Dict[str, Path] = {}
    for digest, path in zip(digests, paths):
        if digest is not None and digest not in first_seen:
            first_seen[digest] = Path(path)
    vectors_by_digest: Dict[str, Any] = {}
    if first_seen:
        encoded = encoder.encode_images(list(first_seen.values()))
        for digest, vector in zip(first_seen.keys(), encoded):
            vectors_by_digest[digest] = vector
    vectors: List[Any] = []
    for path, digest in zip(paths, digests):
        if digest is None:
            encoded = encoder.encode_images([Path(path)])
            vectors.append(encoded[0] if encoded else None)
        else:
            vectors.append(vectors_by_digest.get(digest))
    return vectors, len(first_seen)


def _insufficient(sample_id: str, reason: str, *, model: Dict[str, Any],
                  pool_size: int, top_k: int) -> Dict[str, Any]:
    return {
        "sample_id": sample_id,
        "check_status": "insufficient_evidence",
        "consistent": None,
        "hit_at_1": None,
        "rank": None,
        "similarity": None,
        "own_similarity": None,
        "reason": reason,
        "pool_size": int(pool_size),
        "top_k": int(top_k),
        "candidates": [],
        "model": dict(model),
        "test_double": bool(model.get("test_double")),
    }


def verify_pool(entries: Sequence[Dict[str, Any]], encoder: Optional[Encoder],
                top_k: int = 5, min_consistency: float = 0.25) -> Dict[str, Any]:
    """对整池图文样本做反向检索一致性检查。

    ``entries`` 元素形如 ``{"sample_id", "text", "image", "image_path"}``。
    """
    model = model_payload(encoder)
    result: Dict[str, Any] = {
        "model_available": encoder is not None,
        "model_failed": False,
        "pool_size": 0,
        "top_k": int(top_k),
        "min_consistency": float(min_consistency),
        "model": model,
        "results": {},
    }
    if not entries:
        return result

    if encoder is None:
        for entry in entries:
            result["results"][entry["sample_id"]] = _insufficient(
                entry["sample_id"], "model_unavailable", model=model, pool_size=0,
                top_k=top_k)
        return result

    texts = [str(entry.get("text") or "") for entry in entries]
    paths = [Path(entry["image_path"]) for entry in entries]
    try:
        text_matrix = _normalize(encoder.encode_texts(texts))
    except Exception as exc:  # noqa: BLE001 - 编码失败按模型不可用降级
        result["model_failed"] = True
        model = dict(model, degraded=True, reason="encoder_failed")
        result["model"] = model
        for entry in entries:
            result["results"][entry["sample_id"]] = _insufficient(
                entry["sample_id"], "encoder_failed", model=model, pool_size=0,
                top_k=top_k)
        return result

    raw_images, unique_images = encode_images_deduped(encoder, paths)
    image_vectors: List[Optional[Any]] = []
    for item in raw_images:
        image_vectors.append(None if item is None else _normalize([item])[0])

    valid = [(entry["sample_id"], vector)
             for entry, vector in zip(entries, image_vectors) if vector is not None]
    result["pool_size"] = len(valid)
    # 候选池去重统计：pool_size 仍按「样本图像条目」计，编码只跑唯一图像
    result["unique_image_count"] = int(unique_images)
    result["duplicate_image_entries"] = max(0, len(entries) - int(unique_images))

    for index, entry in enumerate(entries):
        sample_id = entry["sample_id"]
        own_vector = image_vectors[index]
        text_vector = text_matrix[index] if index < len(text_matrix) else None
        if text_vector is None or own_vector is None:
            result["results"][sample_id] = _insufficient(
                sample_id, "image_unreadable", model=model, pool_size=len(valid),
                top_k=top_k)
            continue
        if len(valid) < 2:
            result["results"][sample_id] = _insufficient(
                sample_id, "insufficient_candidates", model=model,
                pool_size=len(valid), top_k=top_k)
            continue

        import numpy as np  # noqa: PLC0415

        similarities: List[Tuple[str, float, bool]] = []
        for candidate_id, vector in valid:
            score = float(np.dot(text_vector, vector))
            similarities.append((candidate_id, score, candidate_id == sample_id))
        similarities.sort(key=lambda item: (-item[1], item[0]))

        own_score = next(score for cid, score, is_own in similarities if is_own)
        rank = 1 + sum(1 for _cid, score, _is_own in similarities
                       if score > own_score + 1e-12)
        hit = rank == 1
        if not hit:
            consistent, reason = False, "not_top1"
        elif own_score < float(min_consistency):
            consistent, reason = False, "below_min_consistency"
        else:
            consistent, reason = True, None

        candidates = [{"sample_id": cid, "similarity": round(score, 6)}
                      for cid, score, _is_own in similarities[:max(1, int(top_k))]]
        result["results"][sample_id] = {
            "sample_id": sample_id,
            "check_status": "checked",
            "consistent": consistent,
            "hit_at_1": hit,
            "rank": rank,
            "similarity": round(own_score, 6),
            "own_similarity": round(own_score, 6),
            "reason": reason,
            "pool_size": len(valid),
            "top_k": int(top_k),
            "candidates": candidates,
            "model": dict(model),
            "test_double": bool(model.get("test_double")),
        }
    return result


def to_reverse_record(sample_id: str, entry_result: Dict[str, Any],
                      extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把单样本结果整理为 ``20_reverse_verification.jsonl`` 的一行。"""
    record = {
        "sample_id": sample_id,
        "check_status": entry_result.get("check_status"),
        "consistent": entry_result.get("consistent"),
        "hit_at_1": entry_result.get("hit_at_1"),
        "rank": entry_result.get("rank"),
        "similarity": entry_result.get("similarity"),
        "source": entry_result.get("source"),
        "reason": entry_result.get("reason"),
        "pool_size": entry_result.get("pool_size"),
        "top_k": entry_result.get("top_k"),
        "candidates": entry_result.get("candidates") or [],
        "model": entry_result.get("model"),
    }
    if extra:
        record.update(extra)
    return record


def upstream_reverse_result(sample_id: str, upstream: Dict[str, Any]) -> Dict[str, Any]:
    """把上游结论整理成本算子内部结果结构（``source = upstream``）。"""
    passed = upstream.get("passed")
    score = upstream.get("score")
    return {
        "sample_id": sample_id,
        "check_status": "checked",
        "consistent": bool(passed) if isinstance(passed, bool) else None,
        "hit_at_1": bool(passed) if isinstance(passed, bool) else None,
        "rank": 1 if passed else None,
        "similarity": float(score) if isinstance(score, (int, float)) else None,
        "own_similarity": float(score) if isinstance(score, (int, float)) else None,
        "reason": None if passed else "reverse_inconsistent",
        "pool_size": None,
        "top_k": None,
        "candidates": [],
        "model": upstream.get("model") or {"name": None, "degraded": False,
                                          "source": "upstream"},
        "source": "upstream",
        "upstream_path": upstream.get("path"),
        "test_double": False,
    }
