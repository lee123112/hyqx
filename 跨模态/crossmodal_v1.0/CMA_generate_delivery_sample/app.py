"""算子 26「生成交付样例」HTTP 服务。

正式接口只有三个（禁止 `/downloadFile`）：
- ``POST /uploadFile?uniqueId=``  上传输入包（zip），解压到 ``OUTPUT_DIR/<uniqueId>/input``
- ``POST /predict``               [v4.2] 双协议：
  - 形态二（application/json）：``{uniqueId, isLastOperator, inputFile?, config?}``，
    统一返回 ``data.outputFile``（crossmodal-files-v1）+ ``data.result``，去 ``lineageFile``；
  - 形态一（multipart/form-data 单文件流）：``?detection=generate_delivery_sample``，
    ``file`` 必传（最终可交付包 ZIP），可带 ``per_route_max`` / ``sampling_seed``，
    返回 int ``status``（1 生成成功 / 0 不可生成）+ ``delivery_sample`` / ``routes``；
- ``POST /removeFile?uniqueId=``  清理本任务目录（幂等）

外层响应统一为 ``{"code": 0|1, "msg": "...", "data": {...}}``。
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
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import io as io_utils
from utils import logger as logger_utils
from utils import sampling, schemas, transport, form1_package
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance

RUN_SUBDIR = inference.RUN_SUBDIR
INPUT_SUBDIR = inference.INPUT_SUBDIR
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 512 * 1024 * 1024))
ALLOWED_SUFFIXES = {".zip"}

app = FastAPI(title="跨模态算子 26：生成交付样例", version=sampling.OPERATOR_VERSION)
logger = logger_utils.get_logger()


# --------------------------------------------------------------------------- #
# 响应工具
# --------------------------------------------------------------------------- #

def ok(data: Optional[Dict[str, Any]] = None, msg: str = "success") -> JSONResponse:
    return JSONResponse(status_code=200, content=inference.envelope(0, msg, data or {}))


def fail(status_code: int, msg: str,
         data: Optional[Dict[str, Any]] = None) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content=inference.envelope(1, msg, data or {}))


def _task_dir(unique_id: Any) -> Path:
    return (inference.default_output_dir().resolve()
            / io_utils.validate_unique_id(unique_id))


# --------------------------------------------------------------------------- #
# 异常处理：所有错误也走统一信封
# --------------------------------------------------------------------------- #

@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return fail(422, "请求参数不合法：%s" % _brief(exc.errors()))


@app.exception_handler(StarletteHTTPException)
async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return fail(int(exc.status_code), str(exc.detail))


@app.exception_handler(ValueError)
async def _value_handler(request: Request, exc: ValueError) -> JSONResponse:
    return fail(422, str(exc))


def _brief(errors: Any) -> str:
    try:
        items = []
        for item in errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            items.append("%s: %s" % (location, item.get("msg")))
        return "; ".join(items[:6])
    except Exception:                                      # noqa: BLE001
        return "参数校验失败"


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    config = inference.load_config()
    return ok({"operator": sampling.OPERATOR,
               "operator_label": sampling.OPERATOR_LABEL,
               "formal_operator_number": sampling.OPERATOR_NUMBER,
               "operator_version": sampling.OPERATOR_VERSION,
               "stage_id": config.stage_id,
               "model": None,
               "rule_based": True,
               "endpoints": ["/uploadFile", "/predict", "/removeFile"],
               "downloadFile": "not_registered",
               "transport": {"format": transport.FILE_OBJECT_FORMAT,
                             "max_response_bytes": config.transport.max_response_bytes},
               "output_dir": str(inference.default_output_dir())},
              msg="ok")


@app.post("/uploadFile")
async def upload_file(request: Request,
                      uniqueId: str = Query(..., description="任务唯一标识"),
                      file: UploadFile = File(...)) -> JSONResponse:
    try:
        task_id = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return fail(422, "uniqueId 不合法：%s" % exc)

    name = Path(file.filename or "").name
    suffix = Path(name).suffix.lower()
    if suffix and suffix not in ALLOWED_SUFFIXES:
        return fail(422, "文件格式不支持（需要 .zip）：%s" % name)
    if not suffix:
        return fail(422, "文件缺少扩展名，无法识别输入包格式：%s" % name)

    task_dir = _task_dir(task_id)
    input_dir = task_dir / INPUT_SUBDIR
    if input_dir.exists():
        shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    target = input_dir / name

    written = 0
    with open(target, "wb") as sink:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                sink.close()
                target.unlink(missing_ok=True)
                return fail(422, "上传文件超过上限（%d 字节）" % MAX_UPLOAD_BYTES)
            sink.write(chunk)
    if written == 0:
        target.unlink(missing_ok=True)
        return fail(422, "上传文件为空")

    try:
        extracted = inference._extract_zip_package(target, task_dir)
        entry, relative = transport.resolve_entrypoint(extracted)
    except (inference.InputError, ValueError) as exc:
        shutil.rmtree(input_dir, ignore_errors=True)
        return fail(422, "输入包不可用：%s" % exc)

    logger.info("upload done | uniqueId=%s | entrypoint=%s | bytes=%d",
                task_id, relative, written)
    return ok({"uniqueId": task_id, "entrypoint": relative,
               "package_path": relative,
               "input_dir": str(input_dir), "bytes": written},
              msg="上传成功")


@app.post("/predict")
async def predict(request: Request) -> JSONResponse:
    """[v4.2] 双协议入口：multipart/form-data → 形态一（单文件流）；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    # [D3] 形态二入口补 detection/operatorId 冲突校验（§2.7 公共约定）
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return fail(422, "detection 与 operatorId 同时出现时取值必须一致")
    try:
        payload = await request.json()
    except Exception:                                      # noqa: BLE001
        return fail(422, "请求体必须是 JSON 对象")
    try:
        body = schemas.PredictRequest.model_validate(payload)
    except Exception as exc:                               # noqa: BLE001
        return fail(422, "请求参数不合法：%s" % str(exc)[:200])
    return _predict_stage(body)


