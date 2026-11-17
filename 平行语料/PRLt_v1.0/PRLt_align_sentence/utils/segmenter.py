"""句子切分：中文终止符规则 + 英文句末标点/空格/大写规则，保留段落内起止索引。

只做切分，不做翻译、不做语义判断；切分结果带 `start`/`end` 便于回溯到原段落，
不会把 `e.g.`、`i.e.`、`Mr.` 等缩写和小数点误判为句末。
"""
from typing import Dict, List, Optional

# 句末标点后面允许跟随的收尾符号（引号、括号等），它们跟随上一句，不单独成句。
_CLOSING = "\"'\u201d\u2019\u3002)\u3001]}\uff09\u300b\u3011\u300d\u300f"

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
    "no", "fig", "al", "inc", "ltd", "co", "u.s", "u.s.a", "a.m", "p.m", "approx",
    "dept", "est", "min", "max", "vol", "pp", "ed", "ca", "cf", "resp",
}


def _ends_with_abbreviation(buffer: str) -> bool:
    """判断句点前的 token 是否为缩写/首字母（如 `e.g.`、`Mr.`、`A.`）。"""
    token = ""
    for ch in reversed(buffer):
        if ch.isalpha() or ch == ".":
            token = ch + token
        else:
            break
    token = token.lower().strip(".")
    if not token:
        return False
    if len(token) == 1:          # 单字母首字母缩写
        return True
    return token in _ABBREVIATIONS


def _is_english_boundary(text: str, pos: int) -> bool:
    """英文句点判定：小数点、缩写表命中、后接非空白（域名/网址）时不断句。

    刻意不采用"后接小写字母就不算句末"的保守口径：该口径会把
    `... tax. then he left.` 这类合法句末误判为句内，损失真实断句。
    """
    if text[pos] != ".":
        return True
    prev_ch = text[pos - 1] if pos > 0 else ""
    next_ch = text[pos + 1] if pos + 1 < len(text) else ""
    if prev_ch.isdigit() and next_ch.isdigit():
        return False
    if _ends_with_abbreviation(text[:pos]):
        return False
    if not next_ch:
        return True
    return bool(next_ch.isspace() or next_ch in _CLOSING)


def split_sentences(text: str, lang: str = "zh",
                    terminators: Optional[List[str]] = None) -> List[Dict]:
    """把一段文本切成句子列表：[{"index", "text", "start", "end"}]，index 从 1 开始。

    `start`/`end` 是原段落文本的字符偏移（左闭右开），`text == text[start:end]`。
    """
    if not isinstance(text, str):
        raise TypeError("切分输入必须是字符串")
    if lang == "zh":
        stops = set(terminators or ["\u3002", "\uff01", "\uff1f", "\uff1b", "\u2026", "!", "?"])
    else:
        stops = set(terminators or [".", "!", "?", "\u2026"])

    sentences: List[Dict] = []
    length = len(text)
    cursor = 0

    while cursor < length:
        while cursor < length and (text[cursor].isspace() or text[cursor] == "\n"):
            cursor += 1
        if cursor >= length:
            break

        end = cursor
        cut = -1
        while end < length:
            ch = text[end]
            if ch == "\n":
                cut = end
                break
            if ch in stops:
                if lang == "zh" or _is_english_boundary(text, end):
                    cut = end + 1
                    # 吸收连续的终止符和收尾符号，如 “真的吗？！” 或 "Yes!)" 中的收尾
                    while cut < length and (text[cut] in stops or text[cut] in _CLOSING):
                        if text[cut] in _CLOSING and cut + 1 < length \
                                and (text[cut + 1] in stops or text[cut + 1] in _CLOSING):
                            cut += 1
                            continue
                        if text[cut] in stops:
                            cut += 1
                            continue
                        cut += 1
                    break
            end += 1
        if cut == -1:
            cut = length

        raw = text[cursor:cut]
        stripped = raw.strip()
        if stripped:
            lead = len(raw) - len(raw.lstrip())
            trails = len(raw) - len(raw.rstrip())
            start = cursor + lead
            stop = cut - trails
            sentences.append({"index": len(sentences) + 1, "text": text[start:stop],
                              "start": start, "end": stop})
        cursor = cut if cut > cursor else cursor + 1

    return sentences


def sentence_ref(paragraph_ref: str, index: int) -> str:
    """句子引用，形如 `para-003-sent-001`（与根 DATA_SCHEMA 一致）。"""
    return f"{paragraph_ref}-sent-{index:03d}"


