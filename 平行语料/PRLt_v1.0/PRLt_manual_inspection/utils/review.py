"""人工复核结果校验、脱敏与幂等标识。

边界（与 TECHNICAL_DESIGN §3 一致）：
- 本模块**不实现**标注工作台、账号认证和人工身份真实性校验；
- 只校验工程侧明确回写的 review payload 字段是否完整、状态是否合法；
- 同一记录出现互相冲突的结果时，调用方不得自动覆盖，只标记 `review_conflict`。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 结果字段：既用于校验，也用于从原始条目中剔除后判断是否带额外字段。
_RESULT_KEYS = {
    "record_id", "id", "review_status", "reviewer_id",
    "reviewed_at", "review_reason", "reviewed_grade",
}


def _pick_record_id(raw: Dict[str, Any], record_id_fields: List[str]) -> Optional[str]:
    for field in record_id_fields:
        if field not in raw:
            continue
        value = raw[field]
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _is_iso_datetime(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(candidate)
        return True
    except ValueError:
        return False


def validate_review_entry(raw: Any, cfg) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """校验单条人工结果；返回 (规范化条目或 None, 稳定原因码列表)。

    一次性收集全部缺失/非法原因，便于异常清单里一条记录带多个原因。
    """
    if not isinstance(raw, dict):
        return None, ["invalid_review_record"]

    reasons: List[str] = []

    record_id = _pick_record_id(raw, cfg.record_id_fields)
    if not record_id:
        reasons.append("missing_field:record_id")

    status_raw = raw.get("review_status")
    status: Optional[str] = None
    if status_raw is None or (isinstance(status_raw, str) and not status_raw.strip()):
        reasons.append("missing_field:review_status")
    elif not isinstance(status_raw, str):
        reasons.append("invalid_review_status:%s" % status_raw)
    else:
        status = status_raw.strip().lower()
        if status not in cfg.review_statuses:
            reasons.append("invalid_review_status:%s" % status)
            status = None

    reviewer_raw = raw.get("reviewer_id")
    reviewer = None
    if isinstance(reviewer_raw, str) and reviewer_raw.strip():
        reviewer = reviewer_raw.strip()
    elif reviewer_raw is not None and not isinstance(reviewer_raw, str):
        reviewer = str(reviewer_raw).strip() or None
    if cfg.require_reviewer and not reviewer:
        reasons.append("missing_field:reviewer_id")

    reviewed_at_raw = raw.get("reviewed_at")
    reviewed_at = reviewed_at_raw.strip() if isinstance(reviewed_at_raw, str) else None
    if cfg.require_reviewed_at and not reviewed_at:
        reasons.append("missing_field:reviewed_at")
    elif reviewed_at and cfg.validate_reviewed_at and not _is_iso_datetime(reviewed_at):
        reasons.append("invalid_reviewed_at")

    reason_raw = raw.get("review_reason")
    review_reason = reason_raw.strip() if isinstance(reason_raw, str) else None
    if cfg.require_review_reason and not review_reason:
        reasons.append("missing_field:review_reason")

    reviewed_grade_raw = raw.get("reviewed_grade")
    if reviewed_grade_raw is None:
        reviewed_grade = None
    elif isinstance(reviewed_grade_raw, str):
        reviewed_grade = reviewed_grade_raw.strip() or None
    else:
        reviewed_grade = str(reviewed_grade_raw).strip() or None

    if reasons:
        return None, reasons

    entry = {
        "record_id": record_id,
        "review_status": status,
        "reviewer_id": reviewer,
        "reviewed_at": reviewed_at,
        "review_reason": review_reason,
        "reviewed_grade": reviewed_grade,
    }
    extra = {key: value for key, value in raw.items() if key not in _RESULT_KEYS}
    if extra:
        entry["extra"] = extra
    return entry, []


def review_identity(entry: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    """用于判断两次提交是否完全一致（幂等）或互相冲突。"""
    return (
        str(entry.get("review_status") or ""),
        str(entry.get("reviewer_id") or ""),
        str(entry.get("reviewed_at") or ""),
        str(entry.get("review_reason") or ""),
        str(entry.get("reviewed_grade") or ""),
    )


def mask_reviewer(value: Optional[str], cfg) -> Optional[str]:
    """按需脱敏复核人标识；默认保留首尾各 2 个字符，其余以 *** 代替。"""
    if value is None:
        return None
    if not cfg.mask_reviewer_id:
        return value
    text = str(value)
    if len(text) <= 4:
        return "***"
    return text[:2] + "***" + text[-2:]


def truncate_reason(value: Optional[str], cfg) -> Tuple[Optional[str], bool]:
    """限制人工备注长度，返回 (文本, 是否被截断)。日志永远不会写入备注内容。"""
    if value is None:
        return None, False
    if len(value) <= cfg.max_reason_chars:
        return value, False
    return value[: cfg.max_reason_chars], True
