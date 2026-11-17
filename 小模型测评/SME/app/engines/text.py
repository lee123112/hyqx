"""Text measurement engine.

The measured path has two backends:

* the default character-level MLM, used when no local BERT is configured and by
  the fast regression tests;
* a local BERT MLM when ``SME_TEXT_MODEL_PATH`` points at a local model.  The BERT
  path uses the model's own tokenizer, 15% MLM masking, the standard 80/10/10
  replacement scheme and a deliberately low learning rate (2e-5 by default).

Both backends train ``candidate`` on the imported sample inputs and ``baseline``
on a reference corpus with the same character volume, then report per-sample
masked-token accuracy, macro-F1 and the coefficient of variation of token losses.

These numbers are the **MLM proxy** used by the online service.  They are not
the CLUE TNEWS/CLUENER/AFQMC downstream scores.  The fixed-protocol CLUE
experiment lives in ``app.experiments.text_downstream`` and is run offline with
its own task train/evaluation files; the online engine never fabricates those
scores.
"""
from __future__ import annotations

from ..experiments.training_lock import training_locked

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import settings
from ..core.envelope import ApiError
from .base import BaseEngine, EngineResult

ENGINE_VERSION = "text-mlm-char-v1"
BERT_ENGINE_VERSION = "text-bert-mlm-v2"
TEXT_METRICS: tuple[str, ...] = ("accuracy", "f1", "lossCv")
METRIC_LIMITS: dict[str, float] = {"accuracy": 100.0, "f1": 100.0, "lossCv": 1.0}
MODEL_KEYS: tuple[str, ...] = ("baseline", "candidate")

PAD_ID = 0
UNK_ID = 1
MASK_ID = 2
SPECIAL_TOKENS: tuple[str, ...] = ("<pad>", "<unk>", "<mask>")

REFERENCE_RELATIVE = "reference/text_reference.jsonl"

MAX_SAMPLES = 5000
MAX_TRAIN_SAMPLES = 2000
MAX_TRAIN_CHARS = 40_000
MAX_TRAIN_CHUNKS = 6000
INFERENCE_BATCH = 64
DEFAULT_TIMEOUT_SECONDS = 240.0


class _EngineUnavailable(RuntimeError):
    """Dependencies or assets required by the engine are not usable."""


@dataclass
class _ModelConfig:
    embedding: int = 128
    layers: int = 2
    heads: int = 4
    chunk_len: int = 48
    batch_size: int = 32
    epochs: int = 3
    lr: float = 3e-3
    dropout: float = 0.1
    mask_rate: float = 0.15
    max_len: int = 128
    max_vocab: int = 4000

    def to_meta(self) -> dict[str, Any]:
        return {
            "architecture": "char-transformer-mlm",
            "embedding": self.embedding,
            "layers": self.layers,
            "heads": self.heads,
            "chunkLength": self.chunk_len,
            "batchSize": self.batch_size,
            "epochs": self.epochs,
            "learningRate": self.lr,
            "dropout": self.dropout,
            "maskRate": self.mask_rate,
            "maxLen": self.max_len,
            "maxVocab": self.max_vocab,
        }


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def _safe_message(message: str) -> str:
    """Keep host absolute paths out of result metadata/errors."""
    text = str(message)
    root = str(settings.project_root)
    for value in (root, root.replace("\\", "/")):
        text = text.replace(value, ".")
    return re.sub(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>]+", "<local-path>", text)


