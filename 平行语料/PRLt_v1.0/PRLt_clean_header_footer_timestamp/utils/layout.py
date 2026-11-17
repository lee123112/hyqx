"""算子5 版式清理纯函数：行切分、区域判定、整行/行内匹配、跨记录重复行扫描。

全部为可手工复算的确定性逻辑，不引入任何模型或随机数。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Pattern, Tuple

from utils.io import iter_decoded_lines
from utils.schemas import OperatorConfig

LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")
INNER_WS_RE = re.compile(r"[ \t]{2,}")
WS_RE = re.compile(r"\s+")

REGION_HEAD = "head"
REGION_BODY = "body"
REGION_TAIL = "tail"


def split_lines(text: str) -> List[str]:
    """按逻辑行切分字段，兼容 \\r\\n、\\r 与 \\n，保持行内容不变。"""
    return LINE_SPLIT_RE.split(text)


def normalize_key(line: str) -> str:
    """重复行比较用的规范化键：折叠空白并裁剪首尾（不改变输出原文）。"""
    return WS_RE.sub(" ", line).strip()


def region_of(index: int, total: int, head_lines: int, tail_lines: int) -> str:
    """判断某个逻辑行属于记录首部、正文还是尾部区域。"""
    if index < head_lines:
        return REGION_HEAD
    if index >= total - tail_lines:
        return REGION_TAIL
    return REGION_BODY


def _compile(specs) -> List[Tuple[str, Pattern[str], bool]]:
    return [(spec.name, re.compile(spec.pattern), spec.boundary_only) for spec in specs]


def compile_patterns(cfg: OperatorConfig) -> Dict[str, List[Tuple[str, Pattern[str], bool]]]:
    """把配置中的模式预编译成 (name, regex, boundary_only)。"""
    return {
        "timestamp": _compile(cfg.timestamp_patterns),
        "page_number": _compile(cfg.page_number_patterns),
        "header_footer": _compile(cfg.header_footer_patterns),
        "inline_timestamp": _compile(cfg.inline_timestamp_patterns),
        "inline_page": _compile(cfg.inline_page_patterns),
    }


def match_whole_line(stripped: str, specs, kind: str):
    """整行匹配（fullmatch）：返回 (模式名, 类别, 是否仅限首尾区域)。"""
    for name, regex, boundary_only in specs:
        if regex.fullmatch(stripped):
            return name, kind, boundary_only
    return None


def strip_inline(text: str, field: str, line_index: int, region: str, specs, kind: str,
                 evidence: List[dict]) -> Tuple[str, int]:
    """删除行内命中片段，返回 (新行, 删除字符数)，并把证据写入 evidence。"""
    removed_total = 0
    for name, regex, _ in specs:
        replaced, count = regex.subn("", text)
        if not count:
            continue
        removed_chars = len(text) - len(replaced)
        evidence.append({
            "field": field,
            "kind": kind,
            "pattern": name,
            "scope": "inline",
            "line_index": line_index,
            "region": region,
            "hits": count,
            "removed_chars": removed_chars,
        })
        removed_total += removed_chars
        text = replaced
    return text, removed_total


def scan_repeated_lines(input_file: str, encoding: str, cfg: OperatorConfig) -> dict:
    """第一遍流式扫描：统计首尾区域内各行在多少条记录中出现。

    只统计首/尾区域的行，既能命中页眉页脚，又把内存限定在可控范围；
    超过 `max_tracked_lines` 时停止新增键并标记 truncated，绝不静默截断。
    """
    counter: Dict[Tuple[str, str], int] = {}
    scanned = 0
    truncated = False

    for line in iter_decoded_lines(input_file, encoding):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        scanned += 1
        for field in cfg.text_fields:
            value = record.get(field)
            if not isinstance(value, str) or not value:
                continue
            lines = split_lines(value)
            total = len(lines)
            seen_in_record = set()
            for index in range(total):
                if region_of(index, total, cfg.head_lines, cfg.tail_lines) == REGION_BODY:
                    continue
                key = normalize_key(lines[index])
                if not key or not (cfg.min_repeat_line_chars <= len(key) <= cfg.max_repeat_line_chars):
                    continue
                pair = (field, key)
                if pair in seen_in_record:
                    continue
                seen_in_record.add(pair)
                if pair not in counter and len(counter) >= cfg.max_tracked_lines:
                    truncated = True
                    continue
                counter[pair] = counter.get(pair, 0) + 1

    repeated: Dict[Tuple[str, str], int] = {}
    if scanned:
        for pair, count in counter.items():
            if count >= cfg.min_repeat_count and (count / scanned) >= cfg.min_repeat_ratio:
                repeated[pair] = count

    return {
        "scanned": scanned,
        "keys": repeated,
        "counts": counter,
        "candidates": len(counter),
        "truncated": truncated,
    }
