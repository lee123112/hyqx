"""算子 25「管理人工复核任务」HTTP 服务：只实现三个正式接口。

| 接口 | 作用 |
| --- | --- |
| ``POST /uploadFile?uniqueId=`` | 上传待复核队列（支持 zip 包） |
| ``POST /predict`` | [v4.2] 双协议：multipart/form-data → 形态一（单文件流）；JSON → 形态二（阶段文件传递） |
| ``POST /removeFile?uniqueId=`` | 删除任务目录（幂等） |

[v4.2] 形态二响应统一为 ``data.outputFile``（crossmodal-files-v1）+ ``data.result``，
不再区分阶段内/阶段末，也不再返回 ``lineageFile``（血缘输出已停用）。

**不实现 ``/downloadFile``**：未匹配到的路径统一返回
``{"code": 1, "msg": "接口不存在", "data": {}}`` 且 HTTP 404。

所有响应外层为 ``{"code": 0 或 1, "msg": "...", "data": {...}}``；
输出只写入 ``OUTPUT_DIR/<uniqueId>/``，拒绝空/非法/绝对路径/目录穿越的 uniqueId。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import dispatch as dp
from utils import io as io_utils
from utils import transport
from utils import form1_package
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.schemas import ConfigError, PredictRequest, validate_overrides

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))
ZIP_MAGIC = b"PK\x03\x04"
INPUT_DIR_NAME = "input"
STAGING_DIR_NAME = ".upload_staging"

app = FastAPI(
    title="25 管理人工复核任务",
    version=dp.OPERATOR_VERSION,
    description="表1 #25 手动触发的人工复核派单、结论回写与样本隔离（三接口）",
)


# --------------------------------------------------------------------------- #
# 统一信封与异常
# --------------------------------------------------------------------------- #

def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": int(code), "msg": msg, "data": data if data is not None else {}}


@app.exception_handler(RequestValidationError)
async def validation_handler(_request, exc: RequestValidationError) -> JSONResponse:
    details = "; ".join(
        "%s %s" % (".".join(str(part) for part in item.get("loc", ())), item.get("msg"))
        for item in exc.errors()[:5])
    return JSONResponse(status_code=422,
                        content=envelope(1, "请求参数不合法：%s" % details))


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        return JSONResponse(status_code=404, content=envelope(1, "接口不存在", {}))
    return JSONResponse(status_code=exc.status_code,
                        content=envelope(1, str(exc.detail), {}))


def output_root() -> Path:
    """输出根目录：运行时读取 ``OUTPUT_DIR``，缺省 ``./output``。"""
    return io_utils.resolve_root(os.environ.get("OUTPUT_DIR", "output"))


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

def _store_upload(task_dir: Path, payload: bytes, filename: str) -> List[str]:
    """把上传内容落到 ``task/input``（zip 走安全解压），返回包内相对路径清单。"""
    staging = task_dir / STAGING_DIR_NAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    written: List[str] = []
    try:
        if payload[:4] == ZIP_MAGIC or (filename or "").lower().endswith(".zip"):
            written = io_utils.safe_extract_zip(io.BytesIO(payload), staging)
        else:
            relative = io_utils.normalize_zip_member_name(
                filename or dp.MAIN_NAME)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            written = [relative]

        manifest = transport.load_manifest(staging)
        entry, entry_relative = transport.resolve_entrypoint(staging, manifest)

        if manifest is None:
            manifest = transport.build_manifest(
                entry_relative, [{"path": name, "role": "uploaded"} for name in written])
            transport.write_manifest(staging, manifest)

        input_dir = task_dir / INPUT_DIR_NAME
        if input_dir.exists():
            shutil.rmtree(input_dir)
        staging.rename(input_dir)
        return written
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


@app.post("/uploadFile")
def upload_file(file: UploadFile,
                uniqueId: str = Query(..., description="任务唯一标识")) -> Dict[str, Any]:
    """上传待复核队列（zip 包或多文件）。"""
    try:
        value = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return envelope(1, str(exc), {})
    try:
        with io_utils.TaskLock(output_root(), value):
            task = io_utils.task_dir(output_root(), value)
            payload = file.file.read(MAX_UPLOAD_BYTES + 1)
            if not payload:
                return envelope(1, "上传内容为空", {})
            if len(payload) > MAX_UPLOAD_BYTES:
                return envelope(1, "上传内容超过限额 %d 字节" % MAX_UPLOAD_BYTES, {})
            written = _store_upload(task, payload, file.filename or "")
            entrypoint = transport.load_manifest(task / INPUT_DIR_NAME)["entrypoint"]
    except (ValueError, transport.TransportError, OSError, UnicodeDecodeError) as exc:
        return envelope(1, "上传失败：%s" % exc, {})
    except io_utils.TaskBusyError as exc:
        return envelope(1, str(exc), {})
    return envelope(0, "上传成功", {
        "uniqueId": value,
        "entrypoint": entrypoint,
        "files": sorted(written),
        "fileCount": len(written),
    })


# --------------------------------------------------------------------------- #
# 推理
# --------------------------------------------------------------------------- #

@app.post("/predict")
async def predict(request: Request):
    """[v4.2] 双协议入口：multipart/form-data → 形态一（单文件流）；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    # [D3] 形态二入口补 detection/operatorId 冲突校验（§2.7 公共约定）
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return JSONResponse(status_code=422, content=envelope(
            1, "detection 与 operatorId 同时出现时取值必须一致", {}))
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return envelope(1, "请求体必须是 JSON 对象", {})
    if not isinstance(raw, dict):
        return envelope(1, "请求体必须是 JSON 对象", {})
    try:
        payload = PredictRequest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - 统一信封
        return JSONResponse(status_code=422,
                            content=envelope(1, "请求参数不合法：%s" % str(exc)[:300], {}))
    return _predict_stage(payload)


