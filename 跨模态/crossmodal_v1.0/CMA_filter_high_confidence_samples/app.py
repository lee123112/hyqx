"""算子 19 HTTP 服务：**只实现三个正式接口**。

- ``POST /uploadFile?uniqueId=<id>``       上传任务压缩包（zip）
- ``POST /predict``                        执行判定（阶段首不传 inputFile）
- ``POST /removeFile?uniqueId=<id>``       清理任务目录（幂等）

**不实现 ``/downloadFile``**；阶段末结果通过 ``/predict`` 的 ``outputFile``
文件内容对象返回，由工程侧自行落盘。

所有响应外层统一为 ``{"code": 0|1, "msg": "...", "data": {...}}``。
"""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import inference
from utils import form1_package
from utils import io as io_utils
from utils import pool_filter, transport
from utils.logger import get_logger
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.schemas import (ConfigError, FROZEN_OVERRIDE_KEYS, FrozenOverrideError,
                           OPERATOR_NAME, OPERATOR_NUMBER, OPERATOR_VERSION,
                           PredictRequest, frozen_override_message,
                           validate_overrides)

__all__ = ["app", "ApiError"]

logger = get_logger("operator_19.api")

API_VERSION = "v1"
INPUT_DIR_NAME = "input"

app = FastAPI(title="跨模态算子 19 筛选高置信样本入池", version=OPERATOR_VERSION)


class ApiError(Exception):
    """业务侧错误：映射为 4xx + 统一信封。"""

    def __init__(self, status_code: int, message: str, error_type: str = "ApiError"):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type


def envelope(code: int, msg: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": data}


def _output_root() -> Path:
    return inference.default_output_dir()


def _task_dir(unique_id: str) -> Path:
    return _output_root() / io_utils.validate_unique_id(unique_id)


# --------------------------------------------------------------------------- #
# 异常 → 统一信封
# --------------------------------------------------------------------------- #

@app.exception_handler(ApiError)
async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    logger.warning("api_error status=%s type=%s msg=%s",
                   exc.status_code, exc.error_type, exc.message)
    return JSONResponse(status_code=exc.status_code,
                        content=envelope(1, exc.message, {"errorType": exc.error_type}))


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request,
                              exc: RequestValidationError) -> JSONResponse:
    details = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        details.append("%s: %s" % (location or "<request>", error.get("msg", "非法取值")))
    message = "请求参数校验失败：" + "；".join(details)
    logger.warning("api_validation_error path=%s detail=%s", request.url.path, message)
    return JSONResponse(status_code=422,
                        content=envelope(1, message, {"errorType": "ValidationError"}))


@app.exception_handler(ConfigError)
async def _config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
    return JSONResponse(status_code=400,
                        content=envelope(1, str(exc), {"errorType": "ConfigError"}))


@app.exception_handler(FrozenOverrideError)
async def _frozen_override_handler(request: Request,
                                   exc: FrozenOverrideError) -> JSONResponse:
    """表1 #19 固定口径被请求覆盖：统一 422 + 明确中文错误（不可削弱四项必过项）。"""
    logger.warning("api_frozen_override path=%s msg=%s", request.url.path, str(exc))
    return JSONResponse(status_code=422,
                        content=envelope(1, str(exc), {"errorType": "FrozenOverride"}))


@app.exception_handler(io_utils.UnsupportedMediaPath)
async def _media_path_handler(request: Request,
                              exc: io_utils.UnsupportedMediaPath) -> JSONResponse:
    return JSONResponse(status_code=400,
                        content=envelope(1, str(exc),
                                         {"errorType": "UnsupportedMediaPath"}))


@app.exception_handler(io_utils.TaskBusyError)
async def _busy_handler(request: Request, exc: io_utils.TaskBusyError) -> JSONResponse:
    return JSONResponse(status_code=409,
                        content=envelope(1, str(exc), {"errorType": "TaskBusy"}))


@app.exception_handler(transport.TransportError)
async def _transport_handler(request: Request,
                             exc: transport.TransportError) -> JSONResponse:
    return JSONResponse(status_code=400,
                        content=envelope(1, str(exc), {"errorType": "TransportError"}))


# --------------------------------------------------------------------------- #
# GET /health
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    return envelope(0, "ok", {
        "operator": OPERATOR_NAME,
        "formal_operator_number": OPERATOR_NUMBER,
        "operator_version": OPERATOR_VERSION,
        "stage_id": "S3",
        "rule_based": True,
        "model": None,
        "downloadFile": "not_registered",
        "endpoints": ["/uploadFile", "/predict", "/removeFile"],
        "output_dir": str(_output_root()),
    })


# --------------------------------------------------------------------------- #
# POST /uploadFile?uniqueId=
# --------------------------------------------------------------------------- #

