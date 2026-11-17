"""算子 01「接入多模态批次」HTTP 服务：只实现三个正式接口。

| 接口 | 作用 |
| --- | --- |
| ``POST /uploadFile?uniqueId=`` | 上传批次清单（支持 ZIP 包或多文件） |
| ``POST /predict`` | [v4.2] 双协议：multipart → 形态一单文件流；JSON → 形态二阶段传递 |
| ``POST /removeFile?uniqueId=`` | 删除任务目录（幂等） |

[v4.2] 形态二统一返回 ``data.outputFile``（crossmodal-files-v1）+ ``data.result``，
不再返回阶段血缘文件（血缘/阶段归档已停用）；``isLastOperator`` 保留严格校验，
但不再改变响应形态。形态一返回 JSON，首字段 int ``status``（处理型：1 成功/0 失败）
+ ``batch_id`` / ``accepted`` / ``isolation_records``。

**不实现 ``/downloadFile``**：未匹配到的路径统一返回
``{"code": 1, "msg": "接口不存在", "data": {}}`` 且 HTTP 404。

所有响应外层为 ``{"code": 0 或 1, "msg": "...", "data": {...}}``；
输出只写入 ``OUTPUT_DIR/<uniqueId>/``，拒绝空/非法/绝对路径/目录穿越的 uniqueId。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import intake as it
from utils import io as io_utils
from utils import transport
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.schemas import ConfigError, PredictRequest, validate_overrides

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))
MAX_UNPACKED_BYTES = int(os.environ.get("MAX_UNPACKED_BYTES", str(512 * 1024 * 1024)))
MAX_UPLOAD_FILES = int(os.environ.get("MAX_UPLOAD_FILES", "1000"))
ZIP_MAGIC = b"PK\x03\x04"
INPUT_DIR_NAME = "input"
STAGING_DIR_NAME = ".upload_staging"

app = FastAPI(
    title="01 接入多模态批次",
    version=it.OPERATOR_VERSION,
    description="表1 #01 批次接入与登记：来源/格式白名单/授权校验、"
                "只保存原文件链接、可加工清单与两类隔离记录（三接口）",
)


# --------------------------------------------------------------------------- #
# 统一信封与异常
# --------------------------------------------------------------------------- #

def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": int(code), "msg": msg, "data": data if data is not None else {}}


@app.exception_handler(RequestValidationError)
async def validation_handler(_request, exc: RequestValidationError) -> JSONResponse:
    details = "; ".join(
        "%s %s" % (".".join(str(part) for part in item.get("loc", ())), item.get("msg"))
        for item in exc.errors()[:5])
    return JSONResponse(status_code=422,
                        content=envelope(1, "请求参数不合法：%s" % details))


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        return JSONResponse(status_code=404, content=envelope(1, "接口不存在", {}))
    return JSONResponse(status_code=exc.status_code,
                        content=envelope(1, str(exc.detail), {}))


def output_root() -> Path:
    """输出根目录：运行时读取 ``OUTPUT_DIR``，缺省 ``./output``。"""
    return io_utils.resolve_root(os.environ.get("OUTPUT_DIR", "output"))


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

def _is_zip(filename: str, payload: bytes) -> bool:
    return payload[:4] == ZIP_MAGIC or (filename or "").lower().endswith(".zip")


def _store_uploads(task_dir: Path, payloads: List[Tuple[str, bytes]]) -> List[str]:
    """把上传内容落到 ``task/input``（单个 zip 走安全解压），返回包内相对路径清单。

    多文件上传时按上传顺序逐个落盘；若出现多个根级 JSONL（无法确定入口）
    或嵌套 zip，则整体失败并清理暂存目录（不产生半截任务）。
    """
    staging = task_dir / STAGING_DIR_NAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        if len(payloads) == 1 and _is_zip(payloads[0][0], payloads[0][1]):
            written = io_utils.safe_extract_zip(
                io.BytesIO(payloads[0][1]), staging, max_files=MAX_UPLOAD_FILES,
                max_unpacked_bytes=MAX_UNPACKED_BYTES)
        else:
            written = []
            for filename, payload in payloads:
                if _is_zip(filename, payload):
                    raise ValueError("多文件上传中不允许再嵌套 zip：%s" % filename)
                relative = io_utils.normalize_zip_member_name(filename or it.MAIN_NAME)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                written.append(relative)

        manifest = transport.load_manifest(staging)
        entry, entry_relative = transport.resolve_entrypoint(staging, manifest)

        if manifest is None:
            manifest = transport.build_manifest(
                entry_relative, [{"path": name, "role": "uploaded"} for name in written])
            transport.write_manifest(staging, manifest)

        input_dir = task_dir / INPUT_DIR_NAME
        if input_dir.exists():
            shutil.rmtree(input_dir)
        staging.rename(input_dir)
        return written
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


@app.post("/uploadFile")
def upload_file(uniqueId: str = Query(..., description="任务唯一标识"),
                file: Optional[UploadFile] = File(None, description="单文件或 zip 包"),
                files: Optional[List[UploadFile]] = File(None, description="多文件上传")
                ) -> Dict[str, Any]:
    """上传批次登记清单（zip 包或多文件）。"""
    try:
        value = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return envelope(1, str(exc), {})

    payloads: List[Tuple[str, bytes]] = []
    for item in [file] + list(files or []):
        if item is None:
            continue
        payload = item.file.read(MAX_UPLOAD_BYTES + 1)
        name = item.filename or ""
        if not payload:
            return envelope(1, "上传内容为空：%s" % (name or "<unnamed>"), {})
        if len(payload) > MAX_UPLOAD_BYTES:
            return envelope(1, "上传内容超过限额 %d 字节" % MAX_UPLOAD_BYTES, {})
        payloads.append((name, payload))
    if not payloads:
        return envelope(1, "未提供上传内容（file 或 files）", {})

    total = sum(len(payload) for _name, payload in payloads)
    if total > MAX_UPLOAD_BYTES:
        return envelope(1, "上传内容合计超过限额 %d 字节" % MAX_UPLOAD_BYTES, {})

    try:
        with io_utils.TaskLock(output_root(), value):
            task = io_utils.task_dir(output_root(), value)
            written = _store_uploads(task, payloads)
            entrypoint = transport.load_manifest(task / INPUT_DIR_NAME)["entrypoint"]
    except (ValueError, transport.TransportError, OSError, UnicodeDecodeError) as exc:
        return envelope(1, "上传失败：%s" % exc, {})
    except io_utils.TaskBusyError as exc:
        return envelope(1, str(exc), {})
    return envelope(0, "上传成功", {
        "uniqueId": value,
        "entrypoint": entrypoint,
        "files": sorted(written),
        "fileCount": len(written),
    })


# --------------------------------------------------------------------------- #
# 推理
# --------------------------------------------------------------------------- #

def _iter_jsonl(path: Optional[Any]):
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


def _isolation_records(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[v4.2] 形态一隔离记录：三类旁路台账逐样本给出去向与原因码。"""
    records: List[Dict[str, Any]] = []
    run_dir = result.get("runDir")
    if not run_dir:
        return records
    for name in (it.ORIGIN_QUARANTINE_NAME, it.SAMPLE_QUARANTINE_NAME, it.RETURN_NAME):
        for entry in _iter_jsonl(Path(str(run_dir)) / name):
            records.append({
                "sample_id": entry.get("sample_id"),
                "batch_id": entry.get("batch_id"),
                "destination": entry.get("destination"),
                "role": entry.get("role"),
                "reason_code": entry.get("reason_code") or entry.get("reason"),
            })
    return records


