"""语言方向归一化：把 zh-en / zh_en / zh->en / zh→en / zh-cn-en-us 统一成 `zh-en`。

本模块只做**声明语言**的写法归一，不做真实语言检测（真实检测属算子 6 职责）。
"""
from typing import List, Optional

_BASE_LANGS = {
    "zh", "en", "ja", "ko", "fr", "de", "es", "ru", "ar", "pt", "it", "th", "vi",
    "id", "ms", "hi", "tr", "nl", "pl", "sv", "fi", "da", "no", "el", "cs", "uk",
    "he", "fa", "bn", "ta", "ur", "my", "km", "lo", "ne", "si", "mn", "kk", "uz",
}
_LANG_ALIASES = {
    "zh-cn": "zh", "zh-tw": "zh", "zh-hk": "zh", "zh-sg": "zh",
    "zh-hans": "zh", "zh-hant": "zh",
    "en-us": "en", "en-gb": "en", "en-au": "en", "en-ca": "en", "en-in": "en",
}
_PAIR_SEPARATORS = ("→", "=>", "->", "|", "/", ":", "_", "~", " ")


def _normalize_side(tokens: List[str]) -> Optional[str]:
    if not tokens:
        return None
    full = "-".join(tokens)
    if full in _LANG_ALIASES:
        return _LANG_ALIASES[full]
    return tokens[0]


def normalize_language_pair(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    for separator in _PAIR_SEPARATORS:
        text = text.replace(separator, "-")
    tokens = [token for token in text.split("-") if token]
    if not tokens:
        return None

    sides: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if current and token in _BASE_LANGS and current[0] in _BASE_LANGS:
            sides.append(current)
            current = [token]
        else:
            current.append(token)
    if current:
        sides.append(current)

    if len(sides) != 2:
        return None
    left = _normalize_side(sides[0])
    right = _normalize_side(sides[1])
    if not left or not right or left == right:
        return None
    return f"{left}-{right}"
