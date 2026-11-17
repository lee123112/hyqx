"""算子 10 HTTP 服务：只对外暴露 uploadFile / predict / removeFile 三个接口。

契约（DEVELOPMENT_CONTRACT §2）与 demo 服务工程约定一致：
- ``POST /uploadFile?uniqueId=xxx``：上传素材包（**跨模态阶段沿用 zip 容器**）到任务目录
  ``OUTPUT_DIR/<uniqueId>/input``，两阶段原子提交；必填 ``uniqueId`` 与表单字段 ``file``；
- ``POST /predict`` [v4.2] 双协议：
  - 形态二（application/json）：``uniqueId`` + 布尔 ``isLastOperator``，可选 ``inputFile`` 与
    ``config``；统一返回 ``outputFile``（crossmodal-files-v1）+ ``result``，去阶段血缘文件；
  - 形态一（multipart/form-data）：``?detection=check_input_admission``，``file`` 必传，
    判定型 int ``status``（1 准入/0 不准入）+ admission / eligible_object_id / reason_code；
- ``POST /removeFile?uniqueId=xxx``：删除整个任务，重复调用成功。
- **不提供 ``/downloadFile``**：接口只在 response 的 data 里带路径，不提供按路径下载。

响应壳固定 ``{"code": 0|1, "msg": "...", "data": {...}}``；入参形状非法一律 HTTP 422，
``inputFile`` 越界或目录穿越等路径问题 HTTP 400，业务执行失败回 200 + ``code=1``。

本文件由算子 10 在本目录内自带，不跨目录 import 兄弟算子。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import inference
from utils import form1_package
from utils import io as io_utils
from utils.admission import DependencyError
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.schemas import ConfigError, PredictRequest

OPERATOR = inference.OPERATOR
MAX_UPLOAD_BYTES = inference.MAX_UPLOAD_BYTES
MAX_FILES = inference.MAX_FILES
MAX_UNPACKED_BYTES = inference.MAX_UNPACKED_BYTES
MAX_RESPONSE_BYTES = inference.MAX_RESPONSE_BYTES

# 标题保持英文、版本取自 OPERATOR（demo 服务约定；不加大字号或页脚）
app = FastAPI(title=OPERATOR, version=inference.FORMAL_OPERATOR_NUMBER and "v1.0.0")


# ---------------------------------------------------------------- 基础工具
def output_root() -> Path:
    """输出根：只由 ``OUTPUT_DIR`` 环境变量决定，请求不可指定。

    每次请求读取环境变量，保证测试用 monkeypatch 设置的临时目录即时生效；
    未设置时落到本算子目录 ``output/``。
    """
    env_root = os.environ.get("OUTPUT_DIR")
    return io_utils.resolve_root(env_root if env_root else Path(__file__).resolve().parent / "output")


def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": data if data is not None else {}}


def fail(msg: str, code: int = 1) -> Dict[str, Any]:
    return envelope(code, msg, {})


def fail_json(msg: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=fail(msg))


def require_unique_id(unique_id: str) -> str:
    """校验 uniqueId；非法直接按 422 拒绝（不创建任何目录）。"""
    try:
        return io_utils.validate_unique_id(unique_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------- 异常处理
@app.exception_handler(RequestValidationError)
async def _validation_handler(_request, exc: RequestValidationError) -> JSONResponse:
    """请求形状（缺字段/类型错/布尔不严格）一律 422 信封。"""
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        details.append("%s: %s" % (location or "<body>", error.get("msg", "非法")))
    return fail_json("请求参数非法——%s" % "; ".join(details), 422)


@app.exception_handler(StarletteHTTPException)
async def _http_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
    """``/downloadFile`` 等未注册路径回 404 信封；方法不允许回 405 信封。"""
    return fail_json("HTTP %s：%s" % (exc.status_code, exc.detail), int(exc.status_code))


# ---------------------------------------------------------------- 上传
@app.post("/uploadFile")
async def upload_file(unique_id: str = Query(..., alias="uniqueId"),
                      file: UploadFile = File(...)) -> Dict[str, Any]:
    """把上传的素材包安全解压到 ``OUTPUT_DIR/<uniqueId>/input``（两阶段原子提交）。"""
    value = require_unique_id(unique_id)
    root = output_root()
    task = io_utils.task_dir(root, value)
    staging = task / ("upload-%s.tmp" % uuid.uuid4().hex)

    fd, tmp_name = tempfile.mkstemp(dir=str(task), prefix="upload-", suffix=".zip")
    os.close(fd)
    tmp_archive = Path(tmp_name)
    returned: Dict[str, Any] = {}
    try:
        writer = None
        try:
            writer = open(tmp_archive, "wb")
        except OSError as exc:
            return fail("无法创建上传暂存文件：%s" % type(exc).__name__)
        written = 0
        try:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    writer.close()
                    return fail("上传文件超过 %d 字节上限" % MAX_UPLOAD_BYTES)
                writer.write(chunk)
        finally:
            if writer is not None and not writer.closed:
                writer.close()

        content_root = staging / "content"
        try:
            io_utils.safe_extract_zip(tmp_archive, content_root,
                                      max_files=MAX_FILES,
                                      max_unpacked_bytes=MAX_UNPACKED_BYTES)
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            return fail("素材包非法：%s" % (exc or type(exc).__name__))
        try:
            _entry, relative = inference_load_entrypoint(content_root)
        except inference.InputError as exc:
            return fail(str(exc))

        input_dir = task / "input"
        if input_dir.exists():
            shutil.rmtree(input_dir)
        io_utils.commit_directory(content_root, input_dir)
        returned = {"entrypoint": relative, "files": len(_list_files(input_dir))}
    finally:
        io_utils.cleanup_files(tmp_archive)
        shutil.rmtree(staging, ignore_errors=True)

    return envelope(0, "上传成功", returned)


def _list_files(directory: Path) -> list:
    files = []
    for root, _dirs, names in os.walk(directory):
        for name in names:
            files.append(str(Path(root) / name))
    return files


def inference_load_entrypoint(content_root: Path):
    """通过传输适配层定位入口，保持与阶段内 ``inputFile`` 解析同一口径。"""
    from utils.transport import TransportError, resolve_entrypoint

    try:
        return resolve_entrypoint(content_root)
    except TransportError as exc:
        raise inference.InputError("素材包入口非法：%s" % exc) from exc


# ---------------------------------------------------------------- 预测
@app.post("/predict")
async def predict(request: Request) -> Any:
    """[v4.2] 双协议入口：multipart/form-data → 形态一；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    # [D3] 形态二入口补 detection/operatorId 冲突校验（§2.7 公共约定）
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return fail_json("detection 与 operatorId 同时出现时取值必须一致", 422)
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return fail_json("请求体必须是 JSON 对象", 400)
    try:
        payload = PredictRequest.model_validate(raw)
    except ValidationError as exc:
        details = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ())
                                if part != "body")
            details.append("%s: %s" % (location or "<body>", error.get("msg", "非法")))
        return fail_json("请求参数非法——%s" % "; ".join(details), 422)
    return _predict_stage(payload)


