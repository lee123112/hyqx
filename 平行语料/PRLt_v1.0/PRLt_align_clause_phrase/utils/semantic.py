"""mDeBERTa-v3-base 语义后端（transformers + torch，可选依赖）。

正式版模型为本地 `models/mdeberta-v3-base`（microsoft/mdeberta-v3-base，768 维）。
中文路径下 sentencepiece 直接加载 spm.model 会因 C++ 非 ASCII 路径问题失败，因此
tokenizer 固定走 `AutoTokenizer(..., use_fast=True)`：模型目录已提供 tokenizer.json，
fast tokenizer 不依赖 sentencepiece 的路径加载。

权重与网络下载不是测试前提：依赖缺失、权重缺失或加载失败一律抛 `BackendUnavailable`，
由 processor 按 `fallback_policy` 选择降级（rule_degraded）或结构化失败，绝不伪造相似度。

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


def semantic_available() -> bool:
    """只检查依赖是否可导入；权重是否可用在 `load()` 时才真正判定。"""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
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


class MdebertaSemanticScorer:
    """惰性加载的 mDeBERTa-v3 编码器，用平均池化向量余弦作为跨语言语义分。"""

    def __init__(self, model_name: str, model_path: Optional[str] = None,
                 device: str = "cpu", batch_size: int = 32,
                 model_service_url: Optional[str] = None, timeout_seconds: float = 30.0):
        self.model_name = model_name
        self.model_path = model_path
        self.device = device
        self.batch_size = int(batch_size)
        self.model_service_url = model_service_url
        self.timeout_seconds = float(timeout_seconds)
        self._tokenizer = None
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
            import torch  # noqa: F401
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise BackendUnavailable(f"transformers_missing:{type(exc).__name__}") from exc
        source = _resolve_local_model(self.model_path or self.model_name)
        try:
            # 固定 fast tokenizer：中文路径下不触发 sentencepiece 的原生路径加载。
            tokenizer = AutoTokenizer.from_pretrained(source, use_fast=True)
            model = AutoModel.from_pretrained(source)
        except (OSError, RuntimeError, ValueError, KeyError, AttributeError) as exc:
            raise BackendUnavailable(f"mdeberta_load_failed:{type(exc).__name__}") from exc
        model.eval()
        if self.device != "cpu":
            try:
                model.to(self._resolve_device())
            except (RuntimeError, ValueError) as exc:
                raise BackendUnavailable(f"mdeberta_device_failed:{type(exc).__name__}") from exc
        self._tokenizer = tokenizer
        self._model = model
        self._model_version = self._detect_version(model, source)

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

    def _detect_version(self, model, source: str) -> str:
        """优先取权重仓库 commit，取不到时退化为模型标识（不编造版本号）。"""
        config = getattr(model, "config", None)
        commit = getattr(config, "_commit_hash", None)
        revision = getattr(config, "_name_or_path", None) or source
        if commit:
            return f"{revision}@{commit}"
        hidden = getattr(config, "hidden_size", None)
        return f"{source}(hidden={hidden})" if hidden else source

    def encode(self, texts: List[str]) -> List[List[float]]:
        self.load()
        if self.model_service_url:
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
                    raise BackendUnavailable(f"mdeberta_service_failed:{type(exc).__name__}") from exc
                vectors = None
                if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                    rows = payload["data"]
                    if all(isinstance(row, dict) for row in rows):
                        vectors = [row.get("embedding") for row in rows]
                if vectors is None and isinstance(payload, dict):
                    vectors = payload.get("embeddings")
                if not isinstance(vectors, list) or len(vectors) != len(missing):
                    raise BackendUnavailable("mdeberta_service_invalid_embeddings")
                self._model_version = (payload.get("model") or payload.get("model_version")
                                       or self.model_service_url
                                       if isinstance(payload, dict) else self.model_service_url)
                try:
                    for text, vector in zip(missing, vectors):
                        self._vector_cache[text] = [float(value) for value in vector]
                except (TypeError, ValueError) as exc:
                    raise BackendUnavailable("mdeberta_service_invalid_vector") from exc
            return [self._vector_cache[text] for text in requested]
        import torch
        vectors: List[List[float]] = []
        with torch.no_grad():
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start:start + self.batch_size])
                try:
                    encoded = self._tokenizer(batch, padding=True, truncation=True,
                                              return_tensors="pt")
                    if self.device != "cpu":
                        encoded = {key: value.to(self._resolve_device())
                                   for key, value in encoded.items()}
                    output = self._model(**encoded).last_hidden_state
                    mask = encoded["attention_mask"].unsqueeze(-1).float()
                    pooled = (output * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-6)
                except (RuntimeError, ValueError, TypeError) as exc:
                    raise BackendUnavailable(f"mdeberta_encode_failed:{type(exc).__name__}") from exc
                vectors.extend(pooled.tolist())
        return [[float(value) for value in vector] for vector in vectors]

    def similarity(self, left_texts: List[str], right_texts: List[str]) -> List[float]:
        if len(left_texts) != len(right_texts):
            raise ValueError("待比较文本数量不一致")
        if not left_texts:
            return []
        return [cosine(a, b) for a, b in zip(self.encode(left_texts), self.encode(right_texts))]

