"""Dependency-free text-downstream metrics for the ``text_clue`` experiment.

The module deliberately imports only the standard library: it must work while a
text model trains, so torch / transformers / numpy / pyarrow are out of scope.

Every score is returned as a percentage in ``[0, 100]`` and rounded to six
decimals to strip binary float noise (``round(x, 6)``, the same convention as
``app/engines/text.py``).  Classification semantics follow that engine:

* classes are the union of the gold and predicted labels;
* macro-F1 averages the per-class F1, and a single gold class degenerates to
  micro accuracy instead of inventing a one-class macro average;
* empty input has no evidence, so it scores ``0.0``.

Entity spans are half-open ``[start, end)`` with Python character offsets and
match only on the exact ``(start, end, label)`` triple; extra keys such as
confidence are ignored.  Both sides empty means "nothing to predict and nothing
to miss", so precision/recall/F1 are all ``100.0``.
"""
from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "OUTSIDE",
    "bio_labels_to_entities",
    "classification_metrics",
    "entities_to_bio",
    "entity_f1",
    "macro_f1",
]

OUTSIDE = "O"
IGNORED_LABEL = -100
_SCALE = 100.0
_DECIMALS = 6


# --------------------------------------------------------------- normalisation
def _percent(value: float) -> float:
    """Clamp a percentage to ``[0, 100]`` and round away float noise."""
    return round(min(max(float(value), 0.0), _SCALE), _DECIMALS)


def _as_items(values: Any, name: str, *, allow_none: bool = False) -> list[Any]:
    """Copy a caller sequence; ``None`` is only accepted when documented."""
    if values is None:
        if allow_none:
            return []
        raise ValueError(f"{name} 不能为 None")
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} 必须是序列，不能是字符串")
    try:
        return list(values)
    except TypeError as exc:
        raise ValueError(f"{name} 必须是可迭代序列") from exc


def _coerce_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数，收到布尔值")
    try:
        return int(operator.index(value))
    except TypeError as exc:
        raise ValueError(
            f"{name} 必须是整数，收到 {type(value).__name__}") from exc


def _as_label_items(values: Any, name: str) -> list[int]:
    items = _as_items(values, name)
    return [_coerce_int(value, f"{name}[{index}]") for index, value in enumerate(items)]


def _same_length(gold: Sequence[int], pred: Sequence[int]) -> None:
    if len(gold) != len(pred):
        raise ValueError(
            f"gold 与 pred 长度必须一致：{len(gold)} != {len(pred)}")


# ----------------------------------------------------------------- classification
def macro_f1(gold: Sequence[int], pred: Sequence[int]) -> float:
    """Macro-averaged per-class F1, as a percentage in ``[0, 100]``.

    Classes are ``set(gold) | set(pred)``.  When the gold labels hold a single
    class the macro average is degenerate, so the result falls back to micro
    accuracy — the same rule as ``app/engines/text.py``.  Empty input scores
    ``0.0``; mismatched lengths raise ``ValueError`` instead of silently
    truncating.
    """
    gold_labels = _as_label_items(gold, "gold")
    pred_labels = _as_label_items(pred, "pred")
    _same_length(gold_labels, pred_labels)
    if not gold_labels:
        return 0.0

    correct = sum(1 for truth, guess in zip(gold_labels, pred_labels) if truth == guess)
    accuracy = correct / len(gold_labels) * _SCALE
    gold_classes = set(gold_labels)
    if len(gold_classes) < 2:
        # Only one target class: macro-F1 degenerates, use micro accuracy.
        return _percent(accuracy)

    pairs = list(zip(gold_labels, pred_labels))
    scores: list[float] = []
    for label in sorted(gold_classes | set(pred_labels)):
        true_positive = sum(1 for truth, guess in pairs
                            if truth == label and guess == label)
        false_positive = sum(1 for truth, guess in pairs
                             if truth != label and guess == label)
        false_negative = sum(1 for truth, guess in pairs
                             if truth == label and guess != label)
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(0.0 if denominator == 0 else 2 * true_positive / denominator)
    return _percent(_SCALE * sum(scores) / len(scores))


def classification_metrics(gold: Sequence[int], pred: Sequence[int]) -> dict[str, float]:
    """Return ``{"accuracy": ..., "macroF1": ...}``, both in ``[0, 100]``.

    Empty input returns zeros for both keys; lengths must match.
    """
    gold_labels = _as_label_items(gold, "gold")
    pred_labels = _as_label_items(pred, "pred")
    _same_length(gold_labels, pred_labels)
    if not gold_labels:
        return {"accuracy": 0.0, "macroF1": 0.0}
    correct = sum(1 for truth, guess in zip(gold_labels, pred_labels) if truth == guess)
    return {
        "accuracy": _percent(correct / len(gold_labels) * _SCALE),
        "macroF1": macro_f1(gold_labels, pred_labels),
    }


