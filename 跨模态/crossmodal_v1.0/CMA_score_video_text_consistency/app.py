"""算子 14 HTTP 服务：仅三个正式接口（表1 #14 评分视文语义一致性）。

正式接口（**没有** ``/downloadFile``）：
- ``POST /uploadFile?uniqueId=``  上传任务输入（zip 素材包或单个 JSONL）
- ``POST /predict``               执行评分；``isLastOperator`` 决定返回形态
- ``POST /removeFile?uniqueId=``  清理任务目录（幂等）

外层响应统一为 ``{"code": 0|1, "msg": "...", "data": {...}}``。
``/predict``（v4.2 双协议）：
- ``multipart/form-data`` → 形态一：单文件流，直接返回判定型 ``status``（1 通过/0 过滤）；
- ``application/json``    → 形态二：``data`` 统一含 ``outputFile``（crossmodal-files-v1）与 ``result``，
  不再返回 ``lineageFile`` / 阶段血缘。
"""
from __future__ import annotations

import io as _io
import json
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from utils import backends
from utils import form1_package
from utils import io as io_utils
from utils import transport
from utils.schemas import (ConfigError, HealthResponse, PredictRequest, PredictResponse,
                           RemoveData, RemoveResponse, UploadData, UploadResponse)

import inference

OPERATOR = inference.OPERATOR
OPERATOR_VERSION = inference.OPERATOR_VERSION
STAGE_ID = inference.STAGE_ID
FORMAL_OPERATOR_NUMBER = inference.FORMAL_OPERATOR_NUMBER
CLIP_NAME = inference.CLIP_NAME
INPUT_DIR_NAME = inference.INPUT_DIR_NAME

#: 上传体上限（demo 口径，与传输层响应上限同源）
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
#: zip 解压限额
MAX_ZIP_FILES = 5000
MAX_ZIP_UNPACKED_BYTES = 512 * 1024 * 1024

#: 编码器提供者钩子：部署方可在此返回「图文评分已加载的 CLIP 实例」以真正复用。
#:   def provider(unique_id: str, overrides: dict | None) -> encoder | None
ENCODER_PROVIDER: Optional[Callable[[str, Optional[Dict[str, Any]]], Any]] = None


def set_encoder_provider(provider: Optional[Callable[[str, Optional[Dict[str, Any]]],
                                                     Any]]) -> None:
    """登记编码器提供者，用于复用图文评分已加载的 CLIP 实例。"""
    global ENCODER_PROVIDER
    ENCODER_PROVIDER = provider


def output_root() -> Path:
    """每次请求重新读取 ``OUTPUT_DIR``（便于测试与多实例部署）。"""
    return inference.default_output_dir()


def config_path() -> Path:
    return inference.default_config_path()


def _validate_unique_id(value: str) -> str:
    try:
        return io_utils.validate_unique_id(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _summarize_validation(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:  # noqa: BLE001
            items = []
        parts = []
        for item in items[:5]:
            location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
            parts.append("%s: %s" % (location, item.get("msg")))
        if parts:
            return "; ".join(parts)
    return str(exc)[:300]


def _encoder_for(unique_id: str, overrides: Optional[Dict[str, Any]]) -> Any:
    if ENCODER_PROVIDER is None:
        return None
    try:
        return ENCODER_PROVIDER(unique_id, overrides)
    except Exception:  # noqa: BLE001 - 提供者失败按未提供处理，由正式流程显式失败
        return None


app = FastAPI(
    title="算子 14 评分视文语义一致性",
    version=OPERATOR_VERSION,
    description="表1 #14：复用 CLIP-ViT/B-32 对视频抽帧序列与文本描述评分；"
                "任一段低于阈值 → 样本隔离；默认关闭时不生成视文产物。",
)


# --------------------------------------------------------------------------- #
# 统一信封
# --------------------------------------------------------------------------- #

@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"code": 1, "msg": str(exc.detail), "data": None})


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"code": 1, "msg": "请求参数校验失败：%s" % _summarize_validation(exc),
                 "data": None})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500,
                        content={"code": 1, "msg": "内部错误：%s" % type(exc).__name__,
                                 "data": None})


# --------------------------------------------------------------------------- #
# /uploadFile
# --------------------------------------------------------------------------- #

