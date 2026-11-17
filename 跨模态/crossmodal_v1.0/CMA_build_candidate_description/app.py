"""算子 #09 准备候选描述 HTTP 服务，严格遵循跨模态三接口契约（V4.2 双协议）。

- ``POST /uploadFile?uniqueId=`` 上传任务包（zip）或单文件；
- ``POST /predict``               [v4.2] 双协议：
  - 形态二（application/json）：``isLastOperator`` 保留严格布尔校验，但不再改变
    响应形态；统一返回 outputFile（crossmodal-files-v1）+ result，去 lineageFile；
  - 形态一（multipart/form-data 单文件流）：``?detection=build_candidate_description``，
    返回 JSON 首字段 int ``status``（处理型：1 成功/0 失败）+ 描述业务字段；
- ``POST /removeFile?uniqueId=`` 清理任务（幂等）。
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
from utils.io import resolve_in_task, task_dir  # noqa: E402
from utils.transport import pack_file_object, resolve_entrypoint  # noqa: E402

OUTPUT_ROOT = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
MAX_FILES = 1000
MAX_UNPACKED_BYTES = 67108864

app = FastAPI(title=inference.OPERATOR, version=inference.VERSION)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException):
    return envelope(1, str(exc.detail), {}, exc.status_code)


def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None,
             status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "msg": msg,
                                                     "data": data or {}})


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """存活探针：算子标识/状态（统一信封；按交付契约不进 OpenAPI 文档）。"""
    return envelope(0, "success", {"status": "ok", "operator": inference.OPERATOR,
                                   "stageId": inference.STAGE_ID})


def _uid(value: Any) -> str:
    """只校验 uniqueId 合法性，不创建目录（删除接口需要真正的幂等语义）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("uniqueId 不能为空")
    value = value.strip()
    task_dir(OUTPUT_ROOT, value, create=False)
    return value


def _task_rel(task: Path, path: Optional[Path]) -> Optional[str]:
    """把运行产物路径转成任务目录内相对路径（posix）；不在任务内返回 None。"""
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(Path(task).resolve()).as_posix()
    except ValueError:
        return None


def _safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise ValueError("zip 成员数超过限制")
        total = sum(info.file_size for info in infos)
        if total > MAX_UNPACKED_BYTES:
            raise ValueError("zip 解压总大小超过限制")
        seen = set()
        for info in infos:
            name = info.filename.replace("\\", "/")
            p = Path(name)
            if not name or p.is_absolute() or ".." in p.parts:
                raise ValueError("zip 成员路径非法")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("zip 不允许符号链接")
            if name.lower() in seen:
                raise ValueError("zip 存在重复成员")
            seen.add(name.lower())
            target = (destination / name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ValueError("zip 成员越出解压目录") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


async def _upload(unique_id: str, upload: UploadFile) -> Path:
    task = task_dir(OUTPUT_ROOT, unique_id)
    incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=str(task)))
    try:
        archive = incoming / "upload.bin"
        with open(archive, "wb") as stream:
            shutil.copyfileobj(upload.file, stream)
        staging = incoming / "content"
        staging.mkdir()
        if zipfile.is_zipfile(archive):
            _safe_extract(archive, staging)
        else:
            name = Path(upload.filename or "input.jsonl").name
            shutil.copy2(archive, staging / name)
        entry, _rel = resolve_entrypoint(staging)
        target = task / "input"
        if target.exists():
            shutil.rmtree(target)
        os.replace(str(staging), str(target))
        return target / entry.relative_to(staging)
    finally:
        shutil.rmtree(incoming, ignore_errors=True)


