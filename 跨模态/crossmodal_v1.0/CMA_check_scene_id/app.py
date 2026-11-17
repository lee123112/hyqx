"""算子 #02 HTTP 服务：严格三接口。

正式接口只有三个（**不实现** ``/downloadFile``）：
- ``POST /uploadFile?uniqueId=``  上传数据集包（zip，安全解压）
- ``POST /predict``               [v4.2] 双协议：multipart → 形态一单文件流；
                                  JSON → 形态二阶段传递（统一 outputFile + result，去阶段血缘文件）
- ``POST /removeFile?uniqueId=``  幂等清理任务目录

统一外层响应：``{"code": 0|1, "msg": "...", "data": {...}}``。
``code=0`` 只表示服务完成了本次请求；业务上仍可能有「改走配对集」「模态不足」等状态，
它们写在主产物、场景组清单和统计里，不用 HTTP 错误代替。
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
from typing import Any, Dict

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import io as io_utils
from utils import scene
from utils import transport
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.schemas import ConfigError, PredictRequest

#: 默认 HTTP 端口（8102–8126 中的 8102 已被 03_数据初筛 占用，8108 空缺，
#: 其余 8109–8126 亦已占用；本算子取唯一空缺的 8108，无冲突）
DEFAULT_PORT = 8108
DEFAULT_HOST = "0.0.0.0"

#: 上传内容目录名（位于任务目录内）
UPLOAD_DIRNAME = "input"

app = FastAPI(title="算子 #02 核验场景编号", version="v1.0.0", docs_url="/docs")


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
        with open(str(archive), "wb") as sink:
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


@app.post("/predict")
async def predict(request: Request):
    """[v4.2] 双协议入口：multipart/form-data → 形态一；否则形态二（阶段 JSON）。"""
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
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return _json(1, "请求体必须是 JSON 对象", {}, 400)
    try:
        payload = PredictRequest.model_validate(raw)
    except ValidationError as exc:
        details = [{"loc": ".".join(str(part) for part in item.get("loc", ())),
                    "msg": item.get("msg", "")} for item in exc.errors()]
        return _json(1, "请求参数校验失败", {"errors": details}, 422)
    return _predict_stage(payload)


def _predict_stage(payload: PredictRequest) -> JSONResponse:
    """形态二：阶段文件传递协议。[v4.2] 响应去阶段血缘文件，统一返回 outputFile。"""
    _validate_unique_id(payload.uniqueId)
    root = output_root()
    with io_utils.TaskLock(root, payload.uniqueId):
        try:
            source = _resolve_prediction_input(root, payload.uniqueId, payload)
            result = inference.run(
                input_file=source, unique_id=payload.uniqueId,
                output_root=str(root), overrides=payload.config or None)
            # [v4.2] mty_compliance 已停用：注释 prepare_stage_result 调用（代码保留注释存档）
            # from utils import mty_compliance as _mty_compliance
            # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
            file_object = inference.stage_file_object(result)
        except ConfigError as exc:
            return _json(1, "配置校验失败：%s" % exc, {}, 200)
        except io_utils.TaskBusyError as exc:
            return _json(1, str(exc), {}, 409)
        except transport.OutputTooLargeError as exc:
            return _json(1, str(exc), {}, 413)
        except (inference.InputError,) as exc:
            return _json(1, "%s: %s" % (type(exc).__name__, exc), {}, 400)
        except ValueError as exc:
            return _json(1, "ValueError: %s" % exc, {}, 400)
        except OSError as exc:
            return _json(1, "运行期 IO 错误：%s" % type(exc).__name__, {}, 500)
    summary = _business_fields(result)
    return _json(0, result["status"], {
        "operator": result.get("operator") or scene.OPERATOR_SLUG,
        "status": result["status"],
        "outputFile": file_object,
        "result": {
            "uniqueId": result.get("uniqueId"),
            "runId": result.get("runId"),
            "status": result["status"],
            "statistics": result.get("statistics", {}),
            "group_id": summary["group_id"],
            "synthetic": summary["synthetic"],
        },
    }, 200)


# ------------------------------------------------------------------ 形态一：单文件流（[v4.2] 新增）
#: detection/operatorId 别名空间（大小写不敏感）：M02/02/2/英文ID/目录名
DETECTION_ALIASES = {"m02", "02", "2", "check_scene_id",
                     "scene_id_verification", "cma_check_scene_id"}

#: 形态一白名单标量（未知字段忽略）；与 config 白名单一致
_FORM1_SCALARS = {
    "enabled": "bool", "scene_id_source": "str", "min_modalities": "int",
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


def _iter_jsonl(path: Any):
    if not path or not Path(str(path)).is_file():
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


def _scene_synthetic(record: Dict[str, Any]) -> bool:
    """是否本产线合成的场景组（补编号口径 scene_source=supplemented / scene-auto-*）。"""
    node = (record.get("processing") or {}).get("02") or {}
    if node.get("scene_source") == scene.SCENE_SOURCE_SUPPLEMENTED:
        return True
    scene_id = record.get("scene_id") or node.get("scene_id") or ""
    return isinstance(scene_id, str) and scene_id.startswith(scene.GENERATED_SCENE_PREFIX)


def _business_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态一业务字段：group_id + synthetic。"""
    payload: Dict[str, Any] = {"group_id": None, "synthetic": False}
    for record in _iter_jsonl(result.get("outputFile")):
        payload["group_id"] = record.get("group_id")
        payload["synthetic"] = _scene_synthetic(record)
        break
    return payload


