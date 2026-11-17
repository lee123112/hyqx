"""父句级结构观测：重复单位（包含度）与跨语言长度比。

为什么需要在算子12 再做一次
--------------------------
融合分 = 子句对齐 / 句子对齐 / 段落对齐三个粒度分的加权和，每一项都只看得见
「这一对子句」。父句内部的结构问题（同一段源文被对齐两遍、译文把同一句贴两次）
和跨语言长度失衡（中文按字、英文按词，比值显著偏离语料常态）在单对子上看不出来。

口径
----
- 包含度：|A ∩ B| / min(|A|, |B|)，A/B 为同一父句内两条子句的 shingle 集合
  （中文相邻两字、英文相邻两词）。用于判「重复/包含」。
- 长度比：父句源侧计量单元数 / 目标侧计量单元数，中文按字、英文按词。

实测（2026-11-05）
    - 自然语料 399 个父句：长度比 0.88–4.08，包含度源侧 <= 0.67、目标侧 <= 0.75；
    - 构造缺陷 length_ratio 15 条：长度比 0.50–1.41（7 条 < 0.88），包含度 7 条 >= 0.83。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Sequence, Tuple

_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")


def shingles(text: str, lang: str = "zh") -> set:
    """相邻二元 shingle 集合；空白与标点不参与。"""
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    if lang == "zh":
        chars = [ch for ch in normalized if _CJK_RE.match(ch) or ch.isalnum()]
        return {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)}
    words = _WORD_RE.findall(normalized)
    return {" ".join(words[i:i + 2]) for i in range(len(words) - 1)}


def containment(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def max_repetition(texts: Sequence[str], lang: str,
                   min_shingles: int = 3) -> Tuple[float, int, int]:
    """同一父句内两两比较，返回 (最大包含度, 左序号, 右序号)；不足两个返回全零。

    任一方的 shingle 数少于 ``min_shingles`` 时跳过这一对：短单位天然是长单位的
    子串，不设下限会把纯长度噪声当成重复。
    """
    sets = [shingles(text, lang) for text in texts]
    best, left_index, right_index = 0.0, -1, -1
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            if len(sets[i]) < min_shingles or len(sets[j]) < min_shingles:
                continue
            score = containment(sets[i], sets[j])
            if score > best:
                best, left_index, right_index = score, i, j
    return best, left_index, right_index


def count_units(text: str, lang: str | None = None) -> int:
    """计量单元数：汉字按字、拉丁词按词，两者相加。

    中英混排（术语、人名、缩写）在两侧都按同一口径计数，比值才可比；
    `lang` 参数保留给调用方表意，不改变计数方式。
    """
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return len(_CJK_RE.findall(normalized)) + len(_WORD_RE.findall(normalized))