def _business_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 业务摘要（形态一业务字段，形态二放 ``data.result``）。"""
    stats = result.get("statistics") or {}
    registry = result.get("registry") or {}
    batches = registry.get("batches") or []
    batch_id = batches[0].get("batch_id") if batches else None
    if not batch_id:
        for item in _iter_jsonl(result.get("outputFile")):
            batch_id = item.get("batch_id")
            break
    return {
        "batch_id": batch_id,
        "accepted": int(stats.get("registered", 0) or 0),
        "isolation_records": _isolation_records(result),
    }


def _predict_stage(payload: PredictRequest):
    """形态二：阶段文件传递协议。[v4.2] 响应去阶段血缘文件，统一返回 outputFile。"""
    try:
        overrides = validate_overrides(payload.config)
        result = inference.run(input_file=payload.inputFile, unique_id=payload.uniqueId,
                              output_root=output_root(), overrides=overrides)
        # [v4.2] mty_compliance 已停用：注释 prepare_stage_result 调用（代码保留注释存档）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
    except (ConfigError, inference.InputError, io_utils.TaskBusyError) as exc:
        return envelope(1, str(exc), {})
    except transport.OutputTooLargeError as exc:
        return envelope(1, str(exc), {})
    except Exception as exc:  # noqa: BLE001 - 接口必须返回统一信封
        return envelope(1, "%s: %s" % (type(exc).__name__, str(exc)[:400]), {})
    try:
        file_object = inference.stage_file_object(result)
    except transport.OutputTooLargeError as exc:
        return envelope(1, str(exc), {})
    summary = _business_fields(result)
    return envelope(0, result.get("message") or "接入多模态批次完成", {
        "operator": it.OPERATOR_NAME,
        "status": result["status"],
        "outputFile": file_object,
        "result": {
            "uniqueId": result.get("uniqueId"),
            "runId": result.get("runId"),
            "status": result.get("status"),
            "statistics": result.get("statistics", {}),
            "batch_id": summary["batch_id"],
            "accepted": summary["accepted"],
        },
    })


# --------------------------------------------------------------------------- #
# 形态一：单文件流（MTY V2，[v4.2] 新增）
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M01/01/1/英文ID/目录名
DETECTION_ALIASES = {"m01", "01", "1", "receive_multimodal_batch",
                     "multimodal_batch_intake", "cma_receive_multimodal_batch"}

#: 形态一白名单标量（未知字段忽略）；与 config 白名单一致
_FORM1_SCALARS = {
    "enabled": "bool", "file_check_mode": "str",
    "dedupe": "bool", "check_source_modality": "bool",
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
    """JSONL 原样使用；JSON 转为 JSONL（批次 ``samples`` 数组逐样本展开）；其他拒绝。"""
    suffix = entry.suffix.lower()
    if suffix == ".jsonl":
        return entry
    if suffix == ".json":
        try:
            payload = json.loads(entry.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 文件解析失败") from exc
        batch_keys: Dict[str, Any] = {}
        if isinstance(payload, dict) and isinstance(payload.get("samples"), list):
            # V4.2 输入示例：批次对象 {batch_id, samples:[...]}，逐样本写入 JSONL
            batch_keys = {key: value for key, value in payload.items()
                          if key != "samples"}
            items = []
            for sample in payload["samples"]:
                if not isinstance(sample, dict):
                    continue
                merged = dict(batch_keys)
                merged.update(sample)
                items.append(merged)
        else:
            items = payload if isinstance(payload, list) else [payload]
        target = entry.with_name(entry.stem + "__native.jsonl")
        with open(str(target), "w", encoding="utf-8", newline="\n") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return target
    raise ValueError("该算子需要 JSON/JSONL/ZIP 输入")


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
    except Exception:  # noqa: BLE001  # 审计写盘失败不影响主流程
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
    """形态一业务字段（V4.2 §4.1）：batch_id + accepted + isolation_records。"""
    payload: Dict[str, Any] = {"status": 1, "uniqueId": uid}
    payload.update(_business_fields(result))
    payload["process_status"] = result.get("status") or "success"
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
            input_dir = task_dir / INPUT_DIR_NAME
            input_dir.mkdir(parents=True, exist_ok=True)
            if zipfile.is_zipfile(io.BytesIO(content)):
                archive = task_dir / "input.zip"
                archive.write_bytes(content)
                io_utils.safe_extract_zip(archive, input_dir,
                                          max_files=MAX_UPLOAD_FILES,
                                          max_unpacked_bytes=MAX_UNPACKED_BYTES)
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
    except Exception as exc:  # noqa: BLE001 - 形态一统一返回 status=0
        return JSONResponse({"status": 0, "msg": "%s: %s"
                             % (type(exc).__name__, str(exc)[:400])})

    elapsed_ms = int((time.monotonic() - started) * 1000)
    detection = request.query_params.get("detection") \
        or request.query_params.get("operatorId") or ""
    if result.get("status") == "failed":
        _form1_audit(uid, detection, filename, len(content), 0, elapsed_ms)
        return JSONResponse({"status": 0, "msg": result.get("message") or "处理失败"})
    payload = _single_file_response(uid, result)
    _form1_audit(uid, detection, filename, len(content),
                 int(payload.get("status", 1)), elapsed_ms)
    return JSONResponse(payload)


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
        return JSONResponse(status_code=422, content=envelope(
            1, "detection 与 operatorId 同时出现时取值必须一致", {}))
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return envelope(1, "请求体必须是 JSON 对象", {})
    try:
        payload = PredictRequest.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            "%s %s" % (".".join(str(part) for part in item.get("loc", ())), item.get("msg"))
            for item in exc.errors()[:5])
        return JSONResponse(status_code=422,
                            content=envelope(1, "请求参数不合法：%s" % details, {}))
    return _predict_stage(payload)



# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #

@app.post("/removeFile")
def remove_file(uniqueId: str = Query(..., description="任务唯一标识")) -> Dict[str, Any]:
    """删除任务目录（幂等：不存在时返回 removed=false）。"""
    try:
        removed = io_utils.remove_task(output_root(), uniqueId)
    except ValueError as exc:
        return envelope(1, str(exc), {})
    except io_utils.TaskBusyError as exc:
        return envelope(1, str(exc), {})
    except OSError as exc:
        return envelope(1, "删除失败：%s" % exc, {})
    return envelope(0, "删除完成", {"uniqueId": uniqueId, "removed": bool(removed)})


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    return {
        "operator": it.OPERATOR_NAME,
        "operatorLabel": it.OPERATOR_LABEL,
        "operatorNumber": it.FORMAL_OPERATOR_NUMBER,
        "operatorVersion": it.OPERATOR_VERSION,
        "stageId": it.STAGE_ID,
        "stageLabel": it.STAGE_LABEL,
        "stageFirst": True,
        "interfaces": ["/uploadFile", "/predict", "/removeFile"],
        "downloadFile": False,
        "model": None,
        "config": str(inference.DEFAULT_CONFIG_PATH.name),
    }