def _form1_audit(uid: str, detection: str, filename: str, size: int,
                 status: int, elapsed_ms: int) -> None:
    """[v4.2] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    record = {"uniqueId": uid, "detection": detection,
              "file": {"name": filename, "bytes": size},
              "status": status, "elapsed_ms": elapsed_ms,
              "time": datetime.now().isoformat(timespec="seconds")}
    try:
        task_dir = output_root() / uid
        task_dir.mkdir(parents=True, exist_ok=True)
        with open(str(task_dir / "form1_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


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


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一业务字段（V4.2 §4.2）：group_id + synthetic（处理型 1 成功/0 失败）。

    ``process_status`` 取记录节点真实处置态（verified / insufficient_modalities /
    no_scene_id / disabled），不把「工具未启用」伪装成 success。
    """
    payload: Dict[str, Any] = {"status": 1, "uniqueId": uid}
    payload.update(_business_fields(result))
    stats = result.get("statistics") or {}
    process_status = result.get("status") or "success"
    for record in _iter_jsonl(result.get("outputFile")):
        node = (record.get("processing") or {}).get("02") or {}
        process_status = node.get("process_status") or process_status
        break
    if stats.get("empty_input"):
        process_status = "not_applicable"
    payload["process_status"] = process_status
    return payload


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（multipart/form-data），处理型 status（1 成功/0 失败）。"""
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
    task_dir = root / uid
    try:
        with io_utils.TaskLock(root, uid):
            task_dir.mkdir(parents=True, exist_ok=True)
            _reset_task(task_dir)
            input_dir = task_dir / UPLOAD_DIRNAME
            input_dir.mkdir(parents=True, exist_ok=True)
            if zipfile.is_zipfile(io.BytesIO(content)):
                archive = task_dir / "bundle.zip"
                archive.write_bytes(content)
                io_utils.safe_extract_zip(archive, input_dir)
                archive.unlink()
                entry_path, _name = transport.resolve_entrypoint(input_dir)
            else:
                entry_path = input_dir / Path(filename.replace("\\", "/")).name
                entry_path.write_bytes(content)
            native = _ensure_native_input(entry_path)
    except (ValueError, transport.TransportError, io_utils.TaskBusyError,
            OSError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    try:
        result = inference.run(input_file=str(native), unique_id=uid,
                               output_root=str(root), overrides=overrides or None)
    except (inference.InputError, ConfigError, ValueError, OSError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": "task_busy: %s" % exc})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": 0, "msg": "%s: %s"
                             % (type(exc).__name__, str(exc)[:400])})

    elapsed_ms = int((time.monotonic() - started) * 1000)
    detection = request.query_params.get("detection") \
        or request.query_params.get("operatorId") or ""
    payload = _single_file_response(uid, result)
    _form1_audit(uid, detection, filename, len(content),
                 int(payload.get("status", 1)), elapsed_ms)
    return JSONResponse(payload)


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
        "operator": scene.OPERATOR_SLUG,
        "operatorNumber": scene.FORMAL_OPERATOR_NUMBER,
        "stageId": scene.DEFAULT_STAGE_ID,
        "interfaces": ["/uploadFile", "/predict", "/removeFile"],
        "downloadFile": False,
        "model": None,
        "config": str(inference.DEFAULT_CONFIG_PATH.name),
    }


if __name__ == "__main__":  # pragma: no cover - 本地启动
    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=int(os.environ.get("PORT", DEFAULT_PORT)))
