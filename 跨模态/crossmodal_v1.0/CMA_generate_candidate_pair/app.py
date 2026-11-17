"""算子 #11 生成候选配对 HTTP 服务：严格三接口，统一 code/msg/data 信封。

正式接口：POST /uploadFile?uniqueId=、POST /predict、POST /removeFile?uniqueId=。
不注册 /downloadFile；阶段末输出用 crossmodal-files-v1 文件内容对象承载。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import inference  # noqa: E402
from utils import form1_package  # noqa: E402
from utils import io as op_io  # noqa: E402
from utils import transport  # noqa: E402
# [v4.2] 血缘输出已停用：public_node 不再使用，导入保留存档（注释）。
# from utils.lineage import public_node  # noqa: E402
from utils.pairing import ALLOWED_OVERRIDES  # noqa: E402
from utils.schemas import validate_predict_payload  # noqa: E402

OUTPUT_ROOT = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
MAX_FILES = int(os.getenv("OP11_MAX_FILES", "1000"))
MAX_UNPACKED_BYTES = int(os.getenv("OP11_MAX_UNPACKED_BYTES", str(64 * 1024 * 1024)))
PORT = int(os.getenv("OP11_PORT", "8111"))
TEXT_SUFFIXES = transport._TEXT_SUFFIXES

app = FastAPI(title="generate_candidate_pairs", version=inference.VERSION)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException):
    return envelope(1, str(exc.detail), {}, exc.status_code)


def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None,
             status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"code": code, "msg": msg, "data": data or {}})


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """存活探针：算子标识/状态（统一信封；按交付契约不进 OpenAPI 文档）。"""
    return envelope(0, "success", {"status": "ok", "operator": inference.OPERATOR,
                                   "stageId": inference.STAGE_ID})


def _entry(directory: Path) -> Path:
    """按 manifest 定位入口；没有 manifest 时要求唯一 JSONL，避免歧义。"""
    target, _relative = transport.resolve_entrypoint(directory)
    return target


def _safe_extract(archive: Path, destination: Path) -> None:
    """安全解压：拒绝越界成员、符号链接、重复/大小写碰撞，并限制文件数与解压总大小。"""
    destination = destination.resolve()
    with zipfile.ZipFile(str(archive)) as handle:
        seen = set()
        infos = handle.infolist()
        if len(infos) > MAX_FILES:
            raise ValueError("ZIP 成员文件数超限：%d > %d" % (len(infos), MAX_FILES))
        total_bytes = 0
        for info in infos:
            name = str(info.filename).replace("\\", "/")
            pure = Path(name)
            if not name or pure.is_absolute() or ".." in pure.parts:
                raise ValueError("ZIP 成员路径非法：%s" % info.filename)
            lowered = name.lower()
            if lowered in seen:
                raise ValueError("ZIP 存在重复或大小写碰撞成员：%s" % info.filename)
            seen.add(lowered)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("ZIP 不允许包含符号链接：%s" % info.filename)
            target = (destination / name).resolve()
            target.relative_to(destination)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total_bytes += info.file_size
            if total_bytes > MAX_UNPACKED_BYTES:
                raise ValueError("ZIP 解压总大小超限：> %d 字节" % MAX_UNPACKED_BYTES)
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as source, open(str(target), "wb") as sink:
                shutil.copyfileobj(source, sink)


async def _store_upload(unique_id: str, upload: UploadFile) -> str:
    task = op_io.task_dir(OUTPUT_ROOT, unique_id)
    incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=str(task)))
    try:
        archive = incoming / "upload.bin"
        with open(str(archive), "wb") as stream:
            shutil.copyfileobj(upload.file, stream)
        staging = incoming / "content"
        staging.mkdir()
        if zipfile.is_zipfile(str(archive)):
            _safe_extract(archive, staging)
        else:
            name = Path(upload.filename or "input.jsonl").name
            shutil.copy2(str(archive), str(staging / name))
        entry = _entry(staging)
        target = task / "input"
        if target.exists():
            shutil.rmtree(str(target))
        os.replace(str(staging), str(target))
        return str(entry.relative_to(staging)).replace("\\", "/")
    finally:
        shutil.rmtree(str(incoming), ignore_errors=True)


def _file_object(run_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    paths = [manifest["entrypoint"]] + [item["path"] for item in manifest["artifacts"]]
    paths.append("manifest.json")
    return transport.pack_file_object(run_dir, manifest["entrypoint"], paths)


# ------------------------------------------------------------------ [v4.2] 双协议

# [v4.2] detection/operatorId 别名空间：M11/11/英文ID/目录名（大小写不敏感）
_DETECTION_ALIASES = {"m11", "11", "generate_candidate_pair", "generate_candidate_pairs",
                      "cma_generate_candidate_pair"}


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
        raise ValueError("detection 与本算子不匹配：%s（本算子为 M11 generate_candidate_pair）"
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
    for key in sorted(ALLOWED_OVERRIDES):
        raw = form.get(key)
        if raw is not None:
            overrides[key] = _to_scalar(raw)
    return overrides


def _read_records(path: str) -> list:
    return [record for _line, record, error in op_io.read_jsonl(Path(path))
            if error is None and record is not None]


def _result_summary(task: Path, result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态二业务摘要：内部绝对路径转任务相对路径。"""
    run_dir = Path(result["runDir"])
    files = {}
    for key in ("outputFile", "unpairedFile", "rejectsFile"):
        raw = result.get(key)
        if raw:
            files[key] = Path(raw).relative_to(task).as_posix()
    return {
        "runId": result.get("runId"),
        "runDir": run_dir.relative_to(task).as_posix(),
        "files": files,
        "statistics": result.get("statistics", {}),
        "config": result.get("config"),
        "stageId": result.get("stageId"),
    }


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态一响应（处理型）：status=1 成功；业务字段 pairs / unpaired。"""
    return {
        "status": 1,
        "uniqueId": uid,
        "pairs": _read_records(result["outputFile"]),
        "unpaired": _read_records(result["unpairedFile"]),
    }


def _audit_single_file(uid: str, detection: Optional[str], upload: Any,
                       status: int, started: float) -> None:
    """[v4.2] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    try:
        task = op_io.task_dir(OUTPUT_ROOT, uid)
        record = {
            "detection": detection,
            "filename": getattr(upload, "filename", None),
            "status": status,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "event_time": datetime.now().isoformat(timespec="seconds"),
        }
        with open(str(task / "single_file_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 审计失败不影响主流程
        pass


async def _predict_single_file(request: Request) -> JSONResponse:
    """[v4.2] 形态一（单文件流）：multipart/form-data + file 字段。

    [v4.3] ``file`` 为 zip 压缩包时按形态二相同的阶段推理执行，返回容器内路径引用
    （``outputFile`` 为运行目录引用、``result`` 为阶段统计），并生成可直传下游的
    ``*_form1.zip``；为单个 JSON/JSONL 时保持原单文件行为。
    """
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
        uid = op_io.validate_unique_id(str(raw_uid))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    try:
        content = await upload.read()
    except OSError as exc:
        return JSONResponse({"status": 0, "msg": "读取上传内容失败：%s" % exc})
    if not content:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})
    is_package = zipfile.is_zipfile(io.BytesIO(content))
    try:
        with op_io.task_lock(OUTPUT_ROOT, uid):
            if is_package:
                # [v4.3] 压缩包模式：按形态二布局解压到任务 input，供阶段推理与媒体携带复用
                task = op_io.task_dir(OUTPUT_ROOT, uid)
                input_dir = task / "input"
                if input_dir.exists():
                    shutil.rmtree(str(input_dir))
                input_dir.mkdir(parents=True, exist_ok=True)
                archive = task / "form1_upload.zip"
                archive.write_bytes(content)
                _safe_extract(archive, input_dir)
                archive.unlink()
                try:
                    input_path = form1_package.resolve_entrypoint(input_dir)
                except ValueError as exc:
                    raise ValueError("素材包入口非法：%s" % exc)
            else:
                await upload.seek(0)
                try:
                    await _store_upload(uid, upload)
                except ValueError as exc:
                    # 上传媒体/非 JSON 素材：本算子只收合格对象集合 JSON/JSONL/ZIP
                    if "入口" in str(exc) or "manifest" in str(exc):
                        _audit_single_file(uid, detection, upload, 0, started)
                        return JSONResponse({"status": 0, "msg": "该算子需要 JSON/JSONL 输入"})
                    raise
                task = op_io.task_dir(OUTPUT_ROOT, uid)
                input_path = _entry(task / "input")
            result = inference.run(str(input_path), uid, output_root=str(OUTPUT_ROOT),
                                   overrides=_form_overrides(form) or None)
        if result.get("status") == "failed":
            _audit_single_file(uid, detection, upload, 0, started)
            return JSONResponse({"status": 0, "msg": result.get("reason", "处理失败")})
        payload = _single_file_response(uid, result)
        _audit_single_file(uid, detection, upload, 1, started)
        if is_package:
            # [v4.3] 压缩包模式：以运行目录为 run_root 返回容器内路径引用 + 阶段统计
            try:
                run_root_raw = result.get("runDir") or result.get("run_dir")
                entry_hint = result.get("entrypoint")
                if run_root_raw:
                    run_root = Path(run_root_raw)
                    entry_hint = entry_hint \
                        or Path(result.get("outputFile") or "").name
                else:
                    # 兼容只写任务根的 B 类实现：任务根作为 run_root（helper 自动排除 input/ 等）
                    run_root = task
                    entry_hint = entry_hint \
                        or Path(result.get("outputFile") or "").name
                reference = form1_package.build_reference(run_root, entry_hint)
            except Exception as exc:  # noqa: BLE001 - 打包失败降级为单文件响应
                payload["package_error"] = str(exc)[:200]
                payload["status"] = 0
                return JSONResponse(payload)
            business = {key: value for key, value in payload.items() if key != "status"}
            return JSONResponse(form1_package.package_payload(
                result, reference, operator=inference.OPERATOR, unique_id=uid,
                business=business))
        return JSONResponse(payload)
    except op_io.TaskBusy as exc:
        return JSONResponse({"status": 0, "msg": str(exc)}, status_code=409)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": 0, "msg": str(exc)})


