"""算子 13 正式三接口服务。

正式接口只有：
- ``POST /uploadFile?uniqueId=`` 上传素材（单个 JSONL/媒体，或包含 manifest 与 media/ 的 zip）；
- ``POST /predict``             执行音文评分，按 ``isLastOperator`` 返回中间态或阶段末形态；
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
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import ValidationError  # [v4.2] 形态二手动校验错误形态

import inference
from utils import form1_package
from utils import io as io_utils
from utils import scoring
from utils import transport
from utils.schemas import ConfigError, PredictRequest

# [v4.2] detection/operatorId 别名空间：M13/13/英文ID/目录名（大小写不敏感）
_DETECTION_ALIASES = {"m13", "13", "score_audio_text_consistency",
                      "cma_score_audio_text_consistency",
                      # [D2] 追加 map.json 登记表英文 operatorId（只追加，不重排既有条目）
                      "audio_text_scoring"}
# [v4.2] 形态一表单可覆盖的 config 白名单标量（与 BusinessOverrides 对齐）
_FORM_OVERRIDE_KEYS = ("enabled", "edit_distance_ratio_max", "traditional_to_simplified",
                       "judge_mode")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8113

app = FastAPI(
    title="算子13 评分音文语义一致性",
    description="表1 #13：复用 Whisper-base 转写，按字符级 CER/编辑距离比例判定音文一致性",
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


@app.exception_handler(transport.OutputTooLargeError)
async def _too_large_handler(_request, exc: transport.OutputTooLargeError) -> JSONResponse:
    return _envelope(1, "OutputTooLargeError: %s" % exc, {}, 413)


@app.exception_handler(io_utils.TaskBusyError)
async def _busy_handler(_request, exc: io_utils.TaskBusyError) -> JSONResponse:
    return _envelope(1, "TaskBusyError: %s" % exc, {}, 409)


@app.exception_handler(scoring.DependencyError)
async def _dependency_handler(_request, exc: scoring.DependencyError) -> JSONResponse:
    return _envelope(1, "ModelUnavailable: %s" % exc, {}, 400)


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


# --------------------------------------------------------------------------- #
# [v4.2] /predict 双协议：形态一（单文件流）
# --------------------------------------------------------------------------- #

def _check_detection(request: Request, form: Optional[Any] = None) -> None:
    """[v4.2] detection/operatorId 宽松校验：缺失或与别名一致即放行；
    两者同时出现取值必须一致，不一致或不匹配报参数错误。"""
    values = []
    for key in ("detection", "operatorId"):
        raw = request.query_params.get(key)
        if raw is None and form is not None:
            raw = form.get(key)
        if raw is not None and str(raw).strip():
            values.append(str(raw).strip())
    if not values:
        return
    lowered = [value.lower() for value in values]
    if len(set(lowered)) > 1:
        raise ValueError("detection 与 operatorId 取值不一致：%s" % values)
    if lowered[0] not in _DETECTION_ALIASES:
        raise ValueError("detection 与本算子不匹配：%s（本算子为 M13 score_audio_text_consistency）"
                         % values[0])


def _to_scalar(value: Any) -> Any:
    """[v4.2] 形态一表单字符串转标量：布尔/整型/浮点，其余保留字符串。"""
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _form_overrides(form: Any) -> Dict[str, Any]:
    """[v4.2] 只收 config 白名单内的标量业务参数，未知字段忽略（形态一宽松）。"""
    overrides: Dict[str, Any] = {}
    for key in _FORM_OVERRIDE_KEYS:
        raw = form.get(key)
        if raw is not None:
            overrides[key] = _to_scalar(raw)
    return overrides


def _read_records(path: Optional[str]) -> list:
    if not path:
        return []
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _result_summary(task: Path, result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态二业务摘要：内部绝对路径转任务相对路径。"""
    files = {}
    for key in ("outputFile", "rejectsFile", "sampleQuarantineFile", "pendingFile"):
        raw = result.get(key)
        if raw:
            files[key] = Path(raw).relative_to(task).as_posix()
    return {
        "runId": result.get("runId"),
        "runDir": Path(result["runDir"]).relative_to(task).as_posix(),
        "entrypoint": result.get("entrypoint"),
        "files": files,
        "statistics": result.get("statistics", {}),
    }


