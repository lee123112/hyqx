"""Qwen3-Embedding 语义向量后端（sentence-transformers）。

交付默认权重为本地 `../models/Qwen3-Embedding-0.6B`（1024 维），加载时强制
`local_files_only=True`，不联网下载、不回退远程模型服务。

权重与依赖不是测试前提：加载失败一律抛 `BackendUnavailable`，
由 processor 决定降级（rule_degraded）或结构化失败，绝不伪造相似度。
"""
import math
import json
import os
from pathlib import Path
from typing import List, Optional
from urllib import request as urlrequest


def _load_local_env() -> None:
    """Load the delivery package .env without adding a runtime dependency."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        break


class BackendUnavailable(RuntimeError):
    """模型后端不可用（缺依赖、缺权重、加载失败）。"""


def _resolve_local_model(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    candidates = [Path.cwd() / path, Path(__file__).resolve().parents[1] / path]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return value


def embedding_available() -> bool:
    """只检查依赖是否可导入；权重是否可用在 `load()` 时才真正判定。"""
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def cosine(left: List[float], right: List[float]) -> float:
    """真实余弦相似度；任一向量为零向量时返回 0.0。"""
    if len(left) != len(right):
        raise ValueError("向量维度不一致")
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


class EmbeddingBackend:
    """惰性加载的 Qwen3-Embedding 编码器，只暴露 encode/similarity 两个能力。"""

    def __init__(self, model_name: str, device: str = "cpu", batch_size: int = 32,
                 model_service_url: Optional[str] = None, timeout_seconds: float = 30.0):
        self.model_name = model_name
        self.device = device
        self.batch_size = int(batch_size)
        self.model_service_url = model_service_url
        self.timeout_seconds = float(timeout_seconds)
        self._model = None
        self._model_version: Optional[str] = None
        self._vector_cache: dict[str, List[float]] = {}

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def model_version(self) -> Optional[str]:
        return self._model_version

    def load(self) -> None:
        if self._model is not None:
            return
        if self.model_service_url:
            self._model = "http"
            self._model_version = self.model_service_url
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:                       # 依赖缺失
            raise BackendUnavailable(f"sentence_transformers_missing:{type(exc).__name__}") from exc
        try:
            model = SentenceTransformer(_resolve_local_model(self.model_name),
                                        device=self._resolve_device(),
                                        local_files_only=True)
        except (OSError, RuntimeError, ValueError, KeyError, AttributeError) as exc:
            raise BackendUnavailable(f"embedding_load_failed:{type(exc).__name__}") from exc
        self._model = model
        self._model_version = self._detect_version(model)

    def _resolve_device(self) -> str:
        if self.device != "auto":
            return self.device
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except ImportError:
            pass
        return "cpu"

    @staticmethod
    def _embedding_dimension(model) -> Optional[int]:
        """兼容 sentence-transformers 新旧接口；取不到维度时返回 None。"""
        for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
            getter = getattr(model, name, None)
            if getter is None:
                continue
            try:
                return int(getter())
            except (TypeError, ValueError, RuntimeError):
                continue
        return None

    def _detect_version(self, model) -> str:
        """如实记录模型标识：优先权重目录名/仓库标识，取不到时退化为模型名。"""
        identity = Path(str(self.model_name).rstrip("/\\")).name or str(self.model_name)
        try:
            inner = model[0].auto_model
            config = getattr(inner, "config", None)
            commit = getattr(config, "_commit_hash", None)
            revision = getattr(config, "_name_or_path", None)
            name = Path(str(revision).rstrip("/\\")).name if revision else ""
            if name:
                identity = name
            if commit:
                return f"{identity}@{commit}"
        except (AttributeError, IndexError, KeyError, TypeError):
            pass
        dim = self._embedding_dimension(model)
        return f"{identity}(dim={dim})" if dim else identity

    def encode(self, texts: List[str]) -> List[List[float]]:
        self.load()
        requested = list(texts)
        missing = list(dict.fromkeys(text for text in requested if text not in self._vector_cache))
        if self.model_service_url:
            _load_local_env()
            if missing:
                body = json.dumps({"model": self.model_name, "input": missing,
                               "encoding_format": "float"},
                                  ensure_ascii=False).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                api_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                req = urlrequest.Request(self.model_service_url, data=body, headers=headers)
                try:
                    with urlrequest.urlopen(req, timeout=self.timeout_seconds) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                except Exception as exc:
                    raise BackendUnavailable(f"embedding_service_failed:{type(exc).__name__}") from exc
                vectors = None
                if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                    rows = payload["data"]
                    if all(isinstance(row, dict) for row in rows):
                        vectors = [row.get("embedding") for row in rows]
                if vectors is None and isinstance(payload, dict):
                    vectors = payload.get("embeddings")
                if not isinstance(vectors, list) or len(vectors) != len(missing):
                    raise BackendUnavailable("embedding_service_invalid_embeddings")
                self._model_version = (payload.get("model") or payload.get("model_version")
                                       or self.model_service_url
                                       if isinstance(payload, dict) else self.model_service_url)
                try:
                    for text, vector in zip(missing, vectors):
                        self._vector_cache[text] = [float(value) for value in vector]
                except (TypeError, ValueError) as exc:
                    raise BackendUnavailable("embedding_service_invalid_vector") from exc
            return [self._vector_cache[text] for text in requested]
        if missing:
            try:
                # The original scorer encodes each candidate side separately.
                # Preserve that input shape: padding across different texts can
                # change GPU scores near the decision thresholds.
                converted = {}
                for text in missing:
                    vectors = self._model.encode([text], batch_size=self.batch_size,
                                                 convert_to_numpy=True, show_progress_bar=False)
                    if len(vectors) != 1:
                        raise ValueError("embedding count mismatch")
                    converted[text] = list(map(float, vectors[0]))
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                raise BackendUnavailable(f"embedding_encode_failed:{type(exc).__name__}") from exc
            self._vector_cache.update(converted)
        return [list(self._vector_cache[text]) for text in requested]

    def similarity(self, left_texts: List[str], right_texts: List[str]) -> List[float]:
        """成对余弦相似度，长度必须一致。"""
        if len(left_texts) != len(right_texts):
            raise ValueError("待比较文本数量不一致")
        if not left_texts:
            return []
        left_vectors = self.encode(left_texts)
        right_vectors = self.encode(right_texts)
        return [cosine(a, b) for a, b in zip(left_vectors, right_vectors)]
