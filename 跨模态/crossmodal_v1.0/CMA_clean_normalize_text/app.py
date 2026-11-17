"""算子 3 三接口服务：``/predict``、``/downloadFile``、``/removeFile``。

接口约定（DEVELOPMENT_CONTRACT §2、§3，[v4.2] 刷新）：
- 只实现三个接口，``/downloadFile`` 保留路径但明确返回 404（不实现下载）；
- [v4.2] ``/predict`` 双协议：形态二（``application/json`` 或带 ``request`` 字段的
  multipart）统一返回 ``data{operator, status, outputFile, result}``，
  不再区分阶段末/非阶段末、不再返回血缘；形态一（单文件流）为不带 ``request``
  字段的 ``multipart/form-data`` + 查询参数 ``detection``/``operatorId``
  （别名 M04/04/4/clean_normalize_text/text_normalization/目录名，大小写不敏感），
  处理型响应：``status`` 1=成功 0=失败 + ``text``/``encoding``。
  [v4.3] 形态一上传内容为压缩包时，按素材包（入口 JSONL + 媒体）跑与形态二相同的
  阶段推理，返回 JSON ``status`` + 指标 + ``outputFile`` 路径引用（含可直传下一算子的
  ``*_form1.zip``）；非压缩包保持上述单文件单记录行为。

启动::

    uvicorn app:app --host 0.0.0.0 --port 8080
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
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inference  # noqa: E402
# [v4.2] 血缘输出已停用：utils.lineage 不再被本模块调用（保留存档）；
# utils.transport 仅保留 crossmodal-files-v1 打包能力。
from utils import form1_package  # noqa: E402
from utils import transport  # noqa: E402
from utils.io import (TaskBusyError, TaskLock, UnsupportedMediaPath,  # noqa: E402
                      commit_directory, remove_task, resolve_in_task,
                      safe_extract_zip, task_dir, validate_unique_id)
from utils.logger import get_logger, log_action  # noqa: E402
from utils.schemas import PredictRequest, RemoveFileRequest  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = os.getenv("OPERATOR_CONFIG", str(BASE_DIR / "configs" / "config.yaml"))

app = FastAPI(
    title="跨模态 demo 算子 3：文本清洗标准化与分词",
    version="v1.0.0",
    description="清洗文本、Unicode 归一、标点/大小写规整与分词，输出分词与字符偏移。",
)

_logger = get_logger("app")


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    kind = "not_implemented" if exc.status_code == 404 and _request.url.path == "/downloadFile" else ("not_found" if exc.status_code == 404 else "http_error")
    return error_response(exc.status_code, str(exc.detail), {"error": kind})


class RequestError(Exception):
    """请求级错误（安全/参数），映射为 4xx/5xx 规范信封。"""

    def __init__(self, status_code: int, message: str, error: str = "invalid_request",
                 data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error = error
        self.data = data


def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": data or {}}


def error_response(status_code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> JSONResponse:
    """失败信封：``success`` 恒为 false，绝不携带成功产物。"""
    payload = envelope(0 if status_code < 400 else 1, msg, data)
    payload["success"] = False
    payload["message"] = msg
    return JSONResponse(status_code=status_code, content=payload)


def _deployment() -> Tuple[Any, Path]:
    try:
        cfg, _snapshot = inference.load_config(CONFIG_PATH, None)
    except Exception as exc:  # noqa: BLE001 - 配置坏了要明确报错
        raise RequestError(500, "服务端配置不可用：%s" % type(exc).__name__, "config_error")
    return cfg, inference.resolve_output_root(cfg)


async def _parse_payload(request: Request) -> Tuple[Dict[str, Any], Any]:
    """解析请求体，返回 (原始 payload, 上传文件或 None)。"""
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
    """校验 predict 参数；未知字段、非严格布尔等一律 422。"""
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
    """把上传的 zip 安全解压并原子提交到 ``<task>/input``。

    拒绝：路径逃逸、符号链接、重复成员、超文件数/超解压字节；覆盖式提交保证重复调用幂等。
    """
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
                raise RequestError(413, "上传包超过限额 %d 字节" % limit_bytes, "upload_too_large")
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

# [v4.2] 血缘输出已停用：本节点血缘节点构造函数归档，不再调用。
# def _lineage_node(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
#     """本节点血缘记录（本地快照保留阶段内临时 ``outputFile`` 绝对路径）。"""
#     records = result.get("stageLineage") or []
#     if not records or not isinstance(records[-1], dict):
#         return None
#     return copy.deepcopy(records[-1])


def _pack_output_object(result: Dict[str, Any], cfg: Any) -> Dict[str, Any]:
    run_dir = Path(result["runDir"])
    manifest = transport.load_manifest(run_dir)
    if manifest is None:
        raise transport.TransportError("运行目录缺少 manifest.json")
    entrypoint = manifest["entrypoint"]
    paths = transport.iter_manifest_paths(manifest)
    paths.append(transport.MANIFEST_NAME)
    return transport.pack_file_object(run_dir, entrypoint, paths, cfg.server.max_response_bytes)


def _statistics_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    statistics = result.get("statistics") or {}
    drop = {"conservation_ok"}
    return {key: value for key, value in statistics.items() if key not in drop}


def _build_predict_response(result: Dict[str, Any], model: PredictRequest,
                            cfg: Any, unique_id: str) -> JSONResponse:
    status = result.get("status")
    identity = {
        "operator": cfg.operator.name,
        "operator_version": cfg.operator.version,
        "stageId": cfg.stage_id,
        "uniqueId": unique_id,
        "runId": result.get("runId"),
    }
    # [v4.2] 血缘输出已停用：不再构造血缘节点。
    # node = _lineage_node(result)

    if status == "failed":
        log_action(_logger, "失败", "predict 结束：%s" % result.get("message"))
        message = result.get("message") or "处理失败"
        payload = envelope(1, message, {
            "error": "processing_failed",
            "status": "failed",
            "statistics": _statistics_summary(result),
        })
        payload["success"] = False
        payload["message"] = message
        return JSONResponse(status_code=200, content=payload)

    # [v4.2] 形态二响应统一：不再区分阶段末/非阶段末，始终返回 outputFile + result，
    # 不再返回 lineageFile；媒体引用校验沿用原阶段末口径（isLastOperator=true 时执行）。
    if bool(model.isLastOperator):
        problems = inference.verify_stage_media(result["outputFile"], Path(result["runDir"]))
        if problems:
            payload = envelope(1, "阶段末媒体引用不可读：%d 处" % len(problems),
                               dict(identity, status="failed", media_errors=problems[:20]))
            payload["success"] = False
            return JSONResponse(status_code=200, content=payload)
    try:
        file_object = _pack_output_object(result, cfg)
    except transport.OutputTooLargeError as exc:
        payload = envelope(1, str(exc), dict(identity, status=status,
                                            error="output_too_large"))
        payload["success"] = False
        return JSONResponse(status_code=413, content=payload)
    except transport.TransportError as exc:
        payload = envelope(1, "输出文件对象打包失败：%s" % exc, dict(identity, status=status))
        payload["success"] = False
        return JSONResponse(status_code=500, content=payload)
    data: Dict[str, Any] = {
        "operator": "clean_normalize_text",
        "status": "success",
        "outputFile": file_object,
        "result": {
            "process_status": status,
            "text": result.get("entrypoint"),
            "encoding": "UTF-8",
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
    """上传阶段素材包；上传与执行分离，阶段首 predict 仅凭 uniqueId 取入口。"""
    try:
        if not uniqueId:
            raise RequestError(422, "缺少 uniqueId", "invalid_unique_id")
        try:
            task_dir(_deployment()[1], uniqueId)
        except ValueError as exc:
            raise RequestError(422, "uniqueId 非法：%s" % exc, "invalid_unique_id") from None
        if file is None:
            raise RequestError(422, "缺少上传文件 file", "invalid_upload")
        cfg, root = _deployment()
        with TaskLock(root, uniqueId):
            destination = await _stage_upload(file, task_dir(root, uniqueId), cfg)
        entry, relative = transport.resolve_entrypoint(destination)
        return JSONResponse(status_code=200, content=envelope(0, "success", {"entrypoint": relative}))
    except RequestError as exc:
        return error_response(exc.status_code, exc.message, {"error": exc.error})
    except TaskBusyError:
        return error_response(409, "任务正在被另一个写操作占用", {"error": "task_busy"})
    except Exception as exc:  # noqa: BLE001
        return error_response(422, "上传失败：%s" % type(exc).__name__, {"error": "invalid_upload"})

# --------------------------------------------------------------------------- #
# [v4.2] /predict 双协议：detection 别名校验 + 形态一（单文件流）
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M04/04/4/英文ID/目录名
DETECTION_ALIASES = {"clean_normalize_text", "text_normalization", "m04", "04", "4",
                     "cma_clean_normalize_text"}

#: 本算子只收文本/JSON 输入；上传这些媒体类型一律拒绝
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
                  ".wav", ".mp3", ".flac", ".mp4", ".mov", ".avi"}


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


def _decode_text(payload: bytes) -> Optional[str]:
    """宽容解码上传文本：UTF-8（含 BOM）优先，回退 GB18030，失败返回 None。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _wrap_json_records(entry: Path) -> Path:
    """[v4.2] 单条样本 JSON（对象或数组）包装为单/多记录 JSONL。"""
    payload = json.loads(entry.read_text(encoding="utf-8"))
    records = payload if isinstance(payload, list) else [payload]
    target = entry.with_suffix(".jsonl")
    with open(target, "w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    entry.unlink()
    return target


async def _predict_single_file(request: Request, form, cfg, root: Path) -> JSONResponse:
    """[v4.2] 形态一（单文件流）：multipart/form-data + file 必传（文本/JSON/JSONL/ZIP）。"""
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    uid_raw = (request.query_params.get("uniqueId") or form.get("uniqueId")
               or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    try:
        unique_id = validate_unique_id(str(uid_raw))
        filename = Path(upload.filename or "input.txt").name
        suffix = Path(filename).suffix.lower()
        if suffix in MEDIA_SUFFIXES:
            return JSONResponse({"status": 0, "msg": "该算子需要 JSON/JSONL 输入"})
        payload_bytes = await upload.read()
        if not payload_bytes:
            return JSONResponse({"status": 0, "msg": "上传内容为空"})
        # [v4.2] 表单覆盖：白名单标量转换，未知字段忽略；形态一缺省开启清洗
        overrides: Dict[str, Any] = {}
        enabled_form = form.get("enabled")
        overrides["enabled"] = (
            str(enabled_form).strip().lower() in ("1", "true", "yes", "on")
            if enabled_form is not None else True)
        task = task_dir(root, unique_id)
        input_dir = task / "input"
        if input_dir.exists():
            shutil.rmtree(input_dir)
        input_dir.mkdir(parents=True)
        stem = Path(filename).stem or unique_id
        is_package = zipfile.is_zipfile(io.BytesIO(payload_bytes))
        if is_package:
            # [v4.3] 压缩包模式：安全解压到任务 input/，入口优先 manifest.json
            try:
                form1_package.extract_zip_into(payload_bytes, input_dir)
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                return JSONResponse({"status": 0, "msg": "素材包解压失败：%s" % exc})
            try:
                entry = form1_package.resolve_entrypoint(input_dir)
            except ValueError as exc:
                return JSONResponse({"status": 0, "msg": str(exc)})
            if entry.suffix.lower() == ".json":
                entry = _wrap_json_records(entry)
        elif suffix == ".jsonl":
            entry = input_dir / filename
            entry.write_bytes(payload_bytes)
        elif suffix == ".json":
            entry = input_dir / filename
            entry.write_bytes(payload_bytes)
            entry = _wrap_json_records(entry)
        elif suffix in (".txt", ".csv"):
            content = _decode_text(payload_bytes)
            if content is None:
                return JSONResponse({"status": 0, "msg": "文本编码不可读"})
            # [v4.2] 单个文本文件合成单记录 JSONL（文本内容直接入 text 字段）
            entry = input_dir / (stem + ".jsonl")
            record = {"sample_id": stem, "text": content}
            entry.write_text(json.dumps(record, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        else:
            return JSONResponse({"status": 0, "msg": "该算子需要 JSON/JSONL 输入"})
        result = inference.run(str(entry), unique_id, CONFIG_PATH, str(root), overrides)
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
                          or Path(result.get("outputFile") or "").name)
            reference = form1_package.build_reference(run_root, entry_hint)
        except (OSError, ValueError) as exc:
            return JSONResponse({"status": 0, "msg": "输出打包失败：%s" % str(exc)[:200]})
        business = {"text": result.get("entrypoint"), "encoding": "UTF-8"}
        if not overrides.get("enabled", True):
            business["process_status"] = "not_applicable"
        return JSONResponse(form1_package.package_payload(
            result, reference, operator="clean_normalize_text",
            unique_id=unique_id, business=business))
    rejects = result.get("rejectsFile")
    if rejects and Path(rejects).is_file() and Path(rejects).stat().st_size:
        first = json.loads(Path(rejects).read_text(encoding="utf-8").splitlines()[0])
        return JSONResponse({"status": 0, "process_status": "failed",
                             "msg": "清洗失败：%s" % first.get("reason", "记录被拒绝")})
    process_status = result.get("status")
    if not overrides.get("enabled", True):
        process_status = "not_applicable"
    return JSONResponse({"status": 1, "process_status": process_status,
                         "text": result.get("entrypoint"), "encoding": "UTF-8",
                         "uniqueId": unique_id})


@app.post("/predict")
async def predict(request: Request) -> JSONResponse:
    """[v4.2] 双协议：multipart 单文件流 / JSON 阶段文件传递（响应统一含 outputFile）。"""
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
            result = inference.run(str(entry), model.uniqueId, CONFIG_PATH, str(root),
                                   model.config)
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
    """清理任务目录；同一 uniqueId 与 /predict 串行，任务不存在时明确返回 deleted=false。"""
    cfg, root = _deployment()
    try:
        # 只校验 uniqueId，不创建任务目录：否则「已删除」的任务会被重新建出来，
        # 导致 removeFile 幂等语义失效（第二次仍报 deleted=true）。
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
        response = envelope(1, "任务不存在或已删除", {"uniqueId": uniqueId, "deleted": False})
        response["success"] = False
        response["message"] = "任务不存在或已删除"
        return JSONResponse(status_code=200, content=response)
    response = envelope(0, "任务已删除", {"uniqueId": uniqueId, "deleted": True})
    response["success"] = True
    response["message"] = "任务已删除"
    return JSONResponse(status_code=200, content=response)


# ------------------------------------------------------------------ 健康检查
@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """[D1] 存活探针（与已有算子 /health 风格一致）。"""
    return JSONResponse(status_code=200, content={"status": "ok",
                                                  "operator": inference.OPERATOR_DIR_NAME})
