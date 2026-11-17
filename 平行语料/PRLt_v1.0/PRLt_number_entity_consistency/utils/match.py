"""类别化匹配与证据生成。

匹配口径（对应 TECHNICAL_DESIGN §2）：

1. 先按「类别 + 归一值」做多重集精确匹配；
2. 对 number / percentage / unit 再按容差做近似匹配（单位要求同一量纲）；
3. 两侧都剩余 -> `value_mismatch`；源侧多出 -> `missing_in_target`；目标侧多出 -> `extra_in_target`；
4. 畸形片段（`1.2.3`、`2024-13-45`）计入 unparsed，不参与一致/冲突判定，但会让整条记录升级为
   `waiting_manual`（无法可靠判定）；
5. 实体类只输出哈希与长度，数值类只输出归一值，任何情况下都不把原文写进证据。
"""
from typing import Dict, List, Tuple

from .extract import CATEGORY_ORDER, extract_tokens

_TOLERANT_CATEGORIES = frozenset({"number", "percentage", "unit"})
_TOLERANCE_EPSILON = 1e-12


def zero_category() -> Dict[str, int]:
    return {
        "source": 0, "target": 0, "matched": 0,
        "missing_in_target": 0, "extra_in_target": 0,
        "value_mismatch": 0, "unparsed": 0,
    }


def within_tolerance(left: float, right: float, cfg) -> bool:
    """容差判据：|a-b| <= abs_tol + rel_tol*max(|a|,|b|)，并吸收浮点表示误差。"""
    diff = abs(left - right)
    if diff == 0:
        return True
    allowed = cfg.numeric_absolute_tolerance + cfg.numeric_relative_tolerance * max(abs(left), abs(right))
    return diff <= allowed + _TOLERANCE_EPSILON * max(1.0, abs(left), abs(right))


def _group(tokens) -> Dict[str, List]:
    grouped: Dict[str, List] = {}
    for token in tokens:
        grouped.setdefault(token.category, []).append(token)
    for items in grouped.values():
        items.sort(key=lambda token: token.span[0])
    return grouped


def _clip(text: str, limit: int) -> Tuple[str, bool]:
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _payload(entry: Dict, prefix: str, token) -> None:
    entry[f"{prefix}_span"] = list(token.span)
    if token.category == "entity":
        # 实体类：只留哈希与长度摘要，绝不输出原文
        entry[f"{prefix}_hash"] = token.digest
        entry[f"{prefix}_length"] = token.length
        entry[f"{prefix}_type"] = token.subtype
        entry["entity_type"] = token.subtype
        return
    entry[prefix] = token.normalized
    if token.subtype:
        if token.category == "unit":
            entry["dimension"] = token.subtype
        elif token.category == "currency":
            entry["currency"] = token.subtype


def _conflict(kind: str, source_token, target_token) -> Dict:
    anchor = source_token if source_token is not None else target_token
    entry = {"category": anchor.category, "type": kind}
    if source_token is not None:
        _payload(entry, "source", source_token)
    if target_token is not None:
        _payload(entry, "target", target_token)
    return entry


def _match_category(source_tokens, target_tokens, tolnerate: bool, cfg) -> Dict:
    source_left = list(source_tokens)
    target_left = list(target_tokens)
    matched = 0

    # 1) 归一值精确匹配
    for source_token in list(source_left):
        for target_token in list(target_left):
            if source_token.normalized == target_token.normalized:
                source_left.remove(source_token)
                target_left.remove(target_token)
                matched += 1
                break

    # 2) 容差近似匹配（最近邻优先，单位要求同一量纲）
    if tolnerate:
        for source_token in list(source_left):
            best = None
            best_diff = None
            for target_token in target_left:
                if source_token.numeric_value is None or target_token.numeric_value is None:
                    continue
                if source_token.category == "unit" and source_token.subtype != target_token.subtype:
                    continue
                if not within_tolerance(source_token.numeric_value, target_token.numeric_value, cfg):
                    continue
                diff = abs(source_token.numeric_value - target_token.numeric_value)
                if best is None or diff < best_diff:
                    best, best_diff = target_token, diff
            if best is not None:
                source_left.remove(source_token)
                target_left.remove(best)
                matched += 1

    # 3) 剩余按位置配对成 value_mismatch，再计漏写/多写
    conflicts: List[Dict] = []
    reasons = set()
    paired = min(len(source_left), len(target_left))
    for index in range(paired):
        conflicts.append(_conflict("value_mismatch", source_left[index], target_left[index]))
        reasons.add("value_mismatch")
    for token in source_left[paired:]:
        conflicts.append(_conflict("missing_in_target", token, None))
        reasons.add("missing_in_target")
    for token in target_left[paired:]:
        conflicts.append(_conflict("extra_in_target", None, token))
        reasons.add("extra_in_target")

    return {
        "matched": matched,
        "value_mismatch": paired,
        "missing_in_target": len(source_left) - paired,
        "extra_in_target": len(target_left) - paired,
        "conflicts": conflicts,
        "reasons": reasons,
    }


