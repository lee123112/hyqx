"""Feedback lifecycle endpoints (pending → processed → verified)."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Query

from ..core.envelope import ApiError, ok
from ..core.ids import now
from ..store import db

router = APIRouter(prefix="/api/v1/eval/feedback", tags=["feedback"])

STATUSES = ("pending", "processed", "verified")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _measurements(task: dict[str, Any] | None, sample_id: str) -> Any:
    if not isinstance(task, dict):
        return None
    result = task.get("result")
    if not isinstance(result, dict):
        return None
    for sample in result.get("samples") or []:
        if str(sample.get("id")) == str(sample_id):
            return sample.get("measurements")
    return None


@router.get("")
def list_feedback(
    status: str = Query(""),
    taskId: str = Query(""),  # noqa: N803 - frontend field name
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),  # noqa: N803
) -> dict:
    status = status.strip()
    if status and status not in STATUSES:
        raise ApiError(422, "REQUEST_INVALID", "反馈状态仅支持 pending/processed/verified")
    items, total = db.list_feedback(status=status, task_id=taskId.strip(),
                                    page=page, page_size=pageSize)
    return ok({"items": items, "total": total, "page": page, "pageSize": pageSize})


@router.get("/{feedback_id}")
def get_feedback(feedback_id: str) -> dict:
    item = db.get_feedback(feedback_id)
    if not item:
        raise ApiError(404, "NOT_FOUND", "反馈记录不存在")
    return ok(item)


def _verification_errors(feedback: dict[str, Any], original: dict[str, Any],
                         verification: dict[str, Any] | None, sample_id: str) -> list[str]:
    errors: list[str] = []
    if verification is None:
        return ["复评任务不存在"]
    if str(verification.get("parentId") or "") != str(feedback.get("taskId") or ""):
        errors.append("复评任务不是该反馈所属任务的子任务")
    if verification.get("status") != "completed":
        errors.append("复评任务尚未完成")
    for field in ("type", "mode", "validationVersion", "seed"):
        if verification.get(field) != original.get(field):
            errors.append(f"复评任务的 {field} 与原任务不一致")
    if _dump(verification.get("config")) != _dump(original.get("config")):
        errors.append("复评任务的判定配置与原任务不一致")
    for field in ("baselineModel", "candidateModel"):
        if verification.get(field) != original.get(field):
            errors.append(f"复评任务的 {field} 与原任务不一致")
    passed = False
    result = verification.get("result")
    if isinstance(result, dict):
        for sample in result.get("samples") or []:
            if str(sample.get("id")) == sample_id:
                passed = sample.get("passed") is True
                break
    if not passed:
        errors.append("复评任务中该样本仍未达标")
    return errors


@router.patch("/{feedback_id}")
def update_feedback(feedback_id: str, payload: dict[str, Any] = Body(...)) -> dict:
    feedback = db.get_feedback(feedback_id)
    if not feedback:
        raise ApiError(404, "NOT_FOUND", "反馈记录不存在")
    if not isinstance(payload, dict):
        raise ApiError(422, "REQUEST_INVALID", "请求体格式不正确")
    note = payload.get("note")
    if not isinstance(note, str) or not note.strip():
        raise ApiError(422, "REQUEST_INVALID", "请填写处理说明，保留参数变更依据")
    parameters_after = payload.get("parametersAfter")
    if not isinstance(parameters_after, dict):
        raise ApiError(422, "REQUEST_INVALID", "处理参数须为有效的 JSON 对象")
    verification_id = payload.get("verificationId")
    if verification_id in ("", None):
        verification_id = None
    elif not isinstance(verification_id, str):
        raise ApiError(422, "REQUEST_INVALID", "复评任务 ID 格式不正确")

    original = db.get_task(feedback["taskId"])
    if original is None:
        raise ApiError(404, "NOT_FOUND", "反馈所属任务不存在")
    sample_id = str(feedback.get("sampleId") or "")

    verified = False
    if verification_id:
        verification = db.get_task(verification_id)
        errors = _verification_errors(feedback, original, verification, sample_id)
        if errors:
            raise ApiError(422, "VERIFICATION_INVALID", "；".join(errors),
                           details={"errors": errors})
        verified = True

    updated = dict(feedback)
    updated["status"] = "verified" if verified else "processed"
    updated["note"] = note.strip()
    updated["parametersAfter"] = parameters_after
    updated["verificationId"] = verification_id
    updated["performanceBefore"] = _measurements(original, sample_id)
    updated["performanceAfter"] = (
        _measurements(db.get_task(verification_id), sample_id) if verified else None
    )
    updated["updatedAt"] = now()
    db.save_feedback(updated)
    return ok(db.get_feedback(feedback_id))
