"""算子 23 HTTP 服务：**只暴露三接口**（不再实现 `/downloadFile`）。

- ``POST /uploadFile?uniqueId=``  上传任务输入包（zip 或单个 JSONL），受路径安全与解压安全约束；
- ``POST /predict``               执行交付判定与打包；``isLastOperator`` 决定返回形态；
- ``POST /removeFile?uniqueId=``  清理任务目录（幂等，只影响本任务）。
（``GET /health`` 仅为存活探针，不属于业务接口。）

所有响应均为 ``{"code": 0|1, "msg": "...", "data": {...}}``。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

import inference
from utils import archive
from utils import delivery
from utils import io as io_utils
from utils import transport
from utils import form1_package
from utils.logger import get_logger
from utils.schemas import (ConfigError, PredictRequest, validate_overrides)

__all__ = ["app", "create_app", "envelope", "DEFAULT_PORT"]

DEFAULT_PORT = int(os.environ.get("PORT", "8123"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
UPLOAD_META_NAME = "upload.json"
UPLOAD_FORMAT = "crossmodal-upload-v1"

logger = get_logger("operator_23.api")

#: [v4.2] 形态一（单文件流）算子标识：detection 与 operatorId 同一别名空间（大小写不敏感）
ENGLISH_ID = "package_aligned_corpus"
DETECTION_ALIASES = frozenset({
    "m23", "23", ENGLISH_ID, "cma_" + ENGLISH_ID, "delivery_package",
})

#: [v4.2] 形态一 config 白名单标量（与 OVERRIDABLE_KEYS 对齐；未知表单字段忽略）
_FORM_SCALAR_TYPES = {
    "include_media": "bool",
    "require_deliverable": "bool",
    "require_token": "bool",
}

#: [v4.2] 两阶段发布表单字段（prepare=允许待发布降级；finalize=严格必须可交付）
_PUBLISH_PHASES = ("prepare", "finalize")

app = FastAPI(title="算子 23 压缩交付对齐语料包",
              description="对齐语料包交付判定与 ZIP 打包（系统功能型，无模型）",
              version=delivery.OPERATOR_VERSION)


# --------------------------------------------------------------------------- #
# 信封与错误处理
# --------------------------------------------------------------------------- #

def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": int(code), "msg": msg, "data": data or {}}


def _error(status_code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=envelope(1, msg, data))


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError
                              ) -> JSONResponse:      # pragma: no cover - 统一信封
    details = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        details.append("%s: %s" % (location or "<body>", error.get("msg", "非法取值")))
    return _error(422, "请求参数不合法（" + "; ".join(details) + "）",
                  {"errorType": "ValidationError"})


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("未处理异常：%s", type(exc).__name__)
    return _error(500, "服务内部错误（%s）" % type(exc).__name__,
                  {"errorType": type(exc).__name__})


# --------------------------------------------------------------------------- #
# /health（存活探针）
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    return envelope(0, "ok", {
        "operator": delivery.OPERATOR,
        "operator_label": delivery.OPERATOR_LABEL,
        "operator_version": delivery.OPERATOR_VERSION,
        "formal_operator_number": delivery.OPERATOR_NUMBER,
        "stage_id": delivery.STAGE_ID,
        "type": "系统功能",
        "model": None,
        "rule_based": True,
        "endpoints": ["/uploadFile", "/predict", "/removeFile"],
        "downloadFile": "not_registered",
        "output_root": str(inference.default_output_dir()),
        "transport_format": transport.FILE_OBJECT_FORMAT,
        "deliverable_requirements": list(delivery.GATE_REQUIREMENTS),
    })


# --------------------------------------------------------------------------- #
# /uploadFile?uniqueId=
# --------------------------------------------------------------------------- #

def _task_dir(unique_id: str) -> Path:
    return Path(inference.default_output_dir()) / io_utils.validate_unique_id(unique_id)


@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(..., description="任务标识"),
                      file: UploadFile = File(...)) -> JSONResponse:
    try:
        task_id = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return _error(422, str(exc), {"errorType": "InvalidUniqueId"})

    filename = Path(file.filename or "upload.bin").name
    payload = await file.read()
    if not payload:
        return _error(422, "上传内容为空", {"errorType": "EmptyUpload"})
    if len(payload) > MAX_UPLOAD_BYTES:
        return _error(422, "上传内容超过上限 %d 字节" % MAX_UPLOAD_BYTES,
                      {"errorType": "UploadTooLarge"})

    task_dir = _task_dir(task_id)
    input_dir = task_dir / "input"
    staging = Path(tempfile.mkdtemp(prefix="op23_upload_", dir=str(task_dir.parent)))
    try:
        upload_path = staging / filename
        upload_path.write_bytes(payload)
        if input_dir.exists():
            shutil.rmtree(input_dir, ignore_errors=True)
        input_dir.mkdir(parents=True, exist_ok=True)

        if filename.lower().endswith(".zip"):
            written = io_utils.safe_extract_zip(upload_path, input_dir)
            if not written:
                raise ValueError("压缩包内没有任何文件")
        elif filename.lower().endswith(".jsonl"):
            target = input_dir / filename
            target.write_bytes(payload)
            written = [filename]
        else:
            raise ValueError("只接受 .zip（输入包）或 .jsonl（单个清单）")

        try:
            entry_path, entry_relative = transport.resolve_entrypoint(input_dir)
        except transport.TransportError as exc:
            shutil.rmtree(input_dir, ignore_errors=True)
            return _error(422, "上传包无法定位业务入口：%s" % exc,
                          {"errorType": "InvalidPackage"})

        io_utils.atomic_write_json(task_dir / UPLOAD_META_NAME, {
            "format": UPLOAD_FORMAT, "uniqueId": task_id, "filename": filename,
            "sizeBytes": len(payload), "files": len(written),
            "entrypoint": entry_relative, "uploadedAt": datetime.now().astimezone()
            .isoformat(timespec="seconds")})
    except (ValueError, OSError) as exc:
        shutil.rmtree(input_dir, ignore_errors=True)
        return _error(422, "上传失败：%s" % exc, {"errorType": type(exc).__name__})
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    logger.info("上传完成 uniqueId=%s files=%d entrypoint=%s",
                task_id, len(written), entry_relative)
    return JSONResponse(status_code=200, content=envelope(0, "上传成功", {
        "uniqueId": task_id, "entrypoint": entry_relative,
        "files": len(written), "sizeBytes": len(payload)}))


# --------------------------------------------------------------------------- #
# /predict
# --------------------------------------------------------------------------- #

@app.post("/predict")
async def predict(request: Request) -> JSONResponse:
    """[v4.2] 双协议：multipart/form-data → 形态一（单文件流）；JSON → 形态二（阶段链式）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    # [D3] 形态二入口补 detection/operatorId 冲突校验（§2.7 公共约定）
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return _error(422, "detection 与 operatorId 同时出现时取值必须一致",
                      {"errorType": "InvalidRequest"})
    try:
        raw = await request.json()
    except Exception:                                        # noqa: BLE001
        return _error(422, "请求体必须是 JSON 对象", {"errorType": "InvalidBody"})
    if not isinstance(raw, dict):
        return _error(422, "请求体必须是 JSON 对象", {"errorType": "InvalidBody"})
    try:
        payload = PredictRequest(**raw)
    except ValidationError as exc:
        details = []
        for error in exc.errors():
            location = ".".join(str(item) for item in error.get("loc", ()))
            details.append("%s: %s" % (location or "<body>",
                                       error.get("msg", "非法取值")))
        return _error(422, "请求参数不合法（" + "; ".join(details) + "）",
                      {"errorType": "ValidationError"})
    try:
        overrides = validate_overrides(payload.config)
        overrides.update(validate_overrides(payload.overrides))
    except ConfigError as exc:
        return _error(400, str(exc), {"errorType": "ConfigError"})

    task_dir = _task_dir(payload.uniqueId)
    input_dir = task_dir / "input"
    try:
        if payload.inputFile:
            input_path = io_utils.resolve_in_task(task_dir, payload.inputFile)
            if not input_path.is_file():
                return _error(400, "inputFile 不存在：%s" % payload.inputFile,
                              {"errorType": "InputMissing"})
        else:
            if not input_dir.is_dir():
                return _error(400, "任务尚未上传输入包（缺少阶段首 inputFile）",
                              {"errorType": "InputMissing"})
            try:
                input_path, _relative = transport.resolve_entrypoint(input_dir)
            except transport.TransportError as exc:
                return _error(400, str(exc), {"errorType": "InvalidPackage"})

        result = inference.run(input_file=str(input_path), unique_id=payload.uniqueId,
                               output_root=str(inference.default_output_dir()),
                               overrides=overrides, allow_external_input=False)
        # [v4.2] 血缘/阶段归档输出已停用：不再调用 prepare_stage_result，
        # 响应不再区分阶段末/非阶段末，统一返回 outputFile（crossmodal-files-v1）。
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
    except (inference.InputError, ConfigError, ValueError) as exc:
        return _error(400, str(exc), {"errorType": type(exc).__name__})
    except io_utils.TaskBusyError as exc:
        return _error(409, str(exc), {"errorType": "TaskBusy"})
    except archive.ArchiveError as exc:                     # pragma: no cover
        return _error(500, "打包失败：%s" % exc, {"errorType": "ArchiveError"})

    run_dir = Path(result["runDir"])
    try:
        paths = [delivery.MAIN_NAME]
        paths.extend(item["path"] for item in result["artifacts"])
        paths.append(transport.MANIFEST_NAME)
        file_object = transport.pack_file_object(
            run_dir, delivery.MAIN_NAME, paths,
            max_response_bytes=result["transport"]["maxResponseBytes"])
        data: Dict[str, Any] = {"operator": ENGLISH_ID,
                                "status": result["status"],
                                "outputFile": file_object,
                                "result": _business_summary(result)}
    except transport.OutputTooLargeError as exc:
        return _error(400, "单次响应超过上限：%s" % exc, {"errorType": "OutputTooLarge"})
    except transport.TransportError as exc:                 # pragma: no cover
        return _error(500, "组装响应失败：%s" % exc, {"errorType": "TransportError"})

    logger.info("predict 完成 uniqueId=%s status=%s versionState=%s packaged=%d",
                payload.uniqueId, result["status"], result["versionState"],
                result["statistics"]["packaged_records"])
    return JSONResponse(status_code=200, content=envelope(0, result["message"], data))


