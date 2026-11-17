"""子句/短语候选打分与单调动态规划。

候选评分只由可解释分项构成，不使用任何伪造分数：
- `semantic`：跨语言语义相似度（模型后端真实计算）；规则降级时恒为 `null`。
- `dictionary`：双语词典命中，源词条译文在目标片段中的覆盖率；源片段无词条命中时为 `null`。
- `position`：源/目标片段在父句内的归一化位置一致性（1 - 位置差）。
- `length`：计量单元（汉字数 + 拉丁词数 + 数字串数）的 Dice 相似度。
- `parent`：上游父句置信度；缺失时为 `null`。
`confidence` 只对实际可用分项按权重重新归一，长度分绝不冒充语义分。
"""
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]+")
_DIGIT_RE = re.compile(r"\d+")

_ACTION_PRIORITY = ("1-1", "1-2", "2-1", "skip-source", "skip-target")


def unit_count(text: str) -> int:
    """计量单元数：汉字逐字计数，拉丁词和数字串各计 1。"""
    if not text:
        return 0
    spans = {match.span() for match in _LATIN_RE.finditer(text)}
    spans |= {match.span() for match in _DIGIT_RE.finditer(text)}
    count = 0
    covered = set()
    for start, end in sorted(spans):
        if any(index in covered for index in range(start, end)):
            continue
        covered.update(range(start, end))
        count += 1
    return count + len(_CJK_RE.findall(text))


def hard_tokens(text: str) -> List[str]:
    """可跨语言比较的硬 token：数字串与长度 >= 2 的拉丁词。"""
    tokens = [match.group(0).lower() for match in _LATIN_RE.finditer(text)]
    tokens += [match.group(0) for match in _DIGIT_RE.finditer(text)]
    return [token for token in tokens if len(token) >= 2 or token.isdigit()]


def dice_ratio(left: float, right: float) -> float:
    """Dice 相似度 2*min/(a+b)，对中英计量单元数量差异比 min/max 更稳健。"""
    total = left + right
    if total <= 0:
        return 0.0
    return 2 * min(left, right) / total


def match_terms(text: str, terminology: Sequence[Dict]) -> List[str]:
    """返回命中源语言片段的双语词条（按长度降序，避免短词条遮蔽长词条）。"""
    if not text or not terminology:
        return []
    lower_text = text.lower()
    hits: List[str] = []
    ordered = sorted(
        (entry for entry in terminology if isinstance(entry, dict) and entry.get("source")),
        key=lambda entry: len(str(entry["source"])), reverse=True)
    for entry in ordered:
        term = str(entry["source"])
        if term.lower() in lower_text and term not in hits:
            hits.append(term)
    return hits


def _expected_targets(terms: Sequence[str], terminology: Sequence[Dict]) -> List[str]:
    expected: List[str] = []
    lookup = {str(entry.get("source")): entry for entry in terminology
              if isinstance(entry, dict) and entry.get("source")}
    for term in terms:
        entry = lookup.get(term) or {}
        targets = entry.get("target") or []
        if isinstance(targets, str):
            targets = [targets]
        for target in targets:
            if isinstance(target, str) and target and target not in expected:
                expected.append(target)
    return expected


def lexicon_evidence(source_text: str, target_text: str,
                     terminology: Sequence[Dict]) -> Dict:
    """计算词典证据：源词条命中、期望译文与目标片段中的实际命中。"""
    source_terms = match_terms(source_text, terminology)
    expected = _expected_targets(source_terms, terminology)
    if not source_terms or not expected:
        return {"score": None, "source_terms": source_terms,
                "expected_targets": expected, "target_hits": []}
    lower_target = (target_text or "").lower()
    target_hits = [term for term in expected if term.lower() in lower_target]
    return {"score": len(target_hits) / len(expected), "source_terms": source_terms,
            "expected_targets": expected, "target_hits": target_hits}


def _position_center(indices: Sequence[int], count: int) -> float:
    """片段在父句内的归一化中心位置，取值 (0, 1]。"""
    if count <= 1:
        return 0.5
    return (sum(indices) / len(indices) + 0.5) / count


def position_score(source_indices: Sequence[int], target_indices: Sequence[int],
                   source_count: int, target_count: int) -> float:
    left = _position_center(source_indices, source_count)
    right = _position_center(target_indices, target_count)
    return 1.0 - abs(left - right)