def _predict_stage(payload: PredictRequest) -> Any:
    """形态二：阶段文件传递协议。[v4.2] 响应去阶段血缘文件，统一返回 outputFile。"""
    value = require_unique_id(payload.uniqueId)
    config_path = str(inference.default_config_path())
    overrides = payload.config or None

    input_file: Optional[str] = None
    if payload.inputFile:
        try:
            task = io_utils.task_dir(output_root(), value)
            input_file = str(io_utils.resolve_in_task(task, payload.inputFile))
        except ValueError as exc:
            return fail_json("inputFile 非法：%s" % exc, 400)

    try:
        result = inference.run(input_file=input_file, unique_id=value,
                               config_path=config_path, output_root=str(output_root()),
                               overrides=overrides)
        # [v4.2] mty_compliance 已停用：注释 prepare_stage_result 调用（代码保留注释存档）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, payload.isLastOperator)
        file_object = inference.stage_file_object(result, MAX_RESPONSE_BYTES)
    except ConfigError as exc:
        return fail(str(exc))
    except io_utils.TaskBusyError as exc:
        return fail("任务正在被另一个写操作占用：%s" % exc)
    except DependencyError as exc:
        return fail_json("外部依赖不可用：%s" % exc, 400)
    except inference.InputError as exc:
        return fail_json(str(exc), 400)
    except ValueError as exc:
        return fail_json(str(exc), 422)
    except OSError as exc:
        return fail("处理失败：%s" % type(exc).__name__)
    summary = _business_fields(result)
    return envelope(0, result["message"], {
        "operator": result.get("operator") or inference.OPERATOR,
        "status": result["status"],
        "outputFile": file_object,
        "result": {
            "uniqueId": result.get("uniqueId"),
            "runId": result.get("runId"),
            "status": result["status"],
            "statistics": result.get("statistics", {}),
            "admission": summary["admission"],
            "eligible_object_id": summary["eligible_object_id"],
            "reason_code": summary["reason_code"],
        },
    })


# ---------------------------------------------------------------- 形态一：单文件流（[v4.2] 新增）
#: detection/operatorId 别名空间（大小写不敏感）：M10/10/英文ID/目录名
DETECTION_ALIASES = {"m10", "10", "check_input_admission",
                     "input_admission", "cma_check_input_admission"}

