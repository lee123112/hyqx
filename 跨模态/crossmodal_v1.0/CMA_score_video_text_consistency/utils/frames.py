"""视频帧与语义段的纯逻辑：归一化、分段、**整段视频合计**上限的确定性抽帧。

设计依据（表1 #14，2026-10-23 口径修正）：
- 输入是**抽帧序列**（由 #6 抽取视频关键帧产出 ``video.frames[]``），
  本算子不新增视频模型、不自行解码视频；
- ``max_frames_per_segment``（范围 1–8）是**整段视频合计**的帧预算：多语义段视频
  把预算分配到各段，全视频被选中帧总数不超过该上限（不再按段各 8 帧累计）；
- 单语义段（或段数 ≤ 上限）时行为与旧口径等价：≤ 上限帧、保序均匀覆盖；
- 长视频仍按语义段分别计算段分，任一段低于阈值即整段不通过。

抽帧策略：
1. 段内按时间区间 ``[start, end)`` 过滤；段未给出时间区间时使用全部候选帧；
2. 过滤结果为空（时间戳缺失或区间不重叠）时回退到全部候选帧，绝不静默产出空段；
3. 段内候选帧上做**均匀采样**（保序、去重）；每段配额由
   :func:`allocate_frame_quotas` 在整段视频内分配：
   段数 ≤ 预算时每段至少 1 帧、其余按候选帧数占比分配；
   段数 > 预算时按段均匀覆盖，部分段可能 0 帧（记无证据、不参与隔离判定）；
4. 0 帧段不参与该段判定，也不因其被隔离，证据里显式注明。

本文件是纯函数，不依赖 torch/PIL，可在无模型环境完整测试。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

#: 表1：``max_frames_per_segment`` 默认 8、范围 1–8。
#: 注意：自 2026-10-23 起该值是**整段视频合计**帧预算（不再按段各 8 帧累计），
#: 配置键名保持不变以兼容既有配置与调用方。
MAX_FRAMES_PER_SEGMENT = 8
MAX_FRAMES_MIN = 1
MAX_FRAMES_MAX = 8

#: 段内文本可用的字段名（按优先级）
SEGMENT_TEXT_FIELDS = ("text", "description", "transcript", "caption", "summary")


def clamp_limit(value: Any) -> int:
    """把帧数上限夹到表1 允许区间 ``[1, 8]``。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return MAX_FRAMES_PER_SEGMENT
    return max(MAX_FRAMES_MIN, min(MAX_FRAMES_MAX, number))


