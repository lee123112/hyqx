"""描述语言识别与词数统计（纯规则，无模型、无外部依赖）。

表1 #09 只做规则型处理，但「语言标记」必须与实际内容一致：上游 #04 的
``text.lang`` 是**声明值**（部署配置默认 ``zh``），英文图注会被写成 ``zh``。
本模块按字符脚本给出保守判定，供 #09 在写描述时标注 ``description_lang``，
并纠正与内容明显矛盾的语言标记（保留原声明值的副本）。
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ["detect_language", "word_count", "MIN_DETECT_CHARS"]

#: 判定为某语言所需的最小「有效字符」数；低于该值返回 ``und``（不猜）。
MIN_DETECT_CHARS = 3

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힯ᄀ-ᇿ]")
_LATIN = re.compile(r"[A-Za-z]")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def detect_language(text: str) -> str:
    """按字符脚本返回 ``zh`` / ``ja`` / ``ko`` / ``en`` / ``und``。

    规则保守、可解释：
    - 出现假名 → ``ja``；出现谚文 → ``ko``；
    - 汉字占「有效字符」≥30% → ``zh``（中英混排仍判中文）；
    - 拉丁字母 ≥3 个且无汉字 → ``en``；
    - 其余（过短、纯数字/符号）→ ``und``，不强行猜测。
    """
    if not isinstance(text, str) or not text.strip():
        return "und"
    kana = len(_KANA.findall(text))
    hangul = len(_HANGUL.findall(text))
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN.findall(text))
    if kana:
        return "ja"
    if hangul:
        return "ko"
    effective = cjk + latin
    if effective < MIN_DETECT_CHARS:
        return "und"
    if cjk and cjk / effective >= 0.30:
        return "zh"
    if latin >= MIN_DETECT_CHARS:
        return "en"
    return "und"


def word_count(text: str) -> int:
    """词数：拉丁文本按单词计；含汉字的文本按「汉字 + 拉丁词」计。

    只用于「描述是否过短」的质量判断（不作分词结果写入下游字段）。
    """
    if not isinstance(text, str) or not text.strip():
        return 0
    latin_words = len(_WORD.findall(text))
    cjk_chars = len(_CJK.findall(text))
    if cjk_chars:
        return cjk_chars + latin_words
    return latin_words


def is_punctuation_only(text: str) -> bool:
    """整段只有标点/空白/符号（没有可用的语言字符）。"""
    if not isinstance(text, str):
        return True
    for ch in text:
        if ch.isspace():
            continue
        if unicodedata.category(ch)[0] in ("L", "N"):
            return False
    return True
