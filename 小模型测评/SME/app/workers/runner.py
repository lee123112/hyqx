"""Background evaluation runner.

Tasks are executed on a small dedicated thread pool so HTTP handlers never
block the event loop.  Pause/resume is coordinated with a per-task
``threading.Condition``; logs and progress are persisted through
:mod:`app.store.db` so they survive a page refresh.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..core.config import settings
from ..core.envelope import ApiError
from ..core.ids import now
from ..core.security import sanitize_text
from ..engines import service as engine_service
from ..eval import rules, templates
from ..store import db

_MODE_LABEL = {"demo": "模拟执行", "measured": "导入实测分析"}


@dataclass
class TaskControl:
    """Pause/resume switch shared between the API and the worker thread."""

    paused: bool = False
    stopped: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)


_LOCK = threading.RLock()
_CONTROLS: dict[str, TaskControl] = {}
_RUNNING: set[str] = set()
_EXECUTOR: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=max(1, settings.max_workers),
                thread_name_prefix="eval-runner",
            )
        return _EXECUTOR


def is_active(task_id: str) -> bool:
    with _LOCK:
        return task_id in _RUNNING


def _measurements_complete(type_: str, samples: list[dict[str, Any]]) -> bool:
    keys = [metric["key"] for metric in templates.TEMPLATES[type_]["metrics"]]
    for sample in samples:
        if not isinstance(sample, dict):
            return False
        measurements = sample.get("measurements")
        if not isinstance(measurements, dict):
            return False
        for model in ("baseline", "candidate"):
            values = measurements.get(model)
            if not isinstance(values, dict):
                return False
            for key in keys:
                if not rules.is_num(values.get(key)):
                    return False
    return True


def start(task_id: str, *, resume: bool = False) -> bool:
    """Mark a task running and submit it to the worker pool.

    Returns ``False`` when the task is unknown, finished or already running.
    """
    with _LOCK:
        if task_id in _RUNNING:
            return False
        task = db.get_task(task_id)
        if not task:
            return False
        if task.get("status") == "completed":
            return False
        if not resume:
            task["step"] = 0
            task["result"] = None
            task["completedAt"] = None
        task["status"] = "running"
        task["error"] = ""
        db.save_task(task)
        control = _CONTROLS.get(task_id)
        if control is None:
            control = TaskControl()
            _CONTROLS[task_id] = control
        control.paused = False
        control.stopped = False
        _RUNNING.add(task_id)
        try:
            _executor().submit(_execute, task_id, resume)
        except RuntimeError:  # executor shut down
            _RUNNING.discard(task_id)
            _CONTROLS.pop(task_id, None)
            return False
    return True


def pause(task_id: str) -> bool:
    """Ask a running worker to wait between its next steps."""
    with _LOCK:
        control = _CONTROLS.get(task_id)
    if control is None:
        return False
    with control.condition:
        control.paused = True
        control.condition.notify_all()
    return True


def resume(task_id: str) -> bool:
    with _LOCK:
        control = _CONTROLS.get(task_id)
    if control is None:
        return False
    with control.condition:
        control.paused = False
        control.condition.notify_all()
    return True


def _cleanup(task_id: str) -> None:
    with _LOCK:
        _RUNNING.discard(task_id)
        _CONTROLS.pop(task_id, None)


def _fail(task_id: str, message: str) -> None:
    message = sanitize_text(str(message))[:300]
    task = db.get_task(task_id)
    if task is None:
        return
    task["status"] = "failed"
    task["error"] = message
    db.append_log(task_id, "错误", f"评测失败 · {message}")
    db.save_task(task)


def _wait_while_paused(task_id: str, task: dict[str, Any]) -> bool:
    """Block while the task is paused.  Returns ``True`` if cancelled."""
    with _LOCK:
        control = _CONTROLS.get(task_id)
    if control is None or not control.paused:
        return False
    task["status"] = "paused"
    db.save_task(task)
    with control.condition:
        while control.paused and not control.stopped:
            control.condition.wait(timeout=0.2)
        stopped = control.stopped
    if not stopped:
        task["status"] = "running"
        db.save_task(task)
    return stopped


def _execute(task_id: str, resume: bool) -> None:
    task = db.get_task(task_id)
    if not task:
        _cleanup(task_id)
        return
    try:
        type_ = str(task.get("type") or "")
        template = templates.TEMPLATES.get(type_)
        if not template:
            raise ApiError(422, "CONFIG_INVALID", "请选择有效的评测类型")
        steps = list(template.get("steps") or [])
        samples = task.get("samples") if isinstance(task.get("samples"), list) else []
        start_index = int(task.get("step") or 0) if resume else 0
        start_index = max(0, min(start_index, len(steps)))
        if not resume:
            task["step"] = start_index
        candidate = sanitize_text(str(task.get("candidateModel") or "待评估模型"))
        if task.get("_needsEngine"):
            candidate = f"{candidate}（引擎即时测量）"
        mode_label = _MODE_LABEL.get(str(task.get("mode") or "demo"), "模拟执行")

        if start_index == 0:
            db.append_log(
                task_id, "开始",
                f"评测开始 · {sanitize_text(str(task.get('baselineModel') or '基线'))}"
                f" → {candidate} · {len(samples)} 条",
            )

        for index in range(start_index, len(steps)):
            if _wait_while_paused(task_id, task):
                _cleanup(task_id)
                return
            db.append_log(
                task_id, "处理",
                f"{steps[index]} · {candidate} · {len(samples)} 条 · {mode_label}",
            )
            if task.get("mode") == "demo":
                delay = float(settings.demo_step_seconds or 0)
                if delay > 0:
                    time.sleep(delay)
            task["step"] = index + 1
            with _LOCK:
                control = _CONTROLS.get(task_id)
            task["status"] = "paused" if (control and control.paused) else "running"
            db.save_task(task)

        config = task.get("config") if isinstance(task.get("config"), dict) else {}
        result = _evaluate_task(task, type_, samples, config)

        _write_feedback(task_id, type_, result)
        task["result"] = result
        task["status"] = "completed"
        task["step"] = len(steps)
        task["completedAt"] = now()
        task["error"] = ""
        issues = int((result.get("summary") or {}).get("issues") or 0)
        db.append_log(
            task_id, "完成",
            f"评测完成 · {issues} 条问题样本 · 已生成质量报告与反馈记录",
        )
        db.save_task(task)
    except ApiError as exc:
        _fail(task_id, exc.message)
    except rules.RuleError as exc:
        _fail(task_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - worker must never crash silently
        _fail(task_id, f"{exc.__class__.__name__}: {exc}")
    finally:
        _cleanup(task_id)


def _evaluate_task(task: dict[str, Any], type_: str, samples: list[dict[str, Any]],
                   config: dict[str, Any]) -> dict[str, Any]:
    from ..experiments import training
    if task.get('protocol') is not None:
        record = training.run_training_protocol(type_, task['protocol'])
        return training.training_evaluation(type_, record)
    needs_engine = bool(task.get('_needsEngine')) or (
        task.get('mode') == 'measured' and
        (type_ == 'benchmark' or not _measurements_complete(type_, samples)))
    audit = None
    if needs_engine:
        measured = engine_service.measure_samples(type_, samples, config,
            seed=engine_service.validate_seed(task.get('seed', 42)), strict=True)
        samples = engine_service.apply_measurements(samples, measured)
        audit = engine_service.audit_block(measured)
    elif task.get('mode') == 'measured':
        audit = engine_service.imported_audit(type_, samples, seed=task.get('seed', 42))
    return training.evaluate_measured(type_, samples, config, audit)


def _write_feedback(task_id: str, type_: str, result: dict[str, Any]) -> None:
    index = 0
    for sample in result.get("samples") or []:
        if sample.get("passed"):
            continue
        index += 1
        issues = sample.get("issues") or []
        issue = "、".join(str(item) for item in issues)
        if not issue:
            grade = sample.get("grade")
            issue = f"{grade} 级样本需按分级策略应用"
        item = {
            "id": f"{task_id}-F{index}",
            "taskId": task_id,
            "sampleId": sample.get("id"),
            "issue": issue,
            "sourceStep": sample.get("sourceStep"),
            "suggestion": rules.suggestion(type_, sample),
            "parameters": sample.get("parameters") or {},
            "status": "pending",
            "note": "",
            "parametersAfter": None,
            "verificationId": None,
            "performanceBefore": None,
            "performanceAfter": None,
            "createdAt": now(),
            "updatedAt": now(),
        }
        db.save_feedback(item)
