"""算子 15 正式三接口服务。

正式接口只有：
- ``POST /uploadFile?uniqueId=`` 上传素材（单个 JSONL/媒体/sidecar，或包含 manifest 的 zip）；
- ``POST /predict``             执行时空一致性判定，按 ``isLastOperator`` 返回中间态或阶段末形态；
- ``POST /removeFile?uniqueId=`` 清理任务目录（幂等）。

不实现、也不恢复 ``/downloadFile``；阶段末承载统一走 ``/predict`` 的 ``data.outputFile``。
外层响应统一为 ``{"code": 0|1, "msg": "...", "data": {...}}``。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import form1_package
from utils import io as io_utils
from utils import temporal
from utils import transport
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.schemas import ConfigError, PredictRequest

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8115

app = FastAPI(
    title="算子15 评分时空一致性",
    description="表1 #15：线性插值校准视频帧/语音/字幕三段时间轴，时间差默认不超过 0.5 秒",
    version=inference.OPERATOR_VERSION,
)


def output_root() -> Path:
    """每次请求读取 OUTPUT_DIR，便于测试与多实例部署隔离。"""
    return io_utils.resolve_root(os.environ.get("OUTPUT_DIR") or (BASE_DIR / "output"))


def _envelope(code: int, msg: str, data: Dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"code": code, "msg": msg, "data": data})


# --------------------------------------------------------------------------- #
# 异常 → 统一信封
# --------------------------------------------------------------------------- #

@app.exception_handler(RequestValidationError)
async def _validation_handler(_request, exc: RequestValidationError) -> JSONResponse:
    details = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        details.append("%s: %s" % (location, item.get("msg", "非法")))
    return _envelope(1, "请求参数非法：%s" % "；".join(details[:5]), {}, 422)


@app.exception_handler(StarletteHTTPException)
async def _http_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
    return _envelope(1, str(exc.detail or "HTTP %s" % exc.status_code), {}, exc.status_code)


@app.exception_handler(inference.InputError)
async def _input_handler(_request, exc: inference.InputError) -> JSONResponse:
    return _envelope(1, "InputError: %s" % exc, {}, 400)


@app.exception_handler(ConfigError)
async def _config_handler(_request, exc: ConfigError) -> JSONResponse:
    return _envelope(1, "ConfigError: %s" % exc, {}, 400)


@app.exception_handler(temporal.TimelineError)
async def _timeline_handler(_request, exc: temporal.TimelineError) -> JSONResponse:
    return _envelope(1, "%s: %s" % (type(exc).__name__, exc), {}, 400)


@app.exception_handler(transport.OutputTooLargeError)
async def _too_large_handler(_request, exc: transport.OutputTooLargeError) -> JSONResponse:
    return _envelope(1, "OutputTooLargeError: %s" % exc, {}, 413)


@app.exception_handler(io_utils.TaskBusyError)
async def _busy_handler(_request, exc: io_utils.TaskBusyError) -> JSONResponse:
    return _envelope(1, "TaskBusyError: %s" % exc, {}, 409)


@app.exception_handler(ValueError)
async def _value_handler(_request, exc: ValueError) -> JSONResponse:
    # 覆盖 ConfigError / InputError / TransportError / UnsupportedMediaPath / 路径校验等
    return _envelope(1, "%s: %s" % (type(exc).__name__, exc), {}, 400)


@app.exception_handler(Exception)
async def _unexpected_handler(_request, exc: Exception) -> JSONResponse:
    # 只回类型与简短原因，不回完整堆栈
    return _envelope(1, "%s: %s" % (type(exc).__name__, exc), {}, 400)


# --------------------------------------------------------------------------- #
# 请求校验辅助
# --------------------------------------------------------------------------- #

def _validated_unique_id(value: str) -> str:
    try:
        return io_utils.validate_unique_id(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="uniqueId 非法：%s" % exc) from exc


def _store_upload(task: Path, upload: UploadFile) -> Dict[str, Any]:
    """把上传内容落进 ``<task>/input``：zip 安全解压，或单文件直存。"""
    filename = Path(upload.filename or "upload.bin").name
    data = upload.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传内容为空")
    if len(data) > inference.MAX_UPLOAD_BYTES:
        raise transport.OutputTooLargeError(
            "上传内容 %d 字节超过限额 %d 字节" % (len(data), inference.MAX_UPLOAD_BYTES))

    task.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(task), prefix=".upload_"))
    content = staging / "content"
    content.mkdir(parents=True, exist_ok=True)
    input_dir = task / inference.INPUT_DIR_NAME
    try:
        if filename.lower().endswith(".zip"):
            archive = staging / "bundle.zip"
            archive.write_bytes(data)
            io_utils.safe_extract_zip(archive, content, max_files=inference.MAX_FILES,
                                      max_unpacked_bytes=inference.MAX_UNPACKED_BYTES)
            archive.unlink()
        else:
            (content / filename).write_bytes(data)
        _path, entrypoint = transport.resolve_entrypoint(content)
        if input_dir.exists():
            shutil.rmtree(input_dir)
        io_utils.commit_directory(content, input_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    files = sorted(str(path.relative_to(input_dir)).replace("\\", "/")
                   for path in input_dir.rglob("*") if path.is_file())
    return {"entrypoint": entrypoint, "files": files}


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #

@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(..., description="任务id_文件id"),
                      file: UploadFile = File(..., description="JSONL 素材包（zip）或单个素材文件")):
    value = _validated_unique_id(uniqueId)
    root = output_root()
    task = io_utils.task_dir(root, value)
    with io_utils.TaskLock(root, value):
        stored = _store_upload(task, file)
    return _envelope(0, "上传成功", stored)


#: detection/operatorId 别名空间（大小写不敏感）：M15/15/英文ID/目录名
DETECTION_ALIASES = {"m15", "15", "score_spatiotemporal_consistency",
                     "cma_score_spatiotemporal_consistency", "temporal_consistency"}

#: 形态一白名单标量（未知字段忽略）；与 V4.2 §4.15 接口二 config 参数名一致
_FORM1_SCALARS = {
    "enabled": "bool",
    "max_time_delta_sec": "float",
    "interpolation_method": "str",
    "min_anchors_for_calibration": "int",
    "require_calibration": "bool",
}

#: 记录级流程态 → 形态一 process_status（V4.2 §4.15：不适用必须显式 not_applicable）
_PROCESS_MAP = {
    "passed": "success",
    "failed": "failed",
    "not_applicable": "not_applicable",
    "pending_calibration": "pending_review",
    "skipped": "route_disabled",
}
_DECISION_MAP = {
    "passed": "pass",
    "failed": "fail",
    "not_applicable": "not_applicable",
    "pending_calibration": "pending",
    "skipped": "not_applicable",
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


def _task_rel(value: str, path) -> Optional[str]:
    try:
        return Path(path).resolve().relative_to(
            io_utils.task_dir(output_root(), value, create=False).resolve()).as_posix()
    except ValueError:
        return None


def _stage_response(value: str, result: Dict[str, Any]) -> JSONResponse:
    """[v4.2] 形态二统一响应：outputFile（crossmodal-files-v1）+ result，无 lineageFile。"""
    stats = result.get("statistics") or {}
    return _envelope(0, result["message"], {
        "operator": inference.OPERATOR,
        "status": result["status"],
        "outputFile": inference.stage_file_object(result),
        "result": {"uniqueId": value,
                   "runId": result["runId"],
                   "entrypoint": result["entrypoint"],
                   "outputFile": _task_rel(value, result["outputFile"]),
                   "statistics": stats,
                   "model": None},
    })


def _extract_verdict(result: Dict[str, Any]):
    """从主产物/隔离台账/待人工台账取首条判定（单文件形态一）。"""
    for key in ("outputFile", "sampleQuarantineFile", "pendingFile"):
        item = _first_jsonl(result.get(key))
        if item is None:
            continue
        record = item.get("record") if isinstance(item.get("record"), dict) else item
        block = record.get("temporal_alignment")
        node = (record.get("processing") or {}).get("15") or {}
        if isinstance(block, dict) or node:
            return record, block if isinstance(block, dict) else None, node
    return None, None, None


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一业务字段（V4.2 §4.15）：status 判定型 + applicable/decision/max_delta_sec。

    [v4.2] 缺时间戳记 not_applicable（status=1），绝不写成「时序通过」。
    """
    payload: Dict[str, Any] = {
        "status": 1, "process_status": "not_applicable", "uniqueId": uid,
        "pair_id": None, "applicable": False, "max_delta_sec": None,
        "threshold_sec": None, "decision": "not_applicable",
        "interpolation_method": None,
    }
    record, block, node = _extract_verdict(result)
    if record is None:
        rejects = _first_jsonl(result.get("rejectsFile"))
        if rejects is not None:
            payload.update({"status": 0, "process_status": "failed",
                            "decision": "fail",
                            "reason_code": rejects.get("reason_code")})
        return payload

    payload["pair_id"] = record.get("pair_id") or record.get("sample_id")
    if block is None:
        # 路线未启用：样本原样透传，不判定、不隔离
        raw = (node or {}).get("process_status") or "skipped"
        payload.update({"status": 1, "process_status": "route_disabled",
                        "decision": "not_applicable",
                        "reason_code": (node or {}).get("reason_code")})
        return payload

    raw = block.get("process_status") or "not_applicable"
    method = (block.get("interpolation") or {}).get("method")
    # [v4.2] §4.15 接口一示例用规范枚举 linear；实现里的 piecewise/global 线性统一外显 linear
    if method in ("piecewise_linear", "global_linear"):
        method = "linear"
    payload.update({
        "status": int(block.get("status", 0)),
        "process_status": _PROCESS_MAP.get(raw, raw),
        "applicable": block.get("passed") is not None,
        "max_delta_sec": block.get("max_delta_sec"),
        "threshold_sec": block.get("threshold_sec"),
        "decision": _DECISION_MAP.get(raw, raw),
        "interpolation_method": method,
        "reason_code": block.get("reason_code"),
    })
    return payload


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（判定型 status：1 通过/不适用，0 不通过）。"""
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
        value = io_utils.validate_unique_id(str(uid_raw))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    data = await upload.read()
    if not data:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})
    if len(data) > inference.MAX_UPLOAD_BYTES:
        return JSONResponse({"status": 0, "msg": "上传内容超过限额"})
    # [v4.3] 形态一压缩包输入识别：压缩包 → 阶段推理 + 输出路径引用；单文件 → 原判定响应
    is_package = zipfile.is_zipfile(io.BytesIO(data))

    filename = Path(str(upload.filename or "upload.json").replace("\\", "/")).name
    root = output_root()
    task = io_utils.task_dir(root, value)
    staging = Path(tempfile.mkdtemp(dir=str(task), prefix=".form1_"))
    try:
        content = staging / "content"
        content.mkdir(parents=True, exist_ok=True)
        if is_package:
            # [v4.3] 压缩包：form1_package 安全解压到暂存目录
            form1_package.extract_zip_into(data, content)
        else:
            (content / filename).write_bytes(data)
        try:
            entry_path = form1_package.resolve_entrypoint(content)
        except ValueError:
            try:
                entry_path, _entrypoint = transport.resolve_entrypoint(content)
            except ValueError:
                # 单条 JSON 不是合法入口：直达本算子期望的原生输入
                entry_path = content / filename
                if not entry_path.is_file():
                    raise
        native = _ensure_native_input(entry_path)
        input_dir = task / inference.INPUT_DIR_NAME
        if input_dir.exists():
            shutil.rmtree(input_dir)
        io_utils.commit_directory(content, input_dir)
        native = input_dir / native.relative_to(content)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    with io_utils.TaskLock(root, value):
        result = inference.run(str(native), value, output_root=str(root),
                               overrides=_form_overrides(form) or None,
                               allow_external_input=False)
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "process_status": "failed",
                             "msg": result.get("message") or "处理失败"})
    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            run_root = Path(result.get("runDir") or result.get("run_dir"))
            entry_hint = result.get("entrypoint") or Path(
                result.get("outputFile") or "").name
            reference = form1_package.build_reference(run_root, entry_hint)
        except Exception as exc:  # noqa: BLE001 - 打包失败降级为仅返回判定结果
            detail = _single_file_response(value, result)
            detail["uniqueId"] = value
            detail["package_error"] = str(exc)[:200]
            detail["status"] = 0
            return JSONResponse(detail)
        detail = _single_file_response(value, result)
        business = {k: v for k, v in detail.items() if k != "status"}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator="score_spatiotemporal_consistency",
            unique_id=value, business=business))
    return JSONResponse(_single_file_response(value, result))


@app.post("/predict")
async def predict(request: Request):
    """[v4.2] 双协议入口：multipart/form-data → 形态一；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
    except ValueError as exc:
        return _envelope(1, str(exc), {}, 422)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _envelope(1, "请求体必须是 JSON 对象", {}, 422)
    try:
        model = PredictRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - pydantic 校验错误统一 422
        return _envelope(1, "请求参数非法：%s" % exc, {}, 422)
    value = _validated_unique_id(model.uniqueId)
    root = output_root()
    io_utils.task_dir(root, value)
    with io_utils.TaskLock(root, value):
        result = inference.run(
            model.inputFile, value, output_root=str(root), overrides=model.config or None,
            allow_external_input=False)
        # [v4.2] 血缘/阶段归档已停用：不再调用 prepare_stage_result/annotate_node
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, model.isLastOperator)
    return _stage_response(value, result)


@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(..., description="任务id_文件id")):
    value = _validated_unique_id(uniqueId)
    removed = io_utils.remove_task(output_root(), value)
    return _envelope(0, "已清理" if removed else "任务不存在，无需清理", {"removed": removed})


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """[D1] 单算子存活探针：算子标识/状态（与已有算子 /health 风格一致）。"""
    return _envelope(0, "ok", {"operator": inference.OPERATOR,
                               "operatorVersion": inference.OPERATOR_VERSION,
                               "formalOperatorNumber": inference.FORMAL_OPERATOR_NUMBER,
                               "stageId": inference.STAGE_ID})


if __name__ == "__main__":  # pragma: no cover - 本地启动辅助
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", DEFAULT_PORT)))