@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(..., description="任务标识"),
                      file: UploadFile = File(..., description="任务 zip 包")
                      ) -> Dict[str, Any]:
    try:
        task_id = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        raise ApiError(422, str(exc), "InvalidUniqueId")

    payload = await file.read()
    if not payload:
        raise ApiError(422, "上传内容为空", "EmptyUpload")
    if len(payload) > transport.MAX_RESPONSE_BYTES:
        raise ApiError(413, "上传内容超过上限 %d 字节" % transport.MAX_RESPONSE_BYTES,
                       "UploadTooLarge")

    task_dir = _task_dir(task_id)
    input_dir = task_dir / INPUT_DIR_NAME
    if input_dir.exists():
        shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(prefix="upload_", suffix=".zip", delete=False) as tmp:
        tmp.write(payload)
        archive = Path(tmp.name)
    try:
        files = io_utils.safe_extract_zip(archive, input_dir)
    except (ValueError, OSError) as exc:
        shutil.rmtree(input_dir, ignore_errors=True)
        raise ApiError(422, "压缩包无法安全解压：%s" % exc, "BadArchive")
    except Exception as exc:                     # noqa: BLE001 - zipfile.BadZipFile 等
        shutil.rmtree(input_dir, ignore_errors=True)
        raise ApiError(422, "压缩包无法解析：%s" % exc, type(exc).__name__)
    finally:
        if archive.exists():
            archive.unlink()

    try:
        entry_path, entrypoint = transport.resolve_entrypoint(input_dir)
    except transport.TransportError as exc:
        raise ApiError(422, str(exc), "EntrypointUnresolved")

    logger.info("upload ok uniqueId=%s files=%d entrypoint=%s",
                task_id, len(files), entrypoint)
    return envelope(0, "上传成功", {"uniqueId": task_id, "taskDir": str(task_dir),
                                   "inputDir": str(input_dir),
                                   "entrypoint": entrypoint,
                                   "entrypointSize": entry_path.stat().st_size,
                                   "files": files})


# --------------------------------------------------------------------------- #
# POST /predict
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M19/19/英文ID/目录名
DETECTION_ALIASES = {"m19", "19", "filter_high_confidence_samples",
                     "cma_filter_high_confidence_samples", "high_confidence_pool"}

#: 形态一白名单标量（未知字段忽略）；与 V4.2 §4.19 接口二可覆盖配置项对齐。
#: 表1 #19 固定口径键（``FROZEN_OVERRIDE_KEYS``）**不在**此表内：表单携带即 422。
_FORM1_SCALARS = {
    "enabled": "bool",
    "training_authorization": "str",
    "min_enabled_routes": "int",
    "missing_upstream_policy": "str",
}

