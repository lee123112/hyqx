"""算子 20 正式服务：**只有三个接口**。

- ``POST /uploadFile?uniqueId=``：上传阶段输入（ZIP 或单个文件），写入 ``OUTPUT_DIR/<uniqueId>/input/``；
- ``POST /predict``：执行质检，``isLastOperator`` 决定中间/阶段末响应形态；
- ``POST /removeFile?uniqueId=``：删除该任务目录（幂等）。
- ``GET /health``：健康与身份自检（非业务接口）。

统一外层信封：``{"code": 0|1, "msg": "...", "data": {...}}``。
**不实现 /downloadFile**（阶段末由工程侧消费 ``data.outputFile`` 文件内容对象）。
"""
from __future__ import annotations

import json
import io
import logging
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:  # 允许 `uvicorn app:app` 直接启动
    sys.path.insert(0, str(BASE_DIR))

import inference  # noqa: E402
from utils import alignment_qc as qc  # noqa: E402
from utils import form1_package  # noqa: E402
from utils import io as io_utils  # noqa: E402
from utils import schemas, transport  # noqa: E402
from utils.schemas import ConfigError  # noqa: E402

LOGGER = logging.getLogger("operator20.api")
if not LOGGER.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
MAX_RESPONSE_BYTES = int(os.environ.get("MAX_RESPONSE_BYTES", str(64 * 1024 * 1024)))

app = FastAPI(
    title="算子 20 质检对齐产物",
    version=qc.OPERATOR_VERSION,
    description=("跨模态数据质量评估阶段（S4）的质检对齐产物算子；"
                 "仅提供 /uploadFile、/predict、/removeFile 三个接口。"),
)


def output_root() -> Path:
    """输出根目录：``OUTPUT_DIR`` 环境变量优先，其次算子目录 ``output/``。"""
    return io_utils.resolve_root(os.environ.get("OUTPUT_DIR") or (BASE_DIR / "output"))


def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": int(code), "msg": str(msg), "data": data or {}}


# --------------------------------------------------------------------------- #
# 统一异常 → 信封
# --------------------------------------------------------------------------- #

@app.exception_handler(RequestValidationError)
async def _validation_handler(_request: Request, exc: RequestValidationError):
    details = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        details.append("%s: %s" % (location or "<root>", item.get("msg")))
    return JSONResponse(status_code=422,
                        content=envelope(1, "请求参数不合法 -> " + "; ".join(details)))


@app.exception_handler(StarletteHTTPException)
async def _http_handler(_request: Request, exc: StarletteHTTPException):
    message = {404: "接口不存在", 405: "方法不允许", 413: "请求体过大"}.get(
        exc.status_code, str(exc.detail))
    if exc.status_code == 404:
        message = "接口不存在；本算子只提供 /uploadFile、/predict、/removeFile（无 /downloadFile）"
    return JSONResponse(status_code=exc.status_code, content=envelope(1, message))


# --------------------------------------------------------------------------- #
# /uploadFile
# --------------------------------------------------------------------------- #

