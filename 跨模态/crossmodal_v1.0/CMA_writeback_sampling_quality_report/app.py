"""算子 24「抽检并回写质量报告」HTTP 服务：只实现三个正式接口。

| 接口 | 作用 |
| --- | --- |
| ``POST /uploadFile?uniqueId=`` | 上传候选池（支持 zip 包，内含 ``20_quality_report.json``） |
| ``POST /predict`` | 执行抽检并回写；``isLastOperator`` 决定返回形态 |
| ``POST /removeFile?uniqueId=`` | 删除任务目录（幂等） |

**不实现 ``/downloadFile``**：未匹配到的路径统一返回
``{"code": 1, "msg": "接口不存在", "data": {}}`` 且 HTTP 404。

所有响应外层为 ``{"code": 0 或 1, "msg": "...", "data": {...}}``；
输出只写入 ``OUTPUT_DIR/<uniqueId>/``，拒绝空/非法/绝对路径/目录穿越的 uniqueId。
"""
from __future__ import annotations

import io
import json
import os
import zipfile
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import io as io_utils
from utils import spot_check as sc
from utils import transport
from utils import form1_package
from utils.schemas import ConfigError, PredictRequest, validate_overrides

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))
ZIP_MAGIC = b"PK\x03\x04"
INPUT_DIR_NAME = "input"
STAGING_DIR_NAME = ".upload_staging"