def _single_file_response(uid: str, result: Dict[str, Any],
                          overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """[v4.2] 形态一响应（判定型）：status=1 通过/0 不通过；
    业务字段 cer / wer / decision / decision_source / model_version。"""
    stats = result.get("statistics", {})
    judge_mode = (overrides or {}).get("judge_mode")
    if not judge_mode:
        try:
            judge_mode = inference.load_config(overrides=overrides).judge_mode
        except Exception:  # noqa: BLE001 - 兜底默认 operator
            judge_mode = "operator"

    entries = []

    def _collect(node: Any, pair_id: Any) -> None:
        if not isinstance(node, dict):
            return
        passed = node.get("passed")
        if passed is True:
            decision = "pass"
        elif passed is False:
            decision = "fail"
        else:
            decision = "pending"
        entries.append({
            "pair_id": pair_id,
            "cer": node.get("cer"),
            "wer": node.get("wer"),
            "edit_distance_ratio": node.get("edit_distance_ratio"),
            "decision": decision,
            "decision_source": node.get("scoring_method") or "transcript_edit_distance",
            "threshold": node.get("edit_distance_ratio_max"),
            "model_version": node.get("transcript_model") or "whisper-base",
        })

    for row in _read_records(result.get("outputFile")):
        _collect((row.get("processing") or {}).get("13"), row.get("pair_id"))
    for row in _read_records(result.get("sampleQuarantineFile")):
        node = row.get("audio_text") or ((row.get("record") or {}).get("processing") or {}).get("13")
        _collect(node, row.get("pair_id"))

    route_enabled = bool(stats.get("enabled"))
    if not route_enabled or (not entries and not stats.get("pending_records")):
        # [v4.2] 路线未启用/无音文对是正常流程态：不伪装成接口失败，也不写成"通过"
        decision = "not_applicable"
        process_status = "not_applicable"
    elif any(entry["decision"] == "fail" for entry in entries):
        decision = "fail"
        process_status = "success"
    elif stats.get("pending_records") or any(entry["decision"] == "pending" for entry in entries):
        decision = "pending"
        process_status = "pending_review"
    else:
        decision = "pass"
        process_status = "success"
    if stats.get("degraded"):
        process_status = "pending_review"

    if judge_mode == "system":
        # [v4.2] §2.8：judge_mode=system 按处理型语义（1 成功/0 失败），分档由平台按任务配置执行
        status = 1
    else:
        status = 0 if decision == "fail" else 1

    single = entries[0] if len(entries) == 1 else {}
    return {
        "status": status,
        "process_status": process_status,
        "uniqueId": uid,
        "judge_mode": judge_mode,
        "pair_id": single.get("pair_id"),
        "cer": single.get("cer"),
        "wer": single.get("wer"),
        "decision": decision,
        "decision_source": single.get("decision_source") or "transcript_edit_distance",
        "model_version": single.get("model_version") or "whisper-base",
        "threshold": single.get("threshold"),
        "results": entries,
    }


def _audit_single_file(uid: str, detection: Optional[str], filename: Optional[str],
                       status: int, started: float) -> None:
    """[v4.2] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    try:
        task = io_utils.task_dir(output_root(), uid)
        record = {
            "detection": detection,
            "filename": filename,
            "status": status,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "event_time": datetime.now().isoformat(timespec="seconds"),
        }
        with open(str(task / "single_file_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 审计失败不影响主流程
        pass


async def _predict_single_file(request: Request) -> JSONResponse:
    """[v4.2] 形态一（单文件流）：multipart/form-data + file 字段。"""
    started = time.monotonic()
    form = await request.form()
    try:
        _check_detection(request, form)
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    detection = request.query_params.get("detection") or form.get("detection") \
        or request.query_params.get("operatorId") or form.get("operatorId")
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    raw_uid = request.query_params.get("uniqueId") or form.get("uniqueId") \
        or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        uid = io_utils.validate_unique_id(str(raw_uid))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    try:
        content = await upload.read()
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": 0, "msg": "读取上传内容失败"})
    if not content:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})
    # [v4.3] 形态一压缩包输入识别：压缩包 → 阶段推理 + 输出路径引用；单文件 → 原判定响应
    is_package = zipfile.is_zipfile(io.BytesIO(content))
    root = output_root()
    task_dir = io_utils.task_dir(root, uid)
    input_dir = task_dir / "input"
    overrides = _form_overrides(form)
    try:
        with io_utils.TaskLock(root, uid):
            if input_dir.exists():
                shutil.rmtree(str(input_dir))
            input_dir.mkdir(parents=True, exist_ok=True)
            try:
                if is_package:
                    # [v4.3] 压缩包：解压到 task/input，按 manifest/唯一 JSONL 定位入口
                    form1_package.extract_zip_into(content, input_dir)
                    try:
                        input_path = form1_package.resolve_entrypoint(input_dir)
                    except ValueError:
                        input_path, _ = transport.resolve_entrypoint(input_dir)
                else:
                    name = io_utils.normalize_zip_member_name(upload.filename or "input.jsonl")
                    (input_dir / name).write_bytes(content)
                    input_path, _ = transport.resolve_entrypoint(input_dir)
            except transport.TransportError as exc:
                shutil.rmtree(str(task_dir), ignore_errors=True)
                # 上传媒体/非 JSON 素材：本算子只收音文对 JSON/JSONL/ZIP
                if "没有可识别的" in str(exc):
                    _audit_single_file(uid, detection, upload.filename, 0, started)
                    return JSONResponse({"status": 0, "msg": "该算子需要 JSON/JSONL 输入"})
                return JSONResponse({"status": 0, "msg": str(exc)})
            except (ValueError, OSError) as exc:
                shutil.rmtree(str(task_dir), ignore_errors=True)
                return JSONResponse({"status": 0, "msg": str(exc)})
            result = inference.run(str(input_path), uid, output_root=str(root),
                                   overrides=overrides or None)
        if result.get("status") == "failed":
            _audit_single_file(uid, detection, upload.filename, 0, started)
            return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})
        _audit_single_file(uid, detection, upload.filename, 1, started)
        if is_package:
            # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
            try:
                run_root = Path(result.get("runDir") or result.get("run_dir"))
                entry_hint = result.get("flowEntrypoint") or result.get("entrypoint") or Path(
                    result.get("outputFile") or "").name
                reference = form1_package.build_reference(run_root, entry_hint)
            except Exception as exc:  # noqa: BLE001 - 打包失败降级为仅返回判定结果
                detail = _single_file_response(uid, result, overrides)
                detail["uniqueId"] = uid
                detail["package_error"] = str(exc)[:200]
                detail["status"] = 0
                return JSONResponse(detail)
            detail = _single_file_response(uid, result, overrides)
            business = {k: v for k, v in detail.items() if k != "status"}
            return JSONResponse(form1_package.package_payload(
                result, reference, operator="score_audio_text_consistency",
                unique_id=uid, business=business))
        return JSONResponse(_single_file_response(uid, result, overrides))
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)}, status_code=409)
    except (inference.InputError, ConfigError, scoring.DependencyError, ValueError) as exc:
        return JSONResponse({"status": 0, "msg": "%s: %s" % (type(exc).__name__, exc)})


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


@app.post("/predict")
async def predict(request: Request) -> JSONResponse:
    """[v4.2] 双协议：
    - 形态一（multipart/form-data）：单文件流，``?detection=score_audio_text_consistency``；
    - 形态二（application/json）：阶段文件传递，``isLastOperator`` 保留严格校验但不再改变响应形态，
      统一返回 outputFile（crossmodal-files-v1）+ result，去 lineageFile。
    """
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _envelope(1, "请求体必须是 JSON 对象", {}, 400)
    try:
        payload = PredictRequest.model_validate(body)
    except ValidationError as exc:
        return _envelope(1, "请求参数校验失败", {"errors": [
            {"loc": ".".join(str(item) for item in error.get("loc", ())),
             "msg": error.get("msg", "非法取值")} for error in exc.errors()]}, 422)
    value = _validated_unique_id(payload.uniqueId)
    root = output_root()
    task = io_utils.task_dir(root, value)
    with io_utils.TaskLock(root, value):
        result = inference.run(
            payload.inputFile, value, output_root=str(root),
            overrides=payload.config or None, allow_external_input=False)
        # [v4.2] mty_compliance 已停用：注释 prepare_stage_result 调用（代码保留注释存档）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
    # [v4.2] 形态二统一响应：不再区分阶段末/非阶段末，始终 outputFile + result，去 lineageFile
    data: Dict[str, Any] = {
        "operator": inference.OPERATOR,
        "status": "success" if result.get("status") != "failed" else "failed",
        "outputFile": inference.stage_file_object(result),
        "result": _result_summary(task, result),
    }
    return _envelope(0, result["message"], data)


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
