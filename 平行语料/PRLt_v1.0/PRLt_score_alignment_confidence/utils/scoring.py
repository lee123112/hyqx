"""算子12 评分核心：真实分项读取、权重归一、加权融合与冲突判定。

设计约束（见 TECHNICAL_DESIGN.md）：
- 只读取上游真实存在的分项分，缺失分项不进分母；
- 所有分项缺失返回 score_missing，绝不补默认分；
- 分项超界/类型错误视为硬错误，不产生伪造分数；
- component_scores 每一项都带 source_operator / source_record_id 供追溯。
"""
from typing import Any, Dict, List, Optional, Tuple

from utils.io import read_jsonl_stream

# 分项读取优先级：路径存在且非 None 即采用；dict 形式必须含数值 "value"。
CANDIDATE_PATHS: Dict[str, List[Tuple[Tuple[str, ...], str]]] = {
    "clause_phrase": [
        (("metadata", "confidence_by_granularity", "clause_phrase"),
         "metadata.confidence_by_granularity.clause_phrase"),
        (("metadata", "component_scores", "clause_phrase"),
         "metadata.component_scores.clause_phrase"),
        (("metadata", "clause_phrase_confidence"), "metadata.clause_phrase_confidence"),
        (("metadata", "confidence"), "metadata.confidence"),
        (("confidence",), "confidence"),
    ],
    "sentence": [
        (("metadata", "confidence_by_granularity", "sentence"),
         "metadata.confidence_by_granularity.sentence"),
        (("metadata", "component_scores", "sentence"),
         "metadata.component_scores.sentence"),
        (("metadata", "sentence_confidence"), "metadata.sentence_confidence"),
        (("metadata", "parent_confidence"), "metadata.parent_confidence"),
        (("parent_confidence",), "parent_confidence"),
    ],
    "paragraph": [
        (("metadata", "confidence_by_granularity", "paragraph"),
         "metadata.confidence_by_granularity.paragraph"),
        (("metadata", "component_scores", "paragraph"),
         "metadata.component_scores.paragraph"),
        (("metadata", "paragraph_confidence"), "metadata.paragraph_confidence"),
        (("paragraph_confidence",), "paragraph_confidence"),
        # 算子11会把09的段落证据保存在父级元数据中。该路径是
        # 明确的契约适配，不从文本或模型分数推导缺失证据。
        (("metadata", "parent_metadata", "paragraph_metadata", "confidence"),
         "metadata.parent_metadata.paragraph_metadata.confidence"),
    ],
}


def _dig(record: dict, path: Tuple[str, ...]) -> Tuple[bool, Any]:
    """按路径取值，返回 (是否存在, 值)。中间遇到非 dict 视为不存在。"""
    current: Any = record
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_valid_score(value: Any) -> bool:
    return _is_number(value) and 0.0 <= float(value) <= 1.0


def pick_pair_id(record: dict, fields: List[str]) -> Optional[str]:
    for field in fields:
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _meta(record: dict) -> dict:
    metadata = record.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _record_id(record: dict) -> Optional[str]:
    return pick_pair_id(record, ["id", "pair_id"])


def _paragraph_keys(meta: dict) -> List[str]:
    keys: List[str] = []
    paragraph_ref = meta.get("paragraph_ref")
    if isinstance(paragraph_ref, str) and paragraph_ref:
        keys.append(paragraph_ref)
    parent_source_ref = meta.get("parent_source_ref")
    if isinstance(parent_source_ref, str) and parent_source_ref:
        keys.append(parent_source_ref)
        head = parent_source_ref.split("-sent-")[0]
        if head and head not in keys:
            keys.append(head)
    return keys


def _sentence_keys(record: dict) -> List[str]:
    meta = _meta(record)
    keys: List[str] = []
    for field in ("parent_pair_id", "parent_source_ref", "pair_id"):
        value = meta.get(field) or record.get(field)
        if isinstance(value, str) and value and value not in keys:
            keys.append(value)
    return keys


def _default_identity(component: str, record: dict, cfg) -> Tuple[str, Optional[str]]:
    """返回 (source_operator, source_record_id) 默认追溯信息。"""
    operator = cfg.component_source_operators.get(component, component)
    meta = _meta(record)
    if component == "clause_phrase":
        return operator, _record_id(record)
    if component == "sentence":
        parent = meta.get("parent_pair_id") or meta.get("parent_source_ref")
        return operator, parent or _record_id(record)
    paragraph_keys = _paragraph_keys(meta)
    return operator, (paragraph_keys[0] if paragraph_keys else _record_id(record))


