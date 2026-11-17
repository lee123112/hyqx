"""HTTP 接口层：**只注册三个正式接口**（禁止实现 ``/downloadFile``）。

- ``POST /uploadFile?uniqueId=``  上传任务压缩包（zip），解压到 ``OUTPUT_DIR/<uniqueId>/input/``；
- ``POST /predict``               执行 Token 生成；响应统一返回 ``outputFile``
                                  （crossmodal-files-v1 文件对象）+ ``result`` 业务摘要，
                                  不再返回 ``lineageFile``/阶段血缘数组；
- ``POST /removeFile?uniqueId=``  清理任务目录（幂等）。

所有响应外层统一为 ``{"code": 0 或 1, "msg": "...", "data": {...}}``。
"""
from __future__ import annotations

import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

# [v4.2] 血缘输出已停用：build_intermediate_lineage / build_stage_payload 不再导入
from inference import (InputError, default_config_path, default_output_dir, run)
from utils import io as io_utils
from utils import names, transport, form1_package, material_handoff
from utils.logger import get_logger
from utils.schemas import ConfigError, Envelope, PredictRequest, validate_overrides

# [v4.2] 血缘/阶段归档输出已停用：mty_compliance 不再被调用（utils/mty_compliance.py 保留存档）

__all__ = ["app", "create_app"]

logger = get_logger("operator_22.api")

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 1800

#: [v4.2] 形态一（单文件流）算子标识：detection 与 operatorId 同一别名空间（大小写不敏感）
ENGLISH_ID = "generate_crossmodal_tokens"
DETECTION_ALIASES = frozenset({
    "m22", "22", ENGLISH_ID, "cma_" + ENGLISH_ID, "crossmodal_token",
})

#: [v4.2] 形态一 config 白名单标量（与 OVERRIDABLE_KEYS 对齐；routes 非标量不接受表单形式）
_FORM_SCALAR_TYPES = {
    "enabled": "bool",
    "missing_quality_policy": "str",
}

app = FastAPI(title="算子 22 生成跨模态Token",
              description="表1 v4.1 #22：把可交付的对齐样本写成带模态标记的 Token 与样本文件",
              version=names.OPERATOR_VERSION)

# 明确声明不提供下载接口
DOWNLOAD_ENDPOINT = "not_registered"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _output_root() -> Path:
    root = default_output_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _task_dir(unique_id: str) -> Path:
    return (_output_root() / unique_id).resolve()


def _input_dir(unique_id: str) -> Path:
    return _task_dir(unique_id) / "input"


def _error(status_code: int, message: str, error_type: str = "RequestError",
           request_id: Optional[str] = None) -> Any:
    from fastapi.responses import JSONResponse

    data: Dict[str, Any] = {"errorType": error_type}
    if request_id:
        data["requestId"] = request_id
    return JSONResponse(status_code=status_code,
                        content={"code": 1, "msg": message, "data": data})


