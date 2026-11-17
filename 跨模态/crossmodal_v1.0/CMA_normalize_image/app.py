"""算子 4 三接口服务：``/predict``、``/downloadFile``、``/removeFile``。

接口约定（DEVELOPMENT_CONTRACT §2、§3，[v4.2] 刷新）：
- 只实现三个接口，``/downloadFile`` 保留路径但明确返回 404（本 demo 不实现下载）；
- [v4.2] ``/predict`` 双协议：形态二（``application/json`` 或带 ``request`` 字段的
  multipart）统一返回 ``data{operator, status, outputFile, result}``，
  不再区分阶段末/非阶段末、不再返回血缘；形态一（单文件流）为不带 ``request``
  字段的 ``multipart/form-data`` + 查询参数 ``detection``/``operatorId``
  （别名 M05/05/5/normalize_image/image_standardization/目录名，大小写不敏感），
  成功时返回标准化图像文件流，失败时返回 ``{"status": 0, "msg": ...}``；
- ``/health`` 为背景规则 §3 要求的存活探针。

启动::

    uvicorn app:app --host 0.0.0.0 --port 8104
"""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inference  # noqa: E402
from utils import form1_package  # noqa: E402
from utils import transport  # noqa: E402
from utils.io import (TaskBusyError, TaskLock, commit_directory,  # noqa: E402
                      remove_task, resolve_in_task, safe_extract_zip, task_dir,
                      validate_unique_id)
from utils.logger import get_logger, log_action  # noqa: E402
from utils.schemas import PredictRequest, RemoveFileRequest  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = os.getenv("OPERATOR_CONFIG", str(BASE_DIR / "configs" / "config.yaml"))

app = FastAPI(
    title="跨模态 demo 算子 4：图像标准化处理",
    version="v1.0.0",
    description="解码校验、EXIF 方向归一、等比尺寸/格式标准化与清晰度启发式标记。",
)

_logger = get_logger("app")


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    kind = "not_implemented" if exc.status_code == 404 and _request.url.path == "/downloadFile" else ("not_found" if exc.status_code == 404 else "http_error")
    return error_response(exc.status_code, str(exc.detail), {"error": kind})


