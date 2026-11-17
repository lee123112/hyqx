"""算子 21 HTTP 服务：严格三接口。

正式接口只有三个（不实现 ``/downloadFile``）：
- ``POST /uploadFile?uniqueId=``  上传数据集包（zip，安全解压）
- ``POST /predict``               阶段内中间 / 阶段末两种返回形态
- ``POST /removeFile?uniqueId=``  幂等清理任务目录

统一外层响应：``{"code": 0|1, "msg": "...", "data": {...}}``。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import form1_package, material_handoff
from utils import io as io_utils
from utils import ledger, transport
from utils.schemas import ConfigError, PredictRequest

# [v4.2] 血缘/阶段归档输出已停用：mty_compliance 不再被调用（utils/mty_compliance.py 保留存档）
# from utils import mty_compliance

#: 默认 HTTP 端口（dolphin demo：8102 保留，8109–8118 已占用，算子 21 用 8121）
DEFAULT_PORT = 8121
DEFAULT_HOST = "0.0.0.0"

#: 上传内容目录名（位于任务目录内）
UPLOAD_DIRNAME = "input"

#: [v4.2] 形态一（单文件流）算子标识：detection 与 operatorId 同一别名空间（大小写不敏感）
ENGLISH_ID = "dispose_mismatched_sample"
DETECTION_ALIASES = frozenset({
    "m21", "21", ENGLISH_ID, "cma_" + ENGLISH_ID, "mismatch_disposal",
})

#: [v4.2] 形态一 config 白名单（与 utils/schemas.BusinessOverrides 对齐；未知表单字段忽略）
_FORM_SCALAR_TYPES = {
    "return_limit": "int",
    "max_retry_per_item": "int",
    "manual_platform_available": "bool",
    "allow_degraded": "bool",
    "max_embedded_record_bytes": "int",
}

app = FastAPI(title="算子 21 处置错配样本", version="v1.0.0", docs_url="/docs")


# ------------------------------------------------------------------ 工具
def output_root() -> Path:
    """输出根：``$OUTPUT_DIR`` 或算子目录下 ``output/``。"""
    value = os.environ.get("OUTPUT_DIR")
    root = Path(value) if value else (inference.OPERATOR_DIR / "output")
    return io_utils.resolve_root(root)


def envelope(code: int, msg: str, data: Any = None) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": data if data is not None else {}}


def _json(code: int, msg: str, data: Any, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=envelope(code, msg, data))


def _validate_unique_id(unique_id: str) -> str:
    try:
        return io_utils.validate_unique_id(unique_id)
    except ValueError as exc:
        raise RequestValidationError([{"loc": ("query", "uniqueId"), "msg": str(exc),
                                       "type": "value_error"}]) from exc


# ------------------------------------------------------------------ 异常处理
@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"loc": ".".join(str(part) for part in item.get("loc", [])),
         "msg": item.get("msg", "")}
        for item in exc.errors()
    ]
    return _json(1, "请求参数校验失败", {"errors": details}, 422)


@app.exception_handler(StarletteHTTPException)
async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _json(1, "未找到路径或方法：%s" % request.url.path, {}, exc.status_code)


# ------------------------------------------------------------------ 上传
def _store_upload(root: Path, unique_id: str, upload: UploadFile) -> Dict[str, Any]:
    staging_root = root / ".tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(staging_root), prefix="upload_"))
    try:
        archive = staging / "bundle.zip"
        with open(archive, "wb") as sink:
            shutil.copyfileobj(upload.file, sink)
        content = staging / "content"
        written = io_utils.safe_extract_zip(archive, content)
        entrypoint, relative = transport.resolve_entrypoint(content)

        task = io_utils.task_dir(root, unique_id)
        target = task / UPLOAD_DIRNAME
        if target.exists():
            shutil.rmtree(target)
        io_utils.commit_directory(content, target)

        size_bytes = sum((target / Path(item)).stat().st_size for item in written)
        return {
            "uniqueId": unique_id,
            "entrypoint": relative,
            "inputFile": str(target / Path(relative)),
            "files": len(written),
            "sizeBytes": size_bytes,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@app.post("/uploadFile")
async def upload_file(request: Request,
                      uniqueId: str = Query(..., description="任务唯一标识"),
                      file: UploadFile = File(..., description="数据集 zip 包")) -> JSONResponse:
    unique_id = _validate_unique_id(uniqueId)
    root = output_root()
    with io_utils.TaskLock(root, unique_id):
        try:
            data = _store_upload(root, unique_id, file)
        except ValueError as exc:
            return _json(1, "上传素材包非法：%s" % exc, {}, 400)
        except OSError as exc:
            return _json(1, "写入上传包失败：%s" % type(exc).__name__, {}, 500)
        finally:
            await file.close()
    return _json(0, "上传成功", data, 200)


# ------------------------------------------------------------------ 预测
def _resolve_prediction_input(root: Path, unique_id: str,
                              request: PredictRequest) -> str:
    """阶段首用本次上传的入口；阶段内后续算子用上一中间响应的输入路径。"""
    if request.inputFile:
        return request.inputFile
    task = io_utils.task_dir(root, unique_id, create=False)
    upload_dir = task / UPLOAD_DIRNAME
    if not upload_dir.exists():
        raise inference.InputError("任务 %s 尚未上传素材包，且未提供 inputFile" % unique_id)
    entrypoint, _relative = transport.resolve_entrypoint(upload_dir)
    return str(entrypoint)


def _business_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """形态二 ``data.result``：业务摘要（保留链式调用所需的输入/输出路径）。"""
    return {
        "inputFile": result.get("inputFile"),
        "outputFile": result.get("outputFile"),
        "runId": result.get("runId"),
        "statistics": result.get("statistics"),
        "startedAt": result.get("startedAt"),
        "finishedAt": result.get("finishedAt"),
    }


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
        return _json(1, "detection 与 operatorId 同时出现时取值必须一致", {}, 422)
    try:
        raw_body = await request.json()
    except Exception:
        return _json(1, "请求参数校验失败", {"errors": [{"loc": "body", "msg": "请求体不是合法 JSON"}]}, 422)
    try:
        payload = PredictRequest(**raw_body)
    except Exception as exc:  # pydantic ValidationError 及同族异常
        details = []
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                details = [{"loc": ".".join(str(part) for part in item.get("loc", [])),
                            "msg": item.get("msg", "")} for item in errors()]
            except Exception:
                details = []
        return _json(1, "请求参数校验失败", {"errors": details}, 422)
    root = output_root()
    with io_utils.TaskLock(root, payload.uniqueId):
        try:
            source = _resolve_prediction_input(root, payload.uniqueId, payload)
            result = inference.run(
                input_file=source, unique_id=payload.uniqueId,
                output_root=str(root), overrides=payload.config or None)
            # [v4.2] 血缘/阶段归档输出已停用：不再调用 prepare_stage_result，
            # 响应不再区分阶段末/非阶段末，统一返回 outputFile（crossmodal-files-v1）。
            # from utils import mty_compliance as _mty_compliance
            # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
            data = {
                "operator": ENGLISH_ID,
                "status": result["status"],
                "outputFile": inference.stage_file_object(result),
                "result": _business_summary(result),
            }
        except ConfigError as exc:
            return _json(1, "配置校验失败：%s" % exc, {}, 200)
        except io_utils.TaskBusyError as exc:
            return _json(1, str(exc), {}, 409)
        except transport.OutputTooLargeError as exc:
            return _json(1, str(exc), {}, 413)
        except (inference.InputError, ledger.DependencyError) as exc:
            return _json(1, "%s: %s" % (type(exc).__name__, exc), {}, 400)
        except ValueError as exc:
            return _json(1, "ValueError: %s" % exc, {}, 400)
        except OSError as exc:
            return _json(1, "运行期 IO 错误：%s" % type(exc).__name__, {}, 500)
    return _json(0, result["status"], data, 200)


# ------------------------------------------------------------------ [v4.2] 形态一：单文件流
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
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
    return overrides


def _store_single_file(root: Path, unique_id: str, upload: Any) -> Path:
    """形态一上传落盘：JSON/JSONL 直接用；ZIP 安全解压后定位入口；其余拒绝。"""
    task = io_utils.task_dir(root, unique_id)
    target_dir = task / UPLOAD_DIRNAME
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(upload.filename or "upload.bin").name
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        archive = target_dir / filename
        with open(archive, "wb") as sink:
            shutil.copyfileobj(upload.file, sink)
        content = target_dir / "content"
        io_utils.safe_extract_zip(archive, content)
        archive.unlink()
        entrypoint, _relative = transport.resolve_entrypoint(content)
        return entrypoint
    if suffix in (".jsonl", ".json"):
        target = target_dir / filename
        with open(target, "wb") as sink:
            shutil.copyfileobj(upload.file, sink)
        return target
    raise ValueError("该算子需要 JSON/JSONL 输入")


def _load_disposal_records(ledger_path: Path) -> list:
    """从处置台账投影形态一 disposal_records 字段。"""
    keys = ("sample_id", "label", "action", "destination", "retry_count", "reason_code")
    records = []
    if ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append({key: entry.get(key) for key in keys})
    return records


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
        unique_id = io_utils.validate_unique_id(str(uid_raw))
        is_package = Path(upload.filename or "").suffix.lower() == ".zip"
        root = output_root()
        with io_utils.TaskLock(root, unique_id):
            entry = _store_single_file(root, unique_id, upload)
            overrides = _form_overrides(form)
            result = inference.run(input_file=str(entry), unique_id=unique_id,
                                   output_root=str(root),
                                   overrides=overrides or None)
            if is_package and result.get("status") != "failed":
                material_handoff.forward(
                    io_utils.task_dir(root, unique_id) / UPLOAD_DIRNAME / "content",
                    Path(result["runDir"]),
                    {"origin_quarantine": inference.ORIGIN_NAME,
                     "sample_quarantine": inference.SAMPLE_NAME})
        if result.get("status") == "failed":
            return JSONResponse({"status": 0, "msg": result.get("reason", "处理失败")})
        stats = result.get("statistics", {})
        records = _load_disposal_records(Path(result["runDir"]) / inference.LEDGER_NAME)
        detail = {
            "status": 1,
            "disposal_records": records,
            "token_eligible": bool(stats.get("passed", 0) > 0),
        }
        if not is_package:
            return JSONResponse(detail)
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            entry_hint = (result.get("entrypoint")
                          or Path(str(result.get("outputFile") or "")).name)
            reference = form1_package.build_reference(Path(result["runDir"]), entry_hint)
        except Exception as exc:  # noqa: BLE001 - 打包失败降级为原响应
            detail["package_error"] = str(exc)[:200]
            detail["status"] = 0
            return JSONResponse(detail)
        business = {k: v for k, v in detail.items() if k != "status"}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator=ENGLISH_ID, unique_id=unique_id,
            business=business))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except Exception as exc:  # 形态一统一失败出口
        return JSONResponse({"status": 0, "msg": "%s: %s" % (type(exc).__name__, exc)})


# ------------------------------------------------------------------ 清理
@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(..., description="任务唯一标识")) -> JSONResponse:
    unique_id = _validate_unique_id(uniqueId)
    root = output_root()
    try:
        removed = io_utils.remove_task(root, unique_id)
    except io_utils.TaskBusyError as exc:
        return _json(1, str(exc), {}, 409)
    except ValueError as exc:
        return _json(1, "ValueError: %s" % exc, {}, 400)
    return _json(0, "已清理" if removed else "任务不存在，按已清理处理",
                 {"uniqueId": unique_id, "removed": removed}, 200)


# ------------------------------------------------------------------ 健康检查
# 契约只暴露三接口（/uploadFile、/predict、/removeFile），探针不进 OpenAPI 文档
@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    """[D1] 单算子存活探针：算子标识/状态（与已有算子 /health 风格一致）。"""
    return {
        "status": "ok",
        "operator": ledger.OPERATOR,
        "operatorNumber": ledger.FORMAL_OPERATOR_NUMBER,
        "interfaces": ["/uploadFile", "/predict", "/removeFile"],
        "downloadFile": False,
        "model": None,
        "config": str(inference.DEFAULT_CONFIG_PATH.name),
    }


if __name__ == "__main__":  # pragma: no cover - 本地启动
    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=int(os.environ.get("PORT", DEFAULT_PORT)))