def _business_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """形态二 ``data.result``：业务摘要（保留链式调用所需的输入/输出路径）。"""
    return {
        "uniqueId": result.get("uniqueId"),
        "inputFile": result.get("inputFile"),
        "outputFile": result.get("outputFile"),
        "packagePath": result.get("packagePath"),
        "packageName": result.get("packageName"),
        "versionState": result.get("versionState"),
        "deliverable": result.get("deliverable"),
        "statusLabel": result.get("statusLabel"),
        "runId": result.get("runId"),
        "statistics": result.get("statistics"),
        "startedAt": result.get("startedAt"),
        "finishedAt": result.get("finishedAt"),
    }


# --------------------------------------------------------------------------- #
# [v4.2] 形态一：单文件流（detection 单文件流协议）
# --------------------------------------------------------------------------- #

def _check_detection(request: Request) -> Optional[str]:
    """detection/operatorId 宽松校验（同一别名空间）：缺失放行；出现的每个取值
    都必须落在本算子别名空间内，否则视为两参数不一致/非本算子，报参数错误。"""
    values = [value.strip() for value in
              (request.query_params.get("detection"), request.query_params.get("operatorId"))
              if value and value.strip()]
    for value in values:
        if value.lower() not in DETECTION_ALIASES:
            return "detection/operatorId 与本算子不一致：%s" % value
    return None