def _ok(message: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return Envelope(code=0, msg=message, data=data).model_dump()


# --------------------------------------------------------------------------- #
# 健康检查（非正式接口，仅用于探活）
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    from inference import load_config

    try:
        config = load_config(str(default_config_path()))
        business = config.business_snapshot()
        config_status = "ok"
    except (InputError, ConfigError) as exc:                # pragma: no cover
        business = {"error": str(exc)}
        config_status = "invalid"
    return _ok("ok", {"operator": names.OPERATOR,
                      "operator_label": names.OPERATOR_LABEL,
                      "operator_version": names.OPERATOR_VERSION,
                      "formal_operator_number": names.OPERATOR_NUMBER,
                      "stage_id": names.STAGE_ID,
                      "config_status": config_status,
                      "business": business,
                      "downloadFile": DOWNLOAD_ENDPOINT,
                      "model": None,
                      "modelsAvailable": False,
                      "ruleBased": True})


# --------------------------------------------------------------------------- #
# 1) 上传
# --------------------------------------------------------------------------- #

@app.post("/uploadFile")
async def upload_file(request: Request,
                      uniqueId: str = Query(..., description="任务标识"),
                      file: Optional[UploadFile] = File(None)) -> Any:
    try:
        task_id = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return _error(422, "uniqueId 非法：%s" % exc, "InvalidUniqueId")

    if file is None:
        return _error(422, "缺少上传文件字段 file", "MissingFile")

    payload = await file.read()
    if not payload:
        return _error(422, "上传文件为空", "EmptyFile")
    if len(payload) > MAX_UPLOAD_BYTES:
        return _error(422, "上传文件超过 %d 字节" % MAX_UPLOAD_BYTES, "FileTooLarge")

    task_dir = _task_dir(task_id)
    input_dir = _input_dir(task_id)
    staging = task_dir / "input.staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / "upload.zip"
    archive.write_bytes(payload)

    try:
        counter = 0

        def _check(zip_file: zipfile.ZipFile) -> None:
            nonlocal counter
            counter = len(zip_file.infolist())

        with zipfile.ZipFile(archive) as bundle:
            _check(bundle)
        extracted = io_utils.safe_extract_zip(archive, staging,
                                              max_unpacked_bytes=MAX_UPLOAD_BYTES)
    except zipfile.BadZipFile:
        shutil.rmtree(staging, ignore_errors=True)
        return _error(422, "上传内容不是合法的 zip 压缩包", "BadZipFile")
    except (ValueError, io_utils.UnsupportedMediaPath) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return _error(422, "压缩包内容非法：%s" % exc, "UnsafeArchive")
    except OSError as exc:                                   # pragma: no cover
        shutil.rmtree(staging, ignore_errors=True)
        return _error(422, "解压失败：%s" % exc, "ExtractFailed")

    archive.unlink(missing_ok=True)
    if input_dir.exists():
        shutil.rmtree(input_dir, ignore_errors=True)
    staging.rename(input_dir)

    try:
        entry_path, entry_name = transport.resolve_entrypoint(input_dir)
    except transport.TransportError as exc:
        return _error(422, "无法确定输入入口：%s" % exc, "EntryPointNotFound")

    logger.info("upload ok uniqueId=%s files=%d entrypoint=%s", task_id,
                len(extracted), entry_name)
    return _ok("上传成功", {"uniqueId": task_id, "operator": names.OPERATOR,
                          "fileCount": len(extracted), "entrypoint": entry_name,
                          "entrypointSize": entry_path.stat().st_size,
                          "files": extracted})


# --------------------------------------------------------------------------- #
# 2) 推理
# --------------------------------------------------------------------------- #

@app.post("/predict")
async def predict(request: Request) -> Any:
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
                      "InvalidRequest")
    try:
        raw = await request.json()
    except Exception:                                        # noqa: BLE001
        return _error(422, "请求体必须是 JSON 对象", "InvalidBody")
    if not isinstance(raw, dict):
        return _error(422, "请求体必须是 JSON 对象", "InvalidBody")

    try:
        payload = PredictRequest(**raw)
    except ValidationError as exc:
        messages = "; ".join(
            "%s: %s" % (".".join(str(item) for item in error.get("loc", ())),
                        error.get("msg", "非法取值"))
            for error in exc.errors())
        return _error(422, "请求参数非法：%s" % messages, "InvalidRequest")

    task_id = payload.uniqueId
    task_dir = _task_dir(task_id)
    input_dir = _input_dir(task_id)
    config_path = str(default_config_path())

    try:
        overrides = validate_overrides(payload.overrides)
    except ConfigError as exc:
        return _error(400, str(exc), "ConfigError")

    if not input_dir.is_dir():
        return _error(400, "任务 %s 尚未上传输入，请先调用 /uploadFile" % task_id,
                      "InputNotUploaded")

    if payload.inputFile:
        try:
            input_path = io_utils.resolve_in_task(task_dir, payload.inputFile)
        except (ValueError, io_utils.UnsupportedMediaPath) as exc:
            return _error(400, "inputFile 非法：%s" % exc, "InvalidInputFile")
        if not input_path.is_file():
            return _error(400, "inputFile 不是文件或不存在：%s" % input_path,
                          "InvalidInputFile")
        allow_external = False
    else:
        try:
            input_path, _entry = transport.resolve_entrypoint(input_dir)
        except transport.TransportError as exc:
            return _error(400, "无法确定输入入口：%s" % exc, "EntryPointNotFound")
        input_path = input_path.resolve()
        allow_external = False

    logger.info("predict start uniqueId=%s isLastOperator=%s input=%s", task_id,
                payload.isLastOperator, input_path.name)
    try:
        result = run(input_file=str(input_path), unique_id=task_id,
                     config_path=config_path, output_root=str(_output_root()),
                     overrides=overrides or None,
                     allow_external_input=allow_external)
        # [v4.2] 血缘/阶段归档输出已停用：不再调用 prepare_stage_result，
        # 响应不再区分阶段末/非阶段末，统一返回 outputFile（crossmodal-files-v1）。
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
    except (InputError, ConfigError, io_utils.TaskBusyError) as exc:
        return _error(400, str(exc), type(exc).__name__)
    except ValueError as exc:
        return _error(400, "执行失败：%s" % exc, "ExecutionError")

    run_dir = Path(result["runDir"])
    entrypoint = result["entrypoint"]
    paths = [item["path"] for item in result["artifacts"]]
    paths.append(transport.MANIFEST_NAME)
    if entrypoint not in paths:
        paths.insert(0, entrypoint)
    try:
        file_object = transport.pack_file_object(
            run_dir, entrypoint, paths,
            max_response_bytes=result["transport"]["maxResponseBytes"])
    except transport.OutputTooLargeError as exc:
        return _error(413, "阶段末产物超过响应上限：%s" % exc, "OutputTooLarge")
    except transport.TransportError as exc:
        return _error(500, "阶段末产物打包失败：%s" % exc, "PackFailed")
    data = {"operator": ENGLISH_ID,
            "status": result["status"],
            "outputFile": file_object,
            "result": _business_summary(result)}
    return _ok(result["message"], data)