#: 记录级流程态 → 形态一 pool_status / process_status
_POOL_STATUS = {
    "admitted": "accepted",
    "pending": "pending",
    "isolated": "rejected",
    "origin": "rejected",
}
_PROCESS_MAP = {
    "admitted": "success",
    "pending": "pending_review",
    "isolated": "failed",
    "origin": "failed",
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


def _convert_scalar(text: str, kind: str):
    text = text.strip()
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


def _form_overrides(form) -> Dict[str, Any]:
    """形态一宽松参数：只取白名单标量，未知字段忽略。"""
    overrides: Dict[str, Any] = {}
    for key, kind in _FORM1_SCALARS.items():
        raw = form.get(key)
        if raw is None or hasattr(raw, "filename"):
            continue
        try:
            overrides[key] = _convert_scalar(str(raw), kind)
        except ValueError:
            continue
    return overrides


def _frozen_form_keys(form) -> list:
    """形态一表单里出现的表1 #19 固定口径键（出现即拒绝，值不参与解析）。"""
    found = []
    for key in FROZEN_OVERRIDE_KEYS:
        raw = form.get(key)
        if raw is None or hasattr(raw, "filename"):
            continue
        found.append(key)
    return found


def _ensure_native_input(entry: Path) -> Path:
    """JSONL 原样使用；单个 JSON（对象或数组）转成同目录 JSONL；其他文件拒绝。"""
    suffix = entry.suffix.lower()
    if suffix == ".jsonl":
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


def _first_jsonl(path) -> Optional[Dict[str, Any]]:
    if not path or not Path(path).is_file():
        return None
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
                return item
    return None


def _pack_file_object(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 所有调用统一返回 crossmodal-files-v1 文件内容对象。"""
    run_dir = Path(result["runDir"])
    paths = [pool_filter.MAIN_NAME]
    paths.extend(item["path"] for item in result["artifacts"])
    paths.append(transport.MANIFEST_NAME)
    return transport.pack_file_object(
        run_dir, pool_filter.MAIN_NAME, paths,
        max_response_bytes=result["transport"]["maxResponseBytes"])


def _task_rel(task_dir: Path, path) -> Optional[str]:
    try:
        return Path(path).resolve().relative_to(Path(task_dir).resolve()).as_posix()
    except ValueError:
        return None


def _single_file_response(task_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一业务字段（V4.2 §4.19）：判定型 status + pool_status/route/reason_code。

    [v4.2] 未配对/待人工/不适用 → status=1 + 对应 process_status，绝不隔离。
    """
    payload: Dict[str, Any] = {
        "status": 0, "process_status": "not_applicable", "uniqueId": task_id,
        "sample_id": None, "pool_status": "rejected", "pool": None,
        "route": None, "reason_code": "empty_input",
    }
    for key in ("outputFile", "quarantineFile", "pendingFile", "rejectsFile"):
        item = _first_jsonl(result.get(key))
        if item is None:
            continue
        if key == "outputFile":
            summary = item.get("operator_19") if isinstance(item.get("operator_19"), dict) \
                else {}
            node = (item.get("processing") or {}).get("19") or summary
            raw = summary.get("process_status") or node.get("process_status")
            status_value = summary.get("status", node.get("status"))
            enabled_routes = summary.get("enabled_routes") or []
            payload["sample_id"] = item.get("sample_id") or summary.get("identifier")
            payload["route"] = item.get("route")
            reason = summary.get("reason_code") or node.get("reason_code")
        else:
            raw = item.get("process_status")
            status_value = item.get("status")
            gates = item.get("gates") or {}
            enabled_routes = (gates.get("route") or {}).get("enabled_routes") or []
            payload["sample_id"] = item.get("sample_id") or item.get("identifier")
            payload["route"] = item.get("route")
            reason = item.get("reason_code") or item.get("reason")
        raw = raw or "origin"
        payload.update({
            "status": int(status_value) if isinstance(status_value, int) else (1 if raw in (
                "admitted", "pending") else 0),
            "process_status": _PROCESS_MAP.get(raw, raw),
            "pool_status": _POOL_STATUS.get(raw, "rejected"),
            "pool": "high_confidence" if raw == "admitted" else None,
            "reason_code": reason,
        })
        if not payload["route"] and enabled_routes:
            payload["route"] = enabled_routes[0]
        break
    return payload


#: [v4.3] 形态一压缩包响应的算子英文标识（与登记表/形态二口径一致）
FORM1_OPERATOR = "filter_high_confidence_samples"


def _package_response(task_id: str, result: Dict[str, Any],
                      detail: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.3] 压缩包模式响应：status + 业务指标 + outputFile 容器内路径引用。"""
    run_root = Path(str(result.get("runDir") or result.get("run_dir") or ""))
    entry_hint = result.get("entrypoint") or Path(str(result.get("outputFile") or "")).name
    reference = form1_package.build_reference(run_root, entry_hint)
    business = {key: value for key, value in detail.items() if key != "status"}
    body = form1_package.package_payload(
        result, reference, operator=FORM1_OPERATOR, unique_id=task_id, business=business)
    # package_payload 的 process_status 取运行状态；形态一的业务流程态以业务字段为准
    if "process_status" in business:
        body["process_status"] = business["process_status"]
    return body


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（判定型 status：1 入池/待人工，0 不入池）。

    [v4.3] 上传内容是压缩包时按「单文件压缩包输入」处理：安全解压到任务 ``input/``，
    用与形态二完全相同的 ``inference.run`` 跑阶段推理，返回 ``status`` + 业务指标 +
    ``outputFile`` 容器内路径引用，并把运行输出目录打成可直传下一算子的
    ``*_form1.zip``；非压缩包仍走原单文件单记录逻辑。
    """
    try:
        _check_detection(request)
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    form = await request.form()
    frozen = _frozen_form_keys(form)
    if frozen:
        # 表1 #19 固定口径：形态一表单携带即 422，绝不静默忽略或削弱
        return JSONResponse(status_code=422,
                            content={"status": 0,
                                     "msg": frozen_override_message(frozen),
                                     "errorType": "FrozenOverride"})
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    uid_raw = (request.query_params.get("uniqueId") or form.get("uniqueId")
               or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    try:
        task_id = io_utils.validate_unique_id(str(uid_raw))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    data = await upload.read()
    if not data:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})
    if len(data) > transport.MAX_RESPONSE_BYTES:
        return JSONResponse({"status": 0, "msg": "上传内容超过上限"})

    filename = Path(str(upload.filename or "upload.json").replace("\\", "/")).name
    task_dir = _task_dir(task_id)
    input_dir = task_dir / INPUT_DIR_NAME
    if input_dir.exists():
        shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    target = input_dir / filename
    is_package = zipfile.is_zipfile(io.BytesIO(data))
    try:
        if is_package:
            # [v4.3] 压缩包模式：helper 安全解包，入口优先 manifest.json
            form1_package.extract_zip_into(data, input_dir)
            try:
                entry_path = form1_package.resolve_entrypoint(input_dir)
            except ValueError:
                try:
                    entry_path, _entrypoint = transport.resolve_entrypoint(input_dir)
                except transport.TransportError:
                    # 兜底：允许唯一的 *.json 作为入口（manifest.json 除外）
                    jsons = sorted(path for path in input_dir.rglob("*.json")
                                   if path.name != "manifest.json")
                    if len(jsons) != 1:
                        raise
                    entry_path = jsons[0]
        else:
            target.write_bytes(data)
            entry_path = target
        native = _ensure_native_input(entry_path)
    except (ValueError, OSError, transport.TransportError, zipfile.BadZipFile) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    try:
        result = inference.run(input_file=str(native), unique_id=task_id,
                               output_root=str(_output_root()),
                               overrides=_form_overrides(form) or None,
                               allow_external_input=False)
    except (ConfigError, inference.InputError, io_utils.TaskBusyError, OSError,
            ValueError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "process_status": "failed",
                             "msg": result.get("message") or "处理失败"})
    payload = _single_file_response(task_id, result)
    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            body = _package_response(task_id, result, payload)
        except (OSError, ValueError) as exc:
            return JSONResponse({"status": 0, "msg": "输出打包失败：%s" % str(exc)[:200]})
        return JSONResponse(body)
    return JSONResponse(payload)