def _new_unique_id() -> str:
    return "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _form_overrides(form: Any) -> Dict[str, Any]:
    """表单标量 → config 覆盖：白名单内字符串转数字/布尔，未知字段忽略（形态一宽松）。

    另支持 ``publish_phase``（两阶段发布）：prepare → 允许待发布降级；
    finalize → 严格模式（require_deliverable=true，不可交付即失败）。
    """
    overrides: Dict[str, Any] = {}
    for key, kind in _FORM_SCALAR_TYPES.items():
        raw = form.get(key)
        if raw is None or hasattr(raw, "filename"):
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            if kind == "int":
                overrides[key] = int(text)
            elif kind == "float":
                overrides[key] = float(text)
            elif kind == "bool":
                lowered = text.lower()
                if lowered in ("1", "true", "yes", "on"):
                    overrides[key] = True
                elif lowered in ("0", "false", "no", "off"):
                    overrides[key] = False
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
    phase = form.get("publish_phase")
    if phase is not None and not hasattr(phase, "filename"):
        phase_text = str(phase).strip().lower()
        if phase_text:
            if phase_text not in _PUBLISH_PHASES:
                raise ValueError("publish_phase 必须是 prepare/finalize：%s" % phase_text)
            overrides["require_deliverable"] = (phase_text == "finalize")
    return overrides


