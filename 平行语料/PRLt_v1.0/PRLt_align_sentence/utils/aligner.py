"""段落内句子对齐：分项打分 + 单调动态规划（1-1 / 1-2 / 2-1 / 跳过）。

打分口径：
- `semantic`：Qwen3-Embedding 真实余弦相似度；规则降级时为 `null`（绝不把长度分叫成语义分）。
- `length`：可解释长度证据，按"计量单元"（汉字数 + 拉丁词数 + 数字串数）的 Dice 相似度。
- `evidence`：数字/拉丁硬 token 的重合度；两端都缺少可比较 token 时为 `null`。
`confidence` 只对实际可用的分项按权重重新归一，不使用任何伪造分数。
"""
import re
from typing import Callable, Dict, List, Optional, Tuple

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]+")
_DIGIT_RE = re.compile(r"\d+")

_ACTION_PRIORITY = ("1-1", "1-2", "2-1", "skip-source", "skip-target")


def unit_count(text: str) -> int:
    """计量单元数：汉字/假名逐字计数，拉丁词和数字串各计 1。"""
    if not text:
        return 0
    latin_digits = set()
    for match in _LATIN_RE.finditer(text):
        latin_digits.add(match.span())
    for match in _DIGIT_RE.finditer(text):
        latin_digits.add(match.span())
    count = 0
    covered = set()
    for start, end in latin_digits:
        if any(i in covered for i in range(start, end)):
            continue
        covered.update(range(start, end))
        count += 1
    return count + len(_CJK_RE.findall(text))


def hard_tokens(text: str) -> List[str]:
    """可跨语言比较的硬 token：数字串与长度 >= 2 的拉丁词。"""
    tokens = [m.group(0).lower() for m in _LATIN_RE.finditer(text)]
    tokens += [m.group(0) for m in _DIGIT_RE.finditer(text)]
    return [t for t in tokens if len(t) >= 2 or t.isdigit()]


def dice_ratio(left: float, right: float) -> float:
    """Dice 相似度 2*min/(a+b)，对中英计量单元数量差异比 min/max 更稳健。"""
    total = left + right
    if total <= 0:
        return 0.0
    return 2 * min(left, right) / total


def combine(semantic: Optional[float], length: Optional[float], evidence: Optional[float],
            weights: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
    """按实际可用分项重新归一化权重，返回 (confidence, 实际使用的权重)。"""
    available = {}
    if semantic is not None:
        available["semantic"] = weights["semantic"]
    if length is not None:
        available["length"] = weights["length"]
    if evidence is not None:
        available["evidence"] = weights["evidence"]
    total_weight = sum(available.values())
    if total_weight <= 0:
        return 0.0, {}
    raw = 0.0
    if semantic is not None:
        raw += available["semantic"] * semantic
    if length is not None:
        raw += available["length"] * length
    if evidence is not None:
        raw += available["evidence"] * evidence
    return raw / total_weight, available


def score_components(source_text: str, target_text: str, semantic: Optional[float],
                     weights: Dict[str, float]) -> Dict:
    """计算一对（或多句合并后的）句对的真实分项分数与置信度。"""
    source_units = unit_count(source_text)
    target_units = unit_count(target_text)
    length = dice_ratio(source_units, target_units)

    source_tokens, target_tokens = hard_tokens(source_text), hard_tokens(target_text)
    evidence: Optional[float] = None
    shared: List[str] = []
    if source_tokens and target_tokens:
        left, right = set(source_tokens), set(target_tokens)
        shared = sorted(left & right)
        evidence = 2 * len(shared) / (len(left) + len(right))

    confidence, used = combine(semantic, length, evidence, weights)
    return {
        "scores": {"semantic": semantic, "length": length, "evidence": evidence},
        "confidence": confidence,
        "used_weights": used,
        "units": {"source": source_units, "target": target_units},
        "evidence_tokens": {"source": source_tokens, "target": target_tokens, "shared": shared},
    }


def _in_window(source_tail: int, target_tail: int, window: int) -> bool:
    """window 表示句对两端在段落内序号的最大偏移量。"""
    return abs(source_tail - target_tail) <= window


def align_paragraph(source_count: int, target_count: int,
                    scorer: Callable[[Tuple[int, ...], Tuple[int, ...]], Dict],
                    window: int = 5, skip_penalty: float = 0.35,
                    allow_1_to_2: bool = True, allow_2_to_1: bool = True,
                    ambiguity_margin: float = 0.02) -> Dict:
    """单调 DP：在窗口内选择 1-1/1-2/2-1 句对，窗口外只能跳过。

    返回 {"actions": [...], "skipped_source": [...], "skipped_target": [...],
          "total_score": float, "ambiguous": int}
    """
    neg = float("-inf")
    dp = [[neg] * (target_count + 1) for _ in range(source_count + 1)]
    back = [[None] * (target_count + 1) for _ in range(source_count + 1)]
    dp[0][0] = 0.0

    # 逐状态计算"进入 (i, j) 的最优动作"；base 为负无穷表示前驱不可达
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
                base = candidate["base"]
                if base == neg:
                    continue
                if candidate["score"] is None:
                    candidate["gain"] = -skip_penalty
                else:
                    candidate["gain"] = candidate["score"]["confidence"]
                candidate["total"] = base + candidate["gain"]
                if best is None or candidate["total"] > best["total"]:
                    best = candidate
            if best is None:
                continue

            # 同状态下最优的另一种"配对"动作，用于判断多候选歧义
            rivals = [c for c in candidates if c["score"] is not None and c is not best
                      and c["total"] != neg]
            second_total = max((c["total"] for c in rivals), default=None)
            best["second_total"] = second_total
            best["second_action"] = max(rivals, key=lambda c: c["total"])["action"] if rivals else None
            dp[i][j] = best["total"]
            back[i][j] = best

    actions: List[Dict] = []
    i, j = source_count, target_count
    while (i, j) != (0, 0) and back[i][j] is not None:
        choice = back[i][j]
        actions.append(choice)
        i, j = i - len(choice["source"]), j - len(choice["target"])
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
            "skipped_target": skipped_target, "total_score": dp[source_count][target_count],
            "ambiguous": ambiguous}


def merge_sentences(sentences: List[Dict], indices: Tuple[int, ...]) -> str:
    """把被合并的多句按顺序拼接（保留原句文本，仅用于打分和输出）。"""
    return " ".join(sentences[idx]["text"].strip() for idx in indices)