def _parse_predict(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    allowed = {"uniqueId", "isLastOperator", "inputFile", "config"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("未知字段：%s" % sorted(unknown)[0])
    if "isLastOperator" not in payload or not isinstance(payload["isLastOperator"], bool):
        raise ValueError("isLastOperator 必须是布尔值")
    if "config" in payload and not isinstance(payload["config"], dict):
        raise ValueError("config 必须是对象")
    value = dict(payload)
    value["uniqueId"] = _uid(value.get("uniqueId"))
    if value.get("inputFile"):
        value["inputFile"] = str(resolve_in_task(
            task_dir(OUTPUT_ROOT, value["uniqueId"], create=False), value["inputFile"]))
    return value


def _collect_media(paths) -> set:
    """Collect native business media without treating diagnostics as live files."""
    media = set()
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            media.update(form1_package.active_media_refs(json.loads(line)))
    return media


def _pack(task: Path, result: Dict[str, Any]) -> tuple:
    """把本次运行的业务产物整理到任务内固定的 run001 目录，并生成 manifest。"""
    output = Path(result["outputFile"])
    rewrite = Path(result["rewriteFile"])
    rejects = Path(result["rejectsFile"])
    run_dir = task / inference.OPERATOR / "run001"
    input_dir = task / "input"
    required_media = _collect_media([output])
    media_paths = required_media | _collect_media([rewrite, rejects])
    missing_optional = []
    sources = {}
    for relative in sorted(media_paths):
        source = (input_dir / relative).resolve()
        try:
            source.relative_to(input_dir.resolve())
        except ValueError:
            raise ValueError("媒体路径越出当前输入包：%s" % relative)
        if not source.is_file():
            if relative in required_media:
                raise ValueError("当前输入包缺少活动媒体：%s" % relative)
            missing_optional.append({'path': relative, 'role': 'review',
                                     'reason': 'media_missing', 'required': False})
            continue
        sources[relative] = source

    manifest = {
        "format": "crossmodal-manifest-v1",
        "entrypoint": output.name,
        "artifacts": [
            {"path": output.name, "role": "main"},
            {"path": rewrite.name, "role": "review"},
            {"path": rejects.name, "role": "sidecar"},
        ],
    }
    if missing_optional:
        manifest['missing_optional_media'] = missing_optional
    for relative in sorted(sources):
        manifest["artifacts"].append({"path": relative, "role": "media"})
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pack-", dir=str(run_dir.parent)))
    try:
        for src in (output, rewrite, rejects):
            shutil.copy2(src, staging / src.name)
        for relative, source in sources.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        if run_dir.exists():
            shutil.rmtree(run_dir)
        os.replace(str(staging), str(run_dir))
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    inference.publish_stage_output(run_dir)
    return run_dir, manifest


def _file_object(run_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    paths = [manifest["entrypoint"]] + [a["path"] for a in manifest["artifacts"]] \
        + ["manifest.json"]
    return pack_file_object(run_dir, manifest["entrypoint"], paths)


@app.post("/uploadFile")
async def upload_file(uniqueId: Optional[str] = None, file: Optional[UploadFile] = File(None)):
    try:
        uid = _uid(uniqueId)
        if file is None:
            raise ValueError("缺少上传文件 file")
        entry = await _upload(uid, file)
        relative = entry.relative_to(task_dir(OUTPUT_ROOT, uid)).as_posix()
        return envelope(0, "success", {"entrypoint": relative})
    except Exception as exc:
        return envelope(1, str(exc), {}, 422)


# --------------------------------------------------------------------------- #
# /predict：[v4.2] 双协议
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M09/09/9/英文ID/目录名
DETECTION_ALIASES = {"m09", "09", "9", "build_candidate_description",
                     "cma_build_candidate_description", "candidate_description"}

#: 形态一白名单标量（未知字段忽略）；min_chars/max_chars 为 V4.2 §4.9 的接口二命名
_FORM1_SCALARS = {
    "min_length": "int", "max_length": "int",
    "min_chars": "int", "max_chars": "int",
    "forbid_filename": "bool", "forbid_tags": "bool",
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


def _form_overrides(form: Any) -> Optional[Dict[str, Any]]:
    """形态一宽松参数：只取白名单标量并归一到 config.description 结构。"""
    scalars: Dict[str, Any] = {}
    for key, kind in _FORM1_SCALARS.items():
        raw = form.get(key)
        if raw is None or hasattr(raw, "filename"):
            continue
        try:
            scalars[key] = _convert_scalar(str(raw), kind)
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
    desc: Dict[str, Any] = {}
    if "min_length" in scalars:
        desc["min_length"] = scalars["min_length"]
    elif "min_chars" in scalars:
        desc["min_length"] = scalars["min_chars"]
    if "max_length" in scalars:
        desc["max_length"] = scalars["max_length"]
    elif "max_chars" in scalars:
        desc["max_length"] = scalars["max_chars"]
    for key in ("forbid_filename", "forbid_tags"):
        if key in scalars:
            desc[key] = scalars[key]
    return {"description": desc} if desc else None


def _ensure_native_input(entry: Path) -> Path:
    """JSONL 原样使用；单个 JSON（对象或数组）转成同目录 JSONL；其他文件拒绝。"""
    suffix = entry.suffix.lower()
    if suffix == ".jsonl":
        return entry
    if suffix == ".json":
        try:
            payload = json.loads(entry.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("JSON 文件解析失败") from exc
        items = payload if isinstance(payload, list) else [payload]
        target = entry.with_name(entry.stem + "__native.jsonl")
        with open(str(target), "w", encoding="utf-8", newline="\n") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return target
    raise ValueError("该算子需要 JSON/JSONL 输入")


def _first_jsonl(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path or not Path(path).is_file():
        return None
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
                return item
    return None


def _single_file_response(uid: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """形态一业务字段（V4.2 §4.9）：description/description_source/description_length。

    [v4.2] 无有效描述是**工作流等待**而非失败：``status=1`` 且
    ``process_status=waiting_description``，绝不写成隔离或失败。
    """
    payload: Dict[str, Any] = {"status": 1, "process_status": "waiting_description",
                               "uniqueId": uid, "sample_id": None,
                               "description": None, "description_source": None,
                               "description_length": None}
    record = _first_jsonl(result.get("outputFile"))
    if record is not None:
        node = (record.get("processing") or {}).get("09") or {}
        payload.update({
            "sample_id": record.get("sample_id"),
            "description": record.get("description"),
            "description_source": record.get("description_source"),
            "description_length": record.get("description_length"),
            "process_status": node.get("process_status") or "success",
        })
        return payload

    rewrite = _first_jsonl(result.get("rewriteFile"))
    if rewrite is not None:
        payload["sample_id"] = rewrite.get("sample_id")
        payload["reason_code"] = rewrite.get("reason")
        return payload

    rejects = _first_jsonl(result.get("rejectsFile"))
    if rejects is not None:
        payload.update({"status": 0, "process_status": "failed",
                        "reason_code": rejects.get("reason")})
        return payload

    stats = result.get("statistics") or {}
    if int(stats.get("total_lines") or 0) == 0:
        payload["process_status"] = "not_applicable"
    return payload


@app.post("/predict")
async def predict(request: Request):
    """[v4.2] 双协议入口：multipart/form-data → 形态一；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
        model = _parse_predict(await request.json())
        uid = model["uniqueId"]
        task = task_dir(OUTPUT_ROOT, uid)
        if model.get("inputFile"):
            input_path = Path(model["inputFile"])
        else:
            input_path, _rel = resolve_entrypoint(task / "input")
        if not input_path.is_file():
            raise ValueError("输入文件不存在")
        result = inference.run(str(input_path), uid, None, str(OUTPUT_ROOT),
                               model.get("config"))
        if result.get("status") == "failed":
            return envelope(1, result.get("reason", "处理失败"),
                            {"status": "failed", "statistics": result.get("statistics", {})})
        run_dir, manifest = _pack(task, result)
        # [v4.2] 所有调用统一返回 outputFile + result；isLastOperator 不再改变响应形态
        return envelope(0, "success", {
            "operator": inference.OPERATOR,
            "status": result.get("status"),
            "outputFile": _file_object(run_dir, manifest),
            "result": {"uniqueId": uid,
                       "outputFile": _task_rel(task, Path(result["outputFile"])),
                       "statistics": result.get("statistics", {})},
        })
    except Exception as exc:
        return envelope(1, str(exc), {}, 400)


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（处理型 status：1 成功/0 失败；无描述→1+waiting）。"""
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
        uid = _uid(str(uid_raw))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    content = await upload.read()
    if not content:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})

    filename = Path(str(upload.filename or "upload.json").replace("\\", "/")).name
    is_package = zipfile.is_zipfile(io.BytesIO(content))
    task = task_dir(OUTPUT_ROOT, uid)
    try:
        if is_package:
            # [v4.3] 压缩包模式：按形态二布局解压到任务 input，供阶段推理与 _pack 复用媒体
            input_dir = task / "input"
            if input_dir.exists():
                shutil.rmtree(input_dir, ignore_errors=True)
            input_dir.mkdir(parents=True, exist_ok=True)
            archive = task / "form1_upload.zip"
            archive.write_bytes(content)
            _safe_extract(archive, input_dir)
            archive.unlink()
            entry_path = form1_package.resolve_entrypoint(input_dir)
        else:
            staging = task / "form1_input"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            entry_path = staging / filename
            entry_path.write_bytes(content)
        native = _ensure_native_input(entry_path)
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    try:
        result = inference.run(str(native), uid, None, str(OUTPUT_ROOT),
                               _form_overrides(form))
    except (inference.ConfigError, ValueError, OSError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if result.get("status") == "failed":
        _form1_audit(uid, request, filename, len(content), 0, elapsed_ms)
        return JSONResponse({"status": 0, "msg": result.get("reason") or "处理失败"})
    payload = _single_file_response(uid, result)
    _form1_audit(uid, request, filename, len(content), int(payload.get("status", 1)),
                 elapsed_ms)
    if is_package:
        # [v4.3] 压缩包模式：复用形态二 _pack 打包，返回容器内路径引用 + 阶段统计
        try:
            run_dir, manifest = _pack(task, result)
            reference = form1_package.build_reference(
                run_dir, manifest.get("entrypoint"))
        except Exception as exc:  # noqa: BLE001 - ZIP failure must not claim success
            return JSONResponse({"status": 0, "msg": "输出打包失败：%s" % str(exc)[:200]})
        business = {key: value for key, value in payload.items() if key != "status"}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator=inference.OPERATOR, unique_id=uid,
            business=business))
    return JSONResponse(payload)


def _form1_audit(uid: str, request: Request, filename: str, size: int,
                 status: int, elapsed_ms: int) -> None:
    """[v4.2] 形态一最小调用审计；审计写盘失败不影响主流程。"""
    record = {"uniqueId": uid, "detection": _detection_value(request),
              "file": {"name": filename, "bytes": size},
              "status": status, "elapsed_ms": elapsed_ms,
              "time": datetime.now().isoformat(timespec="seconds")}
    try:
        task = task_dir(OUTPUT_ROOT, uid)
        with open(str(task / "form1_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _detection_value(request: Request) -> str:
    return (request.query_params.get("detection")
            or request.query_params.get("operatorId") or "")


@app.post("/removeFile")
async def remove_file(uniqueId: Optional[str] = None):
    """幂等删除：只清理由当前 uniqueId 创建的任务目录，重复调用同样成功。"""
    try:
        uid = _uid(uniqueId)
        task = task_dir(OUTPUT_ROOT, uid, create=False)
        if task.exists():
            shutil.rmtree(task)
            return envelope(0, "success", {"deleted": True})
        return envelope(0, "success", {"deleted": False})
    except Exception as exc:
        return envelope(1, str(exc), {}, 400)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8109)
