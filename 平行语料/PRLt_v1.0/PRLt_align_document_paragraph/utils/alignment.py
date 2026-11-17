"""候选生成与单调动态规划：在候选中选择 1:1 / 1:2 / 2:1 段落关系。

DP 状态为「已消费的源段落数 × 已消费的目标段落数」，动作包含对齐
（1:1 / 1:2 / 2:1）与跳过（源或目标段落未对齐）。每个状态保留回溯
动作，因此相同输入必然得到相同结果。
"""
from typing import Dict, List, Optional, Sequence, Tuple

from utils.scoring import score_group, span_summary

SKIP_SOURCE = "skip_source"
SKIP_TARGET = "skip_target"
ALIGN = "align"

EPS = 1e-9


def relation_name(source_span: int, target_span: int) -> str:
    return "%d:%d" % (int(source_span), int(target_span))


def paragraph_ref(index: int) -> str:
    return "para-%03d" % (index + 1)


def build_candidates(*, source_paragraphs: Sequence[str],
                     target_paragraphs: Sequence[str],
                     source_lang: str, target_lang: str,
                     candidate_window: int,
                     allow_1_to_2: bool, allow_2_to_1: bool,
                     length_weight: float, position_weight: float,
                     overlap_weight: float) -> List[Dict[str, object]]:
    """按窗口生成候选关系并逐项真实打分。"""
    source_size = len(source_paragraphs)
    target_size = len(target_paragraphs)
    candidates: List[Dict[str, object]] = []
    for i in range(source_size):
        for j in range(target_size):
            if abs(i - j) > candidate_window:
                continue
            spans: List[Tuple[int, int]] = [(1, 1)]
            if allow_1_to_2 and j + 1 < target_size:
                spans.append((1, 2))
            if allow_2_to_1 and i + 1 < source_size:
                spans.append((2, 1))
            for source_span, target_span in spans:
                source_slice = list(source_paragraphs[i:i + source_span])
                target_slice = list(target_paragraphs[j:j + target_span])
                source_text = "\n\n".join(source_slice)
                target_text = "\n\n".join(target_slice)
                source_chars, source_units = span_summary(source_slice,
                                                          source_lang)
                target_chars, target_units = span_summary(target_slice,
                                                          target_lang)
                scored = score_group(
                    source_chars=source_chars, target_chars=target_chars,
                    source_units=source_units, target_units=target_units,
                    unit=score_unit(source_lang),
                    source_index=i + (source_span - 1) / 2.0,
                    source_size=source_size,
                    target_index=j + (target_span - 1) / 2.0,
                    target_size=target_size,
                    source_text=source_text, target_text=target_text,
                    length_weight=length_weight,
                    position_weight=position_weight,
                    overlap_weight=overlap_weight,
                )
                candidates.append({
                    "source_start": i,
                    "source_span": source_span,
                    "target_start": j,
                    "target_span": target_span,
                    "relation": relation_name(source_span, target_span),
                    "source_refs": [paragraph_ref(k)
                                    for k in range(i, i + source_span)],
                    "target_refs": [paragraph_ref(k)
                                    for k in range(j, j + target_span)],
                    "source_text": source_text,
                    "target_text": target_text,
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "scores": scored["scores"],
                    "weighted": scored["weighted"],
                    "weights": scored["weights"],
                    "base_score": scored["base_score"],
                    "length_evidence": scored["length_evidence"],
                })
    return candidates


def score_unit(lang: str) -> str:
    from utils.scoring import unit_kind
    return unit_kind(lang)


def with_penalty(candidate: Dict[str, object],
                 group_penalty: float) -> Dict[str, object]:
    """多段落合并关系施加结构惩罚，避免 DP 过度合并。"""
    multi = (int(candidate["source_span"]) * int(candidate["target_span"])) > 1
    penalty = float(group_penalty) if multi else 0.0
    confidence = float(candidate["base_score"]) - penalty
    record = dict(candidate)
    record["penalty"] = round(penalty, 6)
    record["confidence"] = round(max(0.0, min(1.0, confidence)), 6)
    return record


def candidate_key(candidate: Dict[str, object]) -> Tuple[int, int, int, int]:
    return (int(candidate["source_start"]), int(candidate["source_span"]),
            int(candidate["target_start"]), int(candidate["target_span"]))


