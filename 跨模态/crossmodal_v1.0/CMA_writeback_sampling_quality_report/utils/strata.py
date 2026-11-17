"""分层维度解析与**确定性**分层抽样（表1 #24 研发测核心规则）。

表1 口径：**按场景、模态、对齐类型分层抽 10%–20%，自动对齐也进入抽样框**。

实现要点（全部为确定性真实计算，不使用随机数）：

1. 三个维度的取值分别来自记录既有字段（不新造口径）：
   ``scene`` → ``scene_id``/``scene``/``group_id``/``metadata.*``；
   ``modality`` → 记录里真实出现的模态字段（排序后拼成 ``image+text`` 形式）；
   ``alignment_type`` → ``alignment.status``/``alignment.type`` 或 ``processing["16"]``。
   维度取不到值记 ``unknown`` 并在 ``applicable`` 中标记为不适用（记录仍保留）。
2. 配额分配用「比例取整 + 最大余数」，总量夹在 ``[10%, 20%]`` 内；
   当样本量过小导致百分比区间为空（``floor(0.20*N) < ceil(0.10*N)``）时至少抽 1 条，
   该边界在报告与文档中显式说明。
3. 层内选取按 ``blake2b(salt|sample_id)`` 升序，**与输入顺序无关、可复现**；
   同一 ``uniqueId`` 重跑得到同一抽检集合。
"""
from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

#: 表1 冻结的三个分层维度
STRATUM_DIMENSIONS = ("scene", "modality", "alignment_type")
#: 模态字段（按字母序拼接成路由串）
MODALITY_FIELDS = ("text", "image", "audio", "video")

UNKNOWN = "unknown"

ALIGNMENT_AUTO = "auto_aligned"
ALIGNMENT_MANUAL = "manual_aligned"
ALIGNMENT_NOT = "not_aligned"
ALIGNMENT_UNCERTAIN = "uncertain"
ALIGNMENT_PENDING = "pending_manual"
ALIGNMENT_OTHER = "other"
#: 判错后取消自动对齐时写入的取值（与 ``not_aligned`` 同义，单独命名便于阅读）
ALIGNMENT_CANCELLED = ALIGNMENT_NOT

_ALIGNMENT_ALIASES = {
    "auto_aligned": ALIGNMENT_AUTO,
    "automatic": ALIGNMENT_AUTO,
    "auto": ALIGNMENT_AUTO,
    "aligned": ALIGNMENT_AUTO,
    "manual_aligned": ALIGNMENT_MANUAL,
    "manually_aligned": ALIGNMENT_MANUAL,
    "human_aligned": ALIGNMENT_MANUAL,
    "not_aligned": ALIGNMENT_NOT,
    "unaligned": ALIGNMENT_NOT,
    "misaligned": ALIGNMENT_NOT,
    "mismatch": ALIGNMENT_NOT,
    "uncertain": ALIGNMENT_UNCERTAIN,
    "doubt": ALIGNMENT_UNCERTAIN,
    "pending_manual": ALIGNMENT_PENDING,
    "pending_manual_review": ALIGNMENT_PENDING,
    "waiting_manual": ALIGNMENT_PENDING,
    "pending": ALIGNMENT_PENDING,
}

DEFAULT_RATE_MIN = 0.10
DEFAULT_RATE_MAX = 0.20


# --------------------------------------------------------------------------- #
# 维度解析
# --------------------------------------------------------------------------- #

def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple, set)):
        return len(value) == 0
    return False


def _first_text(*values: Any) -> Tuple[str, bool]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip(), True
    return UNKNOWN, False


def resolve_scene(record: Mapping[str, Any]) -> Tuple[str, bool]:
    """场景维度：优先记录级，其次 ``metadata``；取不到记 ``unknown`` 且不适用。"""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return _first_text(record.get("scene_id"), record.get("scene"),
                       record.get("group_id"), metadata.get("scene_id"),
                       metadata.get("scene"), metadata.get("group_id"))


