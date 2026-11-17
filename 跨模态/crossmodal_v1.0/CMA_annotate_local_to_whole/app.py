"""算子 #17 HTTP 服务：只提供三个正式接口。

- ``POST /uploadFile?uniqueId=``：接收 ZIP 并安全解压到任务 input；
- ``POST /predict``：[v4.2] 双协议——
  - 形态二（application/json）：严格布尔 ``isLastOperator``（保留校验但不再改变
    响应形态）；统一返回 outputFile（crossmodal-files-v1）+ result，去 lineageFile；
  - 形态一（multipart/form-data 单文件流）：``?detection=annotate_local_to_whole``，
    返回 JSON 首字段 int ``status``（处理型：1 成功/0 失败）+ local_annotations；
- ``POST /removeFile?uniqueId=``：持锁删除当前任务目录。

**不注册 ``/downloadFile``**。所有响应统一 ``{"code":0|1,"msg":...,"data":...}``。
"""
from __future__ import annotations

import io as _io
import json
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, \
    field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import detectors, form1_package, io as io_utils
from utils import localization as loc
from utils import transport
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.schemas import (FORMAL_OPERATOR_NUMBER, OPERATOR_NAME, OPERATOR_NAME_ZH,
                           STAGE_ID, ConfigError, load_config)

SERVICE_TITLE = "算子 #17 标注局部对整体"
DEFAULT_PORT = 8117
FORMAL_ENDPOINTS = ["/uploadFile?uniqueId=", "/predict", "/removeFile?uniqueId="]

app = FastAPI(title=SERVICE_TITLE, version=loc.OPERATOR_VERSION,
              description="图像通用类别预填 + 音视频转写时间段预填；人工确认后方可交付")


def envelope(code: int, msg: str, data: Any = None) -> Dict[str, Any]:
    return {"code": int(code), "msg": msg, "data": data}


def failure(msg: str) -> Dict[str, Any]:
    """失败信封：契约 §2 要求 ``code=1``、可理解 ``msg``、``data={}``。"""
    return {"code": 1, "msg": str(msg), "data": {}}


def output_root() -> Path:
    """任务输出根目录（可被编排层或测试替换）。"""
    return inference.default_output_dir()


# --------------------------------------------------------------------------- #
# 检测后端注入点（编排层可复用同一个已加载后端；默认按配置自行构建）
# --------------------------------------------------------------------------- #

DetectorProvider = Callable[[str, str], Optional[Any]]
_DETECTOR_PROVIDER: Optional[DetectorProvider] = None


def set_detector_provider(provider: Optional[DetectorProvider]) -> None:
    global _DETECTOR_PROVIDER
    _DETECTOR_PROVIDER = provider


def reset_detector_provider() -> None:
    set_detector_provider(None)


def _detector_for(unique_id: str) -> Optional[Any]:
    if _DETECTOR_PROVIDER is None:
        return None
    try:
        return _DETECTOR_PROVIDER(unique_id, OPERATOR_NAME)
    except Exception as exc:  # noqa: BLE001 - 注入失败必须显式报错
        raise HttpFailure(400, "detector provider failed: %s" % str(exc)[:160]) from exc


# --------------------------------------------------------------------------- #
# 统一错误信封
# --------------------------------------------------------------------------- #

class HttpFailure(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.message = str(message)


@app.exception_handler(HttpFailure)
def _handle_http_failure(_request, exc: HttpFailure) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=failure(exc.message))


@app.exception_handler(RequestValidationError)
def _handle_validation(_request, exc: RequestValidationError) -> JSONResponse:
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        details.append("%s: %s" % (location or "body", error.get("msg", "invalid")))
    return JSONResponse(status_code=422,
                        content=failure("invalid request: " + "; ".join(details)))