def analyze_pair(source_text: str, target_text: str, ctx) -> Dict:
    """抽取两侧 token 并生成 evidence_core（不含 backend / model 层）。"""
    cfg = ctx.cfg
    if not isinstance(source_text, str) or not isinstance(target_text, str):
        raise TypeError("待检测字段必须是字符串")

    source_scan, source_clipped = _clip(source_text, cfg.max_text_scan_chars)
    target_scan, target_clipped = _clip(target_text, cfg.max_text_scan_chars)

    source_tokens, source_unparsed = extract_tokens(source_scan, ctx)
    target_tokens, target_unparsed = extract_tokens(target_scan, ctx)

    enabled = list(cfg.normalized_categories())
    category_counts = {name: zero_category() for name in enabled}
    totals = {
        "source": 0, "target": 0, "matched": 0,
        "missing_in_target": 0, "extra_in_target": 0,
        "value_mismatch": 0, "unparsed": 0,
    }
    conflicts: List[Dict] = []
    conflict_reasons: List[str] = []

    grouped_source = _group(source_tokens)
    grouped_target = _group(target_tokens)

    for category in enabled:
        stats = category_counts[category]
        result = _match_category(
            grouped_source.get(category, []),
            grouped_target.get(category, []),
            category in _TOLERANT_CATEGORIES,
            cfg,
        )
        stats["source"] = len(grouped_source.get(category, []))
        stats["target"] = len(grouped_target.get(category, []))
        stats["matched"] = result["matched"]
        stats["missing_in_target"] = result["missing_in_target"]
        stats["extra_in_target"] = result["extra_in_target"]
        stats["value_mismatch"] = result["value_mismatch"]
        conflicts.extend(result["conflicts"])
        for reason in sorted(result["reasons"]):
            code = f"{reason}:{category}"
            if code not in conflict_reasons:
                conflict_reasons.append(code)

    # 无法解析片段（只输出哈希/长度/位置；按类别做启用过滤后再计数）
    unparsed_entries: List[Dict] = []
    for side, items, field in (("source", source_unparsed, cfg.text_fields[0]),
                               ("target", target_unparsed, cfg.text_fields[1])):
        for item in items:
            unparsed_entries.append({
                "side": side,
                "field": field,
                "category": item.category,
                "reason": item.reason,
                "hash": item.digest,
                "length": item.length,
                "span": list(item.span),
            })
            if item.category in category_counts:
                category_counts[item.category]["unparsed"] += 1
    totals["unparsed"] = len(unparsed_entries)

    for stats in category_counts.values():
        for key in ("source", "target", "matched", "missing_in_target",
                    "extra_in_target", "value_mismatch"):
            totals[key] += stats[key]

    scan_truncated = []
    if source_clipped:
        scan_truncated.append(cfg.text_fields[0])
    if target_clipped:
        scan_truncated.append(cfg.text_fields[1])

    return {
        "category_counts": category_counts,
        "totals": totals,
        "conflicts": conflicts[:cfg.max_conflicts_per_record],
        "conflicts_truncated": max(0, len(conflicts) - cfg.max_conflicts_per_record),
        "unparsed": unparsed_entries[:cfg.max_unparsed_per_record],
        "unparsed_truncated": max(0, len(unparsed_entries) - cfg.max_unparsed_per_record),
        "conflict_reasons": conflict_reasons,
        "scan_truncated": scan_truncated,
    }


def decide_status(evidence: Dict) -> str:
    """一致 / 冲突 / 无法判定。冲突优先（可定位的缺陷优先暴露）。"""
    totals = evidence["totals"]
    if (totals["missing_in_target"] or totals["extra_in_target"] or totals["value_mismatch"]):
        return "inconsistent"
    if totals["unparsed"] or evidence["scan_truncated"]:
        return "waiting_manual"
    return "consistent"


__all__ = [
    "CATEGORY_ORDER", "analyze_pair", "decide_status", "within_tolerance", "zero_category",
]