def _evaluate_candidate(component: str, raw: Any, source_field: str,
                        record: dict, cfg) -> Tuple[str, Optional[dict], Optional[str]]:
    """评估单个候选值，返回 (状态, 分项, 错误原因)。

    状态取值：ok / missing / invalid_type / out_of_range。
    """
    if raw is None:
        return "missing", None, None
    identity_operator, identity_record = _default_identity(component, record, cfg)
    if isinstance(raw, dict):
        if "value" not in raw:
            return "invalid_type", None, f"invalid_type:{component}"
        if raw.get("source_operator") is not None:
            identity_operator = raw["source_operator"]
        if raw.get("source_record_id") is not None:
            identity_record = raw["source_record_id"]
        source_field = raw.get("source_field", source_field)
        raw = raw["value"]
        if raw is None:
            return "missing", None, None
    if not _is_number(raw):
        return "invalid_type", None, f"invalid_type:{component}"
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        return "out_of_range", None, f"score_out_of_range:{component}"
    return "ok", {
        "value": value,
        "source_operator": identity_operator,
        "source_record_id": identity_record,
        "source_field": source_field,
    }, None


def build_evidence_index(path, id_fields: List[str],
                         confidence_fields: List[str]) -> Dict[str, float]:
    """从旁路上游产物（10/09）建立 id → 置信度 索引；越界/缺失值不入索引。"""
    index: Dict[str, float] = {}
    for _lineno, record, error in read_jsonl_stream(path):
        if error or not isinstance(record, dict):
            continue
        identity = pick_pair_id(record, id_fields)
        if not identity:
            continue
        confidence = None
        for field in confidence_fields:
            if _is_valid_score(record.get(field)):
                confidence = float(record[field])
                break
        if confidence is None:
            meta = _meta(record)
            for field in confidence_fields:
                if _is_valid_score(meta.get(field)):
                    confidence = float(meta[field])
                    break
        if confidence is not None:
            index[identity] = confidence
    return index


def resolve_components(record: dict, cfg, sentence_index: Optional[dict] = None,
                       paragraph_index: Optional[dict] = None
                       ) -> Tuple[Dict[str, dict], List[str]]:
    """读取启用粒度的真实分项，返回 (available_components, errors)。"""
    components: Dict[str, dict] = {}
    errors: List[str] = []
    for component in cfg.enabled_granularities:
        resolved: Optional[dict] = None
        component_error: Optional[str] = None
        for path, source_field in CANDIDATE_PATHS.get(component, []):
            exists, raw = _dig(record, path)
            if not exists:
                continue
            status, component_value, error = _evaluate_candidate(
                component, raw, source_field, record, cfg)
            if status == "missing":
                continue
            if error:
                component_error = error
                break
            resolved = component_value
            break
        if component_error is not None:
            errors.append(component_error)
            continue
        if resolved is None:
            resolved = _from_evidence(component, record, cfg,
                                      sentence_index, paragraph_index)
        if resolved is not None:
            components[component] = resolved
    return components, errors


def _from_evidence(component: str, record: dict, cfg,
                   sentence_index: Optional[dict],
                   paragraph_index: Optional[dict]) -> Optional[dict]:
    """从旁路证据索引补齐缺失的句子/段落分项；找不到则不补分。"""
    if component == "sentence" and sentence_index:
        candidates = _sentence_keys(record)
        index = sentence_index
    elif component == "paragraph" and paragraph_index:
        candidates = _paragraph_keys(_meta(record))
        index = paragraph_index
    else:
        return None
    for key in candidates:
        if key in index:
            return {
                "value": float(index[key]),
                "source_operator": cfg.component_source_operators.get(component, component),
                "source_record_id": key,
                "source_field": f"evidence_index:{component}",
            }
    return None


def fuse_components(components: Dict[str, dict], cfg) -> Tuple[Optional[float], Dict[str, float]]:
    """按启用粒度的有效权重重新归一并加权融合；无可算分项返回 (None, {})。"""
    used = {name: cfg.component_weights[name]
            for name in cfg.enabled_granularities if name in components}
    total = sum(used.values())
    if total <= 0:
        return None, {}
    confidence = sum(components[name]["value"] * weight
                     for name, weight in used.items()) / total
    used_weights = {name: round(weight / total, 6) for name, weight in used.items()}
    return round(confidence, 6), used_weights


def candidate_score(candidate: dict, record: dict, record_components: Dict[str, dict],
                    cfg) -> Optional[float]:
    """对单个候选计算融合分：候选自身分项覆盖记录级分项，缺失则继承记录级。"""
    merged: Dict[str, dict] = dict(record_components)
    if isinstance(candidate, dict):
        candidate_components, _ = resolve_components(
            {"id": record.get("id"), "metadata": candidate}, cfg)
        merged.update(candidate_components)
    if not merged:
        return None
    confidence, _ = fuse_components(merged, cfg)
    return confidence
