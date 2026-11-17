"""Qwen3-Embedding 语义等价后端（本地权重，可选依赖）。

交付默认权重为 `../models/Qwen3-Embedding-0.6B`（1024 维）：优先用 sentence-transformers
加载（last-token 池化 + L2 归一化，`local_files_only=True`），依赖缺失时退化为
transformers 直接加载并做同样的 last-token 池化 + 归一化。

权重与网络下载不是测试前提：依赖缺失、权重缺失或加载失败一律抛 `BackendUnavailable`，
由 processor 按 `fallback_policy` 选择降级（rule_degraded）或结构化失败，绝不伪造语义分。

本模块只在 `model_backend` 需要模型时被导入；缺依赖时算子仍可正常运行规则降级。
"""
import math
import json
import os
from pathlib import Path
from typing import List, Optional
from urllib import request as urlrequest


def _load_local_env() -> None:
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
    """模型后端不可用（缺依赖、缺权重、加载失败、单条编码失败）。"""


def _resolve_local_model(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    candidates = [Path.cwd() / path, Path(__file__).resolve().parents[1] / path]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return value


def semantic_available() -> bool:
    """只检查依赖是否可导入；权重是否可用在 `load()` 时才真正判定。"""
    for module in ("sentence_transformers", "transformers"):
        try:
            __import__(module)
            return True
        except ImportError:
            continue
    return False


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


class EmbeddingSemanticScorer:
    """惰性加载的 Qwen3-Embedding 编码器，用句子向量余弦作为跨语言语义等价分。"""

    def __init__(self, model_name: str, model_path: Optional[str] = None,
                 device: str = "cpu", batch_size: int = 32,
                 model_service_url: Optional[str] = None, timeout_seconds: float = 30.0):
        self.model_name = model_name
        self.model_path = model_path
        self.device = device
        self.batch_size = int(batch_size)
        self.model_service_url = model_service_url
        self.timeout_seconds = float(timeout_seconds)
        self._impl = None
        self._kind: Optional[str] = None
        self._model_version: Optional[str] = None
        self._vector_cache: dict[str, List[float]] = {}

    @property
    def loaded(self) -> bool:
        return self._impl is not None

    @property
    def model_version(self) -> Optional[str]:
        return self._model_version

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

    def load(self) -> None:
        if self._impl is not None:
            return
        if self.model_service_url:
            self._impl = "http"
            self._kind = "http"
            self._model_version = self.model_service_url
            return
        source = _resolve_local_model(self.model_path or self.model_name)
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            SentenceTransformer = None
        if SentenceTransformer is not None:
            try:
                model = SentenceTransformer(source, device=self._resolve_device(),
                                            local_files_only=True)
            except (OSError, RuntimeError, ValueError, KeyError, AttributeError) as exc:
                raise BackendUnavailable(
                    f"embedding_load_failed:{type(exc).__name__}") from exc
            self._impl = model
            self._kind = "sentence_transformers"
            self._model_version = self._detect_version(model, source)
            return
        try:
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise BackendUnavailable(
                f"transformers_missing:{type(exc).__name__}") from exc
        try:
            tokenizer = AutoTokenizer.from_pretrained(source, local_files_only=True)
            model = AutoModel.from_pretrained(source, local_files_only=True)
        except (OSError, RuntimeError, ValueError, KeyError, AttributeError) as exc:
            raise BackendUnavailable(f"embedding_load_failed:{type(exc).__name__}") from exc
        model.eval()
        self._impl = (tokenizer, model)
        self._kind = "transformers"
        self._model_version = self._detect_version(model, source)

    def _detect_version(self, model, source: str) -> str:
        """如实记录模型标识：优先权重目录名/仓库标识，取不到时退化为来源路径（不编造版本号）。"""
        identity = Path(str(source).rstrip("/\\")).name or str(source)
        config = None
        try:                                  # sentence-transformers 包装：取底层 HF 配置
            config = getattr(model[0].auto_model, "config", None)
        except (AttributeError, IndexError, KeyError, TypeError):
            config = getattr(model, "config", None)
        commit = getattr(config, "_commit_hash", None)
        revision = getattr(config, "_name_or_path", None)
        name = Path(str(revision).rstrip("/\\")).name if revision else ""
        if name:
            identity = name
        if commit:
            return f"{identity}@{commit}"
        hidden = getattr(config, "hidden_size", None)
        return f"{identity}(dim={hidden})" if hidden else identity

    def encode(self, texts: List[str]) -> List[List[float]]:
        self.load()
        if self._kind == "http":
            _load_local_env()
            requested = list(texts)
            missing = []
            seen = set()
            for text in requested:
                if text not in self._vector_cache and text not in seen:
                    missing.append(text)
                    seen.add(text)
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
        if self._kind == "sentence_transformers":
            try:
                vectors = self._impl.encode(list(texts), batch_size=self.batch_size,
                                            convert_to_numpy=True, normalize_embeddings=True)
            except (RuntimeError, ValueError, TypeError) as exc:
                raise BackendUnavailable(
                    f"embedding_encode_failed:{type(exc).__name__}") from exc
            return [[float(value) for value in vector] for vector in vectors]
        import torch
        tokenizer, model = self._impl
        vectors: List[List[float]] = []
        device = self._resolve_device()
        with torch.no_grad():
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start:start + self.batch_size])
                try:
                    encoded = tokenizer(batch, padding=True, truncation=True,
                                        return_tensors="pt")
                    if device != "cpu":
                        encoded = {key: value.to(device) for key, value in encoded.items()}
                    output = model(**encoded).last_hidden_state
                    # Qwen3-Embedding 口径：取每条序列最后一个非 padding token 的表征，
                    # 再做 L2 归一化；不做平均池化，避免输出与交付权重口径不一致。
                    lengths = encoded["attention_mask"].sum(dim=1) - 1
                    index = torch.arange(output.shape[0], device=output.device)
                    pooled = output[index, lengths]
                    pooled = pooled / pooled.norm(dim=1, keepdim=True).clamp(min=1e-12)
                except (RuntimeError, ValueError, TypeError) as exc:
                    raise BackendUnavailable(
                        f"embedding_encode_failed:{type(exc).__name__}") from exc
                vectors.extend(pooled.tolist())
        return [[float(value) for value in vector] for vector in vectors]

    def similarity(self, left_texts: List[str], right_texts: List[str]) -> List[float]:
        if len(left_texts) != len(right_texts):
            raise ValueError("待比较文本数量不一致")
        if not left_texts:
            return []
        return [cosine(a, b) for a, b in zip(self.encode(left_texts), self.encode(right_texts))]