def _store_single_file(output_root: Path, task_id: str, upload: Any) -> Path:
    """形态一上传落盘：JSON/JSONL 直接用；ZIP 安全解压后定位入口；其余拒绝。"""
    input_dir = output_root / task_id / "input"
    if input_dir.exists():
        shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(upload.filename or "upload.bin").name
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        archive_path = input_dir / filename
        archive_path.write_bytes(upload.file.read())
        io_utils.safe_extract_zip(archive_path, input_dir,
                                  max_unpacked_bytes=MAX_UPLOAD_BYTES)
        archive_path.unlink(missing_ok=True)
        entry_path, _entry_name = transport.resolve_entrypoint(input_dir)
        return entry_path
    if suffix in (".jsonl", ".json"):
        target = input_dir / filename
        target.write_bytes(upload.file.read())
        return target
    raise ValueError("该算子需要 JSON/JSONL 输入")


async def _predict_single_file(request: Request) -> JSONResponse:
    """[v4.2] 形态一（单文件流）：处理型 status（1 成功/0 失败）。"""
    try:
        error = _check_detection(request)
        if error:
            return JSONResponse({"status": 0, "msg": error})
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "filename"):
            return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
        uid_raw = (request.query_params.get("uniqueId") or form.get("uniqueId")
                   or _new_unique_id())
        task_id = io_utils.validate_unique_id(str(uid_raw))
        output_root = Path(inference.default_output_dir())
        entry_path = _store_single_file(output_root, task_id, upload)
        overrides = _form_overrides(form)
        result = inference.run(input_file=str(entry_path), unique_id=task_id,
                               output_root=str(output_root),
                               overrides=overrides or None,
                               allow_external_input=False)
        if result.get("status") == "failed":
            return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})
        run_dir = Path(result["runDir"])
        stats = result.get("statistics", {})
        package_ref = os.path.relpath(result["packagePath"], run_dir)
        manifest_ref = os.path.relpath(result["manifestPath"], run_dir)
        # [v4.2] §4.23 形态一返回字段：publish_status（pending/deliverable）/
        # package / manifest / missing_materials（保留原有 package_zip 等字段兼容测试）
        publish_status = ("deliverable" if result.get("deliverable")
                          or stats.get("deliverable") else "pending")
        detail = {
            "status": 1,
            "publish_status": publish_status,
            "package": package_ref,
            "manifest": manifest_ref,
            "missing_materials": list(stats.get("deliverable_missing_requirements")
                                      or stats.get("materials_missing") or []),
            "package_zip": package_ref,
            "package_manifest": manifest_ref,
            "status_label": result.get("statusLabel")
            or stats.get("status_label"),
            "media_count": int(stats.get("media_carried", 0)),
            "sample_count": int(stats.get("packaged_records", 0)),
        }
        if Path(upload.filename or '').suffix.lower() == '.zip':
            try:
                detail['outputFile'] = form1_package.build_reference(run_dir, result.get('entrypoint'), media_policy='reference')
            except Exception as exc:
                detail.update(status=0, package_error=str(exc)[:200])
        return JSONResponse(detail)
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except Exception as exc:  # 形态一统一失败出口
        return JSONResponse({"status": 0, "msg": "%s: %s" % (type(exc).__name__, exc)})


# --------------------------------------------------------------------------- #
# /removeFile?uniqueId=
# --------------------------------------------------------------------------- #

@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(..., description="任务标识")) -> JSONResponse:
    try:
        task_id = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return _error(422, str(exc), {"errorType": "InvalidUniqueId"})
    removed = io_utils.remove_task(Path(inference.default_output_dir()), task_id)
    logger.info("清理任务 uniqueId=%s removed=%s", task_id, removed)
    return JSONResponse(status_code=200,
                        content=envelope(0, "任务已清理" if removed else "任务不存在（幂等）",
                                         {"uniqueId": task_id, "removed": removed}))


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":                       # pragma: no cover
    uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT)
