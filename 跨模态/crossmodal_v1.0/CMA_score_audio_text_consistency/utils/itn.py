"""中文逆文本归一化（ITN）：数字 / 百分比 / 数量单位表达 → 阿拉伯数字。

用途：音文一致性评分在字符级编辑距离比较前，把「三点八亿 / 百分之六点八 / 六月」
等中文数量表达归一为阿拉伯数字口径，消除「同一数值、两种写法」造成的 CER 虚高
（round9 隔离复核确认这类格式差异占音文隔离的相当比例，属 ASR/标注写法差异而非
语义不符）。

设计边界（防止过度归一）：
- 只处理数字表达，不改写普通文字：单字数字仅在明确数量语境（年/月/日/号/成 后缀
  或百分数结构）才转换，「一个人 / 十字路口 / 万历」等保持不变；
- 繁→简由 t2s 先行处理，本模块只接受简体输入；
- 只读字符串、不联网、不加载模型；转换失败的原样保留，绝不猜测。

转换计数（``to_arabic_numbers`` 返回的第二项）用于审计字段
``number_tokens_normalized``，只统计真正发生改写的数字词元个数。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple, Union

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}
_BIG_UNITS = {"万": 10 ** 4, "亿": 10 ** 8}
_POINT = "点"
_TOKEN_CHARS = frozenset(list(_DIGITS) + list(_UNITS) + list(_BIG_UNITS) + [_POINT])

# 单字数字只在明确数量后缀前转换（六月→6月；但「一个人」「十字路口」不动）
_SINGLE_DIGIT_SUFFIX = frozenset("年月日号成")
_SINGLE_TEN_SUFFIX = frozenset("年月日号成百千万亿")

# 百分数结构：百分之六点八 / 千分之五 / 万分之二点五
_PERCENT_RE = re.compile(
    r"(百分之|千分之|万分之)([%s]+)" % "".join(sorted(_TOKEN_CHARS)))

Number = Union[int, float]


def _parse_section(text: str) -> Optional[int]:
    """解析「十/百/千」以内的组合（如 三百二十五 / 十五 / 二十）。"""
    total = 0
    num: Optional[int] = None
    for char in text:
        if char in _DIGITS:
            num = _DIGITS[char]
        elif char in _UNITS:
            total += (num if num is not None else 1) * _UNITS[char]
            num = None
        else:
            return None
    return total + (num or 0)


def _parse_integer(text: str) -> Optional[int]:
    """解析含 万/亿 分段的中文整数；无单位的多字串按逐位数字拼接（一一五→115）。"""
    if not text:
        return None
    if not any(char in _UNITS or char in _BIG_UNITS for char in text):
        if all(char in _DIGITS for char in text):
            return int("".join(str(_DIGITS[char]) for char in text))
        return None
    total = 0
    current = ""
    for char in text:
        if char in _BIG_UNITS:
            value = _parse_section(current) if current else 1
            if value is None:
                return None
            total += value * _BIG_UNITS[char]
            current = ""
        else:
            current += char
    if current:
        tail = _parse_section(current)
        if tail is None:
            return None
        total += tail
    return total


def parse_number(text: str) -> Optional[Number]:
    """解析中文数字串为 int/float；无法解析返回 None（调用方原样保留）。"""
    if not text or any(char not in _TOKEN_CHARS for char in text):
        return None
    if _POINT in text:
        if text.count(_POINT) != 1:
            return None
        int_part, frac_part = text.split(_POINT, 1)
        if not frac_part or any(char not in _DIGITS for char in frac_part):
            return None
        integer = _parse_integer(int_part) if int_part else 0
        if integer is None:
            return None
        frac = "".join(str(_DIGITS[char]) for char in frac_part)
        return float("%d.%s" % (integer, frac))
    return _parse_integer(text)


def format_number(value: Number) -> str:
    """把解析出的数字格式化为最简阿拉伯数字串（3.800→3.8，30000→30000）。"""
    if isinstance(value, float):
        text = ("%f" % value).rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _replace_percent(match: re.Match) -> str:
    scale = {"百分之": 1.0, "千分之": 0.1, "万分之": 0.01}[match.group(1)]
    value = parse_number(match.group(2))
    if value is None:
        return match.group(0)
    return format_number(value * scale) + "%"


def to_arabic_numbers(text: object) -> Tuple[str, int]:
    """把文本中的中文数字表达转为阿拉伯数字，返回 (新文本, 转换词元数)。

    任何无法确定的片段都原样保留；转换计数只统计真正发生改写的词元。
    """
    if text is None:
        return "", 0
    source = str(text)
    converted = 0

    def _counting_percent(match: re.Match) -> str:
        nonlocal converted
        replaced = _replace_percent(match)
        if replaced != match.group(0):
            converted += 1
        return replaced

    value = _PERCENT_RE.sub(_counting_percent, source)

    out = []
    index = 0
    length = len(value)
    def _convert_run(run: str, following: str) -> Optional[str]:
        if len(run) == 1:
            char = run
            if char in _DIGITS and following in _SINGLE_DIGIT_SUFFIX:
                return str(_DIGITS[char])
            if char == "十" and following in _SINGLE_TEN_SUFFIX:
                return "10"
            return None
        parsed = parse_number(run)
        if parsed is not None:
            return format_number(parsed)
        # 小数+数量单位同词（三点八亿 / 一点一一五亿）：单位尾部不参与小数解析
        if _POINT in run:
            for cut in range(len(run) - 1, 0, -1):
                head, tail = run[:cut], run[cut:]
                if not tail or any(c not in _UNITS and c not in _BIG_UNITS for c in tail):
                    continue
                if not head or head[-1] not in _DIGITS:
                    continue
                parsed_head = parse_number(head)
                if parsed_head is not None:
                    return format_number(parsed_head) + tail
        return None

    while index < length:
        char = value[index]
        if char not in _TOKEN_CHARS:
            out.append(char)
            index += 1
            continue
        end = index + 1
        while end < length and value[end] in _TOKEN_CHARS:
            end += 1
        run = value[index:end]
        following = value[end] if end < length else ""
        replacement = _convert_run(run, following)
        if replacement is None:
            out.append(run)
        else:
            out.append(replacement)
            converted += 1
        index = end
    return "".join(out), converted
