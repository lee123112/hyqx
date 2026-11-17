"""Evaluation task endpoints: CRUD, execution control, logs and SSE."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import StreamingResponse

from ..core.envelope import ApiError, ok
from ..eval import task_factory
from ..store import db
from ..workers import runner

router = APIRouter(prefix="/api/v1/eval", tags=["eval-tasks"])

REVIEW_STATUSES = ("pending", "confirmed", "returned")
TERMINAL_STATUSES = ("completed", "failed")


def _get_task(task_id: str) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise ApiError(404, "NOT_FOUND", "评测任务不存在")
    return task


@router.get("/tasks")
def list_tasks(
    keyword: str = Query(""),
    type_: str = Query("", alias="type"),
    status: str = Query(""),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),  # noqa: N803 - frontend field name
) -> dict:
    items, total = db.list_tasks(
        keyword=keyword.strip(), type_=type_.strip(), status=status.strip(),
        page=page, page_size=pageSize,
    )
    return ok({"items": items, "total": total, "page": page, "pageSize": pageSize})


@router.post("/tasks")
def create_task(payload: dict[str, Any] = Body(...)) -> dict:
    auto_run = payload.get("autoRun")
    if auto_run is not None and not isinstance(auto_run, bool):
        raise ApiError(422, "REQUEST_INVALID", "autoRun 必须为布尔值")
    task = task_factory.build_task(payload)
    task_factory.save_new_task(task)
    if auto_run:
        runner.start(task["id"])
    return ok(_get_task(task["id"]))


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    return ok(_get_task(task_id))


@router.post("/tasks/{task_id}/run")
def run_task(task_id: str) -> dict:
    task = _get_task(task_id)
    status = task.get("status")
    if status == "completed":
        raise ApiError(409, "JOB_RUNNING", "任务已完成，如需重新评测请创建复评任务")
    if status == "running" and runner.is_active(task_id):
        raise ApiError(409, "JOB_RUNNING", "任务正在运行，请勿重复启动")
    if status == "paused":
        db.append_log(task_id, "等待", "评测任务继续运行")
        if runner.is_active(task_id):
            runner.resume(task_id)
        else:
            runner.start(task_id, resume=True)
    elif not runner.start(task_id, resume=False):
        raise ApiError(409, "JOB_RUNNING", "任务正在运行，请勿重复启动")
    return ok(_get_task(task_id))


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str) -> dict:
    task = _get_task(task_id)
    status = task.get("status")
    if status == "paused":
        return ok(task)
    if status != "running":
        raise ApiError(422, "REQUEST_INVALID", "仅运行中的任务可以暂停")
    runner.pause(task_id)
    task["status"] = "paused"
    db.save_task(task)
    db.append_log(task_id, "等待", "评测任务已暂停，等待恢复")
    return ok(_get_task(task_id))


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict:
    task = _get_task(task_id)
    status = task.get("status")
    if status == "running":
        return ok(task)
    if status != "paused":
        raise ApiError(422, "REQUEST_INVALID", "仅已暂停的任务可以恢复")
    if not runner.resume(task_id):
        runner.start(task_id, resume=True)
    task["status"] = "running"
    db.save_task(task)
    db.append_log(task_id, "等待", "评测任务已恢复")
    return ok(_get_task(task_id))


@router.post("/tasks/{task_id}/rerun")
def rerun_task(task_id: str, payload: dict[str, Any] | None = Body(default=None)) -> dict:
    parent = _get_task(task_id)
    body = payload or {}
    if not isinstance(body, dict):
        raise ApiError(422, "REQUEST_INVALID", "请求体格式不正确")
    mode = str(body.get("mode") or parent.get("mode") or "demo")
    request: dict[str, Any] = {
        "name": body.get("name") or f"{parent.get('name') or parent['id']} · 复评",
        "type": parent.get("type"),
        "mode": mode,
        "source": {"kind": "rerun"},
        "parentId": parent["id"],
        "config": body.get("config") or parent.get("config"),
        "baselineConfig": body.get("baselineConfig", parent.get("baselineConfig")),
        "baselineModel": parent.get("baselineModel"),
        "candidateModel": parent.get("candidateModel"),
        "downstreamTask": parent.get("downstreamTask"),
        "validationVersion": parent.get("validationVersion"),
        "seed": parent.get("seed"),
        "modelId": parent.get("modelId"),
        "modelVersion": parent.get("modelVersion"),
        "datasetId": parent.get("datasetId"),
        "datasetName": parent.get("datasetName"),
    }
    if body.get("samples") is not None:
        request["samples"] = body["samples"]
    if parent.get('protocol') is not None:
        request['protocol'] = body.get('protocol', parent['protocol'])
    child = task_factory.build_task(request)
    task_factory.save_new_task(child)
    if body.get("autoRun"):
        runner.start(child["id"])
    return ok(_get_task(child["id"]))


@router.patch("/tasks/{task_id}/samples/{sample_id}")
def review_sample(
    task_id: str,
    sample_id: str,
    payload: dict[str, Any] = Body(...),
) -> dict:
    task = _get_task(task_id)
    result = task.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("samples"), list):
        raise ApiError(422, "REQUEST_INVALID", "任务尚未完成，无法进行样本复核")
    status = payload.get("status") or payload.get("reviewStatus")
    if status not in REVIEW_STATUSES:
        raise ApiError(422, "REQUEST_INVALID", "复核状态仅支持 pending/confirmed/returned")
    note_value = payload.get("note", "")
    if note_value is None:
        note_value = ""
    if not isinstance(note_value, (str, int, float)):
        raise ApiError(422, "REQUEST_INVALID", "复核说明格式不正确")
    for sample in result["samples"]:
        if str(sample.get("id")) != sample_id:
            continue
        sample["review"] = {"status": status, "note": str(note_value)}
        db.save_task(task)
        return ok(task)
    raise ApiError(404, "NOT_FOUND", "样本不存在")


@router.get("/tasks/{task_id}/logs")
def task_logs(task_id: str, after: int = Query(0, ge=0)) -> dict:
    _get_task(task_id)
    items = db.list_logs(task_id, after)
    last_seq = int(items[-1]["seq"]) if items else int(after)
    return ok({"taskId": task_id, "after": after, "items": items, "lastSeq": last_seq})


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    _get_task(task_id)

    async def event_stream():
        last_seq = 0
        last_status: str | None = None
        waited = 0.0
        while True:
            if await request.is_disconnected():
                return
            task = db.get_task(task_id)
            if task is None:
                data = json.dumps({"taskId": task_id, "status": "deleted"},
                                  ensure_ascii=False)
                yield f"event: done\ndata: {data}\n\n"
                return
            status = str(task.get("status") or "")
            if status != last_status:
                payload = {
                    "taskId": task_id, "status": status,
                    "step": task.get("step"), "error": task.get("error") or "",
                }
                yield f"event: status\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_status = status
            logs = db.list_logs(task_id, last_seq)
            for entry in logs:
                last_seq = int(entry["seq"])
                yield f"event: log\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"
            if status in TERMINAL_STATUSES:
                payload = {"taskId": task_id, "status": status,
                           "step": task.get("step")}
                yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            waited += 0.4
            if waited > 300:
                payload = {"taskId": task_id, "status": status, "reason": "timeout"}
                yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            await asyncio.sleep(0.4)
            yield ": heartbeat\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str) -> dict:
    task = _get_task(task_id)
    if task.get("status") not in ("draft", "failed"):
        raise ApiError(422, "REQUEST_INVALID", "仅草稿或失败状态的任务可以删除")
    db.delete_task(task_id)
    return ok({"id": task_id, "deleted": True})