def resolve_modality(record: Mapping[str, Any]) -> Tuple[str, bool]:
    """模态维度：记录里真实出现的模态字段按字母序拼成 ``image+text`` 形式。"""
    present = [name for name in MODALITY_FIELDS if not _is_blank(record.get(name))]
    if not present:
        return UNKNOWN, False
    return "+".join(sorted(present)), True


def _alignment_raw(record: Mapping[str, Any]) -> Tuple[str, bool, Any]:
    """返回 ``(原始状态串, 是否取到, 显式 auto_aligned 布尔或 None)``。"""
    alignment = record.get("alignment")
    if isinstance(alignment, dict):
        explicit = alignment.get("auto_aligned")
        explicit = explicit if isinstance(explicit, bool) else None
        for key in ("status", "type", "alignment_type", "decision"):
            value = alignment.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), True, explicit
        if explicit is not None:
            return ("", True, explicit)
    elif isinstance(alignment, str) and alignment.strip():
        return alignment.strip(), True, None

    processing = record.get("processing")
    if isinstance(processing, dict):
        for namespace in ("16", 16):
            node = processing.get(namespace)
            if isinstance(node, dict):
                for key in ("status", "decision", "alignment_status", "alignment_type"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip(), True, None

    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in ("alignment_status", "alignment_type"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), True, None
    return "", False, None


def resolve_alignment(record: Mapping[str, Any]) -> Tuple[str, bool, bool]:
    """对齐类型维度：返回 ``(归一化类型, 是否取到, 是否自动对齐)``。"""
    raw, found, explicit = _alignment_raw(record)
    if not found:
        return UNKNOWN, False, False
    if not raw:
        return (ALIGNMENT_AUTO if explicit else ALIGNMENT_OTHER), True, bool(explicit)
    normalised = _ALIGNMENT_ALIASES.get(raw.lower(), ALIGNMENT_OTHER)
    auto = bool(explicit) if explicit is not None else normalised == ALIGNMENT_AUTO
    return normalised, True, auto


def stratum_key(record: Mapping[str, Any],
                strata: Sequence[str] = STRATUM_DIMENSIONS
                ) -> Tuple[Tuple[str, str, str], Dict[str, bool]]:
    """返回 ``((场景, 模态, 对齐类型), {维度: 是否取到})``（固定按表1 维度顺序）。"""
    resolved: Dict[str, Tuple[str, bool]] = {
        "scene": resolve_scene(record),
        "modality": resolve_modality(record),
        "alignment_type": resolve_alignment(record)[:2],
    }
    del strata  # 表1 三个维度固定，参数仅为显式声明调用方意图
    key = tuple(resolved[name][0] for name in STRATUM_DIMENSIONS)  # type: ignore[assignment]
    applicable = {name: resolved[name][1] for name in STRATUM_DIMENSIONS}
    return key, applicable


def stratum_label(key: Sequence[str]) -> str:
    """层标签：``scene=X|modality=Y|alignment_type=Z``。"""
    values = list(key) + [UNKNOWN] * (len(STRATUM_DIMENSIONS) - len(key))
    return "|".join("%s=%s" % (name, value)
                    for name, value in zip(STRATUM_DIMENSIONS, values))


# --------------------------------------------------------------------------- #
# 配额分配
# --------------------------------------------------------------------------- #

def allocate_quotas(sizes: Sequence[int], *, rate: float,
                    low: float = DEFAULT_RATE_MIN,
                    high: float = DEFAULT_RATE_MAX) -> List[int]:
    """按层大小分配抽样配额：比例取整 + 最大余数，总量夹在 ``[low, high]`` 内。"""
    values = [max(0, int(size)) for size in sizes]
    total = sum(values)
    if total == 0:
        return [0] * len(values)

    target = int(round(rate * total))
    high_n = int(math.floor(high * total))
    low_n = int(math.ceil(low * total))
    if high_n < 1:
        # 表1 #24「抽 10% 到 20%」且「不另设第二套抽检比例」：
        # 候选池过小时任何 1 条都会突破 20% 上限，因此一条都不抽，
        # 不得为「至少抽 1 条」而越界。
        return [0] * len(values)
    target = min(max(target, 1), high_n)
    if low_n <= high_n:
        target = max(target, low_n)
    target = min(target, total)

    exact = [rate * size for size in values]
    quotas = [min(int(math.floor(value)), size) for value, size in zip(exact, values)]
    assigned = sum(quotas)

    if assigned < target:
        order = sorted(range(len(values)),
                       key=lambda index: (-(exact[index] - quotas[index]),
                                          -values[index], index))
        cursor = 0
        limit = len(order) * (target + 1)
        while assigned < target and cursor < limit:
            index = order[cursor % len(order)]
            if quotas[index] < values[index]:
                quotas[index] += 1
                assigned += 1
            cursor += 1
        if assigned < target:  # pragma: no cover - 兜底，理论上不会触发
            for index in range(len(values)):
                while assigned < target and quotas[index] < values[index]:
                    quotas[index] += 1
                    assigned += 1
    elif assigned > target:
        order = sorted(range(len(values)),
                       key=lambda index: (quotas[index], -values[index], index))
        cursor = 0
        limit = len(order) * (assigned + 1)
        while assigned > target and cursor < limit:
            index = order[cursor % len(order)]
            if quotas[index] > 0:
                quotas[index] -= 1
                assigned -= 1
            cursor += 1
    return quotas


# --------------------------------------------------------------------------- #
# 确定性层内选取
# --------------------------------------------------------------------------- #

def sample_rank(sample_id: str, salt: str) -> str:
    """层内排序键：``blake2b(salt|sample_id)`` 十六进制摘要（与输入顺序无关）。"""
    payload = ("%s|%s" % (salt, sample_id)).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def select_samples(items: Iterable[Mapping[str, Any]], *, rate: float, salt: str,
                   low: float = DEFAULT_RATE_MIN,
                   high: float = DEFAULT_RATE_MAX) -> Dict[str, Any]:
    """执行分层抽样。

    ``items`` 每项至少包含 ``sample_id`` 与 ``stratum``（``stratum_key`` 的第一个返回值）。
    返回 ``selected``（抽中的 ``sample_id`` 列表）、``strata``（每层明细）、
    ``target_total`` 与 ``achieved_rate``。
    """
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        raise ValueError("sample_rate 必须是数字")
    if rate < low - 1e-9 or rate > high + 1e-9:
        raise ValueError("sample_rate 必须在 %.2f–%.2f 之间" % (low, high))

    groups: "OrderedDict[Tuple[str, ...], List[str]]" = OrderedDict()
    for item in items:
        sample_id = str(item.get("sample_id") or "")
        key = tuple(item.get("stratum") or ())
        groups.setdefault(key, []).append(sample_id)

    sizes = [len(ids) for ids in groups.values()]
    quotas = allocate_quotas(sizes, rate=rate, low=low, high=high)

    selected: List[str] = []
    detail: Dict[str, Dict[str, Any]] = {}
    for (key, ids), quota in zip(groups.items(), quotas):
        ranked = sorted(ids, key=lambda sid: (sample_rank(sid, salt), sid))
        picked = ranked[:quota]
        selected.extend(picked)
        label = stratum_label(key)
        detail[label] = {
            "scene": key[0] if len(key) > 0 else UNKNOWN,
            "modality": key[1] if len(key) > 1 else UNKNOWN,
            "alignment_type": key[2] if len(key) > 2 else UNKNOWN,
            "size": len(ids),
            "target": int(quota),
            "sampled": len(picked),
            "sample_ids": picked,
        }
    detail = {label: detail[label] for label in sorted(detail)}

    total = sum(sizes)
    return {
        "selected": selected,
        "target_total": int(sum(quotas)),
        "achieved_rate": round(len(selected) / total, 6) if total else 0.0,
        "strata": detail,
        "rate": float(rate),
        "salt": salt,
        "total": total,
    }
