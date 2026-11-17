"""算子 18「标注局部对局部」HTTP 服务：**只实现三个正式接口**。

- ``POST /uploadFile?uniqueId=``  上传任务包（zip），安全解压到 ``OUTPUT_DIR/<uniqueId>/``
- ``POST /predict``               [v4.2] 双协议：
  - 形态二（application/json）：``isLastOperator`` 保留严格校验但不再改变响应形态；
    统一返回 outputFile（crossmodal-files-v1）+ result，去 lineageFile；
  - 形态一（multipart/form-data 单文件流）：``?detection=annotate_local_to_local``，
    返回 JSON 首字段 int ``status``（处理型：1 成功/0 失败）+ phrase_box_relations；
- ``POST /removeFile?uniqueId=``  清理任务

禁止实现 ``/downloadFile``（本模块不注册该路由，并在 ``/health`` 中显式声明）。
所有响应外层统一为 ``{"code": 0|1, "msg": "...", "data": {...}}``。
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

import inference  # noqa: E402
from utils import backends  # noqa: E402
from utils import form1_package  # noqa: E402
from utils import io as io_utils  # noqa: E402
from utils import transport  # noqa: E402
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.schemas import ConfigError, Envelope, PredictRequest  # noqa: E402

OPERATOR = inference.OPERATOR
OPERATOR_VERSION = inference.OPERATOR_VERSION
FORMAL_OPERATOR_NUMBER = inference.FORMAL_OPERATOR_NUMBER

app = FastAPI(title="算子18 标注局部对局部",
              version=OPERATOR_VERSION,
              description="复用 CLIP-ViT/B-32 与 #17 候选框，产出待人工确认的词-区域一对一关系")

_ENCODER_PROVIDER: Optional[Callable[[str, Optional[Dict[str, Any]]], Any]] = None


def set_encoder_provider(provider: Optional[Callable[[str, Optional[Dict[str, Any]]], Any]]
                         ) -> None:
    """注入编码器工厂（离线部署/单元测试复用已加载实例，不涉及任何网络请求）。"""
    global _ENCODER_PROVIDER
    _ENCODER_PROVIDER = provider


def _envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": int(code), "msg": msg, "data": data or {}}


def _output_root() -> Path:
    return inference.default_output_dir()


def _config_path() -> Optional[str]:
    import os

    configured = str(os.environ.get("OPERATOR_CONFIG") or "").strip()
    return configured or None


def _reset_task(task_dir: Path) -> None:
    """清理任务目录内容（保留任务锁目录）。"""
    if not task_dir.is_dir():
        return
    for child in task_dir.iterdir():
        if child.name == ".lock":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:                        # pragma: no cover
                pass


# --------------------------------------------------------------------------- #
# 统一异常形态
# --------------------------------------------------------------------------- #

@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(_request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content=_envelope(1, str(exc.detail or "请求失败")))


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(_request, exc: RequestValidationError):
    details = [{"loc": ".".join(str(item) for item in error.get("loc", ())),
                "msg": error.get("msg", "非法取值")} for error in exc.errors()]
    return JSONResponse(status_code=422,
                        content=_envelope(1, "请求参数校验失败", {"errors": details}))


@app.exception_handler(Exception)
async def _unexpected_exception_handler(_request, exc: Exception):  # noqa: BLE001
    return JSONResponse(status_code=500,
                        content=_envelope(1, "服务内部错误：%s" % type(exc).__name__))


# --------------------------------------------------------------------------- #
# /uploadFile
# --------------------------------------------------------------------------- #

@app.post("/uploadFile", response_model=Envelope)
async def upload_file(uniqueId: str = Query(..., description="任务标识"),
                      file: UploadFile = File(..., description="任务包 zip")):
    try:
        unique = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="上传内容为空")

    root = _output_root()
    task_dir = root / unique
    with io_utils.TaskLock(root, unique):
        task_dir.mkdir(parents=True, exist_ok=True)
        _reset_task(task_dir)
        archive = task_dir / "input.zip"
        archive.write_bytes(raw)
        if not zipfile.is_zipfile(archive):
            _reset_task(task_dir)
            raise HTTPException(status_code=422, detail="上传文件不是合法的 zip")
        try:
            written = io_utils.safe_extract_zip(archive, task_dir)
        except ValueError as exc:
            _reset_task(task_dir)
            raise HTTPException(status_code=422, detail=str(exc))
        archive.unlink()
        try:
            _entry_path, entry_name = transport.resolve_entrypoint(task_dir)
        except transport.TransportError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        media = sorted(item for item in written
                       if Path(item).suffix.lower() in transport.MEDIA_SUFFIXES)
        return _envelope(0, "上传成功", {"uniqueId": unique,
                                        "entrypoint": entry_name,
                                        "files": sorted(written),
                                        "mediaFiles": media,
                                        "taskDir": str(task_dir)})


# --------------------------------------------------------------------------- #
# /predict
# --------------------------------------------------------------------------- #

def _resolve_task_input(task_dir: Path, input_file: Optional[str]) -> Path:
    if input_file:
        candidate = Path(str(input_file).strip())
        if not candidate.is_absolute():
            candidate = task_dir / candidate
        resolved = candidate.resolve()
        base = task_dir.resolve()
        if base not in resolved.parents:
            raise HTTPException(status_code=400,
                                detail="inputFile 必须位于任务目录内：%s" % input_file)
        if not resolved.is_file():
            raise HTTPException(status_code=400,
                                detail="inputFile 不存在：%s" % input_file)
        return resolved
    try:
        entry_path, _name = transport.resolve_entrypoint(task_dir)
    except transport.TransportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return entry_path


@app.post("/predict", response_model=Envelope)
async def predict(request: Request):
    """[v4.2] 双协议入口：multipart/form-data → 形态一（单文件流）；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
        raw = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    try:
        payload = PredictRequest.model_validate(raw)
    except ValidationError as exc:
        details = [{"loc": ".".join(str(item) for item in error.get("loc", ())),
                    "msg": error.get("msg", "非法取值")} for error in exc.errors()]
        return JSONResponse(status_code=422,
                            content=_envelope(1, "请求参数校验失败",
                                              {"errors": details}))
    return _predict_stage(payload)