def _business_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态二 ``data.result``：业务摘要，剔除运行目录等内部绝对路径。"""
    report = result.get("report") or {}
    return {
        "operator": result.get("operator"),
        "operatorVersion": result.get("operatorVersion"),
        "stageId": result.get("stageId"),
        "uniqueId": result.get("uniqueId"),
        "runId": result.get("runId"),
        "status": result.get("status"),
        "reviewStatus": result.get("reviewStatus"),
        "entrypoint": result.get("entrypoint"),
        "inputFile": result.get("inputFile"),
        "statistics": result.get("statistics"),
        "artifacts": result.get("artifacts"),
        "report": {"status": report.get("status"),
                   "review": report.get("review"),
                   "handover": report.get("handover")},
    }


def _predict_stage(payload: PredictRequest):
    """形态二：阶段文件传递协议。``isLastOperator`` 保留严格校验但不再改变响应形态。"""
    try:
        overrides = validate_overrides(payload.config)
        result = inference.run(input_file=payload.inputFile, unique_id=payload.uniqueId,
                               output_root=output_root(), overrides=overrides)
        # [v4.2] mty_compliance 已停用：prepare_stage_result 调用注释存档（不再做阶段归档）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
    except (ConfigError, inference.InputError, io_utils.TaskBusyError) as exc:
        return envelope(1, str(exc), {})
    except transport.OutputTooLargeError as exc:
        return envelope(1, str(exc), {})
    except Exception as exc:  # noqa: BLE001 - 接口必须返回统一信封
        return envelope(1, "%s: %s" % (type(exc).__name__, str(exc)[:400]), {})

    try:
        file_object = inference.stage_file_object(result)
    except transport.OutputTooLargeError as exc:
        return envelope(1, str(exc), {})
    except transport.TransportError as exc:
        return envelope(1, "打包输出失败：%s" % exc, {})
    return envelope(0, "人工复核任务处理完成", {
        "operator": dp.OPERATOR_NAME,
        "status": result["status"],
        "outputFile": file_object,
        "result": _business_result(result),
    })


# --------------------------------------------------------------------------- #
# 形态一：单文件流（MTY V2，[v4.2] 新增）
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M25/25/英文ID/目录名
DETECTION_ALIASES = {"m25", "25", "manage_manual_review_task",
                     "cma_manage_manual_review_task",
                     "manual_review_task_management"}

#: 形态一白名单标量（未知字段忽略）；与 schemas.BusinessOverrides 保持一致
_FORM1_SCALARS = {
    "enabled": "bool",
    "auto_dispatch": "bool",
    "manual_platform_available": "bool",
    "require_reviewer_identity": "bool",
    "allow_degraded": "bool",
}


def _check_detection(request: Request) -> None:
    """detection/operatorId 宽松别名校验：缺失或与算子别名一致放行；不一致报参数错误。"""
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        raise ValueError("detection 与 operatorId 同时出现时取值必须一致")
    value = (detection or operator_id or "").strip().lower()
    if value and value not in DETECTION_ALIASES:
        raise ValueError("detection 与本算子不符：%s" % (detection or operator_id))


def _convert_scalar(text: str, kind: str) -> Any:
    text = text.strip()
    if not text:
        raise ValueError("empty")
    if kind == "bool":
        lowered = text.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise ValueError("not a bool")
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    return text


def _form_overrides(form: Any) -> Dict[str, Any]:
    """形态一宽松参数：只取白名单标量（字符串转布尔），未知字段忽略。"""
    overrides: Dict[str, Any] = {}
    for key, kind in _FORM1_SCALARS.items():
        raw = form.get(key)
        if raw is None or hasattr(raw, "filename"):
            continue
        try:
            overrides[key] = _convert_scalar(str(raw), kind)
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
    return overrides


def _ensure_native_input(entry: Path) -> Path:
    """JSONL 原样使用；单个 JSON（对象或数组）转成 JSONL；其他文件一律拒绝。"""
    suffix = entry.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        return entry
    if suffix == ".json":
        try:
            payload = json.loads(entry.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("JSON 文件解析失败") from exc
        items = payload if isinstance(payload, list) else [payload]
        target = entry.with_name(entry.stem + "__native.jsonl")
        with open(str(target), "w", encoding="utf-8", newline="\n") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return target
    raise ValueError("该算子需要 JSON/JSONL 输入")


def _reset_task(task_dir: Path) -> None:
    """清理任务目录内容（保留任务锁目录），供形态一重新上传时复用同一任务。"""
    if not task_dir.is_dir():
        return
    for child in task_dir.iterdir():
        if child.name == ".lock":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:                        # pragma: no cover
                pass


def _iter_jsonl(path: Optional[Path]):
    if path is None or not Path(path).is_file():
        return
    with open(str(path), "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield item


def _review_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一业务字段（V4.2 §4.25）：review_tasks + review_results。

    处理型语义：只要本次运行完成即 ``status=1``；无复核任务时同样是
    ``status=1`` 的空操作成功（队列为空，不伪造任何任务或结论）。
    """
    run_dir = Path(str(result.get("runDir") or ""))
    tasks: List[Dict[str, Any]] = []
    for item in _iter_jsonl(run_dir / dp.TASKS_NAME):
        dispatch = item.get("dispatch") if isinstance(item.get("dispatch"), dict) else {}
        tasks.append({
            "review_task_id": item.get("task_id"),
            "sample_id": item.get("sample_id"),
            "task_status": (dispatch.get("state")
                            or item.get("process_status") or "waiting_manual"),
            "trigger": item.get("category") or item.get("trigger"),
            "round": item.get("round"),
            "max_review_count": item.get("max_review_count"),
        })

    results: List[Dict[str, Any]] = []
    for item in _iter_jsonl(run_dir / dp.CONCLUSIONS_NAME):
        conclusion = item.get("conclusion") if isinstance(item.get("conclusion"), dict) else {}
        results.append({
            "review_task_id": item.get("task_id"),
            "sample_id": item.get("sample_id"),
            "verdict": conclusion.get("verdict"),
            "reviewer": conclusion.get("reviewer"),
            "reviewed_at": conclusion.get("reviewed_at"),
            "decision": item.get("decision"),
            "human_verified": bool(item.get("human_verified")),
        })
    return {"status": 1,
            "uniqueId": uid,
            "process_status": result.get("status"),
            "review_tasks": tasks,
            "review_results": results}