#: 形态一白名单标量（未知字段忽略）；与 config 白名单一致
_FORM1_SCALARS = {
    "min_modalities": "int", "min_description_chars": "int",
    "max_description_chars": "int", "max_return_count": "int",
    "require_source_and_batch": "bool", "file_check_mode": "str",
    "allow_degraded": "bool",
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


def _eligible_object_id(record: Dict[str, Any]) -> str:
    """准入通过后的不可变快照 ID：按记录内容确定性派生（同内容同 ID，跨运行稳定）。"""
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "eligible_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _business_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态一业务字段：admission + eligible_object_id + reason_code（判定型）。"""
    payload: Dict[str, Any] = {"admission": "rejected", "eligible_object_id": None,
                               "reason_code": None}
    for record in _iter_jsonl(result.get("outputFile")):
        payload["admission"] = "accepted"
        payload["eligible_object_id"] = _eligible_object_id(record)
        payload["reason_code"] = "all_required_checks_passed"
        return payload
    run_dir = result.get("runDir")
    if run_dir:
        for name in (inference.RETURN_NAME, inference.ORIGIN_NAME, inference.SAMPLE_NAME):
            for entry in _iter_jsonl(Path(str(run_dir)) / name):
                payload["reason_code"] = entry.get("reason")
                return payload
    stats = result.get("statistics") or {}
    if stats.get("empty_input"):
        payload["reason_code"] = "empty_input"
    else:
        payload["reason_code"] = "no_record"
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
    """形态一业务字段（V4.2 §4.10）：判定型 1=准入/0=不准入 + 三项业务字段。"""
    payload: Dict[str, Any] = {"uniqueId": uid, "admission": "rejected",
                               "eligible_object_id": None, "reason_code": None}
    payload.update(_business_fields(result))
    payload["status"] = 1 if payload["admission"] == "accepted" else 0
    payload["process_status"] = "success" if payload["status"] == 1 else "rejected"
    return payload


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（multipart/form-data），判定型 status（1 准入/0 不准入）。"""
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
    is_package = zipfile.is_zipfile(io.BytesIO(content))
    root = output_root()
    task_dir = root / uid
    try:
        with io_utils.TaskLock(root, uid):
            task_dir.mkdir(parents=True, exist_ok=True)
            _reset_task(task_dir)
            input_dir = task_dir / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            if is_package:
                # [v4.3] 压缩包模式：按形态二布局解压到任务 input，供阶段推理直接复用
                archive = task_dir / "bundle.zip"
                archive.write_bytes(content)
                io_utils.safe_extract_zip(archive, input_dir,
                                          max_files=MAX_FILES,
                                          max_unpacked_bytes=MAX_UNPACKED_BYTES)
                archive.unlink()
                entry_path = form1_package.resolve_entrypoint(input_dir)
            else:
                entry_path = input_dir / Path(filename.replace("\\", "/")).name
                entry_path.write_bytes(content)
            native = _ensure_native_input(entry_path)
    except (ValueError, inference.InputError, io_utils.TaskBusyError,
            OSError, zipfile.BadZipFile) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    try:
        result = inference.run(input_file=str(native), unique_id=uid,
                               config_path=str(inference.default_config_path()),
                               output_root=str(root), overrides=overrides or None)
    except (ConfigError, inference.InputError, DependencyError, ValueError, OSError) as exc:
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
                 int(payload.get("status", 0)), elapsed_ms)
    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 阶段统计 + 判定业务字段
        try:
            run_root_raw = result.get("runDir") or result.get("run_dir")
            if not run_root_raw:
                raise ValueError("阶段推理未返回运行目录")
            entry_hint = result.get("entrypoint") \
                or Path(result.get("outputFile") or "").name
            reference = form1_package.build_reference(Path(run_root_raw), entry_hint)
        except Exception as exc:  # noqa: BLE001 - 打包失败降级为单文件响应
            payload["package_error"] = str(exc)[:200]
            payload["status"] = 0
            return JSONResponse(payload)
        business = {key: value for key, value in payload.items() if key != "status"}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator=OPERATOR, unique_id=uid,
            business=business))
    return JSONResponse(payload)


# ---------------------------------------------------------------- 清理
@app.post("/removeFile")
async def remove_file(unique_id: str = Query(..., alias="uniqueId")) -> Dict[str, Any]:
    """删除整个任务目录；若不存在也返回成功（幂等）。"""
    value = require_unique_id(unique_id)
    removed = io_utils.remove_task(output_root(), value)
    return envelope(0, "已删除任务" if removed else "任务不存在，已是清理状态", {})


# ---------------------------------------------------------------- 健康检查
@app.get("/health", include_in_schema=False)
def health() -> Dict[str, Any]:
    """[D1] 单算子存活探针：算子标识/状态（与已有算子 /health 风格一致）。"""
    return {
        "status": "ok",
        "operator": inference.OPERATOR,
        "operatorNumber": inference.FORMAL_OPERATOR_NUMBER,
        "interfaces": ["/uploadFile", "/predict", "/removeFile"],
        "downloadFile": False,
        "model": None,
    }


def main() -> int:
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8110")), log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