def _business_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """形态二 ``data.result``：业务摘要（保留链式调用所需的输入/输出路径）。"""
    return {
        "uniqueId": result.get("uniqueId"),
        "runDir": result.get("runDir"),
        "inputFile": result.get("inputFile"),
        "outputFile": result.get("outputFile"),
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
    """表单标量 → config 覆盖：白名单内字符串转数字/布尔，未知字段忽略（形态一宽松）。"""
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
            elif kind == "str":
                overrides[key] = text
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
    return overrides


def _store_single_file(task_id: str, upload: Any) -> Path:
    """形态一上传落盘：JSON/JSONL 直接用；ZIP 安全解压后定位入口；其余拒绝。"""
    input_dir = _input_dir(task_id)
    if input_dir.exists():
        shutil.rmtree(input_dir, ignore_errors=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(upload.filename or "upload.bin").name
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        archive = input_dir / filename
        archive.write_bytes(upload.file.read())
        io_utils.safe_extract_zip(archive, input_dir,
                                  max_unpacked_bytes=MAX_UPLOAD_BYTES)
        archive.unlink(missing_ok=True)
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
        entry_path = _store_single_file(task_id, upload)
        overrides = _form_overrides(form)
        result = run(input_file=str(entry_path), unique_id=task_id,
                     config_path=str(default_config_path()),
                     output_root=str(_output_root()),
                     overrides=overrides or None,
                     allow_external_input=False)
        if result.get("status") == "failed":
            return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})
        run_dir = Path(result["runDir"])
        stats = result.get("statistics", {})
        detail = {
            "status": 1,
            "operator": ENGLISH_ID,
            "uniqueId": task_id,
            "runDir": str(run_dir),
            "outputFile": str(result["outputFile"]),
            "token_records": os.path.relpath(result["tokensFile"], run_dir),
            "samples_jsonl": os.path.relpath(result["outputFile"], run_dir),
            "manifest": os.path.relpath(result["manifestPath"], run_dir),
            "sample_count": int(stats.get("generated_records", 0)),
            "isolated_skipped_count": int(stats.get("isolated_skipped_records", 0)),
            "pending_count": int(stats.get("pending_records", 0)),
            "process_status": result.get("status"),
            "compressed": bool(stats.get("compressed", False)),
            "version_state_changed": bool(stats.get("version_state_changed", False)),
        }
        if Path(upload.filename or '').suffix.lower() == '.zip':
            detail['samples_file'] = str(result['outputFile'])
            try:
                material_handoff.forward(_input_dir(task_id), run_dir)
                detail['outputFile'] = form1_package.build_reference(run_dir, detail['samples_jsonl'])
            except Exception as exc:
                detail.update(status=0, package_error=str(exc)[:200])
        return JSONResponse(detail)
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except Exception as exc:  # 形态一统一失败出口
        return JSONResponse({"status": 0, "msg": "%s: %s" % (type(exc).__name__, exc)})


# --------------------------------------------------------------------------- #
# 3) 清理
# --------------------------------------------------------------------------- #

@app.post("/removeFile")
async def remove_file(request: Request,
                      uniqueId: str = Query(..., description="任务标识")) -> Any:
    try:
        task_id = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return _error(422, "uniqueId 非法：%s" % exc, "InvalidUniqueId")
    removed = io_utils.remove_task(_output_root(), task_id)
    logger.info("removeFile uniqueId=%s removed=%s", task_id, removed)
    return _ok("清理完成" if removed else "任务目录不存在，无需清理",
               {"uniqueId": task_id, "removed": removed})


def create_app() -> FastAPI:                                 # pragma: no cover
    return app


if __name__ == "__main__":                                   # pragma: no cover
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8122")))