app = FastAPI(
    title="24 抽检并回写质量报告",
    version=sc.OPERATOR_VERSION,
    description="按场景/模态/对齐类型分层抽检、写回人工复核结论并回写质量报告（三接口）",
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
        return JSONResponse(status_code=404,
                            content=envelope(1, "接口不存在", {}))
    return JSONResponse(status_code=exc.status_code,
                        content=envelope(1, str(exc.detail), {}))


def output_root() -> Path:
    """输出根目录：运行时读取 ``OUTPUT_DIR``，缺省 ``./output``。"""
    return io_utils.resolve_root(os.environ.get("OUTPUT_DIR", "output"))


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

def _store_upload(task_dir: Path, payload: bytes, filename: str) -> List[str]:
    """把上传内容落到 ``task/input``（zip 走安全解压），返回包内相对路径清单。"""
    staging = task_dir / STAGING_DIR_NAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    written: List[str] = []
    try:
        if payload[:4] == ZIP_MAGIC or (filename or "").lower().endswith(".zip"):
            written = io_utils.safe_extract_zip(io.BytesIO(payload), staging)
        else:
            relative = io_utils.normalize_zip_member_name(filename or "candidate_pool.jsonl")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            written = [relative]

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
def upload_file(file: UploadFile,
                uniqueId: str = Query(..., description="任务唯一标识")) -> Dict[str, Any]:
    """上传候选池（zip 包可同时携带上游待回写质量报告）。"""
    try:
        value = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return envelope(1, str(exc), {})
    try:
        with io_utils.TaskLock(output_root(), value):
            task = io_utils.task_dir(output_root(), value)
            payload = file.file.read(MAX_UPLOAD_BYTES + 1)
            if not payload:
                return envelope(1, "上传内容为空", {})
            if len(payload) > MAX_UPLOAD_BYTES:
                return envelope(1, "上传内容超过限额 %d 字节" % MAX_UPLOAD_BYTES, {})
            written = _store_upload(task, payload, file.filename or "")
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

# [v4.2] 双协议：multipart/form-data → 形态一（单文件流）；application/json → 形态二。
_DETECTION_ALIASES = frozenset({
    "m24", "24", "spot_check_writeback", "writeback_sampling_quality_report",
    "cma_writeback_sampling_quality_report",
})
#: 形态一额外支持的白名单标量（§4.24 接口一：sampling_ratio / sampling_seed）
_FORM_SCALARS = {
    "sampling_ratio": "float",
    "sampling_seed": "int",
}


def _detection_ok(value: Optional[str]) -> bool:
    if value is None or not str(value).strip():
        return True
    return str(value).strip().lower() in _DETECTION_ALIASES


def _convert_scalar(kind: str, raw: Any) -> Any:
    if kind == "int":
        return int(str(raw).strip())
    if kind == "float":
        return float(str(raw).strip())
    return raw


def _form_overrides(form, query=None) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for key, kind in _FORM_SCALARS.items():
        if key in form:
            overrides[key] = _convert_scalar(kind, form.get(key))
        elif query is not None and key in query:
            overrides[key] = _convert_scalar(kind, query.get(key))
    return overrides


def _locate_form1_entry(directory: Path) -> Path:
    jsonl = sorted(Path(directory).rglob("*.jsonl"))
    if len(jsonl) == 1:
        return jsonl[0]
    if len(jsonl) > 1:
        try:
            # [11.3-F25] 与形态一文档 §3.5 对齐：多 JSONL 时优先认 manifest.json 的
            # entrypoint。原实现没传清单，#24 的正式输入（候选池 + 待回写报告两本
            # JSONL）一旦打进同一个包就永远判「无法识别输入入口」。
            manifest = transport.load_manifest(directory)
            entry, _relative = transport.resolve_entrypoint(directory, manifest)
            return entry
        except transport.TransportError as exc:
            raise ValueError("无法识别输入入口：%s" % exc) from exc
    candidates = sorted(Path(directory).rglob("*.json"))
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError("素材包内没有可识别的 JSON/JSONL 入口")


async def _store_form1_input(uid: str, upload, payload: bytes) -> Path:
    root = output_root()
    with io_utils.TaskLock(root, uid):
        task = io_utils.task_dir(root, uid)
        staging = task / ".staging_form1"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        content = staging / "content"
        content.mkdir(parents=True, exist_ok=True)
        try:
            if payload[:4] == ZIP_MAGIC or (upload.filename or "").lower().endswith(".zip"):
                try:
                    io_utils.safe_extract_zip(io.BytesIO(payload), content)
                except ValueError as exc:
                    raise ValueError("压缩包不合法：%s" % exc) from exc
            else:
                name = io_utils.normalize_zip_member_name(
                    upload.filename or "candidate_pool.jsonl")
                (content / name).write_bytes(payload)
            entry = _locate_form1_entry(content)
            relative = entry.relative_to(content)
            input_dir = task / INPUT_DIR_NAME
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


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


_CONCLUSION_MAP = {
    sc.DECISION_PASSED: "pass",
    sc.DECISION_REJECTED: "fail",
    sc.DECISION_UNCERTAIN: "uncertain",
    sc.DECISION_PENDING: "pending",
    sc.DECISION_NOT_SAMPLED: "not_sampled",
    sc.DECISION_SKIPPED: "upstream_isolated",
}


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一响应：处理型 int status（1 回写成功/0 处理失败）+ 抽检摘要。"""
    run_dir = Path(result["runDir"])
    report = _read_json(run_dir / sc.REPORT_NAME)
    reviewed = []
    for row in _read_jsonl(run_dir / sc.SAMPLED_NAME):
        reviewed.append({
            "sample_id": row.get("sample_id"),
            "conclusion": _CONCLUSION_MAP.get(row.get("decision"), row.get("decision")),
            "decision": row.get("decision"),
            "stratum": row.get("stratum"),
        })
    return {
        "status": 1,
        "uniqueId": uid,
        "sampling_report": sc.CONCLUSION_NAME,
        "reviewed_samples": reviewed,
        "quality_report_status": report.get("status"),
        "process_status": result.get("status"),
        "sampled": int((result.get("statistics") or {}).get("sampled", 0)),
    }


async def _predict_single_file(request: Request):
    """[v4.2] 形态一：multipart/form-data 单文件流（§4.24）。"""
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
    if len(payload) > MAX_UPLOAD_BYTES:
        return JSONResponse({"status": 0, "msg": "上传内容超过限额 %d 字节" % MAX_UPLOAD_BYTES})
    try:
        native = _ensure_native_input(await _store_form1_input(uid, upload, payload))
        overrides = validate_overrides(_form_overrides(form, request.query_params) or None)
        result = inference.run(input_file=str(native), unique_id=uid,
                               output_root=output_root(), overrides=overrides,
                               allow_external_input=False)
    except (ValueError, ConfigError, inference.InputError, inference.DependencyError,
            io_utils.TaskBusyError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    detail = _single_file_response(uid, result)
    if zipfile.is_zipfile(io.BytesIO(payload)):
        try:
            detail['outputFile'] = form1_package.build_reference(Path(result['runDir']), result.get('entrypoint'), media_policy='reference')
        except Exception as exc:
            detail.update(status=0, package_error=str(exc)[:200])
    return JSONResponse(detail)


@app.post("/predict")
async def predict(request: Request) -> Dict[str, Any]:
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
        payload = await request.json()
        parsed = PredictRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 参数错误统一 422 信封
        return JSONResponse(status_code=422,
                            content=envelope(1, "请求参数不合法：%s" % exc))
    try:
        overrides = validate_overrides(parsed.config)
        result = inference.run(input_file=parsed.inputFile, unique_id=parsed.uniqueId,
                              output_root=output_root(), overrides=overrides)
        # [v4.2] 阶段归档/血缘输出已停用：不再调用 mty_compliance.prepare_stage_result
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, parsed.isLastOperator)
    except (ConfigError, inference.InputError, io_utils.TaskBusyError) as exc:
        return envelope(1, str(exc), {})
    except Exception as exc:  # noqa: BLE001 - 接口必须返回统一信封
        return envelope(1, "%s: %s" % (type(exc).__name__, str(exc)[:400]), {})

    # [v4.2] 统一返回 outputFile（crossmodal-files-v1），不再区分阶段末/中间
    try:
        file_object = inference.stage_file_object(result)
    except transport.OutputTooLargeError as exc:
        return envelope(1, str(exc), {})
    return envelope(0, "抽检并回写质量报告完成", {
        "operator": sc.OPERATOR_NAME,
        "status": result["status"],
        "reportStatus": result.get("reportStatus"),
        "outputFile": file_object,
        "result": {
            "uniqueId": result.get("uniqueId"),
            "runId": result.get("runId"),
            "status": result["status"],
            "reportStatus": result.get("reportStatus"),
            "statistics": result.get("statistics"),
        },
    })


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
        "operator": sc.OPERATOR_NAME,
        "operatorLabel": sc.OPERATOR_LABEL,
        "operatorNumber": sc.FORMAL_OPERATOR_NUMBER,
        "operatorVersion": sc.OPERATOR_VERSION,
        "stageId": sc.STAGE_ID,
        "interfaces": ["/uploadFile", "/predict", "/removeFile"],
        "downloadFile": False,
        "config": str(inference.DEFAULT_CONFIG_PATH.name),
    }