@app.post("/uploadFile", response_model=UploadResponse)
async def upload_file(uniqueId: str = Query(..., min_length=1),
                      file: UploadFile = File(...)) -> UploadResponse:
    """上传任务输入：zip 素材包（推荐）或单个 JSONL，写入 ``OUTPUT_DIR/<uniqueId>/input/``。"""
    value = _validate_unique_id(uniqueId)
    root = io_utils.resolve_root(output_root())
    task = io_utils.task_dir(root, value)

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=422, detail="上传内容为空")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="上传内容超过 %d 字节限额" % MAX_UPLOAD_BYTES)

    staging = task / (INPUT_DIR_NAME + ".tmp")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    try:
        if zipfile.is_zipfile(_io.BytesIO(payload)):
            try:
                written = io_utils.safe_extract_zip(
                    _io.BytesIO(payload), staging, max_files=MAX_ZIP_FILES,
                    max_unpacked_bytes=MAX_ZIP_UNPACKED_BYTES)
            except ValueError as exc:
                # 素材包内容非法属于客户端错误，必须显式 422 而不是 500
                raise HTTPException(status_code=422,
                                    detail="素材包校验失败：%s" % exc) from exc
        else:
            name = Path(file.filename or "input.jsonl").name
            if not name:
                raise HTTPException(status_code=422, detail="上传文件名非法")
            (staging / name).write_bytes(payload)
            written = [name]

        try:
            _entry, entrypoint = transport.resolve_entrypoint(staging)
        except transport.TransportError as exc:
            raise HTTPException(status_code=422, detail="无法识别输入入口：%s" % exc) from exc

        final = task / INPUT_DIR_NAME
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        io_utils.commit_directory(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    media = [name for name in written if transport.is_media_path(name)]
    return UploadResponse(
        code=0, msg="上传成功：共 %d 个文件" % len(written),
        data=UploadData(uniqueId=value, entrypoint=entrypoint, files=sorted(written),
                        mediaFiles=sorted(media), sizeBytes=len(payload)))


# --------------------------------------------------------------------------- #
# /predict
# --------------------------------------------------------------------------- #

# [v4.2] 双协议：multipart/form-data → 形态一（单文件流）；application/json → 形态二。
_DETECTION_ALIASES = frozenset({
    "m14", "14", "video_text_scoring", "score_video_text_consistency",
    "cma_score_video_text_consistency",
})
#: 形态一允许的表单业务标量（config 白名单；未知字段忽略）
_FORM_SCALARS = {
    "enabled": "bool",
    "low_score_threshold": "float",
    "max_frames_per_segment": "int",
    "max_text_chars": "int",
    "allow_degraded": "bool",
    "temporal_route_exclusion": "bool",
}


def _detection_ok(value: Optional[str]) -> bool:
    if value is None or not str(value).strip():
        return True
    return str(value).strip().lower() in _DETECTION_ALIASES


def _convert_scalar(kind: str, raw: Any) -> Any:
    if kind == "bool":
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError("布尔参数只能是 true/false：%s" % raw)
    if kind == "int":
        return int(str(raw).strip())
    if kind == "float":
        return float(str(raw).strip())
    return raw


def _form_overrides(form) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for key, kind in _FORM_SCALARS.items():
        if key in form:
            overrides[key] = _convert_scalar(kind, form.get(key))
    return overrides


def _locate_form1_entry(directory: Path) -> Path:
    """形态一入口：唯一 JSONL/JSON；多个 JSONL 时回退 manifest 解析。"""
    jsonl = sorted(Path(directory).rglob("*.jsonl"))
    if len(jsonl) == 1:
        return jsonl[0]
    if len(jsonl) > 1:
        try:
            entry, _relative = transport.resolve_entrypoint(directory)
            return entry
        except transport.TransportError as exc:
            raise ValueError("无法识别输入入口：%s" % exc) from exc
    candidates = sorted(Path(directory).rglob("*.json"))
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError("素材包内没有可识别的 JSON/JSONL 入口")


async def _store_form1_input(uid: str, upload, payload: bytes) -> Path:
    """形态一上传落盘到任务 ``input/``（zip 安全解压；单文件直接写入）。"""
    root = io_utils.resolve_root(output_root())
    task = io_utils.task_dir(root, uid)
    with io_utils.TaskLock(root, uid):
        staging = task / (INPUT_DIR_NAME + ".form1")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            if zipfile.is_zipfile(_io.BytesIO(payload)):
                # [v4.3] 压缩包：form1_package 安全解压到暂存目录
                try:
                    form1_package.extract_zip_into(payload, staging)
                except ValueError as exc:
                    raise ValueError("素材包校验失败：%s" % exc) from exc
            else:
                name = Path(upload.filename or "input.jsonl").name
                (staging / name).write_bytes(payload)
            try:
                entry = form1_package.resolve_entrypoint(staging)
            except ValueError:
                entry = _locate_form1_entry(staging)
            relative = entry.relative_to(staging)
            final = task / INPUT_DIR_NAME
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            io_utils.commit_directory(staging, final)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return final / relative


def _ensure_native_input(entry: Path) -> Path:
    """JSON/JSONL 直接用；单个 JSON 对象/数组转为原生单行 JSONL。"""
    suffix = entry.suffix.lower()
    if suffix == ".jsonl":
        return entry
    if suffix == ".json":
        payload = json.loads(entry.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else [payload]
        native = entry.with_name(entry.stem + ".native.jsonl")
        with open(native, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return native
    raise ValueError("该算子需要 JSON/JSONL 输入")


def _first_output_record(result: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(result.get("outputFile") or "")
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                return json.loads(line)
    return {}


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一响应：判定型 int status（1 通过/0 过滤）+ 分段评分。"""
    record = _first_output_record(result)
    processing = record.get("processing") if isinstance(record.get("processing"), dict) else {}
    node = processing.get("14") or record.get("video_text_score") or {}
    process_status = node.get("process_status")
    if process_status == "passed":
        status, decision = 1, "pass"
    elif process_status in ("failed", "isolated"):
        status, decision = 0, "fail"
    else:
        status, decision = 1, "not_applicable"
    segments = []
    for index, segment in enumerate(node.get("segments") or []):
        start = segment.get("start")
        end = segment.get("end")
        segments.append({
            "segment_index": segment.get("index", index),
            "score": segment.get("segment_mean"),
            "frame_count": segment.get("frame_count", 0),
            "has_evidence": segment.get("has_evidence"),
            # 内部段边界为秒，接口按 §4.14 输出毫秒
            "start_ms": None if start is None else int(round(float(start) * 1000)),
            "end_ms": None if end is None else int(round(float(end) * 1000)),
        })
    return {
        "status": status,
        "uniqueId": uid,
        "pair_id": record.get("pair_id") or record.get("sample_id"),
        "segment_scores": segments,
        # 帧预算证据：整段视频合计 frames_used <= max_frames_per_segment
        "frames_used": node.get("frames_used"),
        "max_frames_per_segment": node.get("max_frames_per_segment"),
        "decision": decision,
        "model_version": node.get("clip_model_version") or node.get("clip_model") or CLIP_NAME,
        "process_status": process_status,
    }


async def _predict_single_file(request: Request):
    """[v4.2] 形态一：multipart/form-data 单文件流。"""
    form = await request.form()
    detection = (request.query_params.get("detection")
                 or request.query_params.get("operatorId")
                 or form.get("detection") or form.get("operatorId"))
    if not _detection_ok(detection):
        return JSONResponse({"status": 0, "msg": "detection 参数与算子不匹配：%s" % detection})
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    raw_uid = (request.query_params.get("uniqueId") or form.get("uniqueId")
               or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    try:
        uid = _validate_unique_id(str(raw_uid))
    except HTTPException as exc:
        return JSONResponse({"status": 0, "msg": str(exc.detail)})
    payload = await upload.read()
    if not payload:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})
    if len(payload) > MAX_UPLOAD_BYTES:
        return JSONResponse({"status": 0, "msg": "上传内容超过 %d 字节限额" % MAX_UPLOAD_BYTES})
    # [v4.3] 形态一压缩包输入识别：压缩包 → 阶段推理 + 输出路径引用；单文件 → 原判定响应
    is_package = zipfile.is_zipfile(_io.BytesIO(payload))
    try:
        native = _ensure_native_input(await _store_form1_input(uid, upload, payload))
        overrides = _form_overrides(form)
        result = inference.run(
            input_file=str(native), unique_id=uid,
            config_path=str(config_path()), output_root=str(output_root()),
            overrides=overrides or None, encoder=_encoder_for(uid, overrides or None),
            allow_external_input=False)
    except (HTTPException, ValueError, inference.InputError, ConfigError,
            io_utils.TaskBusyError, inference.DependencyError) as exc:
        message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return JSONResponse({"status": 0, "msg": message})
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})
    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            run_root = Path(result.get("runDir") or result.get("run_dir"))
            entry_hint = result.get("entrypoint") or Path(
                result.get("outputFile") or "").name
            reference = form1_package.build_reference(run_root, entry_hint)
        except Exception as exc:  # noqa: BLE001 - 打包失败降级为仅返回判定结果
            detail = _single_file_response(uid, result)
            detail["uniqueId"] = uid
            detail["package_error"] = str(exc)[:200]
            detail["status"] = 0
            return JSONResponse(detail)
        detail = _single_file_response(uid, result)
        business = {k: v for k, v in detail.items() if k != "status"}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator="score_video_text_consistency",
            unique_id=uid, business=business))
    return JSONResponse(_single_file_response(uid, result))


@app.post("/predict")
async def predict(request: Request):
    """[v4.2] 双协议：multipart → 形态一；JSON → 形态二（统一 outputFile + result）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    # [D3] 形态二入口补 detection/operatorId 冲突校验（§2.7 公共约定）
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        raise HTTPException(status_code=422,
                            detail="detection 与 operatorId 同时出现时取值必须一致")
    try:
        payload = await request.json()
        parsed = PredictRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 参数错误统一 422 信封
        raise HTTPException(status_code=422, detail=_summarize_validation(exc)) from exc
    value = _validate_unique_id(parsed.uniqueId)
    overrides = parsed.config

    try:
        result = inference.run(
            input_file=parsed.inputFile, unique_id=value,
            config_path=str(config_path()), output_root=str(output_root()),
            overrides=overrides, encoder=_encoder_for(value, overrides),
            # HTTP 边界强制沙箱：inputFile 只能落在当前任务目录内
            allow_external_input=False)
        # [v4.2] 阶段归档/血缘输出已停用：不再调用 mty_compliance.prepare_stage_result
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, parsed.isLastOperator)
    except (inference.InputError, ConfigError, io_utils.TaskBusyError,
            inference.DependencyError) as exc:
        raise HTTPException(status_code=400,
                            detail="%s: %s" % (type(exc).__name__, exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if result["status"] == "failed":
        raise HTTPException(status_code=400,
                            detail="InputError: %s" % result["message"])

    data: Dict[str, Any] = {
        "operator": OPERATOR,
        "status": result["status"],
        # [v4.2] 统一返回 outputFile（crossmodal-files-v1），不再区分阶段末/中间
        "outputFile": inference.stage_file_object(result),
        "result": {
            "uniqueId": result["uniqueId"],
            "runId": result["runId"],
            "entrypoint": result["entrypoint"],
            "status": result["status"],
            "message": result["message"],
            "statistics": result["statistics"],
        },
    }
    return {"code": 0, "msg": result["message"], "data": data}


# --------------------------------------------------------------------------- #
# /removeFile
# --------------------------------------------------------------------------- #

@app.post("/removeFile", response_model=RemoveResponse)
def remove_file(uniqueId: str = Query(..., min_length=1)) -> RemoveResponse:
    """清理任务目录（幂等）。只允许删除 ``OUTPUT_DIR`` 内的任务目录。"""
    value = _validate_unique_id(uniqueId)
    try:
        removed = io_utils.remove_task(output_root(), value)
    except io_utils.TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RemoveResponse(code=0, msg="任务目录已清理" if removed else "任务目录不存在",
                          data=RemoveData(uniqueId=value, removed=bool(removed)))


# --------------------------------------------------------------------------- #
# /health（只读，不加载模型）
# --------------------------------------------------------------------------- #

@app.get("/health", response_model=HealthResponse, include_in_schema=False)
def health() -> HealthResponse:
    try:
        config = inference.load_config(str(config_path()))
    except ConfigError as exc:
        return HealthResponse(code=1, msg=str(exc),
                             data={"operator": OPERATOR, "version": OPERATOR_VERSION})
    weights = config.clip.weights
    return HealthResponse(code=0, msg="ok", data={
        "operator": OPERATOR,
        "version": OPERATOR_VERSION,
        "formal_operator_number": FORMAL_OPERATOR_NUMBER,
        "stage_id": STAGE_ID,
        "route_enabled": bool(config.enabled),
        "low_score_threshold": config.low_score_threshold,
        "max_frames_per_segment": config.max_frames_per_segment,
        "allow_degraded": bool(config.allow_degraded),
        "clip": {
            "model": CLIP_NAME,
            "arch": config.clip.model_name,
            "library": config.clip.library,
            "device": config.clip.device,
            "weights_configured": bool(weights),
            "weights_found": bool(backends.resolve_weights_path(weights)),
            "libraries_installed": backends.installed_libraries(),
        },
        "endpoints": ["/uploadFile?uniqueId=", "/predict", "/removeFile?uniqueId="],
        "downloadFile": "not_registered",
    })


def create_app() -> FastAPI:
    """工厂形式（uvicorn ``app:create_app --factory`` 亦可）。"""
    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8114)