def as_float(value: Any) -> Optional[float]:
    """尽力转浮点；失败返回 ``None``（不抛异常，避免单条坏数据中断整批）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _first_text(source: Dict[str, Any], fields: Sequence[str]) -> Optional[str]:
    for field in fields:
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_frames(video_payload: Any) -> List[Dict[str, Any]]:
    """把 ``video.frames[]`` 归一为 ``[{frame_index, path, timestamp}]``。

    只保留可定位到路径的帧；时间戳缺省为 ``None``（表示整段共享）。
    """
    if not isinstance(video_payload, dict):
        return []
    raw_frames = video_payload.get("frames")
    if not isinstance(raw_frames, list):
        return []
    resolved: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_frames):
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        resolved.append({
            "frame_index": as_int(raw.get("frame_index")),
            "path": path.strip(),
            "timestamp": as_float(raw.get("timestamp")),
            "_order": index,
        })
    return resolved


def resolve_segments(video_payload: Any, default_text: Optional[str],
                     default_text_source: Optional[str]) -> List[Dict[str, Any]]:
    """解析语义段；没有 ``video.segments`` 时视为整段。

    每个段返回 ``{index, start, end, text, text_source, frames}``；
    ``frames`` 为该段自带帧（未提供时为 ``None``，表示使用整视频候选帧）。
    """
    raw_segments = None
    if isinstance(video_payload, dict):
        candidate = video_payload.get("segments")
        if isinstance(candidate, list):
            raw_segments = candidate

    segments: List[Dict[str, Any]] = []
    if raw_segments:
        for position, raw in enumerate(raw_segments):
            if not isinstance(raw, dict):
                continue
            text = _first_text(raw, SEGMENT_TEXT_FIELDS)
            if text is not None:
                field = next(name for name in SEGMENT_TEXT_FIELDS
                             if isinstance(raw.get(name), str) and raw.get(name).strip())
                text_source = "video.segments[%d].%s" % (position, field)
            else:
                text, text_source = default_text, default_text_source
            own_frames = resolve_frames(raw) if isinstance(raw.get("frames"), list) else None
            segments.append({
                "index": len(segments),
                "start": as_float(raw.get("start")),
                "end": as_float(raw.get("end")),
                "text": text,
                "text_source": text_source if text is not None else None,
                "frames": own_frames or None,
            })
    if segments:
        return segments

    return [{
        "index": 0,
        "start": None,
        "end": None,
        "text": default_text,
        "text_source": default_text_source if default_text is not None else None,
        "frames": None,
    }]


def evenly_spaced(items: Sequence[Any], limit: int) -> List[Any]:
    """保序、去重地均匀取最多 ``limit`` 个元素；``limit<=0`` 返回空。"""
    if limit <= 0:
        return []
    total = len(items)
    if total <= limit:
        return list(items)
    if limit == 1:
        return [items[(total - 1) // 2]]
    step = (total - 1) / float(limit - 1)
    picked: List[int] = []
    for position in range(limit):
        index = int(round(position * step))
        index = max(0, min(total - 1, index))
        if not picked or index != picked[-1]:
            picked.append(index)
    return [items[index] for index in picked]


def candidate_pool(candidates: Sequence[Dict[str, Any]], start: Optional[float],
                   end: Optional[float]) -> List[Dict[str, Any]]:
    """段候选帧池：按区间 ``[start, end)`` 过滤；空结果回退全部候选帧。

    这是抽帧策略的第 1、2 步，``select_frames`` 与预算分配共用同一口径，
    保证「配额」与「实际可取帧」一致。
    """
    frames = list(candidates or [])
    if not frames:
        return []
    if start is None or end is None:
        return frames
    in_range = [item for item in frames
                if item.get("timestamp") is not None
                and start <= float(item["timestamp"]) < end]
    return in_range if in_range else frames


def select_frames(candidates: Sequence[Dict[str, Any]], start: Optional[float],
                  end: Optional[float], limit: Any) -> List[Dict[str, Any]]:
    """单段选帧：区间过滤 → 空则回退全部 → 均匀采样 → 上限 ``[1, 8]``。"""
    return evenly_spaced(candidate_pool(candidates, start, end), clamp_limit(limit))


def allocate_frame_quotas(available_counts: Sequence[int], limit: Any) -> List[int]:
    """把**整段视频**的帧预算分配到各语义段，返回每段配额（总和 ≤ 上限）。

    表1 #14 口径（2026-10-23）：
    - 段数 ≤ 上限：每个**有候选帧**的段至少 1 帧；剩余预算按各段候选帧数占比
      用最大余数法分配（配额不超过该段候选帧数）；候选帧总量不足时用完全部候选帧；
    - 段数 > 上限：在**有候选帧的段**之间按段序均匀抽取至多 ``limit`` 个段、每段 1 帧，
      未被抽中的段配额为 0（该段记无证据，不参与隔离判定）；
    - 无候选帧的段配额恒为 0，其份额自动顺延给其它段。

    返回列表与 ``available_counts`` 等长；``sum(quotas) <= clamp_limit(limit)``。
    """
    cap = clamp_limit(limit)
    counts = [max(0, int(value)) for value in (available_counts or [])]
    quotas = [0] * len(counts)
    active = [index for index, count in enumerate(counts) if count > 0]
    if not active or cap <= 0:
        return quotas

    if len(active) > cap:
        # 段数 > 上限：按段均匀覆盖（保序），被抽中的段各 1 帧
        for index in evenly_spaced(active, cap):
            quotas[index] = 1
        return quotas

    # 每段先保底 1 帧（有候选帧的段）
    for index in active:
        quotas[index] = 1
    remaining = cap - len(active)
    if remaining <= 0:
        return quotas

    demands = [count - 1 for count in counts]  # 保底 1 帧后的额外需求
    total_demand = sum(demands[index] for index in active)
    if total_demand <= remaining:
        # 候选帧不足：用完全部候选帧（总帧数仍 ≤ 上限）
        return [counts[index] if quotas[index] else 0 for index in range(len(counts))]

    # 最大余数法：按额外需求占比分配剩余预算，余数相同按段序，结果确定
    shares = []
    for index in active:
        exact = remaining * demands[index] / float(total_demand)
        base = int(exact)
        shares.append((index, base, exact - base))
    for index, base, _fraction in shares:
        quotas[index] += base
    leftover = remaining - sum(base for _index, base, _fraction in shares)
    for index, _base, _fraction in sorted(shares, key=lambda item: (-item[2], item[0])):
        if leftover <= 0:
            break
        quotas[index] += 1
        leftover -= 1
    return quotas


def segment_pools(video_payload: Any, segments: Sequence[Dict[str, Any]]
                  ) -> List[List[Dict[str, Any]]]:
    """每段的候选帧池（段自带帧优先，否则整视频候选帧；再按段时间区间过滤）。"""
    whole = resolve_frames(video_payload)
    pools: List[List[Dict[str, Any]]] = []
    for segment in segments:
        own = segment.get("frames")
        candidates = own if own else whole
        pools.append(candidate_pool(candidates, segment.get("start"), segment.get("end")))
    return pools


def frame_plan(video_payload: Any, segments: Sequence[Dict[str, Any]],
               limit: Any) -> Dict[str, Any]:
    """整段视频抽帧计划：候选池 → 预算分配 → 段内保序均匀取帧。

    返回 ``{limit, pools, available, quotas, groups, frames_used,
    frames_within_budget}``；``frames_used`` **恒 ≤ limit**（表1 #14 整段视频合计上限）。
    """
    cap = clamp_limit(limit)
    pools = segment_pools(video_payload, segments)
    available = [len(pool) for pool in pools]
    quotas = allocate_frame_quotas(available, cap)
    groups = [evenly_spaced(pool, quota) for pool, quota in zip(pools, quotas)]
    frames_used = sum(len(group) for group in groups)
    return {
        "limit": cap,
        "pools": pools,
        "available": available,
        "quotas": quotas,
        "groups": groups,
        "frames_used": frames_used,
        "frames_within_budget": frames_used <= cap,
    }


def segment_frames(video_payload: Any, segments: Sequence[Dict[str, Any]],
                   limit: Any) -> List[List[Dict[str, Any]]]:
    """兼容入口：只返回每段实际参与评分的帧列表（分段到帧的唯一定口）。"""
    return frame_plan(video_payload, segments, limit)["groups"]