# ----------------------------------------------------------------------- entities
def _span(entity: Any, index: int) -> tuple[int, int, str]:
    """Validate one entity and return ``(start, end, label)``; extra keys ignored."""
    if not isinstance(entity, Mapping):
        raise ValueError(f"entities[{index}] 必须是 dict，收到 {type(entity).__name__}")
    for key in ("start", "end", "label"):
        if key not in entity:
            raise ValueError(f"entities[{index}] 缺少字段：{key}")
    start = _coerce_int(entity["start"], f"entities[{index}].start")
    end = _coerce_int(entity["end"], f"entities[{index}].end")
    label = entity["label"]
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"entities[{index}].label 必须是非空字符串")
    if start < 0:
        raise ValueError(f"entities[{index}].start 不能为负数：{start}")
    if end <= start:
        raise ValueError(f"entities[{index}] 的 end 必须大于 start：{(start, end)}")
    return start, end, label.strip()


def entity_f1(gold_entities: Sequence[dict],
              pred_entities: Sequence[dict]) -> dict[str, float | int]:
    """Exact-span micro precision/recall/F1 plus ``tp``/``fp``/``fn`` counts.

    Spans match only when ``(start, end, label)`` is identical; ``end`` is
    exclusive and other entity keys are ignored.  Duplicates collapse to a set,
    so a span counts at most once per side.

    Both sides empty scores ``100.0`` on all three percentages (nothing was
    predictable, so nothing was missed); one empty side scores ``0.0``.
    Malformed entities raise ``ValueError``.
    """
    gold_items = _as_items(gold_entities, "gold_entities", allow_none=True)
    pred_items = _as_items(pred_entities, "pred_entities", allow_none=True)
    gold_spans = {_span(entity, index) for index, entity in enumerate(gold_items)}
    pred_spans = {_span(entity, index) for index, entity in enumerate(pred_items)}

    true_positive = len(gold_spans & pred_spans)
    false_positive = len(pred_spans - gold_spans)
    false_negative = len(gold_spans - pred_spans)
    both_empty = not gold_spans and not pred_spans
    # An empty denominator only means 100% when neither side had anything to
    # match; if just one side is empty there was nothing correct, so it is 0.
    predicted_total = true_positive + false_positive
    gold_total = true_positive + false_negative
    if predicted_total:
        precision = _SCALE * true_positive / predicted_total
    else:
        precision = _SCALE if both_empty else 0.0
    if gold_total:
        recall = _SCALE * true_positive / gold_total
    else:
        recall = _SCALE if both_empty else 0.0
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "precision": _percent(precision),
        "recall": _percent(recall),
        "f1": _percent(f1),
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
    }