def _predict_stage(payload: PredictRequest):
    """形态二：阶段文件传递协议。[v4.2] 响应去 lineageFile，统一返回 outputFile。"""
    unique = payload.uniqueId
    root = _output_root()
    task_dir = root / unique
    if not task_dir.is_dir():
        raise HTTPException(status_code=400, detail="任务不存在，请先调用 /uploadFile")

    # [v4.2] isLastOperator 保留严格校验（契约兼容），但不再改变响应形态
    input_path = _resolve_task_input(task_dir, payload.inputFile)
    encoder = _ENCODER_PROVIDER(unique, payload.overrides) if _ENCODER_PROVIDER else None

    try:
        result = inference.run(input_file=str(input_path), unique_id=unique,
                               config_path=_config_path(), output_root=str(root),
                               overrides=payload.overrides,
                               allow_external_input=False, encoder=encoder)
        # [v4.2] mty_compliance 已停用：注释 prepare_stage_result 调用（代码保留注释存档）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
    except inference.InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail="配置非法：%s" % exc)
    except inference.ModelUnavailable as exc:
        raise HTTPException(status_code=400,
                            detail="模型不可用且未开启显式降级：%s" % exc)
    except io_utils.TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    try:
        file_object = transport.pack_file_object(
            Path(result["runDir"]), result["entrypoint"], result["packPaths"])
    except transport.OutputTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except transport.TransportError as exc:
        raise HTTPException(status_code=500, detail="打包输出失败：%s" % exc)
    return _envelope(0, result["message"],
                     {"operator": OPERATOR,
                      "status": result["status"],
                      "outputFile": file_object,
                      "result": {"uniqueId": unique, "runId": result.get("runId"),
                                 "statistics": result.get("statistics", {})}})


# --------------------------------------------------------------------------- #
# 形态一：单文件流（MTY V2，[v4.2] 新增）
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M18/18/英文ID/目录名
DETECTION_ALIASES = {"m18", "18", "annotate_local_to_local",
                     "cma_annotate_local_to_local",
                     # [D2] 追加 map.json 登记表英文 operatorId（只追加，不重排既有条目）
                     "phrase_region_annotation"}