class RequestError(Exception):
    """请求级错误（安全/参数），映射为规范信封。"""

    def __init__(self, status_code: int, message: str, error: str = "invalid_request",
                 data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error = error
        self.data = data


def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": data or {}}


def error_response(status_code: int, msg: str,
                   data: Optional[Dict[str, Any]] = None) -> JSONResponse:
    """失败信封：``success`` 恒为 false，绝不携带成功产物。"""
    payload = envelope(0 if status_code < 400 else 1, msg, data)
    payload["success"] = False
    payload["message"] = msg
    return JSONResponse(status_code=status_code, content=payload)


def _deployment() -> Tuple[Any, Path]:
    try:
        cfg, _snapshot = inference.load_config(CONFIG_PATH, None)
    except Exception as exc:  # noqa: BLE001 - 配置坏了必须明确报错
        raise RequestError(500, "服务端配置不可用：%s" % type(exc).__name__, "config_error")
    return cfg, inference.resolve_output_root(cfg)


async def _parse_payload(request: Request) -> Tuple[Dict[str, Any], Any]:
    """解析请求体，返回 ``(原始 payload, 上传文件或 None)``。"""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise RequestError(400, "请求体不是合法 JSON：%s" % type(exc).__name__)
        if not isinstance(payload, dict):
            raise RequestError(400, "请求体必须是 JSON 对象")
        return payload, None

    if content_type.startswith("multipart/form-data") or content_type.startswith(
            "application/x-www-form-urlencoded"):
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001
            raise RequestError(400, "multipart 解析失败：%s" % type(exc).__name__)
        raw_request = form.get("request")
        if raw_request is None:
            raise RequestError(400, "multipart 缺少 request 字段（JSON 字符串）")
        if not isinstance(raw_request, str):
            raise RequestError(400, "request 字段必须是 JSON 字符串")
        try:
            payload = json.loads(raw_request)
        except json.JSONDecodeError:
            raise RequestError(400, "request 字段不是合法 JSON")
        if not isinstance(payload, dict):
            raise RequestError(400, "request 字段必须是 JSON 对象")
        upload = form.get("file")
        if upload is not None and not hasattr(upload, "read"):
            upload = None
        return payload, upload

    raise RequestError(415, "不支持的 Content-Type：%s" % (content_type or "unknown"))


def _parse_model(payload: Dict[str, Any]) -> PredictRequest:
    """校验 predict 参数；未知字段、非严格布尔、非对象 config 一律 422。"""
    merged = dict(payload)
    overrides = merged.get("config")
    if overrides is not None and not isinstance(overrides, dict):
        raise RequestError(422, "config 必须是对象", "validation_failed")
    try:
        return PredictRequest(**merged)
    except ValidationError as exc:
        details = [
            {"loc": ".".join(str(part) for part in item.get("loc", ())),
             "type": str(item.get("type"))}
            for item in exc.errors()[:10]
        ]
        raise RequestError(422, "参数校验失败：%s" % json.dumps(details, ensure_ascii=False),
                           "validation_failed") from None


# --------------------------------------------------------------------------- #
# 上传素材与入口定位
# --------------------------------------------------------------------------- #

async def _stage_upload(upload, task: Path, cfg: Any) -> Path:
    """把上传的 zip 安全解压并原子提交到 ``<task>/input``（重复调用幂等）。"""
    limits = cfg.server
    temp_root = task / ".incoming"
    if temp_root.exists():
        shutil.rmtree(temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    zip_path = temp_root / "upload.zip"
    written = 0
    limit_bytes = limits.max_unpacked_bytes * 2
    with open(zip_path, "wb") as stream:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > limit_bytes:
                shutil.rmtree(temp_root, ignore_errors=True)
                raise RequestError(413, "上传包超过限额 %d 字节" % limit_bytes,
                                   "upload_too_large")
            stream.write(chunk)
    if written == 0:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise RequestError(400, "上传文件为空", "empty_upload")

    extract_dir = temp_root / "extract"
    try:
        safe_extract_zip(zip_path, extract_dir, limits.max_files, limits.max_unpacked_bytes)
    except (ValueError, OSError) as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise RequestError(400, "素材包解压失败：%s" % exc, "invalid_archive") from None
    try:
        transport.resolve_entrypoint(extract_dir)
    except transport.TransportError as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise RequestError(400, "素材包缺少可识别的 JSONL 入口：%s" % exc,
                           "invalid_archive") from None

    destination = task / "input"
    staging = temp_root / "input"
    os.replace(str(extract_dir), str(staging))
    if destination.exists():
        shutil.rmtree(destination)
    commit_directory(staging, destination)
    shutil.rmtree(temp_root, ignore_errors=True)
    member_count = sum(1 for item in destination.rglob("*") if item.is_file())
    log_action(_logger, "上传", "已提交素材包到 input/，成员=%d 上传字节=%d" % (
        member_count, written))
    return destination


def _locate_entry(task: Path, input_file: Optional[str]) -> Path:
    """定位入口 JSONL：有 inputFile 用 inputFile，否则按 manifest 在 input/ 中查找。"""
    input_dir = task / "input"
    if input_file:
        try:
            target = resolve_in_task(task, input_file)
        except ValueError as exc:
            raise RequestError(400, "inputFile 非法：%s" % exc, "invalid_input_file") from None
        if not target.is_file():
            raise RequestError(400, "inputFile 不存在", "invalid_input_file")
        return target
    if not input_dir.exists():
        raise RequestError(400, "任务没有可用输入：未上传素材且 input/ 不存在",
                           "input_missing")
    try:
        entry, _relative = transport.resolve_entrypoint(input_dir)
    except transport.TransportError as exc:
        raise RequestError(400, "无法定位输入入口：%s" % exc, "input_missing") from None
    return entry


# --------------------------------------------------------------------------- #
# 响应装配
# --------------------------------------------------------------------------- #

def _pack_output_object(result: Dict[str, Any], cfg: Any) -> Dict[str, Any]:
    run_dir = Path(result["run_dir"])
    manifest = transport.load_manifest(run_dir)
    if manifest is None:
        raise transport.TransportError("运行目录缺少 manifest.json")
    entrypoint = manifest["entrypoint"]
    paths = transport.iter_manifest_paths(manifest)
    paths.append(transport.MANIFEST_NAME)
    return transport.pack_file_object(run_dir, entrypoint, paths,
                                     cfg.server.max_response_bytes)


def _statistics_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    statistics = result.get("statistics") or {}
    return {key: value for key, value in statistics.items() if key != "conservation_ok"}


def _identity(result: Dict[str, Any], cfg: Any, unique_id: str) -> Dict[str, Any]:
    return {
        "operator": cfg.operator.name,
        "operator_version": cfg.operator.version,
        "stageId": cfg.stage_id,
        "uniqueId": unique_id,
        "runId": result.get("run_id"),
    }


def _failure(result: Dict[str, Any], cfg: Any, unique_id: str) -> JSONResponse:
    log_action(_logger, "失败", "predict 结束：%s" % result.get("message"))
    message = result.get("message") or "处理失败"
    payload = envelope(1, message, dict(_identity(result, cfg, unique_id), **{
        "error": result.get("code") or "processing_failed",
        "status": result.get("status"),
        "statistics": _statistics_summary(result),
    }))
    payload["success"] = False
    payload["message"] = message
    return JSONResponse(status_code=200, content=payload)


def _build_predict_response(result: Dict[str, Any], model: PredictRequest,
                            cfg: Any, unique_id: str) -> JSONResponse:
    status = result.get("status")
    identity = _identity(result, cfg, unique_id)

    if status == "failed":
        return _failure(result, cfg, unique_id)

    # [v4.2] 形态二响应统一：不再区分阶段末/非阶段末，始终返回 outputFile + result，
    # 不再返回 lineageFile；媒体引用校验沿用原阶段末口径（isLastOperator=true 时执行）。
    try:
        if bool(model.isLastOperator):
            entry = result["output_file"]
            problems = inference.verify_stage_media(entry, Path(result["run_dir"]))
            if problems:
                payload = envelope(1, "阶段末媒体引用不可读：%d 处" % len(problems),
                                   dict(identity, status="failed",
                                        error="media_unreadable",
                                        media_errors=problems[:20]))
                payload["success"] = False
                log_action(_logger, "失败", "阶段末媒体门禁未通过：%d 处" % len(problems))
                return JSONResponse(status_code=200, content=payload)
        file_object = _pack_output_object(result, cfg)
    except transport.OutputTooLargeError as exc:
        payload = envelope(1, str(exc), dict(identity, status=status,
                                            error="output_too_large"))
        payload["success"] = False
        return JSONResponse(status_code=413, content=payload)
    except transport.TransportError as exc:
        payload = envelope(1, "输出文件对象打包失败：%s" % exc,
                           dict(identity, status=status, error="pack_failed"))
        payload["success"] = False
        return JSONResponse(status_code=500, content=payload)
    statistics = result.get("statistics") or {}
    data: Dict[str, Any] = {
        "operator": "normalize_image",
        "status": "success",
        "outputFile": file_object,
        "result": {
            "process_status": status,
            "image": inference.MAIN_NAME,
            "format": statistics.get("output_format"),
            "max_side": statistics.get("max_side"),
            "statistics": _statistics_summary(result),
        },
    }
    message = result.get("message") or "处理完成"

    payload = envelope(0, message, data)
    payload["success"] = True
    payload["message"] = message
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > cfg.server.max_response_bytes:
        oversized = envelope(1, "响应体 %d 字节超过限额 %d 字节" % (
            len(encoded), cfg.server.max_response_bytes),
            dict(identity, status=status, error="output_too_large"))
        oversized["success"] = False
        return JSONResponse(status_code=413, content=oversized)
    log_action(_logger, "输出", "predict 结束：status=%s 响应=%d 字节" % (status, len(encoded)))
    return JSONResponse(status_code=200, content=payload)


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #

@app.post("/uploadFile")
async def upload_file(uniqueId: Optional[str] = None,
                      file: Optional[UploadFile] = File(None)) -> JSONResponse:
    try:
        if not uniqueId:
            raise RequestError(422, "缺少 uniqueId", "invalid_unique_id")
        cfg, root = _deployment()
        task_dir(root, uniqueId)
        if file is None:
            raise RequestError(422, "缺少上传文件 file", "invalid_upload")
        with TaskLock(root, uniqueId):
            destination = await _stage_upload(file, task_dir(root, uniqueId), cfg)
        _entry, relative = transport.resolve_entrypoint(destination)
        return JSONResponse(status_code=200, content=envelope(0, "success", {"entrypoint": relative}))
    except RequestError as exc:
        return error_response(exc.status_code, exc.message, {"error": exc.error})
    except TaskBusyError:
        return error_response(409, "任务正在被另一个写操作占用", {"error": "task_busy"})
    except Exception as exc:  # noqa: BLE001
        return error_response(422, "上传失败：%s" % type(exc).__name__, {"error": "invalid_upload"})

@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """存活探针。"""
    return JSONResponse(status_code=200, content={"status": "ok",
                                                  "operator": inference.OPERATOR_DIR_NAME})


# --------------------------------------------------------------------------- #
# [v4.2] /predict 双协议：detection 别名校验 + 形态一（单文件流，成功返回文件流）
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M05/05/5/英文ID/目录名
DETECTION_ALIASES = {"normalize_image", "image_standardization", "m05", "05", "5",
                     "cma_normalize_image"}

#: 形态一接受的图像扩展名（交给 Pillow 解码校验）
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}

#: 输出格式 -> 响应媒体类型
_FORMAT_MEDIA_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png"}


def _check_detection(request: Request) -> Optional[str]:
    """detection/operatorId 别名校验（宽松）：缺失或与算子别名一致即放行，不一致报错。"""
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return "detection 与 operatorId 取值不一致"
    value = (detection or operator_id or "").strip().lower()
    if value and value not in DETECTION_ALIASES:
        return "detection 参数与算子标识不符：%s" % value
    return None


async def _predict_single_file(request: Request, form, cfg, root: Path):
    """[v4.2] 形态一（单文件流）：上传单个图像返回标准化图像文件流。

    [v4.3] 上传内容为压缩包时改走列表/阶段推理，返回容器内路径引用 JSON。
    """
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    uid_raw = (request.query_params.get("uniqueId") or form.get("uniqueId")
               or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    try:
        unique_id = validate_unique_id(str(uid_raw))
        filename = Path(upload.filename or "image.jpg").name
        suffix = Path(filename).suffix.lower()
        payload_bytes = await upload.read()
        if not payload_bytes:
            return JSONResponse({"status": 0, "msg": "上传内容为空"})
        # [v4.3] 先判断压缩包；非压缩包仍按单图文件流处理（向后兼容）
        is_package = zipfile.is_zipfile(io.BytesIO(payload_bytes))
        if not is_package and suffix not in IMAGE_SUFFIXES:
            return JSONResponse({"status": 0, "msg": "仅支持单个图像文件或 zip 素材包"})
        # [v4.2] 表单覆盖：白名单标量转换，未知字段忽略；形态一缺省开启图像标准化
        overrides: Dict[str, Any] = {}
        enabled_form = form.get("enabled")
        overrides["enabled"] = (
            str(enabled_form).strip().lower() in ("1", "true", "yes", "on")
            if enabled_form is not None else True)
        file_type = form.get("fileType")
        if file_type is not None:
            value = str(file_type).strip().lower()
            if value in ("jpg", "jpeg"):
                overrides["output_format"] = "JPEG"
            elif value == "png":
                overrides["output_format"] = "PNG"
            else:
                return JSONResponse({"status": 0, "msg": "fileType 仅支持 jpg/png"})
        max_edge = form.get("max_long_edge")
        if max_edge is not None:
            try:
                overrides["max_side"] = int(str(max_edge).strip())
            except ValueError:
                return JSONResponse({"status": 0, "msg": "max_long_edge 必须是整数"})
        task = task_dir(root, unique_id)
        input_dir = task / "input"
        if input_dir.exists():
            shutil.rmtree(input_dir)
        input_dir.mkdir(parents=True)
        stem = Path(filename).stem or unique_id
        if is_package:
            # [v4.3] 压缩包：安全解压到任务 input/，入口优先 manifest.json
            try:
                form1_package.extract_zip_into(payload_bytes, input_dir)
                entry = form1_package.resolve_entrypoint(input_dir)
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                return JSONResponse({"status": 0, "msg": "素材包不可用：%s" % exc})
        else:
            # [v4.2] 单个图像文件合成单记录 JSONL（image 字段登记文件名）
            media_name = "media/" + filename
            media_path = input_dir / media_name
            media_path.parent.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes(payload_bytes)
            entry = input_dir / (stem + ".jsonl")
            record = {"sample_id": stem, "image": media_name}
            entry.write_text(json.dumps(record, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        result = inference.run(str(entry), unique_id, True, str(root),
                               config_path=CONFIG_PATH, overrides=overrides)
    except RequestError as exc:
        return JSONResponse({"status": 0, "msg": exc.message})
    except Exception as exc:  # noqa: BLE001 - 形态一失败返回 status 0
        return JSONResponse({"status": 0, "msg": str(exc)[:200]})
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})
    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            run_root = Path(result.get("runDir") or result.get("run_dir") or "")
            entry_hint = (result.get("entrypoint")
                          or Path(result.get("outputFile")
                                  or result.get("output_file") or "").name)
            reference = form1_package.build_reference(run_root, entry_hint)
        except (OSError, ValueError) as exc:
            return JSONResponse({"status": 0, "msg": "输出打包失败：%s" % str(exc)[:200]})
        statistics = result.get("statistics") or {}
        business = {"image": inference.MAIN_NAME,
                    "format": statistics.get("output_format"),
                    "max_side": statistics.get("max_side")}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator="normalize_image",
            unique_id=unique_id, business=business))
    run_dir = Path(result["run_dir"])
    images_dir = run_dir / inference.IMAGES_DIR
    produced = sorted(p for p in images_dir.glob("*") if p.is_file()) \
        if images_dir.is_dir() else []
    if not produced:
        statistics = result.get("statistics") or {}
        reason = "no_output"
        rejects = run_dir / inference.REJECT_NAME
        if rejects.is_file() and rejects.stat().st_size:
            first = json.loads(rejects.read_text(encoding="utf-8").splitlines()[0])
            reason = str(first.get("reason") or reason)
        return JSONResponse({"status": 0, "process_status": "failed",
                             "msg": "图像标准化失败：%s" % reason})
    target = produced[0]
    output_format = (result.get("statistics") or {}).get("output_format", "JPEG")
    media_type = _FORMAT_MEDIA_TYPES.get(str(output_format).upper(), "image/jpeg")
    return FileResponse(str(target), media_type=media_type, filename=target.name)