def combine(components: Dict[str, Optional[float]],
            weights: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
    """按实际可用分项重新归一化权重，返回 (confidence, 实际使用的权重)。"""
    available = {name: weights[name] for name, value in components.items()
                 if value is not None and weights.get(name)}
    total_weight = sum(available.values())
    if total_weight <= 0:
        return 0.0, {}
    raw = sum(available[name] * components[name] for name in available)
    return raw / total_weight, available


def score_units(source_text: str, target_text: str,
                source_indices: Sequence[int], target_indices: Sequence[int],
                source_count: int, target_count: int,
                parent_confidence: Optional[float], terminology: Sequence[Dict],
                weights: Dict[str, float], semantic: Optional[float]) -> Dict:
    """计算一对（或多片段合并后的）子句对的真实分项分数与置信度。"""
    lexicon = lexicon_evidence(source_text, target_text, terminology)
    source_units = unit_count(source_text)
    target_units = unit_count(target_text)
    length = dice_ratio(source_units, target_units)
    position = position_score(source_indices, target_indices, source_count, target_count)

    components = {
        "semantic": semantic,
        "dictionary": lexicon["score"],
        "position": position,
        "length": length,
        "parent": parent_confidence,
    }
    confidence, used = combine(components, weights)
    return {
        "scores": {"semantic": semantic, "dictionary": lexicon["score"],
                   "position": position, "length": length, "parent": parent_confidence},
        "confidence": confidence,
        "used_weights": used,
        "units": {"source": source_units, "target": target_units},
        "evidence": {"source_terms": lexicon["source_terms"],
                     "expected_targets": lexicon["expected_targets"],
                     "target_hits": lexicon["target_hits"],
                     "shared_tokens": shared_tokens(source_text, target_text)},
        "texts": {"source": source_text, "target": target_text},
    }


def shared_tokens(source_text: str, target_text: str) -> List[str]:
    """两端共有的硬 token（数字、拉丁词），用于复核证据。"""
    left, right = set(hard_tokens(source_text)), set(hard_tokens(target_text))
    return sorted(left & right)


def _in_window(source_tail: int, target_tail: int, window: int) -> bool:
    return abs(source_tail - target_tail) <= window


def align_units(source_count: int, target_count: int,
                scorer: Callable[[Tuple[int, ...], Tuple[int, ...]], Dict],
                window: int = 3, skip_penalty: float = 0.35,
                allow_1_to_2: bool = True, allow_2_to_1: bool = True,
                ambiguity_margin: float = 0.02) -> Dict:
    """单调 DP：在窗口内选择 1-1/1-2/2-1 配对，窗口外只能跳过。

    返回 {"actions", "skipped_source", "skipped_target", "total_score", "ambiguous"}。
    """
    neg = float("-inf")
    dp = [[neg] * (target_count + 1) for _ in range(source_count + 1)]
    back = [[None] * (target_count + 1) for _ in range(source_count + 1)]
    dp[0][0] = 0.0

    for i in range(source_count + 1):
        for j in range(target_count + 1):
            candidates: List[Dict] = []
            if i >= 1 and j >= 1 and _in_window(i - 1, j - 1, window):
                candidates.append({"action": "1-1", "source": (i - 1,), "target": (j - 1,),
                                   "base": dp[i - 1][j - 1],
                                   "score": scorer((i - 1,), (j - 1,))})
            if allow_1_to_2 and i >= 1 and j >= 2 and _in_window(i - 1, j - 2, window):
                candidates.append({"action": "1-2", "source": (i - 1,), "target": (j - 2, j - 1),
                                   "base": dp[i - 1][j - 2],
                                   "score": scorer((i - 1,), (j - 2, j - 1))})
            if allow_2_to_1 and i >= 2 and j >= 1 and _in_window(i - 2, j - 1, window):
                candidates.append({"action": "2-1", "source": (i - 2, i - 1), "target": (j - 1,),
                                   "base": dp[i - 2][j - 1],
                                   "score": scorer((i - 2, i - 1), (j - 1,))})
            if i >= 1:
                candidates.append({"action": "skip-source", "source": (i - 1,), "target": (),
                                   "base": dp[i - 1][j], "score": None})
            if j >= 1:
                candidates.append({"action": "skip-target", "source": (), "target": (j - 1,),
                                   "base": dp[i][j - 1], "score": None})

            best = None
            for candidate in candidates:
                if candidate["base"] == neg:
                    continue
                candidate["gain"] = (-skip_penalty if candidate["score"] is None
                                     else candidate["score"]["confidence"])
                candidate["total"] = candidate["base"] + candidate["gain"]
                if best is None or candidate["total"] > best["total"]:
                    best = candidate
            if best is None:
                continue

            rivals = [candidate for candidate in candidates
                      if candidate["score"] is not None and candidate is not best
                      and candidate["total"] != neg]
            best["second_total"] = max((c["total"] for c in rivals), default=None)
            best["second_action"] = (max(rivals, key=lambda c: c["total"])["action"]
                                     if rivals else None)
            dp[i][j] = best["total"]
            back[i][j] = best

    actions: List[Dict] = []
    i, j = source_count, target_count
    while (i, j) != (0, 0) and back[i][j] is not None:
        choice = back[i][j]
        actions.append(choice)
        i -= len(choice["source"])
        j -= len(choice["target"])
    actions.reverse()

    skipped_source: List[int] = []
    skipped_target: List[int] = []
    ambiguous = 0
    for choice in actions:
        if choice["action"] == "skip-source":
            skipped_source.extend(choice["source"])
        elif choice["action"] == "skip-target":
            skipped_target.extend(choice["target"])
        else:
            margin = None
            if choice["second_total"] is not None:
                margin = choice["total"] - choice["second_total"]
                if margin <= ambiguity_margin:
                    ambiguous += 1
            choice["ambiguity_margin"] = margin

    return {"actions": actions, "skipped_source": skipped_source,
            "skipped_target": skipped_target,
            "total_score": dp[source_count][target_count], "ambiguous": ambiguous}


def merge_texts(units: Sequence[Dict], indices: Sequence[int]) -> str:
    """把被合并的多个片段按顺序拼接（不改写原文，仅用空格连接）。"""
    return " ".join(units[index]["text"].strip() for index in indices)

