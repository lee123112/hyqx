"""领域相关性：关键词表解析、指纹、打分口径常量与证据组装。

需求（客户编号 30 号 / 需求表第 32 行）：

1. 领域关键词列表 ``[关键词1;关键词2...]``，输入框按「；」批量录入；
2. 领域相关性阈值 ≥，默认 60%；
3. 低质量进入抽检台：是。

本模块只做"词表 → 规范列表"的无状态处理，**不产生任何分数**：分数只能来自
模型适配器（``score_relevance``）或如实为空（规则降级）。分值是
``logistic_cos_diff``，没有人工相关性 Gold，阈值是业务阈值，不是准确率。
"""
import hashlib
import re
from typing import Iterable, List, Optional, Sequence

# 「；」是输入框契约分隔符；后台同时容忍半角分号与换行（工程侧容错）。
CONTRACT_SEPARATOR = "；"
_KEYWORD_SPLIT = re.compile(r"[；;\r\n]+")
# 需要从关键词首尾剥离的空白：普通空白 + 全角空格 + 不换行空格。
_TRIM_CHARS = " \t　 "

RELEVANCE_STATUSES = (
    "relevant", "low_relevance", "no_keywords", "rule_degraded", "model_error",
    "disabled", "unknown",
)
SCORE_TYPE = "logistic_cos_diff"
CALIBRATION = "no_human_relevance_gold"


class KeywordConfigError(ValueError):
    """关键词表不满足输入契约（数量/长度/类型），必须报错而不是静默截断。"""


def parse_keywords(raw, max_keywords: int = 200, max_length: int = 64) -> List[str]:
    """把输入框字符串（或 JSON 数组）解析为去重后的关键词列表。

    - 分隔符：``；`` 为契约分隔符，同时容忍 ``;`` 与换行；
    - 清洗：去首尾空白（含全角空格/不换行空格），丢弃空项；
    - 去重：忽略大小写，保留首次出现的原写法；
    - 超限：关键词数或单条长度超限即抛 :class:`KeywordConfigError`，指出第几项。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items: Iterable = _KEYWORD_SPLIT.split(raw)
    elif isinstance(raw, (list, tuple)):
        flat: List[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise KeywordConfigError("关键词列表只能包含字符串")
            flat.extend(_KEYWORD_SPLIT.split(item))
        items = flat
    else:
        raise KeywordConfigError("关键词必须是字符串（按「；」分隔）或字符串数组")

    parsed: List[str] = []
    seen = set()
    for index, item in enumerate(items, 1):
        keyword = item.strip(_TRIM_CHARS)
        if not keyword:
            continue
        if len(keyword) > max_length:
            raise KeywordConfigError(
                f"第 {index} 个关键词长度 {len(keyword)} 超过上限 {max_length}：{keyword[:12]}…")
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        parsed.append(keyword)

    if len(parsed) > max_keywords:
        raise KeywordConfigError(
            f"关键词数量 {len(parsed)} 超过上限 {max_keywords}，请删减后重试")
    return parsed


def keyword_fingerprint(keywords: Sequence[str]) -> str:
    """关键词表指纹（SHA-256）。落盘只写指纹，不重复落整张词表。"""
    payload = "\n".join(keywords).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def score_bucket(score) -> str:
    """分值分档（0.0–1.0，10 档），用于 statistics.score_histogram。"""
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return "null"
    low = min(9, max(0, int(float(score) * 10)))
    return f"{low / 10:.1f}-{(low + 1) / 10:.1f}"


def empty_histogram() -> dict:
    return {f"{index / 10:.1f}-{(index + 1) / 10:.1f}": 0 for index in range(10)}


def relevance_result(score, cos_pos, cos_neg, temperature, aggregate,
                     matched_keywords, keyword_count, evidence,
                     score_type: str = SCORE_TYPE, calibration: str = CALIBRATION) -> dict:
    """组装一条记录的相关性结果（字段名与模型适配器入口保持一致）。"""
    return {
        "relevance_score": score,
        "cos_pos": cos_pos,
        "cos_neg": cos_neg,
        "temperature": temperature,
        "aggregate": aggregate,
        "matched_keywords": list(matched_keywords or []),
        "relevance_score_type": score_type,
        "relevance_calibration": calibration,
        "relevance_backend": "model",
        "keyword_count": keyword_count,
        "evidence": list(evidence or []),
    }


# --------------------------------------------------------------------------- #
# 父句/段落聚合（2026-11-04）
# --------------------------------------------------------------------------- #
# 10 号把父句切成子句后，单条子句常常只有几个字（「增编」「虽然当地行为体参与项目的实施，」），
# 与关键词的余弦天然偏低：全链路实测 805 条里 389 条被判低相关，人工抽检发现大量是父句整体
# 相关的误杀。口径改为：同一父句/段落内的记录先按父级文本打一次分，再把父级分下发给子记录；
# 子句不再单独决定去留。分组键按配置顺序查找，找不到就不聚合（保持逐条打分）。
_CLAUSE_SUFFIX_RE = re.compile(r"(?:-|::)clause[_-]?\d+$", re.IGNORECASE)


def parent_group_key(record: dict, fields: Sequence[str]) -> Optional[str]:
    """解析记录所属父级分组的键。

    查找顺序：顶层字段 → ``metadata`` 同名字段 → ``source_ref``/``target_ref``
    去掉末尾的子句后缀（如 ``-clause004``）。全部缺失时返回 None，表示逐条打分。
    """
    if not isinstance(record, dict):
        return None
    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    for field in fields or ():
        for source in (record, metadata):
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for field in ("source_ref", "target_ref"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return _CLAUSE_SUFFIX_RE.sub("", value.strip())
    return None


def join_parent_text(parts: Sequence[str], max_chars: int = 0) -> str:
    """把同组子句拼成父级文本；``max_chars>0`` 时按字符数截断（嵌入模型有长度上限）。"""
    text = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    if max_chars and len(text) > max_chars:
        return text[:max_chars]
    return text