def select_monotonic(candidates: Sequence[Dict[str, object]], source_size: int,
                     target_size: int, group_penalty: float,
                     forbidden: Optional[set] = None) -> Tuple[
                         List[Dict[str, object]], List[int], List[int], float]:
    """单调 DP 选择关系，返回 (对齐关系, 未对齐源索引, 未对齐目标索引, 总分)。

    ``forbidden`` 中的候选被排除，用于计算「禁用某条关系后的第二优对齐」。
    """
    by_cell: Dict[Tuple[int, int], List[Dict[str, object]]] = {}
    blocked = forbidden or set()
    for candidate in candidates:
        if blocked and candidate_key(candidate) in blocked:
            continue
        key = (int(candidate["source_start"]), int(candidate["target_start"]))
        by_cell.setdefault(key, []).append(
            with_penalty(candidate, group_penalty))

    unreachable = float("-inf")
    dp = [[unreachable] * (target_size + 1) for _ in range(source_size + 1)]
    pred: List[List[Optional[Tuple[int, int, str, Optional[dict]]]]] = [
        [None] * (target_size + 1) for _ in range(source_size + 1)
    ]
    dp[0][0] = 0.0

    for i in range(source_size + 1):
        for j in range(target_size + 1):
            current = dp[i][j]
            if current == unreachable:
                continue
            # 先处理对齐动作，等分时优先对齐而不是跳过。
            for candidate in sorted(by_cell.get((i, j), []),
                                    key=lambda item: (
                                        int(item["source_span"]),
                                        int(item["target_span"]))):
                ni = i + int(candidate["source_span"])
                nj = j + int(candidate["target_span"])
                value = current + float(candidate["confidence"])
                if value > dp[ni][nj] + EPS:
                    dp[ni][nj] = value
                    pred[ni][nj] = (i, j, ALIGN, candidate)
            if i < source_size and current > dp[i + 1][j] + EPS:
                dp[i + 1][j] = current
                pred[i + 1][j] = (i, j, SKIP_SOURCE, None)
            if j < target_size and current > dp[i][j + 1] + EPS:
                dp[i][j + 1] = current
                pred[i][j + 1] = (i, j, SKIP_TARGET, None)

    selected: List[Dict[str, object]] = []
    unaligned_source: List[int] = []
    unaligned_target: List[int] = []
    i, j = source_size, target_size
    while (i, j) != (0, 0):
        step = pred[i][j]
        if step is None:
            # 不可达状态（理论上不会出现）：保守回退，避免死循环。
            unaligned_source.append(i - 1)
            unaligned_target.append(j - 1)
            i, j = max(0, i - 1), max(0, j - 1)
            continue
        pi, pj, action, candidate = step
        if action == ALIGN and candidate is not None:
            selected.append(candidate)
        elif action == SKIP_SOURCE:
            unaligned_source.append(pi)
        else:
            unaligned_target.append(pj)
        i, j = pi, pj

    selected.reverse()
    unaligned_source.sort()
    unaligned_target.sort()
    total = 0.0 if dp[source_size][target_size] == unreachable else dp[
        source_size][target_size]
    return selected, unaligned_source, unaligned_target, round(total, 6)


def ambiguity_gap(candidates: Sequence[Dict[str, object]], source_size: int,
                  target_size: int, group_penalty: float,
                  chosen: Dict[str, object], total_score: float,
                  max_cells: int = 10000) -> Tuple[
                      Optional[float], Optional[Dict[str, object]], bool]:
    """禁用已选关系后重算最优对齐，用总分差距判定「多候选」。

    返回 (gap, runner_up, evaluated)：gap 是当前最优对齐与第二优对齐的总分
    差；runner_up 是第二优方案中与已选关系竞争的关系；段落规模过大时返回
    evaluated=False（明确标记为未评估，不猜测）。
    """
    if source_size * target_size > max_cells:
        return None, None, False
    alternative, _, _, alternative_total = select_monotonic(
        candidates, source_size, target_size, group_penalty,
        forbidden={candidate_key(chosen)})
    gap = round(float(total_score) - float(alternative_total), 6)
    chosen_source = set(range(int(chosen["source_start"]),
                              int(chosen["source_start"])
                              + int(chosen["source_span"])))
    runner_up = None
    for item in alternative:
        source_span = set(range(int(item["source_start"]),
                                int(item["source_start"])
                                + int(item["source_span"])))
        if source_span & chosen_source:
            runner_up = item
            break
    return gap, runner_up, True