def _form1_audit(uid: str, detection: str, filename: str, size: int,
                 status: int, elapsed_ms: int) -> None:
    """[v4.2] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    record = {"uniqueId": uid, "detection": detection,
              "file": {"name": filename, "bytes": size},
              "status": status, "elapsed_ms": elapsed_ms,
              "time": datetime.now().isoformat(timespec="seconds")}
    try:
        task = io_utils.task_dir(output_root(), uid)
        with open(str(task / "form1_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  # 审计写盘失败不影响主流程
        pass


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（multipart/form-data），处理型 status（1 成功/0 失败）。"""
    started = time.monotonic()
    try:
        _check_detection(request)
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    uid_raw = (request.query_params.get("uniqueId") or form.get("uniqueId")
               or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    try:
        uid = io_utils.validate_unique_id(str(uid_raw))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    content = await upload.read()
    if not content:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})

    filename = upload.filename or "review_candidates.jsonl"
    root = output_root()
    try:
        with io_utils.TaskLock(root, uid):
            task = io_utils.task_dir(root, uid)
            _reset_task(task)
            task.mkdir(parents=True, exist_ok=True)
            input_dir = task / INPUT_DIR_NAME
            input_dir.mkdir(parents=True, exist_ok=True)
            if zipfile.is_zipfile(io.BytesIO(content)):
                io_utils.safe_extract_zip(io.BytesIO(content), input_dir)
                manifest = transport.load_manifest(input_dir)
                entry_path, _relative = transport.resolve_entrypoint(input_dir, manifest)
            else:
                relative = io_utils.normalize_zip_member_name(filename)
                entry_path = input_dir / relative
                entry_path.parent.mkdir(parents=True, exist_ok=True)
                entry_path.write_bytes(content)
            native = _ensure_native_input(entry_path)
            input_arg = native.relative_to(task).as_posix()
    except (ValueError, transport.TransportError, io_utils.TaskBusyError,
            OSError, UnicodeDecodeError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    try:
        result = inference.run(input_file=input_arg, unique_id=uid,
                               output_root=str(root), overrides=overrides or None)
    except (ConfigError, inference.InputError, ValueError, OSError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": "task_busy: %s" % exc})

    elapsed_ms = int((time.monotonic() - started) * 1000)
    detection = request.query_params.get("detection") \
        or request.query_params.get("operatorId") or ""
    if str(result.get("status")) == "failed":
        _form1_audit(uid, detection, filename, len(content), 0, elapsed_ms)
        return JSONResponse({"status": 0,
                             "msg": (result.get("report") or {}).get("message")
                             or "处理失败"})
    payload = _review_response(uid, result)
    if zipfile.is_zipfile(io.BytesIO(content)):
        try:
            payload['outputFile'] = form1_package.build_reference(Path(result['runDir']), result.get('entrypoint'), media_policy='reference')
        except Exception as exc:
            payload.update(status=0, package_error=str(exc)[:200])
    _form1_audit(uid, detection, filename, len(content),
                 int(payload.get("status", 1)), elapsed_ms)
    return JSONResponse(payload)


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #

@app.post("/removeFile")
def remove_file(uniqueId: str = Query(..., description="任务唯一标识")) -> Dict[str, Any]:
    """删除任务目录（幂等：不存在时返回 removed=false）。"""
    try:
        removed = io_utils.remove_task(output_root(), uniqueId)
    except ValueError as exc:
        return envelope(1, str(exc), {})
    except io_utils.TaskBusyError as exc:
        return envelope(1, str(exc), {})
    except OSError as exc:
        return envelope(1, "删除失败：%s" % exc, {})
    return envelope(0, "删除完成", {"uniqueId": uniqueId, "removed": bool(removed)})


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    return {
        "operator": dp.OPERATOR_NAME,
        "operatorLabel": dp.OPERATOR_LABEL,
        "operatorNumber": dp.FORMAL_OPERATOR_NUMBER,
        "operatorVersion": dp.OPERATOR_VERSION,
        "stageId": dp.STAGE_ID,
        "interfaces": ["/uploadFile", "/predict", "/removeFile"],
        "downloadFile": False,
        "trigger": "manual",
        "config": str(inference.DEFAULT_CONFIG_PATH.name),
    }
