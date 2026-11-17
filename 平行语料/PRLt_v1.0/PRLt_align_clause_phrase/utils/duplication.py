"""重复单位检测：同一父句内出现重复/包含关系的源或目标子句。

为什么需要
----------
子句对齐的置信度是「语义 × 长度 × 位置 × 父句」的加权和，四个分项都只看单个
子句对。当父句里同一段源文被重复了两遍（或译文把同一句贴了两次）时，每个子句对
单独看语义都很高，整体置信度照样 0.78–0.87 —— 但这份对齐在业务上是错的：
重复单位没有唯一的对应关系，位置信息也不再有意义。

口径
----
文字 shingle 包含度（containment）：|A ∩ B| / min(|A|, |B|)，A/B 为两侧单位各自的
shingle 集合。中文按相邻两字、英文按相邻两词取 shingle（与具体分词器无关）。
只用包含度而不要求完全相同，是为了覆盖「一句是另一句的前缀/扩展」这种构造
（defect-12-019 p8：第二句 = 新内容 + 第一句原文）。

实测（2026-11-05，Fullflow-Effect-Bench-v1 + 自然语料 399 个父句单元）
    - 自然语料：源侧最大包含度 0.67，目标侧 0.75，>= 0.80 的 0 个；
    - 构造缺陷：源侧重复的父句 9/9 达到 1.00，目标侧重复的 7/7 >= 0.83。
阈值 0.80 在自然语料上零命中，在构造缺陷上几乎全中。
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Sequence, Tuple

_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_WORD_RE = re.compile(r"[a-z0-9']+")


def shingles(text: str, lang: str = "zh") -> set:
    """返回相邻二元 shingle 集合；中文按字、英文按词。空白与标点不参与。"""
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    if lang == "zh":
        chars = [ch for ch in normalized if _CJK_RE.match(ch) or ch.isalnum()]
        tokens = ["".join(chars[i:i + 2]) for i in range(len(chars) - 1)]
    else:
        words = _WORD_RE.findall(normalized)
        tokens = [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    return set(tokens)


def containment(left: set, right: set) -> float:
    """包含度：交集占较短一侧的比例；任一侧为空返回 0。"""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def max_repetition(texts: Sequence[str], lang: str,
                   min_shingles: int = 3) -> Tuple[float, int, int]:
    """同一父句内两两比较，返回 (最大包含度, 左序号, 右序号)。

    少于两个单位时返回 (0.0, -1, -1)。

    ``min_shingles``：任一方的 shingle 数少于该值时跳过这一对。短单位（"I am"、
    "and light weapons."）天然是长单位的子串，不放这个下限会在自然语料上产生
    纯长度噪声造成的「重复」——实测自然语料 399 个父句里 2 条命中全部来自这种短单位。
    """
    sets: List[set] = [shingles(text, lang) for text in texts]
    best, left_index, right_index = 0.0, -1, -1
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            if len(sets[i]) < min_shingles or len(sets[j]) < min_shingles:
                continue
            score = containment(sets[i], sets[j])
            if score > best:
                best, left_index, right_index = score, i, j
    return best, left_index, right_index
