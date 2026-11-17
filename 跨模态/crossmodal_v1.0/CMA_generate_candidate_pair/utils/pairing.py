"""算子 #11 生成候选配对 业务核心（表1 数据加工 / 配对与证据绑定）。

表1 冻结口径：
- 完整记录先按真实 source_id 生成文本/各媒体视图的同记录 A 级候选；
  原始记录不再进入跨记录搜索，派生视图数独立于原始记录守恒统计。
- 三级证据顺序：同来源编号或场景编号（A）→ 时间重叠（B）→ 共同关键词至少 N 个（C，默认 2，范围 2–5）。
  配上即停，同一个样本不会同时按三套证据重复计数。
- B 级（时间重叠）默认要求共享上下文（scene_id/source_id/batch_id 任一相同），
  防止跨来源素材因时间轴都从 0 秒开始而误配；可用
  ``time_overlap_require_shared_context: false`` 显式关闭（默认 true）。
- 一对多拆成多条一对一，共享 group_id。
- 只记录 A/B/C 证据；本工具不加载 CLIP，不算相似度，不做模型推理。
- 配不出候选的记录进入未配对清单（不隔离、不进入评分）。

本模块是纯规则实现，不 import 任何向量/相似度模型库。
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

OPERATOR = "generate_candidate_pairs"
VERSION = "v1.0.0"

MODALITY_ORDER = ("text", "image", "video", "audio")
TIER_ORDER = ("source", "time_overlap", "keywords")
EVIDENCE_BY_TIER = {"source": "A", "time_overlap": "B", "keywords": "C"}
EVIDENCE_PRIORITY = {"A": 1, "B": 2, "C": 3}

DEFAULT_PRIORITY = "source"
# 证据来源与分词口径（与兄弟算子 08 的 evidence_source/tokenizer 字段同名，便于下游统一读取）
EVIDENCE_SOURCE = "rule"
TOKENIZER_ID = "upstream_tokens+cjk_bigram_rule"
DEFAULT_KEYWORD_MIN_OVERLAP = 2
KEYWORD_MIN_RANGE = (2, 5)
DEFAULT_MAX_PAIRS_PER_GROUP = 200
DEFAULT_TIME_MIN_OVERLAP_SECONDS = 0.0

ALLOWED_OVERRIDES = {"priority", "keyword_min_overlap", "max_pairs_per_group",
                     "time_min_overlap_seconds", "time_overlap_require_shared_context"}

PRIORITY_ALIASES = {
    "source": "source", "same_source": "source", "same_source_id": "source",
    "来源": "source", "同一个来源编号": "source",
    "time_overlap": "time_overlap", "time": "time_overlap", "overlap": "time_overlap",
    "时间重叠": "time_overlap",
    "keywords": "keywords", "keyword": "keywords", "关键词": "keywords",
    "关键词至少2个相同": "keywords",
}

GROUP_ID_MAX_LEN = 128
KEYWORD_MAX_LEN = 8
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN_WORD = re.compile(r"[0-9a-z]+")
_UNSAFE = re.compile(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]")


class ConfigError(ValueError):
    """配置不合法（例如试图关闭必需工具或关键字阈值越界）。"""


# --------------------------------------------------------------------- 配置

def validate_config(cfg: dict) -> dict:
    """校验并规范化配置；返回深拷贝，不修改入参。"""
    if not isinstance(cfg, dict):
        raise ConfigError("配置必须是对象")
    section = cfg.get("pairing")
    if not isinstance(section, dict):
        raise ConfigError("缺少 pairing 配置段")
    if section.get("enabled", True) is not True:
        raise ConfigError("生成候选配对是必须工具，pairing.enabled 不能被关闭")

    raw_priority = section.get("priority", DEFAULT_PRIORITY)
    priority = PRIORITY_ALIASES.get(str(raw_priority).strip().lower())
    if priority is None:
        raise ConfigError("pairing.priority 非法：%s（可选 source / time_overlap / keywords）"
                          % raw_priority)

    keyword_min = section.get("keyword_min_overlap", DEFAULT_KEYWORD_MIN_OVERLAP)
    if isinstance(keyword_min, bool) or not isinstance(keyword_min, int) \
            or not (KEYWORD_MIN_RANGE[0] <= keyword_min <= KEYWORD_MIN_RANGE[1]):
        raise ConfigError("pairing.keyword_min_overlap 必须是 %d–%d 的整数，当前为 %r"
                          % (KEYWORD_MIN_RANGE[0], KEYWORD_MIN_RANGE[1], keyword_min))

    limit = section.get("max_pairs_per_group", DEFAULT_MAX_PAIRS_PER_GROUP)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ConfigError("pairing.max_pairs_per_group 必须是正整数")

    min_overlap = section.get("time_min_overlap_seconds", DEFAULT_TIME_MIN_OVERLAP_SECONDS)
    if isinstance(min_overlap, bool) or not isinstance(min_overlap, (int, float)) \
            or not math.isfinite(float(min_overlap)) or float(min_overlap) < 0:
        raise ConfigError("pairing.time_min_overlap_seconds 必须是非负有限数")

    require_context = section.get("time_overlap_require_shared_context", True)
    if not isinstance(require_context, bool):
        raise ConfigError("pairing.time_overlap_require_shared_context 必须是布尔值")

    normalized = copy.deepcopy(cfg)
    target = normalized["pairing"]
    target["enabled"] = True
    target["priority"] = priority
    target["keyword_min_overlap"] = keyword_min
    target["max_pairs_per_group"] = limit
    target["time_min_overlap_seconds"] = float(min_overlap)
    target["time_overlap_require_shared_context"] = require_context
    return normalized


def apply_overrides(cfg: dict, overrides: Optional[dict]) -> dict:
    """把请求级 config 合并到配置；只允许业务参数，身份/部署项不可被改写。"""
    merged = copy.deepcopy(cfg)
    if overrides is None:
        return merged
    if not isinstance(overrides, dict):
        raise ConfigError("config 必须是对象")
    unknown = sorted(set(overrides) - ALLOWED_OVERRIDES)
    if unknown:
        raise ConfigError("不支持的配置项：%s" % unknown[0])
    section = merged.get("pairing")
    if not isinstance(section, dict):
        section = {}
        merged["pairing"] = section
    for key, value in overrides.items():
        section[key] = value
    return merged


# ----------------------------------------------------------------- 字段解析

def is_present(value: object) -> bool:
    """判断模态字段是否有效存在；空串、空白、空容器视为不存在。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(is_present(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    return True


def modalities_of(record: dict) -> List[str]:
    return [key for key in MODALITY_ORDER if is_present(record.get(key))]


def _as_key(value: object) -> Optional[str]:
    if not is_present(value):
        return None
    text = str(value).strip()
    return text or None


def _first_key(record: dict, keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        found = _as_key(record.get(key))
        if found is not None:
            return found
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            found = _as_key(metadata.get(key))
            if found is not None:
                return found
    return None


def source_id_of(record: dict) -> Optional[str]:
    """来源编号：source_id / sourceId → metadata.source_id / sourceId / source。"""
    return _first_key(record, ("source_id", "sourceId", "source"))


def scene_id_of(record: dict) -> Optional[str]:
    """场景编号：#2 写入的 scene_id 或 group_id → metadata 同名键。"""
    return _first_key(record, ("scene_id", "sceneId", "group_id", "groupId", "scene"))


def batch_id_of(record: dict) -> Optional[str]:
    """批次编号：batch_id / batchId → metadata 同名键（时间重叠防护的兜底上下文）。"""
    return _first_key(record, ("batch_id", "batchId"))


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _range_from(value: object) -> List[Tuple[float, float]]:
    """接受 [start, end] 或 {"start": .., "end": ..} 形式的时间范围。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = _number(value[0]), _number(value[1])
        if start is not None and end is not None:
            return [(start, end)]
        return []
    if isinstance(value, dict):
        start, end = _number(value.get("start")), _number(value.get("end"))
        if start is not None and end is not None:
            return [(start, end)]
    return []


def time_range_of(record: dict) -> Optional[Tuple[float, float]]:
    """汇总记录内全部可用时间信息；没有任何真实时间戳时返回 None（不适用）。"""
    intervals: List[Tuple[float, float]] = []
    intervals += _range_from(record.get("time_range"))
    for key in ("text", "audio", "video"):
        obj = record.get(key)
        if not isinstance(obj, dict):
            continue
        intervals += _range_from(obj.get("time_index"))
        intervals += _range_from(obj.get("time_range"))
        for segment in obj.get("segments") or []:
            if isinstance(segment, dict):
                intervals += _range_from((segment.get("start"), segment.get("end")))
        duration = _number(obj.get("duration"))
        if duration is not None and duration > 0:
            intervals.append((0.0, duration))
        frames = obj.get("frames")
        if isinstance(frames, list):
            stamps = [_number(item.get("timestamp")) for item in frames
                      if isinstance(item, dict)]
            stamps = [stamp for stamp in stamps if stamp is not None]
            if stamps:
                intervals.append((min(stamps), max(stamps)))
    valid = [(start, end) for start, end in intervals if end > start]
    if not valid:
        return None
    return (min(start for start, _ in valid), max(end for _, end in valid))


def overlap_seconds(left: Tuple[float, float], right: Tuple[float, float]) -> float:
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _strings_to_keywords(value: object) -> Set[str]:
    """规则型关键词：拉丁/数字词长 ≥3；中文 2–8 字整体词 + 全部二元字组。

    这是确定性规则分词，不加载分词模型；上游 #4 已给出 tokens 时优先使用 tokens。
    """
    keywords: Set[str] = set()
    text = str(value).strip().lower()
    if not text:
        return keywords
    for run in _CJK_RUN.findall(text):
        if 2 <= len(run) <= KEYWORD_MAX_LEN:
            keywords.add(run)
        for index in range(len(run) - 1):
            keywords.add(run[index:index + 2])
    for word in _LATIN_WORD.findall(text):
        if len(word) >= 3:
            keywords.add(word)
    return keywords


def _candidate_strings(record: dict) -> List[str]:
    values: List[str] = []
    text = record.get("text")
    if isinstance(text, str):
        values.append(text)
    elif isinstance(text, dict):
        for key in ("tokens", "keywords"):
            items = text.get(key)
            if isinstance(items, list):
                values += [str(item) for item in items if is_present(item)]
        if is_present(text.get("content")):
            values.append(str(text["content"]))
    if is_present(record.get("description")):
        values.append(str(record["description"]))
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in ("image_tags", "keywords", "tags", "labels"):
            items = metadata.get(key)
            if isinstance(items, list):
                values += [str(item) for item in items if is_present(item)]
            elif is_present(items):
                values.append(str(items))
    audio = record.get("audio")
    if isinstance(audio, dict) and is_present(audio.get("transcript")):
        values.append(str(audio["transcript"]))
    return values


def keyword_set(record: dict) -> Set[str]:
    """记录关键词集合：文本 tokens/内容、描述、metadata 标签、音频转写；不使用文件名。"""
    keywords: Set[str] = set()
    for value in _candidate_strings(record):
        keywords |= _strings_to_keywords(value)
    return keywords


# ------------------------------------------------------------------- 配对

class _Entry(object):
    __slots__ = ("index", "record", "sample_id", "modalities", "source_id",
                 "scene_id", "batch_id", "time_range", "keywords")

    def __init__(self, index: int, record: dict) -> None:
        self.index = index
        self.record = record
        self.sample_id = str(record.get("sample_id", "")).strip()
        self.modalities = modalities_of(record)
        self.source_id = source_id_of(record)
        self.scene_id = scene_id_of(record)
        self.batch_id = batch_id_of(record)
        self.time_range = time_range_of(record)
        self.keywords = keyword_set(record)


class PairingResult(object):
    __slots__ = ("pairs", "unpaired", "rejects", "stats")

    def __init__(self, pairs: List[dict], unpaired: List[dict], rejects: List[dict],
                 stats: Dict[str, object]) -> None:
        self.pairs = pairs
        self.unpaired = unpaired
        self.rejects = rejects
        self.stats = stats


def _sanitize_token(value: object, fallback: str) -> str:
    text = re.sub(r"\s+", "_", str(value).strip())
    text = _UNSAFE.sub("_", text)[:GROUP_ID_MAX_LEN]
    return text or fallback


def _is_cross_modal(left: _Entry, right: _Entry) -> bool:
    return len(set(left.modalities) | set(right.modalities)) >= 2


def _share_source_or_scene(left: _Entry, right: _Entry) -> Optional[str]:
    if left.source_id is not None and left.source_id == right.source_id:
        return "source_id"
    if left.scene_id is not None and left.scene_id == right.scene_id:
        return "scene_id"
    return None


def _shared_context(left: _Entry, right: _Entry) -> Optional[str]:
    """时间重叠级的共享上下文防护：scene_id → source_id → batch_id 任一相同。

    round9 复核确认：仅按时间重叠会在跨来源记录间产生误配（不同素材时间轴都从
    0 秒开始必然重叠）。要求至少一种上下文一致，防止把无关素材配成一对；
    上下文全都缺失的记录不进入 B 级配对（宁可不配对，不可错配）。
    """
    if left.scene_id is not None and left.scene_id == right.scene_id:
        return "scene_id"
    if left.source_id is not None and left.source_id == right.source_id:
        return "source_id"
    if left.batch_id is not None and left.batch_id == right.batch_id:
        return "batch_id"
    return None


def _candidates(seed: _Entry, entries: Sequence[_Entry], paired: Sequence[bool],
                tier: str, section: dict) -> Tuple[List[_Entry], int]:
    candidates: List[_Entry] = []
    skipped = 0
    for other in entries[seed.index + 1:]:
        if paired[other.index]:
            continue
        if tier == "source":
            if _share_source_or_scene(seed, other) is None:
                continue
        elif tier == "time_overlap":
            if seed.time_range is None or other.time_range is None:
                continue
            overlap = overlap_seconds(seed.time_range, other.time_range)
            if overlap <= 0.0 or overlap < section["time_min_overlap_seconds"]:
                continue
            if section.get("time_overlap_require_shared_context", True) \
                    and _shared_context(seed, other) is None:
                continue
        else:
            if len(seed.keywords & other.keywords) < section["keyword_min_overlap"]:
                continue
        if not _is_cross_modal(seed, other):
            skipped += 1
            continue
        candidates.append(other)
    return candidates, skipped


def _make_pair(seed: _Entry, partner: _Entry, tier: str, unique_id: str,
               section: dict) -> dict:
    if tier == "source":
        matched_by = _share_source_or_scene(seed, partner)
        if matched_by == "source_id":
            criterion = "same_source_id"
            value = {"source_id": seed.source_id}
            group_id = "grp-src-" + _sanitize_token(seed.source_id, "unknown")
        else:
            criterion = "same_scene_id"
            value = {"scene_id": seed.scene_id}
            group_id = "grp-scene-" + _sanitize_token(seed.scene_id, "unknown")
    elif tier == "time_overlap":
        criterion = "time_overlap"
        value = {
            "overlap_seconds": round(overlap_seconds(seed.time_range, partner.time_range), 6),
            "left_range": [round(seed.time_range[0], 6), round(seed.time_range[1], 6)],
            "right_range": [round(partner.time_range[0], 6), round(partner.time_range[1], 6)],
            # 共享上下文防护命中的依据（scene/source/batch），便于审计 B 级配对
            "shared_context": _shared_context(seed, partner),
        }
        group_id = "grp-time-" + _sanitize_token(seed.sample_id, "unknown")
    else:
        criterion = "shared_keywords"
        matched = sorted(seed.keywords & partner.keywords)
        value = {"min_overlap": section["keyword_min_overlap"],
                 "matched": matched,
                 "matched_count": len(matched),
                 "tokenizer": TOKENIZER_ID}
        group_id = "grp-kw-" + _sanitize_token(seed.sample_id, "unknown")

    evidence_type = EVIDENCE_BY_TIER[tier]
    modality_pair = [name for name in MODALITY_ORDER
                     if name in set(seed.modalities) | set(partner.modalities)]
    return {
        "pair_id": "pair-%s--%s" % (seed.sample_id, partner.sample_id),
        "sample_id": "pair-%s--%s" % (seed.sample_id, partner.sample_id),
        "group_id": group_id,
        # [v4.2] 输出记录 status 双字段：int status（1 成功/0 失败）+ process_status 流程态
        "status": 1,
        "process_status": "candidate",
        "pair_status": "candidate",
        "modality_pair": modality_pair,
        "evidence": {"type": evidence_type, "criterion": criterion,
                     "evidence_source": EVIDENCE_SOURCE, "value": value},
        "evidence_priority": EVIDENCE_PRIORITY[evidence_type],
        "left": {"sample_id": seed.sample_id, "modalities": seed.modalities,
                 "record": seed.record},
        "right": {"sample_id": partner.sample_id, "modalities": partner.modalities,
                  "record": partner.record},
        "producer": {"operator": OPERATOR, "operator_version": VERSION, "uniqueId": unique_id},
    }


def _has_text_content(record: dict) -> bool:
    text = record.get("text")
    if isinstance(text, dict):
        text = text.get("content")
    return isinstance(text, str) and bool(text.strip())


def _record_view(entry: _Entry, modality: str) -> _Entry:
    """派生视图只暴露当前路线；其余原生载荷和来源字段保持深拷贝。"""
    view = copy.deepcopy(entry.record)
    digest = hashlib.sha256(entry.sample_id.encode("utf-8")).hexdigest()[:16]
    view["sample_id"] = "%s--view-%s-%s" % (
        _sanitize_token(entry.sample_id, "sample"), digest, modality)
    view["origin_sample_id"] = entry.sample_id
    for other in MODALITY_ORDER:
        if other != modality:
            view[other] = None
    return _Entry(entry.index, view)


def _in_record_pairs(entry: _Entry, unique_id: str, section: dict) -> List[dict]:
    """生成同记录候选，由调用者按原始 seed 应用分组限额。"""
    if entry.source_id is None or not _has_text_content(entry.record):
        return []
    media = [name for name in entry.modalities if name != "text"]
    pairs = []
    for modality in media:
        text_view = _record_view(entry, "text")
        media_view = _record_view(entry, modality)
        pair = _make_pair(text_view, media_view, "source", unique_id, section)
        digest = hashlib.sha256(entry.sample_id.encode("utf-8")).hexdigest()[:16]
        pair_id = "pair-%s--%s--text-%s" % (
            _sanitize_token(entry.sample_id, "sample"), digest, modality)
        pair.update({"pair_id": pair_id, "sample_id": pair_id,
                     "origin_sample_id": entry.sample_id})
        pair["evidence"]["criterion"] = "same_record_source_id"
        pair["evidence"]["value"] = {
            "source_id": entry.source_id, "origin_sample_id": entry.sample_id,
            "media_modality": modality,
        }
        for side in ("left", "right"):
            pair[side]["origin_sample_id"] = entry.sample_id
        pairs.append(pair)
    return pairs


def plan_pairs(records: Sequence[dict], cfg: dict, unique_id: str, now: str) -> PairingResult:
    """先配完整记录，再对剩余分离对象执行三级证据搜索；计数始终以原始记录为准。"""
    validated = validate_config(cfg)
    section = validated["pairing"]
    entries = [_Entry(index, record) for index, record in enumerate(records)]
    start = TIER_ORDER.index(section["priority"])
    active_tiers = TIER_ORDER[start:]

    paired = [False] * len(entries)
    pairs: List[dict] = []
    rejects: List[dict] = []
    same_modality_skipped = 0
    limit_hits = 0
    derived_views = 0
    in_record_pairs = 0

    # 必须先检查全部原始记录，避免较早的分离对象消耗稍后的完整样本。
    for entry in entries:
        native_pairs = _in_record_pairs(entry, unique_id, section)
        if native_pairs:
            limit = section["max_pairs_per_group"]
            accepted, excess = native_pairs[:limit], native_pairs[limit:]
            pairs.extend(accepted)
            paired[entry.index] = True
            in_record_pairs += len(accepted)
            # 文本视图按身份计一次，各媒体各有一个独立视图。
            derived_views += 1 + len(accepted)
            for candidate in excess:
                limit_hits += 1
                rejects.append({
                    "sample_id": entry.sample_id,
                    "peer_sample_id": candidate["right"]["sample_id"],
                    "pair_id": candidate["pair_id"],
                    "modality_pair": candidate["modality_pair"],
                    "reason": "group_pair_limit_exceeded",
                    "group_level": "A",
                    "detectedAt": now,
                })

    for seed in entries:
        if paired[seed.index]:
            continue
        for tier in active_tiers:
            candidates, skipped = _candidates(seed, entries, paired, tier, section)
            same_modality_skipped += skipped
            if not candidates:
                continue
            limit = section["max_pairs_per_group"]
            accepted, excess = candidates[:limit], candidates[limit:]
            for partner in accepted:
                pairs.append(_make_pair(seed, partner, tier, unique_id, section))
                paired[partner.index] = True
            paired[seed.index] = True
            for partner in excess:
                limit_hits += 1
                rejects.append({
                    "sample_id": seed.sample_id,
                    "peer_sample_id": partner.sample_id,
                    "reason": "group_pair_limit_exceeded",
                    "group_level": EVIDENCE_BY_TIER[tier],
                    "detectedAt": now,
                })
            break

    unpaired = []
    tried = [EVIDENCE_BY_TIER[tier] for tier in active_tiers]
    for entry in entries:
        if paired[entry.index]:
            continue
        unpaired.append({
            "sample_id": entry.sample_id,
            "modalities": entry.modalities,
            "source_id": entry.source_id,
            "scene_id": entry.scene_id,
            "reason": "no_candidate",
            "tried_evidence": list(tried),
            "record": entry.record,
        })

    by_evidence = {"A": 0, "B": 0, "C": 0}
    for pair in pairs:
        by_evidence[pair["evidence"]["type"]] += 1

    stats = {
        "valid_records": len(entries),
        "derived_views": derived_views,
        "in_record_pairs": in_record_pairs,
        "paired_records": sum(1 for flag in paired if flag),
        "unpaired_records": len(unpaired),
        "pairs_total": len(pairs),
        "pairs_by_evidence": by_evidence,
        "groups_total": len({pair["group_id"] for pair in pairs}),
        "same_modality_pairs_skipped": same_modality_skipped,
        "group_pair_limit_hits": limit_hits,
    }
    return PairingResult(pairs, unpaired, rejects, stats)