@app.post("/predict")
async def predict(request: Request) -> JSONResponse:
    """[v4.2] 双协议：multipart 单文件流（成功返回文件流）/ JSON 阶段文件传递。"""
    cfg, root = _deployment()
    alias_error = _check_detection(request)
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ctype:
        form = await request.form()
        if "request" not in form:
            # [v4.2] 形态一：单文件流协议（无 request 字段的 multipart）
            if alias_error:
                return JSONResponse({"status": 0, "msg": alias_error})
            return await _predict_single_file(request, form, cfg, root)
    if alias_error:
        return error_response(400, alias_error, {"error": "invalid_detection"})
    try:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cfg.server.max_unpacked_bytes:
            raise RequestError(413, "请求体 %s 字节超过限额 %d 字节" % (
                declared, cfg.server.max_unpacked_bytes), "request_too_large")

        payload, upload = await _parse_payload(request)
        model = _parse_model(payload)
        try:
            task_dir(root, model.uniqueId)
        except ValueError as exc:
            raise RequestError(400, "uniqueId 非法：%s" % exc, "invalid_unique_id") from None
        log_action(_logger, "请求", "predict uniqueId=%s last=%s upload=%s" % (
            model.uniqueId, bool(model.isLastOperator), upload is not None))

        with TaskLock(root, model.uniqueId):
            task = task_dir(root, model.uniqueId)
            if upload is not None:
                await _stage_upload(upload, task, cfg)
            entry = _locate_entry(task, model.inputFile)
            result = inference.run(str(entry), model.uniqueId, bool(model.isLastOperator),
                                   str(root), config_path=CONFIG_PATH,
                                   overrides=model.config)
            # [v4.2] 血缘/阶段归档已停用：mty_compliance 不再调用。
            # from utils import mty_compliance as _mty_compliance
            # _mty_compliance.prepare_stage_result(result, model.isLastOperator)
        return _build_predict_response(result, model, cfg, model.uniqueId)
    except RequestError as exc:
        log_action(_logger, "拒绝", "%s：%s" % (exc.error, exc.message))
        return error_response(exc.status_code, exc.message,
                              {"error": exc.error, **(exc.data or {})})
    except TaskBusyError:
        log_action(_logger, "拒绝", "task_busy")
        return error_response(409, "任务正在被另一个写操作占用", {"error": "task_busy"})
    except Exception as exc:  # noqa: BLE001 - 兜底不泄漏堆栈
        log_action(_logger, "失败", "predict 异常：%s" % type(exc).__name__)
        return error_response(500, "服务内部错误：%s" % type(exc).__name__,
                              {"error": "internal_error"})