@app.exception_handler(StarletteHTTPException)
def _handle_starlette(_request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "http error"
    return JSONResponse(status_code=exc.status_code, content=failure(detail))


def _validated_unique_id(value: Any, status_code: int = 422) -> str:
    try:
        return io_utils.validate_unique_id(value)
    except ValueError as exc:
        raise HttpFailure(status_code, str(exc)) from exc


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #

class PredictRequest(BaseModel):
    """``isLastOperator`` 必须是严格布尔；``config`` 只允许业务参数。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    unique_id: str = Field(alias="uniqueId")
    is_last_operator: StrictBool = Field(alias="isLastOperator")
    input_file: Optional[str] = Field(default=None, alias="inputFile")
    config: Optional[Dict[str, Any]] = None

    @field_validator("unique_id")
    @classmethod
    def _check_unique_id(cls, value: str) -> str:
        return io_utils.validate_unique_id(value)


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    try:
        config = load_config()
    except ConfigError:
        config = None
    return envelope(0, "success", {
        "operator": OPERATOR_NAME,
        "operator_name_zh": OPERATOR_NAME_ZH,
        "formal_operator_number": FORMAL_OPERATOR_NUMBER,
        "stage_id": STAGE_ID,
        "operator_version": loc.OPERATOR_VERSION,
        "enabled_by_default": False,
        "endpoints": list(FORMAL_ENDPOINTS),
        "downloadFile": "not_registered",
        "detector": detectors.detector_availability(config),
        "outputDir": str(output_root()),
    })


@app.post("/uploadFile")
def upload_file(uniqueId: str = Query(...), file: UploadFile = File(...)) -> Dict[str, Any]:
    unique_id = _validated_unique_id(uniqueId)
    try:
        payload = file.file.read()
    except OSError as exc:
        raise HttpFailure(422, "uploaded package is unreadable: %s" % exc) from exc
    finally:
        try:
            file.file.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结论
            pass

    if not payload:
        raise HttpFailure(422, "uploaded package is empty")

    root = output_root()
    input_dir = io_utils.task_input_dir(root, unique_id)
    try:
        extracted = io_utils.safe_extract_zip(_io.BytesIO(payload), input_dir)
    except ValueError as exc:
        shutil.rmtree(input_dir, ignore_errors=True)
        raise HttpFailure(422, "package rejected: %s" % exc) from exc

    try:
        entry, entry_name = transport.resolve_entrypoint(input_dir)
    except transport.TransportError as exc:
        shutil.rmtree(input_dir, ignore_errors=True)
        raise HttpFailure(422, "package rejected: %s" % exc) from exc

    return envelope(0, "success", {
        "uniqueId": unique_id,
        "entrypoint": entry_name,
        "entryFile": str(entry),
        "inputDir": str(input_dir),
        "uploadedFiles": len(extracted),
        "mediaFiles": transport.media_files_in(input_dir),
    })


@app.post("/predict")
async def predict(request: Request) -> Dict[str, Any]:
    """[v4.2] 双协议入口：multipart/form-data → 形态一（单文件流）；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
        payload = await request.json()
    except ValueError as exc:
        raise HttpFailure(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HttpFailure(400, "请求体必须是 JSON 对象") from exc
    try:
        model = PredictRequest.model_validate(payload)
    except ValidationError as exc:
        raise HttpFailure(422, "invalid request: %s" % str(exc)[:300]) from exc
    return _predict_stage(model)


def _predict_stage(request: PredictRequest) -> Dict[str, Any]:
    """形态二：阶段文件传递协议。[v4.2] 响应去 lineageFile，统一返回 outputFile。"""
    root = output_root()
    task_dir = io_utils.task_root(root, request.unique_id)
    if request.input_file is None and not (task_dir / io_utils.INPUT_DIR_NAME).is_dir():
        raise HttpFailure(400, "task input is missing; call /uploadFile?uniqueId=%s first"
                          % request.unique_id)

    # [v4.2] isLastOperator 保留严格校验（契约兼容），但不再改变响应形态
    detector_obj = _detector_for(request.unique_id)
    try:
        result = inference.run(
            input_file=request.input_file, unique_id=request.unique_id,
            output_root=str(root), overrides=request.config or {},
            detector=detector_obj)
        # [v4.2] mty_compliance 已停用：注释 prepare_stage_result 调用（代码保留注释存档）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, request.is_last_operator)
    except io_utils.TaskBusyError as exc:
        # 契约 §3：同一 uniqueId 并发写失败必须显式返回 task_busy
        raise HttpFailure(409, "task_busy: %s" % exc) from exc
    except transport.OutputTooLargeError as exc:
        raise HttpFailure(413, str(exc)) from exc
    except (inference.InputError, inference.DependencyError, ConfigError,
            ValueError, OSError) as exc:
        raise HttpFailure(400, str(exc)) from exc

    return envelope(0, "success", {
        "operator": OPERATOR_NAME,
        "status": result["status"],
        "outputFile": inference.stage_file_object(result),
        "result": {"uniqueId": request.unique_id, "runId": result.get("runId"),
                   "statistics": result.get("statistics", {})},
    })


# --------------------------------------------------------------------------- #
# 形态一：单文件流（MTY V2，[v4.2] 新增）
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M17/17/英文ID/目录名
DETECTION_ALIASES = {"m17", "17", "annotate_local_to_whole",
                     "cma_annotate_local_to_whole",
                     # [D2] 追加 map.json 登记表英文 operatorId（只追加，不重排既有条目）
                     "local_to_whole_annotation"}

#: 形态一白名单标量（未知字段忽略）；detector 阈值在表单中平铺、提交前归位
_FORM1_SCALARS = {
    "enabled": "bool", "allow_degraded": "bool", "require_whole_aligned": "bool",
    "max_text_chars": "int", "max_time_ranges": "int", "max_images_per_record": "int",
    "conf_threshold": "float", "iou_threshold": "float",
    "max_boxes": "int", "image_size": "int",
}
_FORM1_DETECTOR_KEYS = {"conf_threshold", "iou_threshold", "max_boxes", "image_size"}


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
    return float(text)


def _form_overrides(form: Any) -> Dict[str, Any]:
    """形态一宽松参数：只取白名单标量（字符串转数字/布尔），未知字段忽略。"""
    overrides: Dict[str, Any] = {}
    detector: Dict[str, Any] = {}
    for key, kind in _FORM1_SCALARS.items():
        raw = form.get(key)
        if raw is None or hasattr(raw, "filename"):
            continue
        try:
            value = _convert_scalar(str(raw), kind)
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
        if key in _FORM1_DETECTOR_KEYS:
            detector[key] = value
        else:
            overrides[key] = value
    if detector:
        overrides["detector"] = detector
    return overrides


def _ensure_native_input(input_dir: Path) -> Path:
    """JSONL 原样使用；单个 JSON（对象或数组）转成 JSONL；其他文件一律拒绝。"""
    try:
        entry, _name = transport.resolve_entrypoint(input_dir)
    except transport.TransportError:
        candidates = sorted(input_dir.rglob("*.json"))
        if len(candidates) != 1:
            raise ValueError("该算子需要 JSON/JSONL 输入")
        entry = candidates[0]
    if entry.suffix.lower() == ".jsonl":
        return entry
    if entry.suffix.lower() == ".json":
        try:
            payload = json.loads(entry.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 文件解析失败") from exc
        items = payload if isinstance(payload, list) else [payload]
        target = entry.with_name(entry.stem + "__native.jsonl")
        with open(str(target), "w", encoding="utf-8", newline="\n") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return target
    raise ValueError("该算子需要 JSON/JSONL 输入")


def _form1_audit(uid: str, detection: str, filename: str, size: int,
                 status: int, elapsed_ms: int) -> None:
    """[v4.2] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    record = {"uniqueId": uid, "detection": detection,
              "file": {"name": filename, "bytes": size},
              "status": status, "elapsed_ms": elapsed_ms,
              "time": datetime.now().isoformat(timespec="seconds")}
    try:
        task_dir = io_utils.task_root(output_root(), uid)
        task_dir.mkdir(parents=True, exist_ok=True)
        with open(str(task_dir / "form1_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  # 审计写盘失败不影响主流程
        pass


def _iter_jsonl(path: Optional[str]):
    if not path or not Path(path).is_file():
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


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一业务字段（V4.2 §4.17）：sample_id + local_annotations。

    未人工确认的预填结果只回 ``status=1 + process_status=pending_review``，
    不交付任何框/时间段语义；已确认记录才回 local_annotations。
    """
    payload: Dict[str, Any] = {"status": 1, "uniqueId": uid,
                               "sample_id": None, "local_annotations": []}
    statistics = result.get("statistics", {})
    if not statistics.get("enabled", True):
        # 表1 默认关闭：路线未启用属正常流程态，原样透传、不产生任何标注
        payload["process_status"] = "route_disabled"
        return payload

    record: Optional[Dict[str, Any]] = None
    for item in _iter_jsonl(result.get("mainFile")):
        record = item
        break
    if record is None:
        for entry in _iter_jsonl(result.get("sampleQuarantineFile")):
            record = entry.get("record") or {}
            break
    if record is None:
        payload["process_status"] = "not_applicable"
        return payload
    payload["sample_id"] = record.get("sample_id")

    block = record.get("local_annotations")
    if not isinstance(block, dict):
        payload["process_status"] = "not_applicable"
        return payload
    process_status = block.get("process_status")
    if process_status == loc.STATUS_CONFIRMED:
        payload["process_status"] = "success"
        payload["local_annotations"] = list(block.get("boxes") or []) \
            + list(block.get("time_ranges") or [])
    elif process_status == loc.STATUS_PENDING:
        # 未人工确认 → 待人工，预填框/时间段不得交付
        payload["process_status"] = "pending_review"
    elif process_status == loc.STATUS_NOT_APPLICABLE:
        payload["process_status"] = "not_applicable"
    else:
        payload["process_status"] = process_status or "failed"
    return payload


#: [v4.3] 形态一压缩包响应的算子英文标识（与登记表/形态二口径一致）
FORM1_OPERATOR = "annotate_local_to_whole"


def _package_response(uid: str, result: Dict[str, Any],
                      detail: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.3] 压缩包模式响应：status + 业务指标 + outputFile 容器内路径引用。"""
    run_root = Path(str(result.get("runDir") or result.get("run_dir") or ""))
    entry_hint = result.get("entrypoint") or Path(str(result.get("outputFile") or "")).name
    reference = form1_package.build_reference(run_root, entry_hint)
    business = {key: value for key, value in detail.items() if key != "status"}
    body = form1_package.package_payload(
        result, reference, operator=FORM1_OPERATOR, unique_id=uid, business=business)
    # package_payload 的 process_status 取运行状态；形态一的业务流程态以业务字段为准
    if "process_status" in business:
        body["process_status"] = business["process_status"]
    return body


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（multipart/form-data），处理型 status（1 成功/0 失败）。

    [v4.3] 上传内容是压缩包时按「单文件压缩包输入」处理：安全解压到任务 ``input/``，
    用与形态二完全相同的 ``inference.run`` 跑阶段推理，返回 ``status`` + 业务指标 +
    ``outputFile`` 容器内路径引用，并把运行输出目录打成可直传下一算子的
    ``*_form1.zip``；非压缩包仍走原单文件单记录逻辑。
    """
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

    filename = upload.filename or "upload.jsonl"
    root = output_root()
    input_dir = io_utils.task_input_dir(root, uid)
    is_package = zipfile.is_zipfile(_io.BytesIO(content))
    try:
        if is_package:
            # [v4.3] 压缩包模式：helper 安全解包，入口优先 manifest.json
            form1_package.extract_zip_into(content, input_dir)
            try:
                form1_package.resolve_entrypoint(input_dir)
            except ValueError:
                pass  # 入口留给 _ensure_native_input 按形态二同口径兜底解析
            # 本算子的 _ensure_native_input 接收「目录」：入口由 manifest/唯一 JSONL
            # 解析，单个 JSON 包会被包装成 JSONL（与形态二完全一致）
            native = _ensure_native_input(input_dir)
        else:
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / Path(filename.replace("\\", "/")).name).write_bytes(content)
            native = _ensure_native_input(input_dir)
        if not native.is_file():
            raise ValueError("该算子需要 JSON/JSONL 输入")
    except (ValueError, transport.TransportError, zipfile.BadZipFile) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    detector_obj = _detector_for(uid)
    try:
        result = inference.run(input_file=None, unique_id=uid,
                               output_root=str(root), overrides=overrides,
                               detector=detector_obj)
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": "task_busy: %s" % exc})
    except (inference.InputError, inference.DependencyError, ConfigError,
            ValueError, OSError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    elapsed_ms = int((time.monotonic() - started) * 1000)
    detection = request.query_params.get("detection") \
        or request.query_params.get("operatorId") or ""
    if result.get("status") == "failed":
        _form1_audit(uid, detection, filename, len(content), 0, elapsed_ms)
        return JSONResponse({"status": 0, "msg": result.get("statistics", {}).get(
            "status_reason") or "处理失败"})
    payload = _single_file_response(uid, result)
    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            body = _package_response(uid, result, payload)
        except (OSError, ValueError) as exc:
            _form1_audit(uid, detection, filename, len(content), 0, elapsed_ms)
            return JSONResponse({"status": 0, "msg": "输出打包失败：%s" % str(exc)[:200]})
        _form1_audit(uid, detection, filename, len(content),
                     int(body.get("status", 1)), elapsed_ms)
        return JSONResponse(body)
    payload.setdefault("uniqueId", uid)
    _form1_audit(uid, detection, filename, len(content),
                 int(payload.get("status", 1)), elapsed_ms)
    return JSONResponse(payload)


@app.post("/removeFile")
def remove_file(uniqueId: str = Query(...)) -> Dict[str, Any]:
    unique_id = _validated_unique_id(uniqueId)
    root = output_root()
    task_dir = io_utils.task_root(root, unique_id)
    try:
        with io_utils.TaskLock(root, unique_id):
            removed = task_dir.exists()
            if removed:
                shutil.rmtree(task_dir)
    except io_utils.TaskBusyError as exc:
        raise HttpFailure(409, "task_busy: %s" % exc) from exc
    return envelope(0, "success", {"uniqueId": unique_id, "removed": bool(removed)})


if __name__ == "__main__":  # pragma: no cover - 本地调试入口
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT, log_level="info")