def _is_metric_value(key: str, value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and 0.0 <= number <= METRIC_LIMITS[key]


def _clean_measurements(value: Any) -> dict[str, dict[str, float]] | None:
    """Return a strict copy of complete text measurements, or ``None``."""
    if not isinstance(value, dict):
        return None
    cleaned: dict[str, dict[str, float]] = {}
    for model_key in MODEL_KEYS:
        values = value.get(model_key)
        if not isinstance(values, dict):
            return None
        model_values: dict[str, float] = {}
        for metric in TEXT_METRICS:
            metric_value = values.get(metric)
            if not _is_metric_value(metric, metric_value):
                return None
            model_values[metric] = float(metric_value)
        cleaned[model_key] = model_values
    return cleaned


def _stable_mask_seed(seed: int, sample_id: str) -> int:
    digest = hashlib.sha256(f"{seed}|{sample_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _split_90_10(texts: list[str], seed: int) -> tuple[list[str], list[str]]:
    """Deterministic 90/10 split used by both corpora."""
    if not texts:
        return [], []
    import random
    from ..experiments.text_downstream import _normalized_text
    groups: dict[str, list[str]] = {}
    for text in texts:
        groups.setdefault(_normalized_text(text), []).append(text)
    keys = list(groups)
    random.Random(seed).shuffle(keys)
    if len(keys) < 2:
        return texts[:], []
    count = min(max(1, round(len(keys) * 0.1)), len(keys) - 1)
    return ([text for key in keys[:-count] for text in groups[key]],
            [text for key in keys[-count:] for text in groups[key]])


def _equalize_reference(lines: list[str], target_chars: int) -> str:
    """Cycle reference lines until the requested character volume is reached."""
    lines = [line for line in lines if line]
    if not lines or target_chars <= 0:
        return ""
    pieces: list[str] = []
    total = 0
    index = 0
    while total < target_chars:
        line = lines[index % len(lines)]
        separator = "\n" if pieces else ""
        remaining = target_chars - total - len(separator)
        if remaining <= 0:
            break
        if len(line) <= remaining:
            pieces.append(separator + line)
            total += len(separator) + len(line)
        else:
            pieces.append(separator + line[:remaining])
            total += len(separator) + remaining
            break
        index += 1
    return "".join(pieces)


def _reference_path() -> Path:
    return settings.dataset_dir / REFERENCE_RELATIVE


def _reference_label() -> str:
    path = _reference_path()
    try:
        return str(path.relative_to(settings.project_root))
    except ValueError:
        return str(path)


def _load_reference_texts() -> list[str]:
    path = _reference_path()
    if not path.is_file():
        raise _EngineUnavailable(f"文本参考语料缺失：{_reference_label()}")
    texts: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            texts.append(line)
            continue
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("input") or item.get("text") or item.get("content") or ""
        else:
            value = ""
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
    if not texts:
        raise _EngineUnavailable(f"文本参考语料为空：{_reference_label()}")
    return texts


def _load_backend():
    """Import torch/numpy only when the measured path actually runs."""
    if not settings.engine_enable_torch:
        raise _EngineUnavailable("SME_ENGINE_ENABLE_TORCH=0 已禁用 PyTorch 文本引擎")
    try:
        import numpy as np
        import torch
        from torch import nn
        from torch.nn import functional as F
    except Exception as exc:  # pragma: no cover - depends on environment
        raise _EngineUnavailable(
            f"文本测量需要 PyTorch 与 NumPy，当前环境不可用：{exc}") from exc
    return torch, nn, F, np


def _training_stride(chars: int, fast: bool) -> int:
    if fast or chars <= 3000:
        return 2
    if chars <= 10000:
        return 8
    if chars <= 25000:
        return 16
    return 32


def _build_chunks(text: str, chunk_len: int, stride: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_len:
        return [text]
    chunks: list[str] = []
    position = 0
    while position < len(text):
        chunk = text[position:position + chunk_len]
        if len(chunk) >= 4:
            chunks.append(chunk)
        if position + chunk_len >= len(text):
            break
        position += stride
    return chunks


def _macro_f1(labels: list[int], predictions: list[int]) -> float:
    """Macro-F1 over the union of labels/predictions.

    With a single class the metric falls back to micro accuracy, matching the
    contract "若类别 < 2 则用 micro/accuracy 兜底".
    """
    if not labels:
        return 0.0
    correct = sum(1 for gold, pred in zip(labels, predictions) if gold == pred)
    accuracy = correct / len(labels)
    gold_classes = set(labels)
    if len(gold_classes) < 2:
        # Only one target class: macro-F1 degenerates, use micro/accuracy.
        return accuracy
    classes = sorted(gold_classes | set(predictions))
    scores: list[float] = []
    for cls in classes:
        true_positive = sum(1 for gold, pred in zip(labels, predictions)
                            if gold == cls and pred == cls)
        false_positive = sum(1 for gold, pred in zip(labels, predictions)
                             if gold != cls and pred == cls)
        false_negative = sum(1 for gold, pred in zip(labels, predictions)
                             if gold == cls and pred != cls)
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(0.0 if denominator == 0 else 2 * true_positive / denominator)
    return sum(scores) / len(scores)


def _loss_cv(losses: list[float]) -> float:
    if not losses:
        return 0.0
    mean = sum(losses) / len(losses)
    if mean <= 1e-12:
        return 0.0
    variance = sum((value - mean) ** 2 for value in losses) / len(losses)
    cv = math.sqrt(max(variance, 0.0)) / mean
    return min(max(cv, 0.0), 1.0)


def _score_values(labels: list[int], predictions: list[int],
                  losses: list[float]) -> dict[str, float] | None:
    if not labels:
        return None
    correct = sum(1 for gold, pred in zip(labels, predictions) if gold == pred)
    accuracy = correct / len(labels) * 100.0
    f1 = _macro_f1(labels, predictions) * 100.0
    values = {
        "accuracy": round(min(max(accuracy, 0.0), 100.0), 6),
        "f1": round(min(max(f1, 0.0), 100.0), 6),
        "lossCv": round(_loss_cv(losses), 6),
    }
    if not all(_is_metric_value(key, value) for key, value in values.items()):
        return None
    return values


class _CharVocab:
    def __init__(self, texts: list[str], max_size: int) -> None:
        counts: dict[str, int] = {}
        for text in texts:
            for char in text:
                counts[char] = counts.get(char, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        chars = [char for char, _ in ranked[:max_size]]
        self.itos: list[str] = list(SPECIAL_TOKENS) + chars
        self.stoi: dict[str, int] = {char: index for index, char in enumerate(self.itos)}

    def encode(self, text: str) -> list[int]:
        return [self.stoi.get(char, UNK_ID) for char in text]

    def __len__(self) -> int:
        return len(self.itos)


class _CharPair:
    """Two character-MLMs sharing one vocabulary, trained on different corpora."""

    backend = "char-transformer"

    def __init__(self, torch, nn, F, np, vocab: _CharVocab, config: _ModelConfig,
                 seed: int, timeout: float) -> None:
        self.torch = torch
        self.nn = nn
        self.F = F
        self.np = np
        self.vocab = vocab
        self.config = config
        self.seed = seed
        self.timeout = timeout
        self.candidate = self._build_model()
        self.baseline = self._build_model()
        self.stats: dict[str, dict[str, Any]] = {}

    def _build_model(self):
        torch = self.torch
        nn = self.nn
        torch.manual_seed(self.seed)
        config = self.config
        vocab = self.vocab
        embedding = config.embedding
        layer = nn.TransformerEncoderLayer(
            d_model=embedding,
            nhead=config.heads,
            dim_feedforward=embedding * 4,
            dropout=config.dropout,
            activation="gelu",
            batch_first=True,
        )
        encoder = nn.TransformerEncoder(
            layer, num_layers=config.layers, enable_nested_tensor=False)

        class _Model(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.token_embedding = nn.Embedding(len(vocab), embedding, padding_idx=PAD_ID)
                self.position_embedding = nn.Embedding(config.max_len, embedding)
                self.encoder = encoder
                self.norm = nn.LayerNorm(embedding)
                self.head = nn.Linear(embedding, len(vocab))

            def forward(self, input_ids, attention_mask=None):
                length = input_ids.shape[1]
                positions = torch.arange(length, device=input_ids.device).unsqueeze(0)
                hidden = self.token_embedding(input_ids) + self.position_embedding(positions)
                padding_mask = None
                if attention_mask is not None:
                    padding_mask = attention_mask == 0
                hidden = self.encoder(hidden, src_key_padding_mask=padding_mask)
                hidden = self.norm(hidden)
                return self.head(hidden)

        return _Model()

    def _chunks(self, text: str) -> list[list[int]]:
        stride = _training_stride(len(text), _env_flag("SME_TEXT_FAST"))
        chunks = _build_chunks(text, self.config.chunk_len, stride)
        chunks = chunks[:MAX_TRAIN_CHUNKS]
        return [self.vocab.encode(chunk) for chunk in chunks if chunk]

    def fit(self, role: str, text: str, deadline: float) -> dict[str, Any]:
        torch = self.torch
        model = self.candidate if role == "candidate" else self.baseline
        chunks = self._chunks(text)
        if not chunks:
            raise ValueError(f"{role} 训练语料过短，无法构造训练样本")
        config = self.config
        rng = self.np.random.default_rng(self.seed)
        optimizer = torch.optim.AdamW(model.parameters(), lr=config.lr, weight_decay=0.01)
        torch.manual_seed(self.seed)
        model.train()
        steps = 0
        losses: list[float] = []
        for epoch in range(config.epochs):
            order = rng.permutation(len(chunks))
            for start in range(0, len(order), config.batch_size):
                batch_indices = order[start:start + config.batch_size]
                input_ids = torch.full(
                    (len(batch_indices), config.chunk_len), PAD_ID, dtype=torch.long)
                attention = torch.zeros(
                    (len(batch_indices), config.chunk_len), dtype=torch.long)
                labels = torch.full(
                    (len(batch_indices), config.chunk_len), -100, dtype=torch.long)
                has_target = False
                for row, chunk_index in enumerate(batch_indices):
                    ids = chunks[int(chunk_index)]
                    length = len(ids)
                    input_ids[row, :length] = torch.tensor(ids, dtype=torch.long)
                    attention[row, :length] = 1
                    eligible = [index for index, token in enumerate(ids) if token != UNK_ID]
                    if not eligible:
                        continue
                    count = min(len(eligible),
                                max(1, int(round(len(eligible) * config.mask_rate))))
                    chosen = rng.choice(self.np.asarray(eligible), size=count, replace=False)
                    for position in chosen.tolist():
                        input_ids[row, position] = MASK_ID
                        labels[row, position] = ids[position]
                        has_target = True
                if not has_target:
                    continue
                logits = model(input_ids, attention)
                loss = self.F.cross_entropy(
                    logits.reshape(-1, len(self.vocab)),
                    labels.reshape(-1),
                    ignore_index=-100,
                )
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                steps += 1
                losses.append(float(loss.detach()))
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{role} 训练超过 {self.timeout:.0f} 秒")
        model.eval()
        stats = {
            "chunks": len(chunks),
            "steps": steps,
            "epochs": config.epochs,
            "meanTrainLoss": round(sum(losses) / len(losses), 6) if losses else None,
        }
        self.stats[role] = stats
        return stats

    def prepare(self, text: str, mask_seed: int) -> dict[str, Any] | None:
        ids = self.vocab.encode(text[:self.config.max_len])
        if not ids:
            return None
        eligible = [index for index, token in enumerate(ids) if token not in (PAD_ID, UNK_ID)]
        if not eligible:
            return None
        rng = self.np.random.default_rng(mask_seed)
        count = min(len(eligible),
                    max(1, int(round(len(eligible) * self.config.mask_rate))))
        positions = sorted(
            int(value) for value in rng.choice(self.np.asarray(eligible), size=count,
                                               replace=False))
        masked = list(ids)
        for position in positions:
            masked[position] = MASK_ID
        return {
            "ids": masked,
            "length": len(ids),
            "positions": positions,
            "labels": [ids[position] for position in positions],
        }

    def predict_all(self, prepared: list[dict[str, Any]],
                    deadline: float) -> list[dict[str, dict[str, float] | None]]:
        torch = self.torch
        output: list[dict[str, dict[str, float] | None]] = []
        batches = range(0, len(prepared), INFERENCE_BATCH)
        for start in batches:
            batch = prepared[start:start + INFERENCE_BATCH]
            max_length = max(item["length"] for item in batch)
            input_ids = torch.full((len(batch), max_length), PAD_ID, dtype=torch.long)
            attention = torch.zeros((len(batch), max_length), dtype=torch.long)
            for row, item in enumerate(batch):
                length = item["length"]
                input_ids[row, :length] = torch.tensor(item["ids"][:length], dtype=torch.long)
                attention[row, :length] = 1
            role_scores: dict[str, list[dict[str, float] | None]] = {}
            with torch.no_grad():
                for role, model in (("candidate", self.candidate),
                                    ("baseline", self.baseline)):
                    model.eval()
                    logits = model(input_ids, attention)
                    scores: list[dict[str, float] | None] = []
                    for row, item in enumerate(batch):
                        positions = torch.tensor(item["positions"], dtype=torch.long)
                        selected = logits[row].index_select(0, positions)
                        labels = torch.tensor(item["labels"], dtype=torch.long)
                        losses = self.F.cross_entropy(
                            selected, labels, reduction="none").tolist()
                        predictions = selected.argmax(dim=-1).tolist()
                        scores.append(_score_values(item["labels"], predictions, losses))
                    role_scores[role] = scores
            for index in range(len(batch)):
                output.append({
                    "candidate": role_scores["candidate"][index],
                    "baseline": role_scores["baseline"][index],
                })
            if time.monotonic() > deadline:
                raise TimeoutError(f"文本推理超过 {self.timeout:.0f} 秒")
        return output


class _BertPair:
    """Optional transformers backend: fine-tune two AutoModelForMaskedLM copies."""

    backend = "transformers"

    def __init__(self, torch, F, np, tokenizer, candidate, baseline,
                 config: _ModelConfig, seed: int, timeout: float,
                 model_name: str) -> None:
        self.torch = torch
        self.F = F
        self.np = np
        self.tokenizer = tokenizer
        self.candidate = candidate
        self.baseline = baseline
        self.config = config
        self.seed = seed
        self.timeout = timeout
        self.model_name = model_name
        self.pretrained_config = getattr(candidate, "config", None)
        self.pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else 0
        self.mask_id = tokenizer.mask_token_id
        self.special_ids = set(tokenizer.all_special_ids)
        self.stats: dict[str, dict[str, Any]] = {}

    def _tokenize(self, text: str) -> list[int]:
        encoded = self.tokenizer(
            text, truncation=True, max_length=self.config.max_len,
            add_special_tokens=True,
        )
        return list(encoded["input_ids"])

    def fit(self, role: str, text: str, deadline: float) -> dict[str, Any]:
        torch = self.torch
        model = self.candidate if role == "candidate" else self.baseline
        texts = [line for line in text.split("\n") if line.strip()][:256]
        if not texts:
            raise ValueError(f"{role} 训练语料过短，无法构造训练样本")
        learning_rate = _env_float("SME_TEXT_MLM_LR", 2e-5)
        epochs = _env_int("SME_TEXT_BERT_EPOCHS", 1, 1, 10)
        optimizer = torch.optim.AdamW(
            model.parameters(), lr=learning_rate, weight_decay=0.01)
        rng = self.np.random.default_rng(self.seed)
        torch.manual_seed(self.seed)
        model.train()
        steps = 0
        losses: list[float] = []
        for _epoch in range(epochs):
            order = rng.permutation(len(texts))
            for start in range(0, len(order), 8):
                batch_texts = [texts[int(index)] for index in order[start:start + 8]]
                encoded = [self._tokenize(item) for item in batch_texts]
                max_length = max(len(ids) for ids in encoded)
                input_ids = torch.full((len(encoded), max_length), self.pad_id,
                                       dtype=torch.long)
                attention = torch.zeros((len(encoded), max_length), dtype=torch.long)
                labels = torch.full((len(encoded), max_length), -100, dtype=torch.long)
                for row, ids in enumerate(encoded):
                    input_ids[row, :len(ids)] = torch.tensor(ids, dtype=torch.long)
                    attention[row, :len(ids)] = 1
                    eligible = [index for index, token in enumerate(ids)
                                if token not in self.special_ids]
                    if not eligible:
                        continue
                    count = min(len(eligible), max(
                        1, int(round(len(eligible) * self.config.mask_rate))))
                    chosen = rng.choice(self.np.asarray(eligible), size=count, replace=False)
                    for position in chosen.tolist():
                        labels[row, position] = ids[position]
                        draw = float(rng.random())
                        if draw < 0.8:
                            input_ids[row, position] = self.mask_id
                        elif draw < 0.9:
                            input_ids[row, position] = int(
                                rng.integers(0, int(getattr(
                                    self.tokenizer, "vocab_size", len(self.tokenizer)))))
                        # The remaining 10% keep the original token.
                if int((labels != -100).sum()) == 0:
                    continue
                outputs = model(input_ids=input_ids, attention_mask=attention, labels=labels)
                loss = outputs.loss
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                steps += 1
                losses.append(float(loss.detach()))
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{role} 训练超过 {self.timeout:.0f} 秒")
        model.eval()
        stats = {
            "chunks": len(texts),
            "steps": steps,
            "epochs": epochs,
            "batchSize": 8,
            "learningRate": learning_rate,
            "maskRate": self.config.mask_rate,
            "maskScheme": "80/10/10",
            "meanTrainLoss": round(sum(losses) / len(losses), 6) if losses else None,
        }
        self.stats[role] = stats
        return stats

    def prepare(self, text: str, mask_seed: int) -> dict[str, Any] | None:
        ids = self._tokenize(text)
        if not ids:
            return None
        eligible = [index for index, token in enumerate(ids)
                    if token not in self.special_ids]
        if not eligible:
            return None
        rng = self.np.random.default_rng(mask_seed)
        count = min(len(eligible),
                    max(1, int(round(len(eligible) * self.config.mask_rate))))
        positions = sorted(
            int(value) for value in rng.choice(self.np.asarray(eligible), size=count,
                                               replace=False))
        masked = list(ids)
        for position in positions:
            masked[position] = self.mask_id
        return {
            "ids": masked,
            "length": len(ids),
            "positions": positions,
            "labels": [ids[position] for position in positions],
        }

    def predict_all(self, prepared: list[dict[str, Any]],
                    deadline: float) -> list[dict[str, dict[str, float] | None]]:
        torch = self.torch
        output: list[dict[str, dict[str, float] | None]] = []
        for start in range(0, len(prepared), INFERENCE_BATCH):
            batch = prepared[start:start + INFERENCE_BATCH]
            max_length = max(item["length"] for item in batch)
            input_ids = torch.full((len(batch), max_length), self.pad_id, dtype=torch.long)
            attention = torch.zeros((len(batch), max_length), dtype=torch.long)
            for row, item in enumerate(batch):
                length = item["length"]
                input_ids[row, :length] = torch.tensor(item["ids"][:length], dtype=torch.long)
                attention[row, :length] = 1
            role_scores: dict[str, list[dict[str, float] | None]] = {}
            with torch.no_grad():
                for role, model in (("candidate", self.candidate),
                                    ("baseline", self.baseline)):
                    model.eval()
                    logits = model(input_ids=input_ids, attention_mask=attention).logits
                    scores: list[dict[str, float] | None] = []
                    for row, item in enumerate(batch):
                        positions = torch.tensor(item["positions"], dtype=torch.long)
                        selected = logits[row].index_select(0, positions)
                        labels = torch.tensor(item["labels"], dtype=torch.long)
                        losses = self.F.cross_entropy(
                            selected, labels, reduction="none").tolist()
                        predictions = selected.argmax(dim=-1).tolist()
                        scores.append(_score_values(item["labels"], predictions, losses))
                    role_scores[role] = scores
            for index in range(len(batch)):
                output.append({
                    "candidate": role_scores["candidate"][index],
                    "baseline": role_scores["baseline"][index],
                })
            if time.monotonic() > deadline:
                raise TimeoutError(f"文本推理超过 {self.timeout:.0f} 秒")
        return output


def _build_models(config: _ModelConfig, seed: int, timeout: float,
                  candidate_text: str, baseline_text: str, backend, deadline: float):
    """Create and fit either the char backend or the optional transformers one."""
    torch, nn, F, np = backend
    configured_model = settings.text_model_path
    if not configured_model and not _env_flag("SME_TEXT_FAST"):
        from ..core.model_assets import bundled_model_path
        configured_model = str(bundled_model_path('text'))
    if configured_model:
        path = Path(configured_model).expanduser()
        if not path.is_absolute():
            path = settings.project_root / path
        if not path.exists():
            raise _EngineUnavailable(
                "SME_TEXT_MODEL_PATH 指向的本地模型不存在，文本引擎未回退到随机初始化")
        try:
            from transformers import AutoModelForMaskedLM, AutoTokenizer
            tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
            model_name = path.name or "local-bert"
            torch.manual_seed(seed)
            candidate = AutoModelForMaskedLM.from_pretrained(
                str(path), local_files_only=True)
            torch.manual_seed(seed)
            baseline = AutoModelForMaskedLM.from_pretrained(
                str(path), local_files_only=True)
        except Exception as exc:  # pragma: no cover - optional local asset
            raise _EngineUnavailable(
                f"SME_TEXT_MODEL_PATH 本地模型加载失败：{type(exc).__name__}: {exc}") from exc
        if tokenizer.mask_token_id is None:
            raise _EngineUnavailable("SME_TEXT_MODEL_PATH 对应的分词器缺少 mask token")
        pair = _BertPair(torch, F, np, tokenizer, candidate, baseline,
                         config, seed, timeout, model_name)
        pair.fit("candidate", candidate_text, deadline)
        pair.fit("baseline", baseline_text, deadline)
        return pair, "transformers"

    vocab = _CharVocab([candidate_text, baseline_text], config.max_vocab)
    pair = _CharPair(torch, nn, F, np, vocab, config, seed, timeout)
    pair.fit("candidate", candidate_text, deadline)
    pair.fit("baseline", baseline_text, deadline)
    return pair, pair.backend


class TextEngine(BaseEngine):
    type = "text"

    def measure(self, samples: list[dict[str, Any]], config: dict[str, Any], *,
                seed: int = 42, strict: bool = True) -> EngineResult:
        config = config if isinstance(config, dict) else {}
        rows = self._validate_samples(samples)
        imported = self._imported(rows)
        if imported is not None:
            return EngineResult(
                measurements=imported,
                provenance={metric: "imported" for metric in TEXT_METRICS},
                meta={
                    "engineVersion": ENGINE_VERSION,
                    "mode": "imported",
                    "seed": seed,
                    "sampleCount": len(rows),
                    "metricNature": "imported_measurements",
                    "qualityConclusionEligible": False,
                },
                notes=["样本已包含完整的逐条指标，按导入数据处理。"],
            )

        try:
            return self._measure_models(rows, config, seed=seed, strict=strict)
        except ApiError:
            raise
        except _EngineUnavailable as exc:
            return self._failure(rows, strict, "ENGINE_UNAVAILABLE", str(exc), seed)
        except TimeoutError as exc:
            return self._failure(rows, strict, "TEXT_ENGINE_FAILED",
                                 f"文本测量超时：{exc}", seed)
        except Exception as exc:
            return self._failure(
                rows, strict, "TEXT_ENGINE_FAILED",
                f"文本测量训练失败：{type(exc).__name__}: {exc}", seed)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _validate_samples(samples: Any) -> list[dict[str, Any]]:
        if not isinstance(samples, list) or not samples:
            raise ApiError(422, "SAMPLE_INVALID", "文本测量至少需要一条样本")
        if len(samples) > MAX_SAMPLES:
            raise ApiError(422, "SAMPLE_INVALID",
                           f"单次文本测量最多支持 {MAX_SAMPLES} 条样本")
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                raise ApiError(422, "SAMPLE_INVALID", f"第 {index + 1} 条样本格式不正确")
            sample_id = sample.get("id")
            if sample_id is None or str(sample_id).strip() == "":
                raise ApiError(422, "SAMPLE_INVALID", f"第 {index + 1} 条样本缺少 id")
            sample_id = str(sample_id)
            if sample_id in seen:
                raise ApiError(422, "SAMPLE_INVALID", f"样本 ID 重复：{sample_id}")
            seen.add(sample_id)
            text = sample.get("input")
            if not isinstance(text, str) or not text.strip():
                raise ApiError(422, "SAMPLE_INVALID", f"{sample_id} 缺少 input 字段")
            rows.append({
                "id": sample_id,
                "input": text,
                "measurements": sample.get("measurements"),
            })
        return rows

    @staticmethod
    def _imported(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, float]]] | None:
        imported: dict[str, dict[str, dict[str, float]]] = {}
        for row in rows:
            cleaned = _clean_measurements(row.get("measurements"))
            if cleaned is None:
                return None
            imported[row["id"]] = cleaned
        return imported

    @training_locked
    def _measure_models(self, rows: list[dict[str, Any]], config: dict[str, Any], *,
                        seed: int, strict: bool) -> EngineResult:
        started = time.monotonic()
        timeout = _env_float("SME_TEXT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        deadline = started + timeout
        backend = _load_backend()

        fast = _env_flag("SME_TEXT_FAST")
        model_config = _ModelConfig(
            embedding=64 if fast else 128,
            layers=1 if fast else 2,
            heads=4,
            chunk_len=32 if fast else 48,
            batch_size=8 if fast else 32,
            lr=5e-3 if fast else 3e-3,
            epochs=_env_int("SME_TEXT_EPOCHS", 1 if fast else 3, 1, 50),
            max_vocab=2000 if fast else 4000,
        )

        all_texts = [row["input"] for row in rows]
        train_texts, validation_texts = _split_90_10(all_texts, seed)
        if not validation_texts:
            raise ApiError(422, "EVALUATION_INVALID", "At least two distinct normalized texts are required for heldout evaluation")
        from ..experiments.text_downstream import _normalized_text
        validation_keys = {_normalized_text(text) for text in validation_texts}
        training_keys = {_normalized_text(text) for text in train_texts[:MAX_TRAIN_SAMPLES]}
        evaluation_ids = [row["id"] for row in rows if _normalized_text(row["input"]) in validation_keys]
        training_ids = [row["id"] for row in rows if _normalized_text(row["input"]) in training_keys]
        train_texts = train_texts[:MAX_TRAIN_SAMPLES]
        candidate_text = "\n".join(train_texts)[:MAX_TRAIN_CHARS]
        if not candidate_text.strip():
            raise _EngineUnavailable("候选训练语料为空，无法训练文本测量模型")

        reference_texts = _load_reference_texts()
        excluded_reference = sum(_normalized_text(text) in validation_keys for text in reference_texts)
        reference_texts = [text for text in reference_texts if _normalized_text(text) not in validation_keys]
        reference_train, _reference_validation = _split_90_10(reference_texts, seed)
        if not reference_train:
            reference_train = reference_texts
        baseline_text = _equalize_reference(reference_train, len(candidate_text))
        if not baseline_text.strip():
            raise _EngineUnavailable("参考训练语料为空，无法训练基线模型")

        pair, backend_name = _build_models(
            model_config, seed, timeout, candidate_text, baseline_text, backend, deadline)

        prepared: list[dict[str, Any] | None] = []
        missing: list[dict[str, Any]] = []
        for row in rows:
            item = pair.prepare(row["input"], _stable_mask_seed(seed, row["id"]))
            prepared.append(item)
            if item is None:
                for model_key in MODEL_KEYS:
                    for metric in TEXT_METRICS:
                        missing.append({"sampleId": row["id"], "model": model_key,
                                        "metric": metric})
            if time.monotonic() > deadline:
                raise TimeoutError(f"文本预处理超过 {timeout:.0f} 秒")

        measured_rows: dict[str, dict[str, dict[str, float]]] = {}
        valid_indices = [index for index, item in enumerate(prepared) if item is not None]
        valid_items = [prepared[index] for index in valid_indices]
        if valid_items:
            predictions = pair.predict_all(valid_items, deadline)
            for index, score in zip(valid_indices, predictions):
                if score.get("candidate") is None or score.get("baseline") is None:
                    for model_key in MODEL_KEYS:
                        for metric in TEXT_METRICS:
                            missing.append({"sampleId": rows[index]["id"],
                                            "model": model_key, "metric": metric})
                    continue
                measured_rows[rows[index]["id"]] = {
                    "baseline": score["baseline"],
                    "candidate": score["candidate"],
                }

        if missing and strict:
            preview = "、".join(
                f"{item['sampleId']}.{item['model']}.{item['metric']}"
                for item in missing[:5])
            raise ApiError(
                422, "MEASUREMENTS_INCOMPLETE",
                f"以下样本缺少完整文本指标：{preview}"
                + (" 等" if len(missing) > 5 else ""),
                details=missing,
            )

        duration = round(time.monotonic() - started, 3)
        reference_used = {
            "name": "reference",
            "path": _reference_label(),
            "trainLines": len(reference_train),
            "trainChars": len(baseline_text),
            "equalizedToCandidateChars": len(candidate_text),
        }
        stats = {
            "candidate": {
                "name": "samples",
                "trainSamples": len(train_texts),
                "validationSamples": len(validation_texts),
                "trainChars": len(candidate_text),
                **pair.stats.get("candidate", {}),
            },
            "baseline": {
                **reference_used,
                **pair.stats.get("baseline", {}),
            },
        }
        meta = {
            "engineVersion": (BERT_ENGINE_VERSION if backend_name == "transformers"
                              else ENGINE_VERSION),
            "mode": "measured",
            "backend": backend_name,
            "seed": seed,
            "sampleCount": len(rows),
            "metricNature": ("bert_mlm_proxy" if backend_name == "transformers"
                             else "char_mlm_proxy"),
            "modelConfig": model_config.to_meta(),
            "qualityConclusionEligible": False,
            "evaluation": {"sampleIds": evaluation_ids, "trainingSampleIds": training_ids,
                           "policy": "heldout", "normalization": "NFKC+remove-whitespace",
                           "excludedReferenceSamples": excluded_reference,
                           "qualityConclusionEligible": False, "metricNature": "MLM proxy"},
            "trainedCorpus": stats,
            "split": {
                "trainSamples": len(train_texts),
                "validationSamples": len(validation_texts),
                "ratio": 0.9,
            },
            "maxTrainSamples": MAX_TRAIN_SAMPLES,
            "maxTrainChars": MAX_TRAIN_CHARS,
            "inferenceBatchSize": INFERENCE_BATCH,
            "referenceCorpus": _reference_label(),
            "durationSeconds": duration,
        }
        if pair.backend == "transformers":
            pretrained = getattr(pair, "pretrained_config", None)
            bert_fields = {}
            for field, key in (
                ("hidden_size", "hiddenSize"),
                ("num_hidden_layers", "layers"),
                ("num_attention_heads", "heads"),
                ("intermediate_size", "intermediateSize"),
                ("vocab_size", "vocabSize"),
                ("max_position_embeddings", "maxPositionEmbeddings"),
            ):
                value = getattr(pretrained, field, None)
                if isinstance(value, int):
                    bert_fields[key] = value
            meta["modelConfig"] = {
                "architecture": "bert-base-chinese-mlm-finetune",
                **bert_fields,
                "batchSize": pair.stats.get("candidate", {}).get("batchSize", 8),
                "epochs": pair.stats.get("candidate", {}).get("epochs", 1),
                "learningRate": _env_float("SME_TEXT_MLM_LR", 2e-5),
                "maskRate": model_config.mask_rate,
                "maxLen": model_config.max_len,
                "modelPath": getattr(pair, "model_name", "local-model"),
                "maskScheme": "80/10/10",
            }
            meta["maskScheme"] = "80/10/10"
        if len(train_texts) >= MAX_TRAIN_SAMPLES:
            meta["trainingTruncated"] = True
        if len("\n".join(train_texts)) > MAX_TRAIN_CHARS:
            meta["trainingCharsTruncated"] = True
        provenance = ({metric: "measured_model" for metric in TEXT_METRICS}
                      if measured_rows else {})
        notes = [
            "候选模型在样本输入上训练，基线模型在同字符量的参考语料上训练。",
            "accuracy/f1/lossCv 是掩码语言模型（MLM）代理指标，不等同于 CLUE 下游任务准确率，"
            "不承诺达到前端 80/80/0.2 阈值。",
        ]
        if backend_name == "transformers":
            notes.append(
                "BERT 路径采用 15% 掩码与 80/10/10 替换，默认学习率 2e-5；"
                "CLUE TNEWS/CLUENER/AFQMC 下游指标需运行离线固定协议实验，"
                "在线任务不会把 MLM 分数冒充下游分数。")
        return EngineResult(
            measurements=measured_rows,
            provenance=provenance,
            meta=meta,
            missing=missing,
            notes=notes,
        )

    @staticmethod
    def _failure(rows: list[dict[str, Any]], strict: bool, error_code: str,
                 message: str, seed: int) -> EngineResult:
        message = _safe_message(message)
        if strict:
            raise ApiError(422, error_code, message)
        missing = [
            {"sampleId": row["id"], "model": model_key, "metric": metric}
            for row in rows
            for model_key in MODEL_KEYS
            for metric in TEXT_METRICS
        ]
        return EngineResult(
            measurements={},
            provenance={},
            meta={
                "engineVersion": ENGINE_VERSION,
                "mode": "measured",
                "seed": seed,
                "sampleCount": len(rows),
                "metricNature": "char_mlm_proxy",
                "error": message,
            },
            missing=missing,
            notes=[message],
        )