def _offset(offset: Any) -> tuple[int, int] | None:
    """Return a usable ``(start, end)`` or ``None`` when missing/malformed."""
    if offset is None:
        return None
    if isinstance(offset, (str, bytes)):
        return None
    try:
        values = tuple(offset)
    except TypeError:
        return None
    if len(values) != 2:
        return None
    try:
        start = _coerce_int(values[0], "offset")
        end = _coerce_int(values[1], "offset")
    except ValueError:
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _parse_tag(raw: Any, index: int) -> None | str | tuple[str, str]:
    """Classify one token label.

    Returns ``None`` for tokens that carry no BIO meaning (padding ``-100``,
    special-token labels), ``"O"`` for outside, ``("B", label)``/``("I", label)``
    for entity tags, and ``"O"`` for unrecognised tags (they close the current
    entity because they clearly are not a continuation).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"labels[{index}] 必须是 BIO 字符串，收到布尔值")
    if not isinstance(raw, str):
        try:
            number = _coerce_int(raw, f"labels[{index}]")
        except ValueError as exc:
            raise ValueError(
                f"labels[{index}] 必须是 BIO 字符串，收到 {type(raw).__name__}") from exc
        if number == IGNORED_LABEL:
            return None
        raise ValueError(f"labels[{index}] 必须是 BIO 字符串，收到整数 {number}")
    tag = raw.strip()
    if not tag or tag == str(IGNORED_LABEL):
        return None
    if ((tag.startswith("[") and tag.endswith("]"))
            or (tag.startswith("<") and tag.endswith(">"))):
        return None
    if tag == OUTSIDE:
        return OUTSIDE
    if tag[:2] in ("B-", "I-"):
        label = tag[2:].strip()
        return (tag[0], label) if label else OUTSIDE
    return OUTSIDE


def bio_labels_to_entities(labels: Sequence[str],
                           offsets: Sequence[tuple[int, int] | None] | None) -> list[dict]:
    """Rebuild entities from a per-token BIO sequence and token offsets.

    ``END is exclusive`` and offsets are Python character offsets, so the
    returned spans can be fed back into :func:`entities_to_bio` unchanged.

    Tolerated input (skipped, never fabricated):

    * ``labels`` / ``offsets`` entries for padding, ``-100`` or special tokens
      (``None`` offsets or ``-100`` / ``"[CLS]"`` labels) are ignored without
      touching the entity being built;
    * an ``I-X`` without a matching open ``B-X`` is skipped;
    * a missing or malformed offset (``None``, wrong arity, non-integer,
      negative start, ``end <= start``) is skipped;
    * a ``B-`` span that starts before the previous accepted entity ends is
      skipped, because flat BIO cannot represent overlapping entities.

    A gap between consecutive tokens of one entity (whitespace, punctuation)
    is allowed and absorbed into the span.  ``offsets=None`` yields ``[]``.
    """
    label_items = _as_items(labels, "labels")
    if offsets is None:
        return []
    offset_items = _as_items(offsets, "offsets")
    if len(label_items) != len(offset_items):
        raise ValueError(
            f"labels 与 offsets 长度必须一致：{len(label_items)} != {len(offset_items)}")

    entities: list[dict] = []
    current: dict | None = None
    last_end = 0

    def flush() -> None:
        nonlocal current, last_end
        if current is not None:
            entities.append(current)
            last_end = current["end"]
            current = None

    for index, (raw_label, raw_offset) in enumerate(zip(label_items, offset_items)):
        tag = _parse_tag(raw_label, index)
        if tag is None:
            continue
        if tag == OUTSIDE:
            flush()
            continue
        kind, label = tag
        span = _offset(raw_offset)
        if kind == "B":
            flush()
            if span is None or span[0] < last_end:
                continue
            current = {"start": span[0], "end": span[1], "label": label}
            continue
        # "I" tag: only continues an open entity of the very same label.
        if current is None or current["label"] != label:
            continue
        if span is None:
            continue
        current["end"] = max(current["end"], span[1])
    flush()
    return entities


def _label_scheme(labels: Sequence[str]) -> list[str]:
    items = _as_items(labels, "labels")
    scheme: list[str] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"labels[{index}] 必须是非空字符串")
        scheme.append(raw.strip())
    if not scheme:
        raise ValueError("labels 不能为空，至少需要包含 \"O\"")
    if len(set(scheme)) != len(scheme):
        raise ValueError("labels 存在重复标签")
    if OUTSIDE not in scheme:
        raise ValueError("labels 必须包含 \"O\"")
    return scheme


def entities_to_bio(text: str, entities: Sequence[dict],
                    labels: Sequence[str]) -> list[str]:
    """Render entities as one BIO tag per Python character of ``text``.

    ``labels`` is the fixed tag order used by the training pipeline (for
    example ``["O", "B-name", "I-name", ...]``) and defines the allowed tags;
    ``"O"`` must be present.  Spans are half-open and must stay inside
    ``text``.

    Out-of-range, empty, repeated or overlapping entities raise ``ValueError``:
    silently dropping them would shift the training alignment without telling
    anyone.  ``entities`` may be given in any order; output order is by
    character position.
    """
    if not isinstance(text, str):
        raise ValueError("text 必须是 str")
    scheme = _label_scheme(labels)
    allowed = set(scheme)
    spans = [_span(entity, index)
             for index, entity in enumerate(_as_items(entities, "entities"))]
    length = len(text)
    if not spans:
        return [OUTSIDE] * length
    if length == 0:
        raise ValueError("text 为空但传入了实体")

    tags = [OUTSIDE] * length
    cursor = 0
    for start, end, label in sorted(spans):
        if end > length:
            raise ValueError(
                f"实体越界：{(start, end)} 超出文本长度 {length}")
        if start < cursor:
            raise ValueError(f"实体重叠或重复：{(start, end, label)}")
        opening = f"B-{label}"
        if opening not in allowed:
            raise ValueError(f"labels 缺少实体标签：{opening}")
        tags[start] = opening
        continuation = f"I-{label}"
        if end > start + 1 and continuation not in allowed:
            raise ValueError(f"labels 缺少实体标签：{continuation}")
        for position in range(start + 1, end):
            tags[position] = continuation
        cursor = end
    return tags