@app.post("/uploadFile")
async def upload_file(uniqueId: Optional[str] = None,
                      file: Optional[UploadFile] = File(None)):
    try:
        uid = op_io.validate_unique_id(uniqueId)
        if file is None:
            raise ValueError("缺少上传文件 file")
        with op_io.task_lock(OUTPUT_ROOT, uid):
            entry = await _store_upload(uid, file)
        return envelope(0, "success", {"entrypoint": entry})
    except op_io.TaskBusy as exc:
        return envelope(1, str(exc), {}, 409)
    except Exception as exc:  # noqa: BLE001 - 统一转成失败信封
        return envelope(1, str(exc), {}, 422)


@app.post("/predict")
async def predict(request: Request):
    # [v4.2] 双协议：multipart/form-data 走形态一（单文件流），其余为形态二（阶段文件传递）
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
    except ValueError as exc:
        return envelope(1, str(exc), {}, 400)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return envelope(1, "请求体必须是 JSON 对象", {}, 400)
    try:
        model = validate_predict_payload(payload)
        uid = model["uniqueId"]
        task = op_io.task_dir(OUTPUT_ROOT, uid)
    except ValueError as exc:
        return envelope(1, str(exc), {}, 400)

    try:
        with op_io.task_lock(OUTPUT_ROOT, uid):
            if model.get("inputFile"):
                input_path = op_io.resolve_in_task(task, model["inputFile"])
            else:
                input_path = _entry(task / "input")
            if not input_path.is_file():
                raise ValueError("输入文件不存在：%s" % model.get("inputFile"))
            result = inference.run(str(input_path), uid, output_root=str(OUTPUT_ROOT),
                                   overrides=model.get("config"))
            # [v4.2] mty_compliance（血缘/阶段归档）已停用：prepare_stage_result 调用注释，代码保留存档。
            # from utils import mty_compliance as _mty_compliance
            # _mty_compliance.prepare_stage_result(result, model["isLastOperator"])
            if result.get("status") == "failed":
                return envelope(1, result.get("reason", "处理失败"),
                                {"status": "failed"})
            # [v4.2] 形态二统一响应：不再区分阶段末/非阶段末，去 lineageFile、始终返回 outputFile；
            # isLastOperator 仅保留严格校验（契约兼容），不再改变响应形态。
            run_dir = Path(result["runDir"])
            manifest = transport.read_manifest(run_dir)
            if manifest is None:
                raise ValueError("缺少 manifest.json，无法生成阶段文件对象")
            files = _file_object(run_dir, manifest)
            return envelope(0, "success", {
                "operator": inference.OPERATOR,
                "status": "success",
                "outputFile": files,
                "result": _result_summary(task, result),
            })
    except op_io.TaskBusy as exc:
        return envelope(1, str(exc), {}, 409)
    except Exception as exc:  # noqa: BLE001
        return envelope(1, str(exc), {}, 400)


@app.post("/removeFile")
async def remove_file(uniqueId: Optional[str] = None):
    try:
        uid = op_io.validate_unique_id(uniqueId)
        task = op_io.task_dir(OUTPUT_ROOT, uid, create=False)
        with op_io.task_lock(OUTPUT_ROOT, uid):
            if task.exists():
                shutil.rmtree(str(task))
                return envelope(0, "success", {"deleted": True})
        return envelope(1, "任务不存在或已删除", {"deleted": False})
    except op_io.TaskBusy as exc:
        return envelope(1, str(exc), {}, 409)
    except Exception as exc:  # noqa: BLE001
        return envelope(1, str(exc), {}, 400)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
