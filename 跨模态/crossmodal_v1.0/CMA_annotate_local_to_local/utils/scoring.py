"""评分与匹配的纯算法层（无 IO、无模型，便于单测）。

职责：
- ``cosine_similarity``：L2 单位向量余弦（缺失/零向量返回 0.0，不抛错）；
- ``meets_threshold``：表1 「达到阈值才预填」的判定，边界含等于；
- ``assign_one_to_one``：把词与候选框做成**一对一**关系（按分数降序贪心）；
- ``class_calibrate``：按框类别（COCO 类）做列方向标准化，**只用于指派**，
  原始余弦与 ``min_clip_score`` 阈值判定不变。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["assign_one_to_one", "class_calibrate", "cosine_similarity", "mean",
           "meets_threshold", "round_score", "summarize"]

EPSILON = 1e-9
SCORE_DECIMALS = 6


def round_score(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), SCORE_DECIMALS)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """两个等长向量的余弦相似度，结果裁剪到 ``[-1, 1]``。"""
    if not left or not right:
        raise ValueError("余弦相似度需要非空向量")
    if len(left) != len(right):
        raise ValueError("向量维度不一致：%d vs %d" % (len(left), len(right)))
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for index in range(len(left)):
        a = float(left[index])
        b = float(right[index])
        if math.isnan(a) or math.isnan(b) or math.isinf(a) or math.isinf(b):
            raise ValueError("向量包含 NaN/Inf")
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    value = dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
    if value > 1.0:
        value = 1.0
    elif value < -1.0:
        value = -1.0
    return round(value, SCORE_DECIMALS)


def mean(values: Optional[Sequence[Optional[float]]]) -> Optional[float]:
    if not values:
        return None
    usable = [float(item) for item in values if item is not None]
    if not usable:
        return None
    return round(sum(usable) / len(usable), SCORE_DECIMALS)


def meets_threshold(score: Optional[float], threshold: float) -> Optional[bool]:
    """``score >= threshold`` 即为达标（边界含等于）；``None`` 返回 ``None``。"""
    if score is None:
        return None
    return float(score) >= float(threshold) - EPSILON


def assign_one_to_one(scores: Sequence[Sequence[Optional[float]]]
                      ) -> List[Optional[int]]:
    """一对一词-区域匹配。

    输入 ``scores[word_index][box_index]``；返回每个词命中的框下标（无命中为 ``None``）。
    规则：按分数降序贪心，一个框最多被一个词占用；``None`` 不参与竞争。
    """
    if not scores:
        return []
    pairs = []
    for word_index, row in enumerate(scores):
        if not row:
            continue
        for box_index, score in enumerate(row):
            if score is None:
                continue
            pairs.append((float(score), word_index, box_index))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))

    result: List[Optional[int]] = [None] * len(scores)
    used_boxes: set = set()
    for _score, word_index, box_index in pairs:
        if result[word_index] is not None or box_index in used_boxes:
            continue
        result[word_index] = box_index
        used_boxes.add(box_index)
    return result


CALIBRATION_NONE = "none"
CALIBRATION_ZSCORE = "znorm"
CALIBRATION_PRIOR = "prior"
CALIBRATION_CHOICES = (CALIBRATION_NONE, CALIBRATION_ZSCORE, CALIBRATION_PRIOR)


def class_prior(scores: Sequence[Sequence[Optional[float]]],
                text_vectors: Sequence[Sequence[float]],
                winners: Sequence[Sequence[Optional[str]]],
                class_names: Sequence[Optional[str]],
                class_vectors: Dict[str, Sequence[float]],
                weight: float = 0.2):
    """类别名先验：``s + weight * cos(匹配文本, 框类别名)``，用于指派。

    ``class_vectors`` 是类别名（``person`` / ``surfboard`` …）经同一文本编码器得到
    的向量。词与它对应的目标类别天然更近（``surfboard`` vs ``person``），
    因此该先验能纠正「大而常见目标抢词」。落盘分数仍是原始余弦，阈值判定不变。
    """
    calibrated: List[List[Optional[float]]] = []
    for row_index, row in enumerate(scores):
        out_row: List[Optional[float]] = []
        for box_index, value in enumerate(row):
            if value is None:
                out_row.append(None)
                continue
            name = class_names[box_index] if box_index < len(class_names) else None
            vector = class_vectors.get(str(name)) if name else None
            if not vector:
                out_row.append(float(value))
                continue
            text = winners[row_index][box_index] if row_index < len(winners) else None
            text_vector = text_vectors.get(text) if text else None
            if text_vector is None:
                out_row.append(float(value))
                continue
            prior = cosine_similarity(text_vector, vector)
            out_row.append(float(value) + float(weight) * prior)
        calibrated.append(out_row)
    return calibrated, {"method": CALIBRATION_PRIOR, "weight": float(weight)}


def class_calibrate(scores: Sequence[Sequence[Optional[float]]],
                    class_names: Sequence[Optional[str]],
                    method: str = CALIBRATION_ZSCORE):
    """按类别标准化打分矩阵，返回 ``(calibrated, stats)``。

    ``scores`` 形状为 ``[词][框]``，``class_names[框]`` 是该框的类别名。
    ``znorm``：对每个类别 c 取该类所有框上的分数均值 μc 与标准差 σc，
    校准分 = ``(s - μc) / σc``（σc 为 0 时退化为 ``s - μc``）。

    校准分**只用于一词一框的指派**：它回答「这个词相对该类别的通常水平有多突出」，
    从而避免大而常见的目标（如 person）因为普遍高分抢走本应属于具体类别的词。
    落盘分数与 ``min_clip_score`` 判定仍使用原始余弦，阈值口径不变。
    """
    if method == CALIBRATION_NONE or not scores:
        return [list(row) for row in scores], {}
    if method != CALIBRATION_ZSCORE:
        raise ValueError("未知的类别校准方法：%r" % (method,))
    values: Dict[str, List[float]] = {}
    for box_index, name in enumerate(class_names):
        key = str(name) if name else ""
        bucket = values.setdefault(key, [])
        for row in scores:
            if box_index < len(row) and row[box_index] is not None:
                bucket.append(float(row[box_index]))
    stats: Dict[str, Dict[str, Any]] = {}
    for key, bucket in values.items():
        if not bucket:
            continue
        mu = sum(bucket) / len(bucket)
        var = sum((item - mu) ** 2 for item in bucket) / len(bucket)
        stats[key] = {"mean": round(mu, SCORE_DECIMALS),
                      "std": round(math.sqrt(var), SCORE_DECIMALS),
                      "scores": len(bucket)}
    calibrated: List[List[Optional[float]]] = []
    for row in scores:
        out_row: List[Optional[float]] = []
        for box_index, value in enumerate(row):
            if value is None:
                out_row.append(None)
                continue
            key = str(class_names[box_index]) if class_names[box_index] else ""
            stat = stats.get(key)
            if not stat:
                out_row.append(float(value))
                continue
            sigma = stat["std"]
            out_row.append((float(value) - stat["mean"]) / sigma if sigma > EPSILON
                           else float(value) - stat["mean"])
        calibrated.append(out_row)
    return calibrated, stats


def summarize(scores: Sequence[Optional[float]], threshold: float) -> Dict[str, Any]:
    """统计一组分数：关系数、预填数、最高分、是否存在达标项。"""
    usable = [float(item) for item in scores if item is not None]
    prefilled = sum(1 for item in usable if meets_threshold(item, threshold))
    return {"relations": len(scores),
            "prefilled": prefilled,
            "best_score": round_score(max(usable)) if usable else None,
            "any_above": bool(prefilled)}