def _is_zip(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in (".zip",)


def _stream_upload(upload: UploadFile, target: Path) -> int:
    """流式落盘并统计字节数（避免整包读进内存）。"""
    size = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as sink:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise ValueError("上传内容超过 MAX_UPLOAD_BYTES=%d" % MAX_UPLOAD_BYTES)
            sink.write(chunk)
    if size == 0:
        raise ValueError("上传内容为空")
    return size


def _prepare_input_dir(content_dir: Path) -> Dict[str, Any]:
    """校验入口并补齐运行清单（返回的 ``fileCount`` 只统计**上传内容**）。"""
    manifest = transport.load_manifest(content_dir)
    try:
        entry, relative = transport.resolve_entrypoint(content_dir, manifest)
    except transport.TransportError as exc:
        raise ValueError(str(exc)) from exc
    file_count = sum(1 for path in content_dir.rglob("*") if path.is_file())
    if manifest is None:
        jsonl_count = sum(1 for path in content_dir.iterdir()
                          if path.is_file() and path.suffix.lower() == ".jsonl")
        if jsonl_count == 1:
            try:
                transport.write_manifest(content_dir, transport.build_manifest(
                    relative, [{"path": relative, "role": "main"}]))
            except transport.TransportError:  # pragma: no cover - 名称异常时不写清单
                pass
    return {"entrypoint": entry.name, "relativePath": relative,
            "fileCount": file_count}


@app.post("/uploadFile")
def upload_file(uniqueId: str = Query(..., description="任务唯一标识"),
                file: UploadFile = File(...)):
    try:
        value = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return envelope(1, str(exc))
    if not file or not getattr(file, "filename", None):
        return envelope(1, "缺少上传文件")

    root = output_root()
    try:
        with io_utils.TaskLock(root, value):
            task = io_utils.task_dir(root, value)
            staging = task / ".staging_upload"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            content = staging / "content"
            content.mkdir(parents=True, exist_ok=True)
            try:
                if _is_zip(file.filename):
                    archive = staging / "upload.zip"
                    _stream_upload(file, archive)
                    try:
                        io_utils.safe_extract_zip(archive, content,
                                                  max_files=2000,
                                                  max_unpacked_bytes=MAX_UPLOAD_BYTES)
                    except ValueError as exc:
                        return envelope(1, "压缩包不合法：%s" % exc)
                    archive.unlink(missing_ok=True)
                else:
                    try:
                        safe_name = io_utils.normalize_zip_member_name(file.filename)
                    except ValueError:
                        safe_name = "20_candidate_pool.jsonl"
                    _stream_upload(file, content / safe_name)

                info = _prepare_input_dir(content)
            except ValueError as exc:
                return envelope(1, str(exc))

            input_dir = task / "input"
            if input_dir.exists():
                shutil.rmtree(input_dir, ignore_errors=True)
            os.replace(str(content), str(input_dir))
            shutil.rmtree(staging, ignore_errors=True)

        LOGGER.info("upload uniqueId=%s files=%d entry=%s", value,
                    info["fileCount"], info["entrypoint"])
        return envelope(0, "上传成功", {"uniqueId": value, "fileCount": info["fileCount"],
                                      "entrypoint": info["entrypoint"],
                                      "inputDir": "input"})
    except io_utils.TaskBusyError as exc:
        return envelope(1, str(exc))
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("upload 失败")
        return envelope(1, "上传失败：%s" % type(exc).__name__)


# --------------------------------------------------------------------------- #
# /predict
# --------------------------------------------------------------------------- #

# [v4.2] 双协议：multipart/form-data → 形态一（单文件流）；application/json → 形态二。
_DETECTION_ALIASES = frozenset({
    "m20", "20", "alignment_qc", "check_alignment_quality",
    "cma_check_alignment_quality",
})
_FORM_SCALARS = {
    "quality_subset_fallback": "float",
    "topic_skew_threshold": "float",
    "allow_degraded": "bool",
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
    root = Path(directory).resolve()
    try:
        manifest = transport.load_manifest(root)
        if manifest is not None:
            entry, _relative = transport.resolve_entrypoint(root, manifest)
            return entry
    except transport.TransportError as exc:
        raise ValueError("无法识别输入入口：%s" % exc) from exc

    # 仅没有 manifest 时启用既有单文件兼容；空 JSONL 仍是有效文件。
    jsonls = sorted(path for path in root.rglob("*.jsonl") if path.is_file())
    if len(jsonls) == 1:
        target = jsonls[0].resolve()
        if not io_utils.is_within(root, target):
            raise ValueError("输入入口越出素材包")
        return target
    if len(jsonls) > 1:
        raise ValueError("无法识别输入入口：缺少 manifest.json 且存在多个 JSONL")
    jsons = sorted(path for path in root.rglob("*.json")
                   if path.is_file() and path.name != "manifest.json")
    if len(jsons) == 1:
        target = jsons[0].resolve()
        if not io_utils.is_within(root, target):
            raise ValueError("输入入口越出素材包")
        return target
    raise ValueError("素材包内没有唯一且可识别的 JSON/JSONL 入口")


def _store_form1_input(uid: str, upload, payload: bytes) -> Path:
    root = output_root()
    with io_utils.TaskLock(root, uid):
        task = io_utils.task_dir(root, uid)
        staging = task / ".staging_form1"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        content = staging / "content"
        content.mkdir(parents=True, exist_ok=True)
        try:
            if _is_zip(getattr(upload, "filename", "") or ""):
                archive = staging / "upload.zip"
                archive.write_bytes(payload)
                try:
                    io_utils.safe_extract_zip(archive, content, max_files=2000,
                                              max_unpacked_bytes=MAX_UPLOAD_BYTES)
                except ValueError as exc:
                    raise ValueError("压缩包不合法：%s" % exc) from exc
                archive.unlink(missing_ok=True)
            else:
                try:
                    safe_name = io_utils.normalize_zip_member_name(
                        getattr(upload, "filename", "") or "input.jsonl")
                except ValueError:
                    safe_name = "input.jsonl"
                (content / safe_name).write_bytes(payload)
            entry = _locate_form1_entry(content)
            relative = entry.relative_to(content)
            input_dir = task / "input"
            if input_dir.exists():
                shutil.rmtree(input_dir, ignore_errors=True)
            os.replace(str(content), str(input_dir))
            shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return input_dir / relative


def _ensure_native_input(entry: Path) -> Path:
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


def _read_jsonl(path: Path, limit: int = 100) -> list:
    rows = []
    if not path.is_file():
        return rows
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            rows.append(json.loads(line))
            if len(rows) >= limit:
                break
    return rows


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一响应：处理型 int status（1 质检完成/0 处理失败）+ 质量报告摘要。"""
    run_dir = Path(result["runDir"])
    try:
        report = json.loads((run_dir / qc.REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    hard_failures = []
    for name in (qc.ORIGINAL_QUARANTINE_NAME, qc.SAMPLE_QUARANTINE_NAME):
        for entry in _read_jsonl(run_dir / name):
            hard_failures.append({
                "sample_id": entry.get("sample_id"),
                "reason": entry.get("reason") or entry.get("reason_code"),
                "destination": entry.get("destination"),
            })
    statistics = result.get("statistics") or {}
    checks = statistics.get("checks") or {}
    modality = checks.get(qc.CHECK_MODALITIES) or {}
    ok = int(modality.get("ok", 0))
    failed = int(modality.get("failed", 0))
    completeness = round(ok / (ok + failed), 6) if (ok + failed) else 1.0
    topic = report.get("topic_skew") or {}
    diagnostics = {
        "modality_completeness": completeness,
        "topic_skew": topic.get("dominant_share"),
        "reverse_verification": report.get("reverse_verification"),
        "flagged": int(statistics.get("flagged", 0)),
    }
    return {
        "status": 1,
        "uniqueId": uid,
        "quality_report_id": report.get("report_id"),
        "report_status": qc.REPORT_STATUS,
        "hard_failures": hard_failures,
        "diagnostics": diagnostics,
        "sample_count": int(statistics.get("input", 0)),
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
        uid = io_utils.validate_unique_id(str(raw_uid))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    payload = await upload.read()
    if not payload:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})
    is_package = zipfile.is_zipfile(io.BytesIO(payload))
    try:
        native = _ensure_native_input(_store_form1_input(uid, upload, payload))
        overrides = schemas.validate_overrides(_form_overrides(form) or None)
        result = inference.run(input_file=str(native), unique_id=uid,
                               output_root=output_root(), overrides=overrides,
                               allow_external_input=False)
    except (ValueError, ConfigError, inference.InputError,
            io_utils.TaskBusyError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except inference.DependencyError as exc:
        return JSONResponse({"status": 0, "msg": "依赖不可用且不允许降级：%s" % exc})
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})
    detail = _single_file_response(uid, result)
    if not is_package:
        return JSONResponse(detail)
    # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
    try:
        run_root = Path(result["runDir"])
        entry_hint = (result.get("entrypoint")
                      or Path(str(result.get("outputFile") or "")).name)
        reference = form1_package.build_reference(run_root, entry_hint)
    except Exception as exc:  # noqa: BLE001 - 打包失败降级为原响应
        detail["package_error"] = str(exc)[:200]
        detail["status"] = 0
        return JSONResponse(detail)
    business = {k: v for k, v in detail.items() if k != "status"}
    return JSONResponse(form1_package.package_payload(
        result, reference, operator=qc.OPERATOR_NAME, unique_id=uid,
        business=business))


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
        return JSONResponse(status_code=422, content=envelope(
            1, "detection 与 operatorId 同时出现时取值必须一致"))
    try:
        payload = schemas.PredictRequest.model_validate(await request.json())
    except Exception as exc:  # noqa: BLE001 - 参数错误统一 422 信封
        return JSONResponse(status_code=422,
                            content=envelope(1, "请求参数不合法：%s" % exc))
    try:
        value = io_utils.validate_unique_id(payload.uniqueId)
    except ValueError as exc:
        return envelope(1, str(exc))
    try:
        overrides = schemas.validate_overrides(payload.config)
    except ConfigError as exc:
        return envelope(1, str(exc))

    root = output_root()
    try:
        result = inference.run(
            input_file=payload.inputFile, unique_id=value, output_root=root,
            overrides=overrides)
        # [v4.2] 阶段归档/血缘输出已停用：不再调用 mty_compliance.prepare_stage_result
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
    except io_utils.TaskBusyError as exc:
        return envelope(1, str(exc))
    except (ConfigError, inference.InputError) as exc:
        return envelope(1, str(exc))
    except inference.DependencyError as exc:
        return envelope(1, "依赖不可用且不允许降级：%s" % exc)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("predict 失败")
        return envelope(1, "处理失败：%s" % type(exc).__name__)

    try:
        # [v4.2] 统一返回 outputFile（crossmodal-files-v1），不再区分阶段末/中间
        data = {
            "operator": qc.OPERATOR_NAME,
            "status": result["status"],
            "outputFile": inference.stage_file_object(result),
            "result": {
                "uniqueId": result["uniqueId"],
                "runId": result["runId"],
                "status": result["status"],
                "message": result["message"],
                "statistics": result["statistics"],
            },
        }
    except transport.OutputTooLargeError as exc:
        return envelope(1, str(exc))

    LOGGER.info("predict uniqueId=%s status=%s output=%d",
                value, result["status"], result["statistics"]["output"])
    return envelope(0, result["message"], data)


# --------------------------------------------------------------------------- #
# /removeFile
# --------------------------------------------------------------------------- #

@app.post("/removeFile")
def remove_file(uniqueId: str = Query(..., description="任务唯一标识")):
    try:
        value = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return envelope(1, str(exc))
    try:
        root = output_root()
        removed = io_utils.remove_task(root, value)
        LOGGER.info("removeFile uniqueId=%s removed=%s", value, removed)
        return envelope(0, "任务已删除" if removed else "任务不存在，无需删除",
                        {"uniqueId": value, "removed": bool(removed)})
    except io_utils.TaskBusyError as exc:
        return envelope(1, str(exc))
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("removeFile 失败")
        return envelope(1, "删除失败：%s" % type(exc).__name__)


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "operator": qc.OPERATOR_NAME,
        "operatorNumber": qc.FORMAL_OPERATOR_NUMBER,
        "operatorVersion": qc.OPERATOR_VERSION,
        "stageId": qc.STAGE_ID,
        "reportStatus": qc.REPORT_STATUS,
        "interfaces": ["/uploadFile", "/predict", "/removeFile"],
        "downloadFile": False,
        "outputDir": str(output_root()),
    }
