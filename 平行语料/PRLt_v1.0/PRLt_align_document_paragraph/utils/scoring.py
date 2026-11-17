"""算子9 真实打分：长度相似度、段落位置先验、可比 token 重叠。

三项分数全部来自当前输入文本与段落索引的真实计算；本模块不包含任何
模型推理，也不返回常量或伪造分数。
"""
import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

CJK_LANGS = ("zh", "ja", "ko")

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                    r"\u3040-\u30ff\uac00-\ud7af]")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*")
COMPARABLE_RE = re.compile(r"[A-Za-z0-9]+")


def unit_kind(lang: Optional[str]) -> str:
    """中文/日文/韩文按字符计数，其他语言按词计数。"""
    if isinstance(lang, str) and lang.strip().lower()[:2] in CJK_LANGS:
        return "cjk"
    return "word"


def text_units(text: str, kind: str) -> int:
    """按可比单元计数：cjk 计汉字数 + 非汉字词数，word 计词数。"""
    if not text:
        return 0
    if kind == "cjk":
        return len(CJK_RE.findall(text)) + len(
            WORD_RE.findall(CJK_RE.sub(" ", text)))
    return len(WORD_RE.findall(text))


def length_score(source_units: int, target_units: int,
                 kind: str = "normalized") -> float:
    """长度相似度：可比单元数的归一化差异，取值 [0, 1]。

    中文段落与英文段落的原始字符数天然不同，因此本算子用可比单元
    （中文按字符、英文按词）的归一化差异，而不是直接比较字符数。
    """
    top = max(int(source_units), int(target_units))
    if top <= 0:
        return 0.0
    diff = abs(int(source_units) - int(target_units)) / top
    return max(0.0, min(1.0, 1.0 - diff))


def log_length_score(source_units: int, target_units: int,
                     ratio_cap: float = 3.0) -> float:
    """长度相似度的对数口径（备用口径，供对照与测试）。"""
    a = max(int(source_units), 0) + 1
    b = max(int(target_units), 0) + 1
    ratio = max(a, b) / min(a, b)
    if ratio <= 1.0:
        return 1.0
    span = math.log(max(ratio_cap, 1.0001))
    return max(0.0, min(1.0, 1.0 - math.log(ratio) / span))


def position_score(source_index: float, source_size: int,
                   target_index: float, target_size: int) -> float:
    """位置先验：段落跨度中心的归一化位置差异，取值 [0, 1]。

    位置用「跨度中心 / 文档段落总数」表示，因此源/目标段落数不同时
    仍然可比。
    """
    if source_size <= 0 or target_size <= 0:
        return 0.0
    pos_s = (float(source_index) + 0.5) / float(source_size)
    pos_t = (float(target_index) + 0.5) / float(target_size)
    return max(0.0, min(1.0, 1.0 - abs(pos_s - pos_t)))


def comparable_tokens(text: str) -> set:
    """可比较 token：数字与 ASCII 词（中英段落之间唯一可靠的字面交集）。"""
    return {token.lower() for token in COMPARABLE_RE.findall(text or "")}


def overlap_score(source_text: str, target_text: str) -> Optional[float]:
    """可比 token 的 Jaccard 重叠；任一方向没有可比 token 时返回 None。

    中英段落之间常常没有共享词，此时不计算重叠分（返回 None），
    不能把“零词汇重叠”当作必然失败。
    """
    source_tokens = comparable_tokens(source_text)
    target_tokens = comparable_tokens(target_text)
    if not source_tokens or not target_tokens:
        return None
    union = source_tokens | target_tokens
    if not union:
        return None
    return len(source_tokens & target_tokens) / len(union)


def effective_weights(length_weight: float, position_weight: float,
                      overlap_weight: float,
                      has_overlap: bool) -> Dict[str, float]:
    """重叠分缺失时对长度/位置权重重新归一化，保证置信度仍在 [0, 1]。"""
    if has_overlap:
        return {"length": float(length_weight),
                "position": float(position_weight),
                "overlap": float(overlap_weight)}
    total = float(length_weight) + float(position_weight)
    if total <= 0:
        return {"length": 0.5, "position": 0.5, "overlap": 0.0}
    return {"length": float(length_weight) / total,
            "position": float(position_weight) / total,
            "overlap": 0.0}


def score_group(*,
                source_chars: int,
                target_chars: int,
                source_units: int,
                target_units: int,
                unit: str,
                source_index: float,
                source_size: int,
                target_index: float,
                target_size: int,
                source_text: str,
                target_text: str,
                length_weight: float,
                position_weight: float,
                overlap_weight: float) -> Dict[str, object]:
    """对单个候选关系打分，返回各分项与总分（均为真实计算值）。"""
    length = length_score(source_units, target_units)
    position = position_score(source_index, source_size,
                              target_index, target_size)
    overlap = overlap_score(source_text, target_text)
    weights = effective_weights(length_weight, position_weight,
                                overlap_weight, overlap is not None)
    weighted = {
        "length": round(length * weights["length"], 6),
        "position": round(position * weights["position"], 6),
        "overlap": (round(overlap * weights["overlap"], 6)
                    if overlap is not None else 0.0),
    }
    combined = (weighted["length"] + weighted["position"]
                + weighted["overlap"])
    return {
        "scores": {"length": round(length, 6),
                   "position": round(position, 6),
                   "overlap": (round(overlap, 6)
                               if overlap is not None else None)},
        "weighted": weighted,
        "weights": weights,
        "base_score": round(combined, 6),
        "length_evidence": {
            "unit": unit,
            "source_chars": int(source_chars),
            "target_chars": int(target_chars),
            "source_units": int(source_units),
            "target_units": int(target_units),
        },
    }


def span_summary(texts: Sequence[str], lang: Optional[str]) -> Tuple[int, int]:
    """返回 (字符数, 可比单元数)。"""
    kind = unit_kind(lang)
    joined = "\n\n".join(texts)
    return len(joined), text_units(joined, kind)


def normalize_weights(length_weight: float, position_weight: float,
                      overlap_weight: float) -> List[float]:
    """配置校验用：返回权重列表。"""
    return [float(length_weight), float(position_weight),
            float(overlap_weight)]