#: 形态一白名单标量（未知字段忽略）；与 config 白名单一致（clip.* 不接受表单平铺）
_FORM1_SCALARS = {
    "enabled": "bool", "allow_degraded": "bool", "merge_phrases": "bool",
    "min_clip_score": "float", "max_tokens": "int", "max_text_chars": "int",
    "tokenizer": "str",
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
    if kind == "float":
        return float(text)
    return text


def _form_overrides(form: Any) -> Dict[str, Any]:
    """形态一宽松参数：只取白名单标量（字符串转数字/布尔），未知字段忽略。"""
    overrides: Dict[str, Any] = {}
    for key, kind in _FORM1_SCALARS.items():
        raw = form.get(key)
        if raw is None or hasattr(raw, "filename"):
            continue
        try:
            overrides[key] = _convert_scalar(str(raw), kind)
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
    return overrides


def _ensure_native_input(entry: Path) -> Path:
    """JSONL 原样使用；单个 JSON（对象或数组）转成 JSONL；其他文件一律拒绝。"""
    suffix = entry.suffix.lower()
    if suffix == ".jsonl":
        return entry
    if suffix == ".json":
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
        task_dir = _output_root() / uid
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


#: 节点流程态 → 形态一 process_status（未人工确认的候选一律 pending_review）
_FORM1_PROCESS_STATUS = {
    "skipped": "route_disabled",
    "prefilled": "pending_review",
    "pending_manual": "pending_review",
    "degraded": "pending_review",
    "isolated": "isolated",
}


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一业务字段（V4.2 §4.18）：sample_id + phrase_box_relations + unconfirmed_count。

    本算子无人工复核平台接入，所有词-区域关系均为待人工确认候选：
    正常流程态一律 ``status=1 + process_status=pending_review``（路线未启用/不适用
    另有对应 process_status），绝不把候选标成已确认。
    """
    payload: Dict[str, Any] = {"status": 1, "uniqueId": uid, "sample_id": None,
                               "phrase_box_relations": [], "unconfirmed_count": 0}
    record: Optional[Dict[str, Any]] = None
    for item in _iter_jsonl(result.get("outputFile")):
        record = item
        break
    if record is None:
        payload["process_status"] = "not_applicable"
        return payload
    payload["sample_id"] = record.get("sample_id") or record.get("pair_id")

    node = (record.get("processing") or {}).get("18") or {}
    node_status = node.get("process_status")
    payload["process_status"] = _FORM1_PROCESS_STATUS.get(node_status, "pending_review")
    relations = list(record.get("phrase_box_relations") or [])
    payload["phrase_box_relations"] = relations
    payload["unconfirmed_count"] = sum(
        1 for item in relations if not item.get("human_verified"))
    return payload


#: [v4.3] 形态一压缩包响应的算子英文标识（与登记表/形态二口径一致）
FORM1_OPERATOR = "annotate_local_to_local"


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
    root = _output_root()
    task_dir = root / uid
    input_dir = task_dir / "input"
    is_package = zipfile.is_zipfile(io.BytesIO(content))
    try:
        with io_utils.TaskLock(root, uid):
            task_dir.mkdir(parents=True, exist_ok=True)
            _reset_task(task_dir)
            if is_package:
                # [v4.3] 压缩包模式：helper 安全解包，入口优先 manifest.json
                form1_package.extract_zip_into(content, input_dir)
                try:
                    entry_path = form1_package.resolve_entrypoint(input_dir)
                except ValueError:
                    try:
                        entry_path, _name = transport.resolve_entrypoint(input_dir)
                    except transport.TransportError:
                        # 兜底：允许唯一的 *.json 作为入口（manifest.json 除外）
                        jsons = sorted(path for path in input_dir.rglob("*.json")
                                       if path.name != "manifest.json")
                        if len(jsons) != 1:
                            raise
                        entry_path = jsons[0]
            else:
                entry_path = task_dir / Path(filename.replace("\\", "/")).name
                entry_path.write_bytes(content)
            native = _ensure_native_input(entry_path)
    except (ValueError, transport.TransportError, zipfile.BadZipFile,
            io_utils.TaskBusyError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    encoder = _ENCODER_PROVIDER(uid, overrides) if _ENCODER_PROVIDER else None
    try:
        result = inference.run(input_file=str(native), unique_id=uid,
                               config_path=_config_path(), output_root=str(root),
                               overrides=overrides or None,
                               allow_external_input=False, encoder=encoder)
    except (inference.InputError, ConfigError, inference.ModelUnavailable,
            ValueError, OSError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": "task_busy: %s" % exc})

    elapsed_ms = int((time.monotonic() - started) * 1000)
    detection = request.query_params.get("detection") \
        or request.query_params.get("operatorId") or ""
    if result.get("status") == "failed":
        _form1_audit(uid, detection, filename, len(content), 0, elapsed_ms)
        return JSONResponse({"status": 0, "msg": result.get("message") or "处理失败"})
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


# --------------------------------------------------------------------------- #
# /removeFile
# --------------------------------------------------------------------------- #

@app.post("/removeFile", response_model=Envelope)
def remove_file(uniqueId: str = Query(..., description="任务标识")):
    try:
        unique = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    removed = io_utils.remove_task(_output_root(), unique)
    return _envelope(0, "清理完成", {"uniqueId": unique, "removed": bool(removed)})


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #

@app.get("/health", response_model=Envelope, include_in_schema=False)
def health():
    route_enabled: Optional[bool] = None
    min_clip_score: Optional[float] = None
    tokenizer: Optional[str] = None
    merge_phrases: Optional[bool] = None
    crop: Optional[Dict[str, Any]] = None
    config_error: Optional[str] = None
    try:
        config = inference.load_config(_config_path())
    except (inference.InputError, ConfigError) as exc:
        config_error = str(exc)
    else:
        route_enabled = bool(config.enabled)
        min_clip_score = float(config.min_clip_score)
        tokenizer = config.tokenizer
        merge_phrases = bool(config.merge_phrases)
        crop = config.crop_snapshot()
    return _envelope(0, "ok", {
        "operator": OPERATOR,
        "operator_version": OPERATOR_VERSION,
        "formal_operator_number": FORMAL_OPERATOR_NUMBER,
        "stage_id": inference.STAGE_ID,
        "route_enabled": route_enabled,
        "min_clip_score": min_clip_score,
        "tokenizer": tokenizer,
        "merge_phrases": merge_phrases,
        "crop": crop,
        "clip_model": inference.CLIP_NAME,
        "config_error": config_error,
        "downloadFile": "not_registered",
        "libraries": backends.installed_libraries(),
    })


if __name__ == "__main__":                     # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8118)
