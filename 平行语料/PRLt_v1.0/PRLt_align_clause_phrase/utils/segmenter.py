"""子句切分：中文按标点/连词/配置词表切分，英文按标点/依存降级规则切分。

只做可解释的切分，不做翻译、不做语义判断；结果带 `start`/`end` 便于回溯父句原文，
`text == text[start:end]`。片段过短时并入相邻片段，单侧片段数受 `max_clauses` 限制，
避免对齐碎片爆炸。
"""
from typing import Dict, List, Sequence, Tuple

from utils.phrase_aligner import match_terms, unit_count

# 句末标点后允许跟随的收尾符号（引号、括号等），它们跟随上一段，不单独成段。
_CLOSING = "\"'\u201d\u2019)）]】」』》］"

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e",
    "no", "fig", "al", "inc", "ltd", "co", "u.s", "u.s.a", "a.m", "p.m", "approx",
    "dept", "est", "min", "max", "vol", "pp", "ed", "ca", "cf", "resp",
}


def clause_ref(parent_ref: str, index: int) -> str:
    """子句引用，形如 `para-003-sent-001-clause-002`。"""
    return f"{parent_ref}-clause-{index:03d}"


def _ends_with_abbreviation(buffer: str) -> bool:
    token = ""
    for ch in reversed(buffer):
        if ch.isalpha() or ch == ".":
            token = ch + token
        else:
            break
    token = token.lower().strip(".")
    if not token:
        return False
    if len(token) == 1:
        return True
    return token in _ABBREVIATIONS


def _english_boundary(text: str, pos: int) -> bool:
    """英文句点判定：小数点、缩写表命中、后接非空白（域名/网址）时不断段。"""
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


def _pass_punctuation(text: str, lang: str, stops: Sequence[str]) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    length = len(text)
    cursor = 0
    while cursor < length:
        while cursor < length and text[cursor].isspace():
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
                if lang == "zh" or _english_boundary(text, end):
                    cut = end + 1
                    while cut < length and (text[cut] in stops or text[cut] in _CLOSING):
                        cut += 1
                    break
            end += 1
        if cut == -1:
            cut = length
        spans.append((cursor, cut))
        cursor = cut if cut > cursor else cursor + 1
    return spans


def _find_terms(text: str, start: int, end: int, term: str) -> List[int]:
    """在 [start, end) 内查找词表命中位置；纯拉丁词条要求词边界，避免误切。"""
    latin = term.isascii() and term.replace(" ", "").isalpha()
    positions: List[int] = []
    index = text.find(term, start, end)
    while index != -1:
        if latin:
            before = text[index - 1] if index > 0 else ""
            after = text[index + len(term)] if index + len(term) < len(text) else ""
            if before.isalnum() or after.isalnum():
                index = text.find(term, index + 1, end)
                continue
        positions.append(index)
        index = text.find(term, index + len(term), end)
    return positions


def _pass_terms(text: str, start: int, end: int, boundaries: Sequence[str],
                min_units: int) -> List[Tuple[int, int]]:
    positions = set()
    for term in boundaries:
        if term:
            positions.update(_find_terms(text, start, end, term))
    if not positions:
        return [(start, end)]

    spans: List[Tuple[int, int]] = []
    cursor = start
    for position in sorted(positions):
        if position <= cursor or position >= end:
            continue
        if unit_count(text[cursor:position].strip()) < min_units:
            continue
        spans.append((cursor, position))
        cursor = position
    spans.append((cursor, end))
    return spans


def _merge_short(text: str, spans: List[Tuple[int, int]], min_units: int) -> List[Tuple[int, int]]:
    if not spans:
        return spans
    merged: List[List[int]] = [list(spans[0])]
    for start, end in spans[1:]:
        if unit_count(text[start:end].strip()) < min_units:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    if len(merged) > 1 and unit_count(text[merged[0][0]:merged[0][1]].strip()) < min_units:
        merged[1][0] = merged[0][0]
        merged.pop(0)
    return [(start, end) for start, end in merged]


def _cap(text: str, spans: List[Tuple[int, int]], max_clauses: int) -> List[Tuple[int, int]]:
    working: List[List[int]] = [list(span) for span in spans]
    while len(working) > max_clauses:
        best = None
        for index in range(len(working) - 1):
            combined = unit_count(text[working[index][0]:working[index + 1][1]].strip())
            if best is None or combined < best[0]:
                best = (combined, index)
        if best is None:
            break
        _, index = best
        working[index][1] = working[index + 1][1]
        working.pop(index + 1)
    return [(start, end) for start, end in working]


def split_clauses(text: str, lang: str = "zh", cfg=None) -> List[Dict]:
    """把父句文本切成子句片段：[{"index", "text", "start", "end"}]，index 从 1 开始。"""
    if not isinstance(text, str):
        raise TypeError("切分输入必须是字符串")
    if cfg is None:
        raise ValueError("split_clauses 需要算子配置")
    stops = list(cfg.zh_clause_punctuation if lang == "zh" else cfg.en_clause_punctuation)
    boundaries = list(cfg.zh_conjunctions if lang == "zh" else cfg.en_conjunctions)
    boundaries += list(cfg.split_terms)

    spans: List[Tuple[int, int]] = []
    for start, end in _pass_punctuation(text, lang, stops):
        spans.extend(_pass_terms(text, start, end, boundaries, cfg.min_clause_units))
    spans = _merge_short(text, spans, cfg.min_clause_units)
    spans = _cap(text, spans, cfg.max_clauses)

    units: List[Dict] = []
    for start, end in spans:
        raw = text[start:end]
        stripped = raw.strip()
        if not stripped:
            continue
        lead = len(raw) - len(raw.lstrip())
        trails = len(raw) - len(raw.rstrip())
        begin, stop = start + lead, end - trails
        units.append({"index": len(units) + 1, "text": text[begin:stop],
                      "start": begin, "end": stop})
    return units


def build_units(text: str, lang: str = "zh", cfg=None) -> List[Dict]:
    """在切分结果上补充 `source_terms` 与 `unit_type`（clause 或词典命中的 phrase）。"""
    units = split_clauses(text, lang, cfg)
    trims = "".join(cfg.zh_clause_punctuation) + "".join(cfg.en_clause_punctuation)
    for unit in units:
        terms = match_terms(unit["text"], cfg.terminology)
        unit["source_terms"] = terms
        stripped = unit["text"].strip().rstrip(trims)
        exact = any(term.lower() == stripped.lower() for term in terms)
        unit["unit_type"] = "phrase" if exact else "clause"
    return units

