"""抽样策略：触发原因识别 + 可复算的随机抽检选择。

设计要点：
- 触发原因（风险/低置信/冲突/需人工）由显式字段与阈值规则共同推导，规则可配置；
- 随机抽样使用 `random.Random(random_seed)`，候选按 (record_id, line) 排序后抽样，
  因此相同输入 + 相同配置 + 相同种子一定得到相同集合，不依赖文件遍历顺序；
- 触发命中的记录一定入队，不受 sample_ratio 影响；min/max 只调整总量。
"""
import math
import random
from typing import Any, Dict, List, Optional

_INACTIVE_STRINGS = {"false", "0", "none", "null", "no", "n", ""}


def lookup(record: Dict[str, Any], field: str) -> Any:
    """顶层字段优先，其次 metadata 同名键；找不到返回 None。"""
    if not isinstance(record, dict):
        return None
    if field in record:
        return record[field]
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get(field)
    return None


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def as_reason_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            if isinstance(item, bool):
                continue
            text = str(item).strip()
            if text and text.lower() not in _INACTIVE_STRINGS:
                out.append(text)
        return out
    if isinstance(value, str):
        text = value.strip()
        return [text] if text and text.lower() not in _INACTIVE_STRINGS else []
    return []


def detect_trigger_reasons(record: Dict[str, Any], cfg) -> List[str]:
    """返回排序后的触发原因列表；无触发返回空列表。"""
    reasons = set()

    for field in cfg.trigger_fields:
        value = lookup(record, field)
        if isinstance(value, bool):
            if value:
                reasons.add(field)
        else:
            reasons.update(as_reason_list(value))

    if as_bool(lookup(record, cfg.review_required_field)):
        reasons.add("review_required")

    grade_values = {str(item).strip().upper() for item in cfg.c_grade_values}
    for field in cfg.grade_fields:
        value = lookup(record, field)
        if isinstance(value, str) and value.strip().upper() in grade_values:
            reasons.add("grade_c")
            break

    low_confidence = False
    for field in cfg.low_confidence_fields:
        if as_bool(lookup(record, field)):
            low_confidence = True
            break
    if not low_confidence:
        for field in cfg.confidence_fields:
            value = lookup(record, field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and value < cfg.low_confidence_threshold:
                low_confidence = True
                break
    if low_confidence:
        reasons.add("low_confidence")

    # Downstream safety states are mandatory review triggers.  They are
    # checked explicitly because upstream operators use different field names
    # and these states are not always represented by a generic risk flag.
    entity_status = lookup(record, "entity_consistency_status")
    if str(entity_status).strip().lower() in {"inconsistent", "waiting_manual"}:
        reasons.add("entity_conflict")
    fluency_status = str(lookup(record, "fluency_status") or "").strip().lower()
    if fluency_status in {"model_unavailable", "model_error", "waiting_manual"}:
        reasons.add("model_unavailable" if fluency_status != "waiting_manual"
                    else "upstream_waiting_manual")
    domain_status = str(lookup(record, "domain_status") or "").strip().lower()
    if domain_status in {"unknown_domain", "model_unavailable", "model_error", "waiting_manual"}:
        reasons.add("unknown_domain" if domain_status == "unknown_domain"
                    else ("model_unavailable" if domain_status != "waiting_manual"
                          else "upstream_waiting_manual"))
    upstream_review = str(lookup(record, "review_status") or "").strip().lower()
    if upstream_review in {"waiting_manual", "needs_revision", "rejected"}:
        reasons.add("upstream_waiting_manual")

    for field in cfg.conflict_fields:
        if as_bool(lookup(record, field)):
            reasons.add("conflict")
            break

    return sorted(reasons)


def _sort_key(candidate: Dict[str, Any]):
    return (str(candidate.get("record_id")), int(candidate.get("line") or 0))


def select_for_review(candidates: List[Dict[str, Any]], cfg) -> Dict[str, Any]:
    """按触发原因与抽样比例选出复核队列；结果可复算。

    `candidates` 每项至少包含 `record_id`、`line`、`trigger_reasons`。
    """
    allowed = set(cfg.trigger_reasons)
    triggered: List[Dict[str, Any]] = []
    pool: List[Dict[str, Any]] = []
    for candidate in candidates:
        reasons = set(candidate.get("trigger_reasons") or [])
        if reasons and (reasons & allowed):
            triggered.append(candidate)
        else:
            pool.append(candidate)
    triggered.sort(key=_sort_key)
    pool.sort(key=_sort_key)

    rng = random.Random(cfg.random_seed)
    target = 0
    if pool and cfg.sample_ratio > 0:
        target = min(len(pool), int(math.ceil(len(pool) * cfg.sample_ratio)))
    if cfg.max_sample_count:
        # The cap applies only to ordinary audit sampling. Mandatory triggers
        # are safety evidence and must never be silently truncated.
        target = min(target, max(0, cfg.max_sample_count - len(triggered)))
    indices = sorted(rng.sample(range(len(pool)), target)) if target else []
    chosen = set(indices)
    random_sampled = [pool[i] for i in indices]

    selected = [dict(c, selection_source="triggered") for c in triggered]
    selected.extend(dict(c, selection_source="sampled") for c in random_sampled)

    desired_minimum = cfg.min_sample_count
    if cfg.max_sample_count:
        desired_minimum = min(desired_minimum, max(cfg.max_sample_count, len(triggered)))
    if len(selected) < desired_minimum:
        for index, candidate in enumerate(pool):
            if len(selected) >= desired_minimum:
                break
            if index in chosen:
                continue
            selected.append(dict(candidate, selection_source="sampled"))
            chosen.add(index)

    return {
        "selected": selected,
        "triggered_count": len(triggered),
        "sample_pool_count": len(pool),
        "random_sampled_count": len(random_sampled),
        "audit_sample_count": len(selected) - len(triggered),
        "sampled_count": len(selected),
    }
