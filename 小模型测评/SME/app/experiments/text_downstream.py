"""Fixed-protocol Chinese text experiment: BERT MLM + CLUE downstream tasks.

This module is deliberately offline.  The online ``TextEngine`` still returns a
per-sample MLM proxy, while this experiment answers the stricter question:

    Hold the proxy model, task train set, fixed evaluation set ``E``, seeds and
    optimization budget fixed.  Train MLM on ``D_ours`` and ``D_ref`` with the
    same token budget.  Then fine-tune one fresh task head per side on the same
    labelled task train set and score both on the same ``E``.

Supported tasks are the three CLUE tasks named in the methodology:

* ``tnews``  : news-title classification, primary metric accuracy;
* ``afqmc``  : sentence-pair matching, primary metric accuracy;
* ``cluener``: named-entity recognition, entity-level micro F1.

The record includes ``S_ours``, ``S_ref`` and ``delta`` plus the audit data needed
to reproduce or reject the comparison.  It never claims that a different model
or a different task split is a paper ``S_ref``.
"""
from __future__ import annotations

from .training_lock import training_locked

import hashlib
import json
import math
import random
import statistics
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..core.envelope import ApiError
from ..core.ids import now
from .text_metrics import (
    bio_labels_to_entities,
    classification_metrics,
    entities_to_bio,
    entity_f1,
)

try:  # optional heavy dependencies; imported only by the offline experiment
    import torch
    from transformers import (
        AutoModelForMaskedLM,
        AutoModelForSequenceClassification,
        AutoModelForTokenClassification,
        AutoTokenizer,
    )
except Exception:  # pragma: no cover - exercised when optional deps are absent
    torch = None  # type: ignore[assignment]
    AutoModelForMaskedLM = None  # type: ignore[assignment]
    AutoModelForSequenceClassification = None  # type: ignore[assignment]
    AutoModelForTokenClassification = None  # type: ignore[assignment]
    AutoTokenizer = None  # type: ignore[assignment]

ENGINE_VERSION = "text-clue-bert-v1"
TASKS = ("tnews", "afqmc", "cluener")
MASK_RATE = 0.15
MASK_SCHEME = "80/10/10"