def _route_counts(report: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态一/二共用：各路线实际数量与上限（不足按实际导出）。"""
    routes: Dict[str, Any] = {}
    for route, item in (report.get("routes") or {}).items():
        if not isinstance(item, dict):
            continue
        routes[route] = {"sample_count": item.get("sampled"),
                         "max_count": item.get("limit"),
                         "available": item.get("available"),
                         "insufficient": bool(item.get("insufficient"))}
    return routes


def _business_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态二 ``data.result``：业务摘要，剔除运行目录等内部绝对路径。"""
    statistics = dict(result.get("statistics") or {})
    # 输入文件只保留入口相对名，不把任务目录的绝对路径回传给调用方
    statistics["input_file"] = statistics.get("entrypoint")
    report = dict(result.get("report") or {})
    report.pop("statistics", None)
    return {
        "uniqueId": result.get("uniqueId"),
        "runId": result.get("runId"),
        "status": result.get("status"),
        "message": result.get("message"),
        "statistics": statistics,
        "report": report,
        "delivery_sample": sampling.EXCEL_NAME,
        "routes": _route_counts(report),
        "source_package_version": (statistics.get("dataset_version")
                                   or (report.get("source") or {}).get("version_state")),
    }


def _predict_stage(body: schemas.PredictRequest) -> JSONResponse:
    """形态二：阶段文件传递协议。``isLastOperator`` 保留严格校验但不再改变响应形态。"""
    task_id = body.task_id()
    task_dir = _task_dir(task_id)

    if body.inputFile:
        try:
            input_path = io_utils.resolve_in_task(task_dir, body.inputFile)
        except ValueError as exc:
            return fail(400, "inputFile 不合法：%s" % exc)
        if not input_path.exists():
            return fail(400, "inputFile 不存在：%s" % body.inputFile)
    else:
        input_path = task_dir / INPUT_SUBDIR
        if not input_path.is_dir():
            return fail(400, "阶段首个算子必须先用 /uploadFile 上传输入包"
                             "（或把交付包放入 %s）" % input_path)

    overrides = body.override_payload()
    try:
        result = inference.run(input_file=str(input_path), unique_id=task_id,
                              output_root=str(inference.default_output_dir()),
                              overrides=overrides or None,
                              allow_external_input=False)
        # [v4.2] mty_compliance 已停用：prepare_stage_result 调用注释存档（不再做阶段归档）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, body.isLastOperator)
    except schemas.ConfigError as exc:
        return fail(400, "配置不合法：%s" % exc)
    except inference.InputError as exc:
        return fail(400, "输入不合法：%s" % exc)
    except io_utils.TaskBusyError as exc:
        return fail(409, "任务正在运行：%s" % exc)
    except transport.OutputTooLargeError as exc:
        return fail(413, "响应体超过上限：%s" % exc)

    logger.info("predict done | uniqueId=%s | runId=%s | status=%s | sampled=%s",
                task_id, result["runId"], result["status"],
                result["statistics"]["sampled_records"])

    return ok({"operator": sampling.OPERATOR,
               "status": result["status"],
               "outputFile": inference.build_file_object(result),
               "result": _business_result(result)},
              msg=result["message"] or result["status"])


# --------------------------------------------------------------------------- #
# 形态一：单文件流（MTY V2，[v4.2] 新增）
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M26/26/英文ID/目录名
DETECTION_ALIASES = {"m26", "26", "generate_delivery_sample",
                     "cma_generate_delivery_sample", "delivery_sample"}

#: 形态一白名单标量（未知字段忽略）：表单名 → (配置键, 类型)
_FORM1_SCALARS = {
    "per_route_max": ("max_samples_per_route", "int"),
    "sampling_seed": ("sampling_seed", "int"),
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


def _form_overrides(form: Any) -> Dict[str, Any]:
    """形态一宽松参数：只取白名单标量（字符串转整数），未知字段忽略。"""
    overrides: Dict[str, Any] = {}
    for field, (key, kind) in _FORM1_SCALARS.items():
        raw = form.get(field)
        if raw is None or hasattr(raw, "filename"):
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            overrides[key] = int(text) if kind == "int" else text
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
    return overrides


def _reset_input(task_dir: Path) -> None:
    """清理任务 input 目录，供形态一重新上传同一 uniqueId 时复用。"""
    input_dir = task_dir / INPUT_SUBDIR
    if input_dir.is_dir():
        shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True, exist_ok=True)


def _form1_audit(uid: str, detection: str, filename: str, size: int,
                 status: int, elapsed_ms: int) -> None:
    """[v4.2] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    record = {"uniqueId": uid, "detection": detection,
              "file": {"name": filename, "bytes": size},
              "status": status, "elapsed_ms": elapsed_ms,
              "time": datetime.now().isoformat(timespec="seconds")}
    try:
        task_dir = _task_dir(uid)
        with open(str(task_dir / "form1_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:                                      # noqa: BLE001
        pass


def _form1_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一业务字段（V4.2 §4.26）：1=生成成功；0=不可生成（如包仍为待发布）。

    待发布包/候选池/隔离库都不是可交付包，本算子**不得生成样例**：
    这类运行结果一律映射为 ``status=0`` 并给出不可生成原因，不回传样例引用。
    """
    statistics = dict(result.get("statistics") or {})
    version = (statistics.get("dataset_version")
               or statistics.get("package_version_state")
               or ((result.get("report") or {}).get("source") or {}).get("version_state"))
    if str(result.get("status")) != "success":
        return {"status": 0,
                "uniqueId": uid,
                "process_status": result.get("status"),
                "msg": result.get("message") or "不可生成交付样例",
                "source_package_version": version}
    report = result.get("report") or {}
    return {"status": 1,
            "uniqueId": uid,
            "process_status": result.get("status"),
            "delivery_sample": sampling.EXCEL_NAME,
            "routes": _route_counts(report),
            "source_package_version": version,
            "sample_count": statistics.get("sampled_records")}


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（multipart/form-data），处理型 status（1 成功/0 不可生成）。"""
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
    if not zipfile.is_zipfile(io.BytesIO(content)):
        return JSONResponse({"status": 0,
                             "msg": "该算子需要最终可交付包 ZIP 输入"})

    filename = Path(upload.filename or "delivery_package.zip").name
    task_dir = _task_dir(uid)
    try:
        with io_utils.TaskLock(inference.default_output_dir(), uid):
            _reset_input(task_dir)
            archive = task_dir / INPUT_SUBDIR / filename
            archive.write_bytes(content)
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": "task_busy: %s" % exc})
    except OSError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    try:
        result = inference.run(input_file=str(archive), unique_id=uid,
                               output_root=str(inference.default_output_dir()),
                               overrides=overrides or None,
                               allow_external_input=False)
    except (schemas.ConfigError, inference.InputError, ValueError, OSError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": "task_busy: %s" % exc})
    except transport.OutputTooLargeError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    elapsed_ms = int((time.monotonic() - started) * 1000)
    detection = request.query_params.get("detection") \
        or request.query_params.get("operatorId") or ""
    payload = _form1_response(uid, result)
    if payload.get('status') == 1:
        try:
            payload['outputFile'] = form1_package.build_reference(Path(result['runDir']), result.get('entrypoint'), media_policy='reference')
        except Exception as exc:
            payload.update(status=0, package_error=str(exc)[:200])
    _form1_audit(uid, detection, filename, len(content),
                 int(payload.get("status", 0)), elapsed_ms)
    return JSONResponse(payload)


@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(..., description="任务唯一标识")
                      ) -> JSONResponse:
    try:
        task_id = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return fail(422, "uniqueId 不合法：%s" % exc)
    removed = io_utils.remove_task(inference.default_output_dir(), task_id)
    logger.info("remove done | uniqueId=%s | removed=%s", task_id, removed)
    return ok({"uniqueId": task_id, "removed": removed}, msg="任务目录已清理"
              if removed else "任务目录不存在")


if __name__ == "__main__":                                # pragma: no cover
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("PORT", "8126")), log_level="info")