@app.post("/removeFile")
async def remove_file(uniqueId: Optional[str] = None, request: Request = None) -> JSONResponse:
    """清理任务目录；任务不存在时明确返回 deleted=false，保证幂等。"""
    cfg, root = _deployment()
    try:
        # 只校验 uniqueId，不创建任务目录：否则「已删除」的任务会被重新建出来。
        if not uniqueId and request is not None:
            try:
                body = await request.json()
                uniqueId = body.get("uniqueId") if isinstance(body, dict) else None
            except Exception:
                uniqueId = None
        if not uniqueId:
            return error_response(422, "缺少 uniqueId", {"error": "invalid_unique_id"})
        task_dir(root, uniqueId, create=False)
    except ValueError as exc:
        return error_response(400, "uniqueId 非法：%s" % exc, {"error": "invalid_unique_id"})
    try:
        removed = remove_task(root, uniqueId)
    except TaskBusyError:
        return error_response(409, "任务正在被另一个写操作占用", {"error": "task_busy"})
    except OSError as exc:
        return error_response(500, "任务目录删除失败：%s" % type(exc).__name__,
                              {"error": "remove_failed"})
    log_action(_logger, "清理", "removeFile uniqueId=%s removed=%s" % (uniqueId, removed))
    if not removed:
        response = envelope(1, "任务不存在或已删除",
                            {"uniqueId": uniqueId, "deleted": False})
        response["success"] = False
        response["message"] = "任务不存在或已删除"
        return JSONResponse(status_code=200, content=response)
    response = envelope(0, "任务已删除", {"uniqueId": uniqueId, "deleted": True})
    response["success"] = True
    response["message"] = "任务已删除"
    return JSONResponse(status_code=200, content=response)