@app.post("/predict")
async def predict(request: Request) -> Any:
    """[v4.2] 双协议入口：multipart/form-data → 形态一；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
    except ValueError as exc:
        return JSONResponse(status_code=422, content=envelope(1, str(exc), {}))
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=422,
                            content=envelope(1, "请求体必须是 JSON 对象", {}))
    try:
        payload = PredictRequest.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=422,
                            content=envelope(1, "请求参数校验失败：%s" % exc,
                                             {"errorType": "ValidationError"}))
    task_id = payload.uniqueId
    # 请求级配置先于任务状态校验：固定口径键被覆盖 → 422（FrozenOverride），
    # 未知键 → 400（ConfigError）；不因「尚未上传输入」而被 400 抢先掩盖。
    validate_overrides(payload.overrides)
    task_dir = _task_dir(task_id)

    try:
        if payload.inputFile:
            # 阶段内后续算子：传上一响应的任务内相对 outputFile
            input_path = io_utils.resolve_in_task(task_dir, payload.inputFile)
        else:
            # 阶段首：从任务目录的输入区解析入口
            input_dir = task_dir / INPUT_DIR_NAME
            if not input_dir.is_dir():
                raise ApiError(400, "任务 %s 尚未上传输入，请先调用 /uploadFile" % task_id,
                               "InputNotUploaded")
            input_path, _ = transport.resolve_entrypoint(input_dir)
    except ApiError:
        raise
    except ValueError as exc:
        raise ApiError(400, "inputFile 非法：%s" % exc, "InvalidInputFile")

    try:
        result = inference.run(input_file=str(input_path), unique_id=task_id,
                               output_root=str(_output_root()),
                               overrides=payload.overrides,
                               allow_external_input=False)
        # [v4.2] 血缘/阶段归档已停用：不再调用 prepare_stage_result
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
    except io_utils.TaskBusyError:
        raise
    except ConfigError:
        raise
    except inference.InputError as exc:
        raise ApiError(400, str(exc), "InputError")

    file_object = _pack_file_object(result)
    logger.info("predict ok uniqueId=%s isLast=%s status=%s admitted=%s",
                task_id, payload.isLastOperator, result["status"],
                result["statistics"]["admitted_records"])
    # [v4.2] 所有调用统一返回 outputFile + result；isLastOperator 不再改变响应形态
    return envelope(0, result["message"], {
        "operator": pool_filter.OPERATOR,
        "status": result["status"],
        "outputFile": file_object,
        "result": {"uniqueId": task_id,
                   "runId": result["runId"],
                   "entrypoint": result["entrypoint"],
                   "outputFile": _task_rel(task_dir, result["outputFile"]),
                   "statistics": result["statistics"],
                   "model": None},
    })


# --------------------------------------------------------------------------- #
# POST /removeFile?uniqueId=
# --------------------------------------------------------------------------- #

@app.post("/removeFile")
def remove_file(uniqueId: str = Query(..., description="任务标识")) -> Dict[str, Any]:
    try:
        task_id = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        raise ApiError(422, str(exc), "InvalidUniqueId")
    removed = io_utils.remove_task(_output_root(), task_id)
    logger.info("removeFile uniqueId=%s removed=%s", task_id, removed)
    return envelope(0, "已清理" if removed else "任务不存在（幂等返回）",
                    {"uniqueId": task_id, "removed": removed})


if __name__ == "__main__":                       # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8119)