@dataclass(frozen=True)
class TextProtocolSpec:
    """Inputs and fixed protocol knobs for one CLUE task comparison."""

    ours_corpus: Path
    ref_corpus: Path
    train_file: Path
    eval_file: Path
    task: str
    model_path: Path
    tokenizer_path: Path | None = None
    seed: int = 42
    seeds: tuple[int, ...] | None = None
    purpose: str = "exploration"
    max_length: int = 128
    mlm_token_budget: int = 8192
    mlm_steps: int = 20
    mlm_batch_size: int = 8
    mlm_lr: float = 2e-5
    ours_mlm_lr: float | None = None
    ref_mlm_lr: float | None = None
    task_epochs: int = 3
    task_max_steps: int | None = None
    task_batch_size: int = 8
    task_lr: float = 2e-5
    max_train: int | None = None
    max_eval: int | None = None

    def __post_init__(self) -> None:
        if self.purpose not in ("exploration", "quality", "formal"):
            raise ApiError(422, "CONFIG_INVALID", "purpose must be exploration/quality/formal")
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ApiError(422, 'CONFIG_INVALID', 'seed 必须是0–2**32-1的整数')
        if self.seeds is not None and (not 1 <= len(self.seeds) <= 10
                or any(type(x) is not int or not 0 <= x < 2**32 for x in self.seeds)
                or len(set(self.seeds)) != len(self.seeds)):
            raise ApiError(422, "CONFIG_INVALID", "seeds must contain distinct integers")
        if self.task not in TASKS:
            raise ApiError(422, "CONFIG_INVALID",
                           f"task 必须是 {', '.join(TASKS)} 之一")
        for name in ("ours_corpus", "ref_corpus", "train_file", "eval_file"):
            path = getattr(self, name)
            if not isinstance(path, Path) or not path.is_file():
                raise ApiError(422, "DATASET_INVALID", f"{name} 文件不存在：{path}")
        if not isinstance(self.model_path, Path) or not self.model_path.is_dir():
            raise ApiError(422, "ASSET_INVALID", f"model_path 目录不存在：{self.model_path}")
        if self.tokenizer_path is not None and not self.tokenizer_path.is_dir():
            raise ApiError(422, "ASSET_INVALID", "tokenizer_path 目录不存在")
        for name in ("max_length", "mlm_token_budget", "mlm_steps",
                     "mlm_batch_size", "task_epochs", "task_batch_size"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) <= 0:
                raise ApiError(422, "CONFIG_INVALID", f"{name} 必须是正整数")
        if self.task_max_steps is not None and (
                not isinstance(self.task_max_steps, int) or self.task_max_steps <= 0):
            raise ApiError(422, "CONFIG_INVALID", "task_max_steps 必须是正整数")
        for name in ("mlm_lr", "task_lr"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ApiError(422, "CONFIG_INVALID", f"{name} 必须是正数")
        for name in ("ours_mlm_lr", "ref_mlm_lr"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value <= 0):
                raise ApiError(422, "CONFIG_INVALID", f"{name} 必须是正数")
        for name in ("max_train", "max_eval"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise ApiError(422, "CONFIG_INVALID", f"{name} 必须是正整数")


# --------------------------------------------------------------------- loading
def _require_backend() -> None:
    if torch is None or AutoTokenizer is None or AutoModelForMaskedLM is None:
        raise ApiError(
            422, "ENGINE_UNAVAILABLE",
            "文本 CLUE 实验需要 torch + transformers；当前环境未安装或导入失败")


def _read_records(path: Path) -> list[Any]:
    if not path.is_file():
        raise ApiError(422, "DATASET_INVALID", f"数据文件不存在：{path}")
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ApiError(422, "DATASET_INVALID", f"数据文件为空：{path}")
    records: list[Any] = []
    if text.startswith("["):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiError(422, "DATASET_INVALID",
                           f"JSON 数组无法解析：{path.name}: {exc}") from exc
        if not isinstance(value, list):
            raise ApiError(422, "DATASET_INVALID", f"JSON 根节点必须是数组：{path}")
        records.extend(value)
    elif text.startswith("{"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            nested = next((value[key] for key in
                           ("samples", "data", "records", "rows", "items")
                           if isinstance(value.get(key), list)), None)
            records.extend(nested if nested is not None else [value])
        else:
            # JSONL where the first line starts with "{": parse line by line.
            for line_number, line in enumerate(text.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ApiError(
                        422, "DATASET_INVALID",
                        f"{path.name} 第 {line_number} 行不是 JSON: {exc}") from exc
    else:
        for line_number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ApiError(
                    422, "DATASET_INVALID",
                    f"{path.name} 第 {line_number} 行不是 JSON: {exc}") from exc
    if not records:
        raise ApiError(422, "DATASET_INVALID", f"数据文件没有记录：{path}")
    return records


def _same_id(prefix: str, index: int, value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return text or f"{prefix}-{index + 1:06d}"


def _corpus_texts(path: Path) -> list[str]:
    records = _read_records(path)
    values: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        if isinstance(raw, str):
            value = raw
        elif isinstance(raw, dict):
            value = (raw.get("input") or raw.get("text") or raw.get("content")
                     or raw.get("sentence") or "")
        else:
            value = ""
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if value not in seen:
            seen.add(value)
            values.append(value)
    if not values:
        raise ApiError(422, "DATASET_INVALID", f"语料没有可用文本：{path}")
    return values


def _as_int(value: Any, field: str, index: int) -> int:
    if isinstance(value, bool):
        raise ApiError(422, "DATASET_INVALID",
                       f"第 {index + 1} 条 {field} 不能是布尔值")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(422, "DATASET_INVALID",
                       f"第 {index + 1} 条 {field} 必须是整数") from exc


def _normalize_entities(raw: Any, index: int) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApiError(422, "DATASET_INVALID",
                       f"第 {index + 1} 条 entities 必须是数组")
    entities: list[dict[str, Any]] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ApiError(422, "DATASET_INVALID",
                           f"第 {index + 1} 条 entities[{position}] 必须是对象")
        start = item.get("start", item.get("start_offset"))
        end = item.get("end", item.get("end_offset"))
        label = item.get("label")
        if start is None or end is None or label is None:
            raise ApiError(
                422, "DATASET_INVALID",
                f"第 {index + 1} 条 entities[{position}] 缺少 start/end/label")
        start = _as_int(start, "entity.start", index)
        end = _as_int(end, "entity.end", index)
        label = str(label).strip()
        if start < 0 or end <= start:
            raise ApiError(
                422, "DATASET_INVALID",
                f"第 {index + 1} 条 entities[{position}] 的 offset 非法："
                f"{start}..{end}")
        if not label:
            raise ApiError(
                422, "DATASET_INVALID",
                f"第 {index + 1} 条 entities[{position}] 的 label 为空")
        entities.append({"start": start, "end": end, "label": label})
    return entities


def _task_rows(path: Path, task: str, limit: int | None = None) -> list[dict[str, Any]]:
    records = _read_records(path)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise ApiError(422, "DATASET_INVALID",
                           f"第 {index + 1} 条任务样本必须是对象")
        sample_id = _same_id(task, index, raw.get("id"))
        if sample_id in seen:
            raise ApiError(422, "DATASET_INVALID", f"样本 ID 重复：{sample_id}")
        seen.add(sample_id)
        if task == "tnews":
            text = raw.get("text") or raw.get("sentence") or raw.get("input")
            label = raw.get("label")
            if not isinstance(text, str) or not text.strip():
                raise ApiError(422, "DATASET_INVALID",
                               f"{sample_id} 缺少 text/sentence")
            if label is None:
                raise ApiError(422, "DATASET_INVALID", f"{sample_id} 缺少 label")
            label = _as_int(label, "label", index)
            if label < 0:
                continue
            rows.append({"id": sample_id, "text": text.strip(), "label": label})
        elif task == "afqmc":
            text1 = raw.get("text1") or raw.get("sentence1")
            text2 = raw.get("text2") or raw.get("sentence2")
            label = raw.get("label")
            if not isinstance(text1, str) or not text1.strip():
                raise ApiError(422, "DATASET_INVALID", f"{sample_id} 缺少 text1")
            if not isinstance(text2, str) or not text2.strip():
                raise ApiError(422, "DATASET_INVALID", f"{sample_id} 缺少 text2")
            if label is None:
                raise ApiError(422, "DATASET_INVALID", f"{sample_id} 缺少 label")
            label = _as_int(label, "label", index)
            if label < 0:
                continue
            rows.append({"id": sample_id, "text1": text1.strip(),
                         "text2": text2.strip(), "label": label})
        else:
            text = raw.get("text") or raw.get("sentence") or raw.get("input")
            if not isinstance(text, str) or not text.strip():
                raise ApiError(422, "DATASET_INVALID",
                               f"{sample_id} 缺少 text/sentence")
            entities = _normalize_entities(raw.get("entities"), index)
            rows.append({"id": sample_id, "text": text.strip(), "entities": entities})
        if limit is not None and len(rows) >= limit:
            break
    if not rows:
        raise ApiError(422, "DATASET_INVALID", f"任务数据没有可用标签样本：{path}")
    return rows


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_text(text: str) -> str:
    """NFKC plus whitespace removal; IDs and labels never determine content."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _content_key(row: dict[str, Any], task: str) -> tuple[str, ...]:
    if task == "afqmc":
        return tuple(sorted((_normalized_text(row["text1"]), _normalized_text(row["text2"]))))
    return (_normalized_text(row["text"]),)


def _audit_content(train: list[dict[str, Any]], evaluation: list[dict[str, Any]],
                   task: str, ours: list[str] | None = None,
                   reference: list[str] | None = None) -> dict[str, Any]:
    eval_keys = {_content_key(row, task) for row in evaluation}
    checks = {"taskTrain/E": len({_content_key(row, task) for row in train} & eval_keys)}
    # For sentence pairs, exposure to either constituent in MLM is conservatively rejected.
    eval_texts = {part for key in eval_keys for part in key}
    for name, texts in (("D_ours/E", ours), ("D_ref/E", reference)):
        if texts is not None:
            checks[name] = len({_normalized_text(text) for text in texts} & eval_texts)
    if any(checks.values()):
        raise ApiError(422, "LEAKAGE_DETECTED", "Normalized content leakage: " + str(checks))
    return {"passed": True, "normalization": "NFKC+remove-whitespace", "checks": checks,
            "identityPolicy": "role-scoped IDs; normalized content determines overlap"}


def _task_pair(train_file: Path, eval_file: Path, task: str,
               max_train: int | None, max_eval: int | None) -> dict[str, Any]:
    train_rows = _task_rows(train_file, task, max_train)
    eval_rows = _task_rows(eval_file, task, max_eval)
    _audit_content(train_rows, eval_rows, task)

    if task in ("tnews", "afqmc"):
        labels = sorted({int(row["label"]) for row in train_rows + eval_rows})
        if len(labels) < 2:
            raise ApiError(422, "DATASET_INVALID", f"{task} 至少需要两个类别")
        label_to_id = {label: index for index, label in enumerate(labels)}
        train_labels = [label_to_id[int(row["label"])] for row in train_rows]
        eval_labels = [label_to_id[int(row["label"])] for row in eval_rows]
        return {
            "task": task,
            "train": train_rows,
            "eval": eval_rows,
            "labels": labels,
            "labelToId": label_to_id,
            "trainLabels": train_labels,
            "evalLabels": eval_labels,
            "tags": None,
            "tagToId": None,
        }

    entity_labels = sorted({
        str(entity["label"])
        for row in train_rows + eval_rows
        for entity in row["entities"]
    })
    if not entity_labels:
        raise ApiError(422, "DATASET_INVALID",
                       "CLUENER train/E 都没有实体，无法评测实体 F1")
    tags = ["O"]
    for label in entity_labels:
        tags.extend([f"B-{label}", f"I-{label}"])
    return {
        "task": task,
        "train": train_rows,
        "eval": eval_rows,
        "labels": entity_labels,
        "labelToId": None,
        "trainLabels": None,
        "evalLabels": None,
        "tags": tags,
        "tagToId": {tag: index for index, tag in enumerate(tags)},
    }


# --------------------------------------------------------------- tokenization
def _max_length(tokenizer: Any, requested: int) -> int:
    model_max = getattr(tokenizer, "model_max_length", requested)
    if not isinstance(model_max, int) or model_max <= 0 or model_max > 4096:
        model_max = requested
    return max(8, min(requested, model_max, 512))


def _collect_token_ids(texts: Sequence[str], tokenizer: Any, cap: int,
                       max_length: int, seed: int) -> tuple[list[int], int]:
    order = list(range(len(texts)))
    random.Random(seed).shuffle(order)
    ids: list[int] = []
    lines = 0
    for index in order:
        encoded = tokenizer(
            texts[index], add_special_tokens=False, truncation=True,
            max_length=max(1, max_length - 2))
        part = list(encoded.get("input_ids") or [])
        if not part:
            continue
        remaining = cap - len(ids)
        if remaining <= 0:
            break
        ids.extend(part[:remaining])
        lines += 1
        if len(ids) >= cap:
            break
    return ids, lines


def _make_sequences(ids: Sequence[int], tokenizer: Any, max_length: int) -> list[list[int]]:
    cls_id = getattr(tokenizer, "cls_token_id", None)
    sep_id = getattr(tokenizer, "sep_token_id", None)
    if cls_id is None or sep_id is None:
        raise ApiError(422, "ASSET_INVALID", "tokenizer 缺少 CLS/SEP token")
    width = max(4, max_length - 2)
    sequences: list[list[int]] = []
    for start in range(0, len(ids), width):
        chunk = list(ids[start:start + width])
        if chunk:
            sequences.append([int(cls_id), *chunk, int(sep_id)])
    if not sequences:
        raise ApiError(422, "DATASET_INVALID", "语料没有可训练的 token")
    return sequences


def _masked_batch(sequences: Sequence[Sequence[int]], tokenizer: Any,
                  rng: random.Random) -> tuple[list[list[int]], list[list[int]]]:
    mask_id = getattr(tokenizer, "mask_token_id", None)
    vocab_size = int(getattr(tokenizer, "vocab_size", 0))
    if mask_id is None or vocab_size <= 0:
        raise ApiError(422, "ASSET_INVALID", "tokenizer 缺少 mask token 或词表大小")
    special_ids = set(getattr(tokenizer, "all_special_ids", []) or [])
    masked_rows: list[list[int]] = []
    label_rows: list[list[int]] = []
    for sequence in sequences:
        input_ids = list(sequence)
        labels = [-100] * len(input_ids)
        eligible = [index for index, token in enumerate(input_ids)
                    if index not in (0, len(input_ids) - 1)
                    and token not in special_ids]
        if not eligible:
            masked_rows.append(input_ids)
            label_rows.append(labels)
            continue
        count = min(len(eligible), max(1, int(round(len(eligible) * MASK_RATE))))
        chosen = rng.sample(eligible, count)
        for position in chosen:
            original = input_ids[position]
            labels[position] = original
            draw = rng.random()
            if draw < 0.8:
                input_ids[position] = int(mask_id)
            elif draw < 0.9:
                input_ids[position] = rng.randrange(vocab_size)
            # Remaining 10%: keep the original token.
        masked_rows.append(input_ids)
        label_rows.append(labels)
    return masked_rows, label_rows


# --------------------------------------------------------------------- training
def _train_mlm(model: Any, sequences: Sequence[Sequence[int]], tokenizer: Any,
               *, steps: int, batch_size: int, learning_rate: float,
               seed: int) -> dict[str, Any]:
    if torch is None:  # pragma: no cover - guarded by caller
        raise ApiError(422, "ENGINE_UNAVAILABLE", "torch 不可用")
    torch.manual_seed(seed)
    model.train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate,
                                  weight_decay=0.01)
    rng = random.Random(seed)
    losses: list[float] = []
    masked_tokens = 0
    for _step in range(steps):
        batch = [sequences[rng.randrange(len(sequences))]
                 for _ in range(batch_size)]
        input_rows, label_rows = _masked_batch(batch, tokenizer, rng)
        width = max(len(row) for row in input_rows)
        pad_id = int(getattr(tokenizer, "pad_token_id", 0) or 0)
        input_ids = torch.full((len(input_rows), width), pad_id, dtype=torch.long)
        attention = torch.zeros((len(input_rows), width), dtype=torch.long)
        labels = torch.full((len(label_rows), width), -100, dtype=torch.long)
        for row, (tokens, row_labels) in enumerate(zip(input_rows, label_rows)):
            input_ids[row, :len(tokens)] = torch.tensor(tokens, dtype=torch.long)
            attention[row, :len(tokens)] = 1
            labels[row, :len(row_labels)] = torch.tensor(row_labels, dtype=torch.long)
        masked_tokens += int((labels != -100).sum())
        outputs = model(input_ids=input_ids, attention_mask=attention, labels=labels)
        loss = outputs.loss
        if loss is None or not math.isfinite(float(loss.detach())):
            raise ApiError(422, "TEXT_ENGINE_FAILED", "MLM loss 不是有限数值")
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        losses.append(float(loss.detach()))
    model.eval()
    return {
        "steps": steps,
        "batchSize": batch_size,
        "maskedTokens": masked_tokens,
        "meanTrainLoss": round(sum(losses) / len(losses), 6),
        "learningRate": learning_rate,
        "maskRate": MASK_RATE,
        "maskScheme": MASK_SCHEME,
    }


def _load_mlm(path: Path, seed: int) -> Any:
    if AutoModelForMaskedLM is None:
        raise ApiError(422, "ENGINE_UNAVAILABLE", "transformers 未安装")
    try:
        torch.manual_seed(seed)
        model = AutoModelForMaskedLM.from_pretrained(
            str(path), local_files_only=True)
    except Exception as exc:
        raise ApiError(422, "ASSET_INVALID",
                       f"本地 BERT 加载失败：{type(exc).__name__}: {exc}") from exc
    if not hasattr(model, "bert"):
        raise ApiError(422, "ASSET_INVALID",
                       "本地模型不是 BERT 结构，无法导出共享 encoder")
    return model


def _task_model_from_mlm(mlm: Any, task: str, num_labels: int,
                         path: Path, seed: int) -> Any:
    if torch is None:
        raise ApiError(422, "ENGINE_UNAVAILABLE", "torch 不可用")
    torch.manual_seed(seed)
    try:
        if task == "cluener":
            model = AutoModelForTokenClassification.from_pretrained(
                str(path), num_labels=num_labels, local_files_only=True)
        else:
            model = AutoModelForSequenceClassification.from_pretrained(
                str(path), num_labels=num_labels, local_files_only=True)
    except Exception as exc:
        raise ApiError(422, "ASSET_INVALID",
                       f"任务头初始化失败：{type(exc).__name__}: {exc}") from exc
    base = getattr(model, "bert", None)
    if base is None:
        raise ApiError(422, "ASSET_INVALID", "任务模型缺少 BERT encoder")
    incompatible = base.load_state_dict(mlm.bert.state_dict(), strict=False)
    # BertForMaskedLM is built without a pooler; the task model has one and it
    # is trained by the task head.  Any other mismatch means the encoder is not
    # the same architecture and must not be silently accepted.
    allowed_missing = {"pooler.dense.weight", "pooler.dense.bias"}
    unexpected = set(incompatible.unexpected_keys)
    missing = set(incompatible.missing_keys) - allowed_missing
    if missing or unexpected:
        raise ApiError(
            422, "ASSET_INVALID",
            "MLM encoder 与任务模型不匹配："
            f"missing={sorted(missing)[:5]} "
            f"unexpected={sorted(unexpected)[:5]}")
    head = getattr(model, "classifier", None)
    if head is not None and hasattr(head, "weight"):
        torch.manual_seed(seed + 1)
        head.weight.data.normal_(mean=0.0, std=float(
            getattr(model.config, "initializer_range", 0.02)))
        if getattr(head, "bias", None) is not None:
            head.bias.data.zero_()
    return model


def _collate(features: Sequence[dict[str, Any]], labels: Sequence[int] | None,
             pad_id: int) -> dict[str, Any]:
    width = max(len(feature["input_ids"]) for feature in features)
    input_ids = torch.full((len(features), width), pad_id, dtype=torch.long)
    attention = torch.zeros((len(features), width), dtype=torch.long)
    token_types = torch.zeros((len(features), width), dtype=torch.long)
    for row, feature in enumerate(features):
        length = len(feature["input_ids"])
        input_ids[row, :length] = torch.tensor(feature["input_ids"], dtype=torch.long)
        attention[row, :length] = 1
        if "token_type_ids" in feature:
            token_types[row, :length] = torch.tensor(
                feature["token_type_ids"], dtype=torch.long)
    batch: dict[str, Any] = {
        "input_ids": input_ids,
        "attention_mask": attention,
        "token_type_ids": token_types,
    }
    if labels is not None:
        if len(labels) > 0 and isinstance(labels[0], (list, tuple)):
            label_tensor = torch.full((len(labels), width), -100, dtype=torch.long)
            for row, row_labels in enumerate(labels):
                label_tensor[row, :len(row_labels)] = torch.tensor(
                    row_labels, dtype=torch.long)
            batch["labels"] = label_tensor
        else:
            batch["labels"] = torch.tensor(labels, dtype=torch.long)
    return batch


def _seq_features(rows: Sequence[dict[str, Any]], tokenizer: Any,
                  task: str, max_length: int) -> list[dict[str, Any]]:
    features = []
    for row in rows:
        if task == "tnews":
            encoded = tokenizer(row["text"], truncation=True, max_length=max_length)
        else:
            encoded = tokenizer(row["text1"], row["text2"], truncation=True,
                                max_length=max_length)
        feature = {"input_ids": list(encoded["input_ids"])}
        if "token_type_ids" in encoded:
            feature["token_type_ids"] = list(encoded["token_type_ids"])
        features.append(feature)
    return features


def _ner_features(rows: Sequence[dict[str, Any]], tokenizer: Any,
                  tags: Sequence[str], max_length: int) -> tuple[list[dict[str, Any]], list[list[int]]]:
    tag_to_id = {tag: index for index, tag in enumerate(tags)}
    features: list[dict[str, Any]] = []
    label_rows: list[list[int]] = []
    for row in rows:
        text = row["text"]
        char_text = list(text)
        gold_tags = entities_to_bio(text, row["entities"], tags)
        encoded = tokenizer(
            char_text, is_split_into_words=True, truncation=True,
            max_length=max_length, add_special_tokens=True)
        word_ids = encoded.word_ids()
        token_labels = [-100] * len(encoded["input_ids"])
        seen: set[int] = set()
        for token_index, word_id in enumerate(word_ids):
            if word_id is None or word_id in seen:
                continue
            seen.add(word_id)
            token_labels[token_index] = tag_to_id[gold_tags[word_id]]
        feature = {"input_ids": list(encoded["input_ids"]),
                   "word_ids": list(word_ids)}
        if "token_type_ids" in encoded:
            feature["token_type_ids"] = list(encoded["token_type_ids"])
        features.append(feature)
        label_rows.append(token_labels)
    return features, label_rows


def _train_task(model: Any, features: Sequence[dict[str, Any]],
                labels: Sequence[Any], *, task: str, epochs: int,
                max_steps: int | None, batch_size: int,
                learning_rate: float, seed: int,
                pad_id: int) -> dict[str, Any]:
    if torch is None:
        raise ApiError(422, "ENGINE_UNAVAILABLE", "torch 不可用")
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate,
                                  weight_decay=0.01)
    rng = random.Random(seed)
    torch.manual_seed(seed)
    model.train()
    losses: list[float] = []
    steps = 0
    order = list(range(len(features)))
    for _epoch in range(epochs):
        rng.shuffle(order)
        for start in range(0, len(order), batch_size):
            indices = order[start:start + batch_size]
            batch_features = [features[index] for index in indices]
            batch_labels = [labels[index] for index in indices]
            batch = _collate(batch_features, batch_labels, pad_id)
            outputs = model(**batch)
            loss = outputs.loss
            if loss is None or not math.isfinite(float(loss.detach())):
                raise ApiError(422, "TEXT_ENGINE_FAILED", "任务 loss 不是有限数值")
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach()))
            steps += 1
            if max_steps is not None and steps >= max_steps:
                break
        if max_steps is not None and steps >= max_steps:
            break
    model.eval()
    return {
        "epochs": epochs,
        "steps": steps,
        "batchSize": batch_size,
        "meanTrainLoss": round(sum(losses) / len(losses), 6) if losses else None,
        "learningRate": learning_rate,
    }


def _predict_seq(model: Any, features: Sequence[dict[str, Any]],
                 pad_id: int, batch_size: int) -> list[int]:
    predictions: list[int] = []
    model.eval()
    with torch.no_grad():
        for start in range(0, len(features), batch_size):
            batch = _collate(features[start:start + batch_size], None, pad_id)
            logits = model(**batch).logits
            predictions.extend(logits.argmax(dim=-1).tolist())
    return predictions


def _predict_ner(model: Any, features: Sequence[dict[str, Any]],
                 rows: Sequence[dict[str, Any]], id_to_tag: Sequence[str],
                 pad_id: int, batch_size: int) -> list[list[dict[str, Any]]]:
    predictions: list[int] = []
    model.eval()
    with torch.no_grad():
        for start in range(0, len(features), batch_size):
            batch = _collate(features[start:start + batch_size], None, pad_id)
            logits = model(**batch).logits
            predictions.extend(logits.argmax(dim=-1).tolist())

    entities: list[list[dict[str, Any]]] = []
    for row, feature, prediction in zip(rows, features, predictions):
        text = row["text"]
        tags = ["O"] * len(text)
        seen: set[int] = set()
        for token_index, word_id in enumerate(feature["word_ids"]):
            if word_id is None or word_id in seen or token_index >= len(prediction):
                continue
            seen.add(word_id)
            tags[word_id] = id_to_tag[int(prediction[token_index])]
        offsets = [(index, index + 1) for index in range(len(text))]
        entities.append(bio_labels_to_entities(tags, offsets))
    return entities


def _task_scores(task: str, rows: Sequence[dict[str, Any]],
                 features: Sequence[dict[str, Any]], labels: Sequence[Any],
                 model: Any, id_to_tag: Sequence[str] | None, pad_id: int,
                 batch_size: int) -> tuple[dict[str, float], dict[str, Any]]:
    if task in ("tnews", "afqmc"):
        predictions = _predict_seq(model, features, pad_id, batch_size)
        metrics = classification_metrics(labels, predictions)
        detail = {"sampleCount": len(rows), "primaryMetric": "accuracy"}
        return {"accuracy": metrics["accuracy"],
                "macroF1": metrics["macroF1"]}, detail

    predicted_entities = _predict_ner(
        model, features, rows, id_to_tag or [], pad_id, batch_size)
    true_positive = false_positive = false_negative = 0
    for row, row_predicted in zip(rows, predicted_entities):
        row_metrics = entity_f1(row["entities"], row_predicted)
        true_positive += int(row_metrics["tp"])
        false_positive += int(row_metrics["fp"])
        false_negative += int(row_metrics["fn"])
    precision = (100.0 if true_positive + false_positive == 0 else
                 100.0 * true_positive / (true_positive + false_positive))
    recall = (100.0 if true_positive + false_negative == 0 else
              100.0 * true_positive / (true_positive + false_negative))
    entity_f1_score = (0.0 if precision + recall == 0 else
                       2 * precision * recall / (precision + recall))
    tag_ids = {tag: index for index, tag in enumerate(id_to_tag or [])}
    gold_tags = [tag_ids[tag] for row in rows
                 for tag in entities_to_bio(row["text"], row["entities"], id_to_tag or [])]
    pred_tags = [tag_ids[tag] for row, entities in zip(rows, predicted_entities)
                 for tag in entities_to_bio(row["text"], entities, id_to_tag or [])]
    token_metrics = classification_metrics(gold_tags, pred_tags)
    detail = {
        "sampleCount": len(rows),
        "primaryMetric": "entityF1",
        "mentions": true_positive + false_negative,
        "entityTp": true_positive,
        "entityFp": false_positive,
        "entityFn": false_negative,
    }
    return {
        "accuracy": token_metrics["accuracy"],
        "macroF1": token_metrics["macroF1"],
        "entityPrecision": round(min(max(precision, 0.0), 100.0), 6),
        "entityRecall": round(min(max(recall, 0.0), 100.0), 6),
        "entityF1": round(min(max(entity_f1_score, 0.0), 100.0), 6),
    }, detail


# ------------------------------------------------------------------ public API
def _run_text_experiment_once(spec: TextProtocolSpec) -> dict[str, Any]:
    """Run the fixed BERT-MLM + CLUE downstream protocol for one task."""
    _require_backend()
    assert torch is not None  # narrowed for type checkers
    assert AutoTokenizer is not None
    tokenizer_path = spec.tokenizer_path or spec.model_path
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            str(tokenizer_path), local_files_only=True)
    except Exception as exc:
        raise ApiError(422, "ASSET_INVALID",
                       f"tokenizer 加载失败：{type(exc).__name__}: {exc}") from exc
    if getattr(tokenizer, "mask_token_id", None) is None:
        raise ApiError(422, "ASSET_INVALID", "tokenizer 缺少 mask token")
    max_length = _max_length(tokenizer, spec.max_length)

    ours_texts = _corpus_texts(spec.ours_corpus)
    ref_texts = _corpus_texts(spec.ref_corpus)
    task_data = _task_pair(spec.train_file, spec.eval_file, spec.task,
                           spec.max_train, spec.max_eval)
    audit = _audit_content(task_data["train"], task_data["eval"], spec.task, ours_texts, ref_texts)
    label_count = (len(task_data["labels"]) if spec.task in ("tnews", "afqmc")
                   else len(task_data["tags"]))
    if label_count < 2:
        raise ApiError(422, "DATASET_INVALID", "任务标签不足")

    ours_ids, ours_lines = _collect_token_ids(
        ours_texts, tokenizer, spec.mlm_token_budget, max_length, spec.seed)
    ref_ids, ref_lines = _collect_token_ids(
        ref_texts, tokenizer, spec.mlm_token_budget, max_length, spec.seed)
    budget = min(len(ours_ids), len(ref_ids), spec.mlm_token_budget)
    if budget < 8:
        raise ApiError(
            422, "DATASET_INVALID",
            f"D_ours/D_ref 可对齐 token 太少（{budget}），无法训练 MLM；"
            "请补充语料或降低 max_length")
    ours_ids = ours_ids[:budget]
    ref_ids = ref_ids[:budget]
    ours_sequences = _make_sequences(ours_ids, tokenizer, max_length)
    ref_sequences = _make_sequences(ref_ids, tokenizer, max_length)

    ours_lr = float(spec.ours_mlm_lr if spec.ours_mlm_lr is not None else spec.mlm_lr)
    ref_lr = float(spec.ref_mlm_lr if spec.ref_mlm_lr is not None else spec.mlm_lr)
    ours_mlm = _load_mlm(spec.model_path, spec.seed)
    ref_mlm = _load_mlm(spec.model_path, spec.seed)
    ours_mlm_stats = _train_mlm(
        ours_mlm, ours_sequences, tokenizer, steps=spec.mlm_steps,
        batch_size=spec.mlm_batch_size, learning_rate=ours_lr, seed=spec.seed)
    ref_mlm_stats = _train_mlm(
        ref_mlm, ref_sequences, tokenizer, steps=spec.mlm_steps,
        batch_size=spec.mlm_batch_size, learning_rate=ref_lr, seed=spec.seed)

    if spec.task in ("tnews", "afqmc"):
        features = _seq_features(task_data["train"], tokenizer, spec.task, max_length)
        eval_features = _seq_features(task_data["eval"], tokenizer, spec.task, max_length)
        train_labels = task_data["trainLabels"]
        eval_labels = task_data["evalLabels"]
        id_to_tag = None
    else:
        features, train_labels = _ner_features(
            task_data["train"], tokenizer, task_data["tags"], max_length)
        eval_features, eval_labels = _ner_features(
            task_data["eval"], tokenizer, task_data["tags"], max_length)
        id_to_tag = task_data["tags"]

    pad_id = int(getattr(tokenizer, "pad_token_id", 0) or 0)
    task_model_ours = _task_model_from_mlm(
        ours_mlm, spec.task, label_count, spec.model_path, spec.seed)
    task_model_ref = _task_model_from_mlm(
        ref_mlm, spec.task, label_count, spec.model_path, spec.seed)
    task_ours_stats = _train_task(
        task_model_ours, features, train_labels, task=spec.task,
        epochs=spec.task_epochs, max_steps=spec.task_max_steps,
        batch_size=spec.task_batch_size, learning_rate=spec.task_lr,
        seed=spec.seed, pad_id=pad_id)
    task_ref_stats = _train_task(
        task_model_ref, features, train_labels, task=spec.task,
        epochs=spec.task_epochs, max_steps=spec.task_max_steps,
        batch_size=spec.task_batch_size, learning_rate=spec.task_lr,
        seed=spec.seed, pad_id=pad_id)
    ours_scores, details = _task_scores(
        spec.task, task_data["eval"], eval_features, eval_labels,
        task_model_ours, id_to_tag, pad_id, spec.task_batch_size)
    ref_scores, _ = _task_scores(
        spec.task, task_data["eval"], eval_features, eval_labels,
        task_model_ref, id_to_tag, pad_id, spec.task_batch_size)
    delta = {
        key: round(ours_scores[key] - ref_scores[key], 6)
        for key in ours_scores
    }
    primary = "entityF1" if spec.task == "cluener" else "accuracy"

    return {
        "leakageAudit": audit,
        "schemaVersion": "1.0",
        "createdAt": now(),
        "engineVersion": ENGINE_VERSION,
        "experiment": "text-clue-downstream",
        "task": spec.task,
        "protocol": {
            "proxyModel": "bert-base-chinese",
            "parameters": "102M",
            "modelPath": spec.model_path.name or "local-bert",
            "tokenizerPath": tokenizer_path.name or "local-tokenizer",
            "modelConfig": {
                "hiddenSize": getattr(ours_mlm.config, "hidden_size", None),
                "layers": getattr(ours_mlm.config, "num_hidden_layers", None),
                "heads": getattr(ours_mlm.config, "num_attention_heads", None),
                "vocabSize": getattr(ours_mlm.config, "vocab_size", None),
            },
            "mlm": {
                "task": "masked-language-model",
                "maskRate": MASK_RATE,
                "maskScheme": MASK_SCHEME,
                "learningRate": {"ours": ours_lr, "ref": ref_lr},
                "tokenBudget": budget,
                "steps": spec.mlm_steps,
                "batchSize": spec.mlm_batch_size,
                "maxLength": max_length,
            },
            "downstream": {
                "task": spec.task,
                "trainSize": len(task_data["train"]),
                "evalSize": len(task_data["eval"]),
                "epochs": spec.task_epochs,
                "maxSteps": spec.task_max_steps,
                "batchSize": spec.task_batch_size,
                "learningRate": spec.task_lr,
                "primaryMetric": primary,
            },
            "seed": spec.seed,
            "alignment": {
                "oursTokens": len(ours_ids),
                "refTokens": len(ref_ids),
                "oursLines": ours_lines,
                "refLines": ref_lines,
                "oursCorpusLines": len(ours_texts),
                "refCorpusLines": len(ref_texts),
                "oursCorpusChars": sum(len(text) for text in ours_texts),
                "refCorpusChars": sum(len(text) for text in ref_texts),
                "taskTrainFixed": True,
                "taskEvalFixed": True,
                "sameSeed": True,
            },
        },
        "data": {
            "oursDigest": _digest(ours_texts),
            "refDigest": _digest(ref_texts),
            "taskTrainDigest": _digest(task_data["train"]),
            "taskEvalDigest": _digest(task_data["eval"]),
            "oursCorpus": spec.ours_corpus.name,
            "refCorpus": spec.ref_corpus.name,
            "trainFile": spec.train_file.name,
            "evalFile": spec.eval_file.name,
        },
        "mlm": {"ours": ours_mlm_stats, "ref": ref_mlm_stats},
        "taskTraining": {"ours": task_ours_stats, "ref": task_ref_stats},
        "details": details,
        "S_ours": ours_scores,
        "S_ref": ref_scores,
        "delta": delta,
        "provenance": {key: "measured_model" for key in ours_scores},
        "provenanceRef": {key: "measured_model" for key in ref_scores},
        "notes": [
            "同一 BERT-base 起点、同一任务 train、同一固定评测集 E；只替换 MLM 预训练语料。",
            "MLM 使用 15% 掩码与 80/10/10 替换；默认低学习率 2e-5，任务头也使用低学习率。",
            "D_ours/D_ref 按 token 预算强制对齐；任务 train/E 不重叠并做 ID 去重。",
            "CLUE 三个任务指标不能相加成一个总分；S_ref 不是论文数值，除非模型与协议完全一致。",
            ("若 ours/ref MLM 学习率不同，结果包含调参差异，不能单独归因于数据质量。"
             if abs(ours_lr - ref_lr) > 1e-12 else
             "两侧 MLM 学习率相同；如需数据特定调优，应在记录中显式保留两侧学习率。"),
        ],
    }


@training_locked
def run_text_experiment(spec: TextProtocolSpec) -> dict[str, Any]:
    """Run paired seeds; score dictionaries remain numeric means for compatibility."""
    seeds = spec.seeds if spec.seeds is not None else (spec.seed,)
    runs = [_run_text_experiment_once(replace(spec, seed=seed, seeds=None)) for seed in seeds]
    if any(run["data"] != runs[0]["data"] for run in runs[1:]):
        raise ApiError(422, "DATASET_CHANGED", "多种子运行期间文本数据发生变化")
    record = dict(runs[0])
    stats = {}
    for side in ("S_ours", "S_ref", "delta"):
        stats[side] = {}
        for metric in runs[0][side]:
            values = [run[side][metric] for run in runs]
            stats[side][metric] = {"mean": statistics.mean(values),
                                   "std": statistics.stdev(values) if len(values) > 1 else None,
                                   "n": len(values)}
        record[side] = {metric: value["mean"] for metric, value in stats[side].items()}
    comparable = record["protocol"]["mlm"]["learningRate"]["ours"] == record["protocol"]["mlm"]["learningRate"]["ref"]
    required = 5 if spec.purpose == "formal" else 3
    record.update(runs=runs, statistics=stats,
                  qualityConclusionEligible=(spec.purpose in ("quality", "formal") and len(seeds) >= required and comparable and all(run["leakageAudit"]["passed"] for run in runs)))
    record["protocol"] = dict(record["protocol"], seeds=list(seeds), purpose=spec.purpose,
                              requiredSeeds=required, comparable=comparable)
    return record
