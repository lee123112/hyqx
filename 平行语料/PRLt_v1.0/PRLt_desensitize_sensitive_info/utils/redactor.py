"""算子8 规则引擎：规则编译、命中收集、重叠消解、序号分配与占位符替换。

安全约束：本模块只在内存中持有原始敏感值用于替换与比对；
任何返回值一旦离开本模块用于落盘/日志/血缘，必须是「类型 + 数量 + 占位符」，
不得携带原始值、规范化值或值→序号映射表。
"""
import re
from collections import namedtuple
from typing import Dict, Iterable, List, Sequence, Tuple

from utils.schemas import OperatorConfig, municipality_regions

CompiledRule = namedtuple("CompiledRule", "name type pattern group priority")

Hit = Dict[str, object]

_WS_RE = re.compile(r"\s+")
_SEP_RE = re.compile(r"[\s\-]")


def _alternation(items: Iterable[str]) -> str:
    """生成安全的长词优先交替组；空集合退化为永不匹配。"""
    ordered = sorted({item for item in items if item}, key=lambda value: (-len(value), value))
    if not ordered:
        return r"(?!)"
    return "(?:" + "|".join(re.escape(item) for item in ordered) + ")"


def expand_pattern(pattern: str, cfg: OperatorConfig) -> str:
    """把 `{provinces}` / `{municipalities}` 展开为地址锚点词表。"""
    if "{provinces}" not in pattern and "{municipalities}" not in pattern:
        return pattern
    municipalities = set(municipality_regions())
    regions = cfg.address_regions
    return (pattern
            .replace("{municipalities}", _alternation(r for r in regions if r in municipalities))
            .replace("{provinces}", _alternation(r for r in regions if r not in municipalities)))


def compile_rules(cfg: OperatorConfig) -> List[CompiledRule]:
    """编译启用规则；正则非法直接抛 ValueError（上层转 `failed`）。"""
    compiled: List[CompiledRule] = []
    for rule in list(cfg.rules) + list(cfg.extra_rules):
        if not rule.enabled:
            continue
        pattern = expand_pattern(rule.pattern, cfg)
        try:
            regex = re.compile(pattern)
        except re.error as exc:  # 兜底：配置层已校验，此处防御动态替换后的失败
            raise ValueError(f"规则 {rule.name} 正则编译失败：{exc}") from exc
        compiled.append(CompiledRule(rule.name, rule.type, regex, rule.group, rule.priority))
    return compiled


def normalize_value(kind: str, raw: str) -> str:
    """按类型规范化命中值，只用于同值判定，不落盘、不写日志。"""
    value = raw.strip()
    if kind == "email":
        return value.lower()
    if kind == "phone":
        digits = re.sub(r"\D", "", value)
        if len(digits) > 11 and digits.startswith("86"):
            digits = digits[2:]
        return digits
    if kind == "id_card":
        return _SEP_RE.sub("", value).upper()
    if kind == "account":
        return _SEP_RE.sub("", value)
    if kind == "address":
        return _WS_RE.sub("", value)
    if kind == "name":
        return _WS_RE.sub("", value)
    return _WS_RE.sub(" ", value).strip()


def find_hits(text: str, compiled: Sequence[CompiledRule]) -> List[Hit]:
    """收集一条文本上的全部命中并按规则处理重叠。

    排序口径：`priority` 升序 → 匹配更长者优先 → 起点靠前优先；
    被已接受区间覆盖的候选直接丢弃，保证同一字符只被替换一次。
    """
    candidates: List[Hit] = []
    for rule in compiled:
        for match in rule.pattern.finditer(text):
            if rule.group:
                span = match.span(rule.group)
                if span[0] < 0:
                    continue
                raw = match.group(rule.group)
            else:
                span = match.span()
                raw = match.group(0)
            if span[1] <= span[0]:
                continue
            candidates.append({
                "start": span[0],
                "end": span[1],
                "type": rule.type,
                "rule": rule.name,
                "priority": rule.priority,
                "raw": raw,
            })

    candidates.sort(key=lambda hit: (hit["priority"], -(hit["end"] - hit["start"]), hit["start"]))
    accepted: List[Hit] = []
    for hit in candidates:
        if any(hit["start"] < item["end"] and item["start"] < hit["end"] for item in accepted):
            continue
        accepted.append(hit)

    for hit in accepted:
        hit["normalized"] = normalize_value(str(hit["type"]), str(hit["raw"]))
    accepted.sort(key=lambda hit: hit["start"])
    return accepted


def build_value_index(all_fields: Sequence[Tuple[str, str]],
                      plan: Dict[str, List[Hit]]) -> Dict[str, Dict[str, int]]:
    """按字段顺序为「类型 + 规范化值」分配共享序号。

    同一记录内同类同值共享序号，因此源/目标可确认的对应敏感片段会得到同一 token。
    """
    index: Dict[str, Dict[str, int]] = {}
    for name, _text in all_fields:
        for hit in plan.get(name, []):
            bucket = index.setdefault(str(hit["type"]), {})
            key = str(hit["normalized"])
            if key not in bucket:
                bucket[key] = len(bucket) + 1
    return index


def apply_field(text: str, hits: Sequence[Hit], index: Dict[str, Dict[str, int]],
                template: str) -> str:
    """把命中区间替换为占位符；hits 已保证互不重叠且按起点升序。"""
    if not hits:
        return text
    parts: List[str] = []
    cursor = 0
    for hit in hits:
        parts.append(text[cursor:hit["start"]])
        parts.append(template.format(
            type=str(hit["type"]).upper(),
            index=index[str(hit["type"])][str(hit["normalized"])],
        ))
        cursor = hit["end"]
    parts.append(text[cursor:])
    return "".join(parts)


def _counter(values: Iterable) -> Dict:
    counts: Dict = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def sync_signature(hits: Sequence[Hit], mode: str) -> Dict:
    """源/目标同步比对签名：`type_counts` 只比类型计数，其余比「类型+规范化值」多重集。"""
    if mode == "type_counts":
        return _counter(str(hit["type"]) for hit in hits)
    return _counter((str(hit["type"]), str(hit["normalized"])) for hit in hits)
