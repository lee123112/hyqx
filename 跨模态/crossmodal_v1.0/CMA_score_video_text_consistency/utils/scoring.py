"""纯算法：余弦相似度、均值与「帧分 → 段分 → 视频分 → 通过结论」的聚合判定。

判定口径（表1 #14，2026-10-23 修正）：
- 段分（``segment_mean``）= 该段**被选中帧**的 CLIP 余弦均值（0 帧段为 ``None``）；
- ``video_text_score``（视文分数）= **所有被选中帧**的余弦均值（不再取段均值的均值），
  同时保留 ``segment_mean`` 别名便于 #19/#16 直接读取；
- ``min_segment_score`` = 最低段分（只统计有证据的段）；
- ``passed`` 要求**所有有证据的段分** ``>= low_score_threshold``（配置默认 0.23），
  任一段低于阈值即整段不通过；0 帧段不参与判定（记无证据）。

本文件不依赖 torch/numpy，纯 Python 实现，行为确定、可完整单测。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: 分数保留位数（避免浮点尾差导致同一输入产生不同输出）
SCORE_PRECISION = 6

#: 阈值比较容差：抵消 float 表示误差，保证「恰好等于阈值」判定为通过
EPSILON = 1e-9


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度；结果裁剪到 ``[-1, 1]``。维度不一致抛 ``ValueError``。

    任一向量为零向量时返回 ``0.0``（可解释、不中断整批）。
    """
    if len(left) != len(right):
        raise ValueError("余弦相似度要求向量维度一致：%d != %d" % (len(left), len(right)))
    if not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    value = dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
    return max(-1.0, min(1.0, value))


def mean(values: Optional[Iterable[float]]) -> Optional[float]:
    """算术平均；空或 ``None`` 返回 ``None``（不使用 0 代替缺失）。"""
    if values is None:
        return None
    items = [float(value) for value in values if value is not None]
    if not items:
        return None
    return round(sum(items) / len(items), SCORE_PRECISION)


def segment_passed(segment_mean: Optional[float], threshold: float) -> Optional[bool]:
    """段均值是否不低于阈值（含边界）；段均值为 ``None`` 时无法判定。"""
    if segment_mean is None:
        return None
    return float(segment_mean) + EPSILON >= float(threshold)


def aggregate(segment_means: Sequence[Optional[float]],
              threshold: float,
              frame_scores: Optional[Sequence[Optional[float]]] = None) -> Dict[str, Any]:
    """段分 + 逐帧分数 → 视频分 / 最低段分 / 通过结论 / 失败段索引。

    - 传入 ``frame_scores``（业务路径必传）：``video_text_score`` =
      **所有被选中帧**余弦分值的均值（表1 #14 现行口径）；
    - 未传 ``frame_scores``：回退为段均值的均值，仅用于纯段均值聚合的兼容调用，
      不代表业务判定口径；
    - ``passed``：所有**有证据**的段分都不低于阈值；无任何段分时为 ``None``；
    - 0 帧段（段分为 ``None``）不参与判定，也不计入 ``failing_segments``。
    """
    means: List[float] = [float(value) for value in segment_means if value is not None]
    if not means:
        return {
            "passed": None,
            "video_text_score": None,
            "segment_mean": None,
            "min_segment_score": None,
            "segment_count": 0,
            "failing_segments": [],
        }
    failing = [index for index, value in enumerate(segment_means)
               if value is not None and not segment_passed(value, threshold)]
    frame_values = [float(value) for value in (frame_scores or []) if value is not None]
    overall = mean(frame_values) if frame_values else mean(means)
    return {
        "passed": not failing,
        "video_text_score": overall,
        "segment_mean": overall,
        "min_segment_score": round(min(means), SCORE_PRECISION),
        "segment_count": len(means),
        "failing_segments": failing,
    }
