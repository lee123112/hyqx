"""算子 7：跨模态协同预处理 —— HTTP 服务（三接口 + [v4.2] 双协议）。

接口契约（DEVELOPMENT_CONTRACT §2 + [v4.2]）：
- ``POST /uploadFile?uniqueId=<任务id_文件id>``（multipart）上传完整素材包；
- ``POST /predict`` 双协议：multipart/form-data 为形态一单文件流（返回 status/segments），
  application/json 为形态二阶段文件传递（统一响应：去血缘字段、始终 outputFile）；
- ``POST /removeFile?uniqueId=<任务id_文件id>`` 清理任务目录，幂等。

**不实现** ``/downloadFile``（请求该路径返回 404）。响应统一信封
``{"code":0,"msg":"success","data":{...}}``，失败时 ``data`` 缺省为 ``{}``。
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

import inference
from utils import form1_package
from utils import io as io_utils
from utils.logger import close_logger, get_logger, log_action
from utils.schemas import ConfigError, PredictRequest
from utils.transport import OutputTooLargeError, TransportError, resolve_entrypoint

app = FastAPI(title="算子 7：跨模态协同预处理", version="v1.0.0")


# ---------------------------------------------------------------- 公共工具

def output_root() -> Path:
    """结果落盘根目录：只由部署环境变量决定，请求不可指定。"""
    return io_utils.resolve_root(os.environ.get("OUTPUT_DIR")
                                 or Path(__file__).resolve().parent / "output")


def envelope(code: int, msg: str, data: Optional[Any] = None) -> Dict[str, Any]:
    return {"code": int(code), "msg": msg, "data": data if data is not None else {}}


def fail(msg: str, code: int = 1, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return envelope(code, msg, data if data is not None else {})


# --------------------------------------------------------------------------- #
# [v4.2] detection/operatorId 别名空间（M08/08/8/英文ID/目录名，大小写不敏感）
# --------------------------------------------------------------------------- #

DETECTION_ALIASES = frozenset({
    "m08", "08", "8", "align_semantic_unit", "cma_align_semantic_unit",
    # [D2] 追加 map.json 登记表英文 operatorId（只追加，不重排既有条目）
    "multimodal_preprocessing",
})
OPERATOR_ENGLISH_ID = inference.OPERATOR_ENGLISH_ID


def _check_operator_alias(request: Request) -> Optional[JSONResponse]:
    """detection/operatorId 双查询参数兼容校验（[v4.2] §2.7 公共约定）。"""
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return JSONResponse(status_code=422, content=fail(
            "detection 与 operatorId 取值不一致：%s vs %s" % (detection, operator_id)))
    value = detection or operator_id
    if value is not None and value.strip().lower() not in DETECTION_ALIASES:
        return JSONResponse(status_code=422, content=fail(
            "detection/operatorId 与本算子（M08/align_semantic_unit）不符：%s" % value))
    return None


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request, exc: RequestValidationError) -> JSONResponse:
    """参数错误：HTTP 422，但同样给出统一失败信封。"""
    problems = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "<body>"
        problems.append("%s: %s" % (location, item.get("msg", "非法取值")))
    message = "请求参数校验失败——%s" % "; ".join(problems[:5])
    return JSONResponse(status_code=422, content=fail(message))


# ---------------------------------------------------------------- /uploadFile

def _sanitize_filename(name: Optional[str]) -> str:
    raw = Path(name or "upload.bin").name
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in raw).strip("._")
    return cleaned or "upload.bin"


def _save_upload(file: UploadFile, destination: Path,
                 max_bytes: int = inference.MAX_UPLOAD_BYTES) -> int:
    written = 0
    with open(destination, "wb") as sink:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise ValueError("上传内容超过限额 %d 字节" % max_bytes)
            sink.write(chunk)
    return written


@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(...), file: UploadFile = File(...)) -> Dict[str, Any]:
    """接收 zip 素材包或单个 JSONL；zip 校验后原子提交为 input/。"""
    try:
        value = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return fail(str(exc))

    root = output_root()
    try:
        with io_utils.TaskLock(root, value):
            task = io_utils.task_dir(root, value)
            staging_zone = Path(tempfile.mkdtemp(prefix="upload_", dir=str(task)))
            logger = get_logger("upload.%s" % value, task / "run.log")
            try:
                filename = _sanitize_filename(file.filename)
                staged_file = staging_zone / filename
                size = _save_upload(file, staged_file)
                log_action(logger, "upload", "uniqueId=%s name=%s bytes=%d"
                           % (value, filename, size))

                input_dir = task / "input"
                if filename.lower().endswith(".zip"):
                    extract_dir = staging_zone / "content"
                    try:
                        io_utils.safe_extract_zip(
                            staged_file, extract_dir,
                            max_files=inference.MAX_FILES,
                            max_unpacked_bytes=inference.MAX_UNPACKED_BYTES)
                        entry, relative = resolve_entrypoint(extract_dir)
                    except (ValueError, TransportError) as exc:
                        return fail("素材包校验失败：%s" % exc)
                    if input_dir.exists():
                        shutil.rmtree(input_dir)
                    io_utils.commit_directory(extract_dir, input_dir)
                    file_count = sum(1 for p in input_dir.rglob("*") if p.is_file())
                    return envelope(0, "success", {
                        "uniqueId": value, "entrypoint": relative,
                        "fileCount": file_count, "bytes": size,
                    })

                input_dir.mkdir(parents=True, exist_ok=True)
                os.replace(str(staging_zone / filename), str(input_dir / filename))
                return envelope(0, "success", {
                    "uniqueId": value, "entrypoint": filename,
                    "fileCount": 1, "bytes": size,
                })
            finally:
                close_logger(logger)
                shutil.rmtree(staging_zone, ignore_errors=True)
    except io_utils.TaskBusyError as exc:
        return fail("任务正被占用：%s" % exc)
    except (ValueError, OSError) as exc:
        return fail("上传失败：%s" % exc)


# ---------------------------------------------------------------- /predict

@app.post("/predict")
async def predict(request: Request) -> JSONResponse:
    """[v4.2] 双协议入口：multipart/form-data → 形态一单文件流；JSON → 形态二阶段文件传递。"""
    alias_error = _check_operator_alias(request)
    if alias_error is not None:
        return alias_error
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 请求体不是 JSON
        return JSONResponse(status_code=422, content=fail("请求体必须是 JSON 对象"))
    try:
        body = PredictRequest.model_validate(payload)
    except ValidationError as exc:
        problems = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ())) or "<body>"
            problems.append("%s: %s" % (location, item.get("msg", "非法取值")))
        return JSONResponse(status_code=422,
                            content=fail("请求参数校验失败——%s" % "; ".join(problems[:5])))
    try:
        result = inference.run(
            input_file=body.inputFile,
            unique_id=body.uniqueId,
            config_path=str(inference.default_config_path()),
            output_root=str(output_root()),
            overrides=body.config,
        )
        # [v4.2] 血缘/阶段归档已停用：mty_compliance 调用注释存档（模块保留，不再调用）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, body.isLastOperator)
    except (ConfigError, io_utils.TaskBusyError) as exc:
        return JSONResponse(content=fail(str(exc)))
    except inference.InputError as exc:
        return JSONResponse(content=fail(str(exc)))
    except (OSError, ValueError) as exc:
        return JSONResponse(content=fail("处理失败：%s" % type(exc).__name__))

    if result["status"] == "failed":
        # [v4.2] 失败信封统一 data={}（与其余算子一致）
        return JSONResponse(content=fail(result["message"]))

    # [v4.2] 统一响应：isLastOperator 保留严格校验（契约兼容）但不再改变响应形态；
    # 去除血缘文件字段，始终返回 outputFile（crossmodal-files-v1）。
    try:
        file_object = inference.stage_file_object(result)
    except OutputTooLargeError as exc:
        return JSONResponse(content=fail("output_too_large：%s" % exc))
    return JSONResponse(content=envelope(0, "success", {
        "operator": OPERATOR_ENGLISH_ID,
        "status": "success",
        "outputFile": file_object,
        "result": _business_summary(result),
    }))


def _business_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态二 data.result 业务摘要：status 双字段 + 内部绝对路径转任务相对路径。"""
    summary: Dict[str, Any] = {
        "status": 1,
        "process_status": result.get("status"),
        "statistics": result.get("statistics") or {},
        "entrypoint": result.get("entrypoint"),
    }
    run_dir = result.get("runDir")
    input_file = result.get("inputFile")
    if run_dir and input_file:
        try:
            task = Path(run_dir).resolve().parent.parent
            summary["input"] = Path(input_file).resolve().relative_to(task).as_posix()
        except (ValueError, OSError):
            pass
    return summary


# --------------------------------------------------------------------------- #
# [v4.2] 形态一：单文件流协议（MTY V2，POST /predict?detection=align_semantic_unit）
# --------------------------------------------------------------------------- #

#: 形态一表单白名单：enabled + transcribe（字符串 true/false，默认 false）；未知字段忽略
_FORM_SCALAR_KEYS = frozenset({"enabled", "transcribe"})
#: 形态一接受的单媒体扩展名（加工白名单：音频 + 带音轨视频）
_AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".flac"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".avi"})


def _coerce_scalar(raw: str) -> Any:
    """字符串转数字/布尔；无法转换时原样返回（由 config 白名单校验兜底报错）。"""
    text = raw.strip()
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
        return text


def _form_overrides(form: Any) -> Dict[str, Any]:
    """白名单标量转换；``transcribe`` 映射到 config 的 transcription_enabled；未知字段忽略。

    [v4.2 §4.8] 不做任何默认开启：总开关与 transcribe 均默认关闭，关闭时接口层
    不会触发 Whisper 模型加载（模型加载逻辑本身不变）。
    """
    overrides: Dict[str, Any] = {}
    for key, value in form.items():
        if key in ("file", "uniqueId"):
            continue
        if key not in _FORM_SCALAR_KEYS or not isinstance(value, str):
            continue
        target = "transcription_enabled" if key == "transcribe" else key
        overrides[target] = _coerce_scalar(value)
    # 形态一 transcribe=true 即显式请求转写：接口层连带打开总开关（表单 enabled=false 可 veto）；
    # 模型加载逻辑本身不变——任一开关关闭，Whisper 都不会加载。
    if overrides.get("transcription_enabled") is True and "enabled" not in overrides:
        overrides["enabled"] = True
    return overrides


def _stage_single_upload(root: Path, uid: str, content: bytes,
                         filename: Optional[str]) -> Tuple[Path, str]:
    """形态一上传落盘：zip 安全解压 / 单文件直存到任务 input，返回 (input目录, 文件摘要)。"""
    task = io_utils.task_dir(root, uid)
    staging = Path(tempfile.mkdtemp(prefix="single_", dir=str(task)))
    try:
        target = task / "input"
        if target.exists():
            shutil.rmtree(target)
        if zipfile.is_zipfile(io.BytesIO(content)):
            # [v4.3] 压缩包模式：按形态二布局解压到任务 input，供阶段推理直接复用
            target.mkdir(parents=True)
            io_utils.safe_extract_zip(
                _write_staged_archive(staging, content), target,
                max_files=inference.MAX_FILES,
                max_unpacked_bytes=inference.MAX_UNPACKED_BYTES)
        else:
            content_dir = staging / "content"
            content_dir.mkdir(parents=True)
            (content_dir / _sanitize_filename(filename)).write_bytes(content)
            io_utils.commit_directory(content_dir, target)
        return target, hashlib.sha256(content).hexdigest()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write_staged_archive(staging: Path, content: bytes) -> Path:
    """把上传字节落成临时 zip 文件，供 ``io_utils.safe_extract_zip`` 做限额校验。"""
    archive = staging / "upload.zip"
    archive.write_bytes(content)
    return archive


def _ensure_native_input(uid: str, input_dir: Path) -> Path:
    """定位或合成形态一入口：JSON/JSONL（含唯一 *.json）原样；单音频/视频合成单记录 JSONL。"""
    try:
        entry, _relative = resolve_entrypoint(input_dir)
        return entry
    except TransportError:
        pass
    files = [path for path in sorted(input_dir.rglob("*"))
             if path.is_file()
             and not any(part.startswith(".") for part in path.relative_to(input_dir).parts)]
    # [v4.2] 找不到 *.jsonl 时允许唯一的 *.json 作为入口
    jsons = [path for path in files if path.suffix.lower() == ".json"
             and path.name != "manifest.json"]
    if not any(path.suffix.lower() == ".jsonl" for path in files) \
            and len(files) == 1 and len(jsons) == 1:
        return jsons[0]
    if len(files) == 1:
        media = files[0]
        suffix = media.suffix.lower()
        modality = "audio" if suffix in _AUDIO_SUFFIXES \
            else ("video" if suffix in _VIDEO_SUFFIXES else None)
        if modality:
            # [v4.2] 媒体文件合成单记录 JSONL：{"sample_id": <文件名去扩展名>, <模态键>: "<文件名>"}
            record = {"sample_id": media.stem, modality: media.name}
            entry = input_dir / ("%s.jsonl" % uid)
            entry.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            return entry
    raise ValueError("该算子需要 JSON/JSONL 输入或单个音频/带音轨视频文件（wav/mp3/flac/mp4/mov/avi）")


def _single_file_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2 §4.8] 形态一成功响应：status + text（结果 JSONL 引用）+ segments + model_version。

    转写关闭（默认）时不加载模型：segments 为空、model_version 为 None，status 仍为 1。
    """
    payload: Dict[str, Any] = {
        "status": 1,
        "process_status": "no_media",
        "text": result.get("entrypoint"),
        "segments": [],
        "model_version": None,
    }
    try:
        lines = Path(result["outputFile"]).read_text(encoding="utf-8").splitlines()
    except OSError:
        return payload
    record = None
    for line in lines:
        if line.strip():
            record = json.loads(line)
            break
    if not record:
        return payload
    processing = (record.get("processing") or {}).get("07") or {}
    transcription = record.get("transcription") or {}
    state = transcription.get("status")
    # 记录级流程态以 processing["07"] 为准（transcribed/skipped/not_applicable/
    # no_speech/failed），缺省时回落转写载荷状态
    payload["process_status"] = processing.get("process_status") or state or "not_applicable"
    if transcription.get("status") == "ok":
        payload["model_version"] = (transcription.get("model_version")
                                    or transcription.get("model"))
        payload["transcript"] = record.get("audio_transcript")
        segments: List[Dict[str, Any]] = []
        for item in record.get("time_index") or []:
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
            if not isinstance(start_ms, int) or not isinstance(end_ms, int):
                start_ms = int(round(float(item.get("start") or 0.0) * 1000))
                end_ms = int(round(float(item.get("end") or 0.0) * 1000))
            segments.append({
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": item.get("text"),
            })
        payload["segments"] = segments
    return payload


def _audit_single_file(uid: str, filename: Optional[str], sha256: str,
                       result: Dict[str, Any], started: float) -> None:
    """[v4.2 §2.7] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    run_dir = result.get("runDir")
    if not run_dir:
        return
    record = {
        "detection": OPERATOR_ENGLISH_ID,
        "uniqueId": uid,
        "filename": filename,
        "sha256": sha256,
        "status": 0 if result.get("status") == "failed" else 1,
        "process_status": result.get("status"),
        "elapsed_ms": int((time.time() - started) * 1000),
        "ts": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
    }
    try:
        with open(Path(run_dir) / "single_file_audit.jsonl", "a", encoding="utf-8") as sink:
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


async def _predict_single_file(request: Request) -> JSONResponse:
    """[v4.2] 形态一：单音频/带音轨视频 → 语义单元（可选 Whisper 转写，默认关闭）。

    [v4.3] ``file`` 为 zip 压缩包时按形态二相同的阶段推理执行，返回容器内路径引用
    （``outputFile`` 为目录引用、``result`` 为阶段统计），并生成可直传下游的
    ``*_form1.zip``；为单个媒体/JSONL 时保持原单文件单记录行为。
    """
    started = time.time()
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    uid_raw = (request.query_params.get("uniqueId") or form.get("uniqueId")
               or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    try:
        uid = io_utils.validate_unique_id(str(uid_raw))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": "uniqueId 非法：%s" % exc})

    root = output_root()
    filename = getattr(upload, "filename", None)
    try:
        payload = await upload.read()
    finally:
        await upload.close()
    if not payload:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})
    if len(payload) > inference.MAX_UPLOAD_BYTES:
        return JSONResponse({"status": 0, "msg": "上传内容超过限额 %d 字节"
                             % inference.MAX_UPLOAD_BYTES})
    is_package = zipfile.is_zipfile(io.BytesIO(payload))
    try:
        with io_utils.TaskLock(root, uid):
            input_dir, sha256 = _stage_single_upload(root, uid, payload, filename)
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except (ValueError, TransportError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except OSError as exc:
        return JSONResponse({"status": 0, "msg": "上传失败：%s" % type(exc).__name__})

    try:
        if is_package:
            try:
                entry = form1_package.resolve_entrypoint(input_dir)
            except ValueError:
                entry = _ensure_native_input(uid, input_dir)
        else:
            entry = _ensure_native_input(uid, input_dir)
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    try:
        result = inference.run(str(entry), uid,
                               config_path=str(inference.default_config_path()),
                               output_root=str(root), overrides=overrides)
    except (ConfigError, inference.InputError, ValueError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except OSError as exc:
        return JSONResponse({"status": 0, "msg": "处理失败：%s" % type(exc).__name__})
    _audit_single_file(uid, filename, sha256, result, started)
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})
    detail = _single_file_payload(result)
    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 生成可直传下游的 *_form1.zip
        try:
            run_root_raw = result.get("runDir") or result.get("run_dir")
            if not run_root_raw:
                raise ValueError("阶段推理未返回运行目录")
            entry_hint = result.get("entrypoint") \
                or Path(result.get("outputFile") or "").name
            reference = form1_package.build_reference(Path(run_root_raw), entry_hint)
        except Exception as exc:  # noqa: BLE001 - 打包失败降级为单文件响应
            detail["uniqueId"] = uid
            detail["package_error"] = str(exc)[:200]
            detail["status"] = 0
            return JSONResponse(detail)
        business = {key: value for key, value in detail.items() if key != "status"}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator=OPERATOR_ENGLISH_ID, unique_id=uid,
            business=business))
    detail.setdefault("uniqueId", uid)
    return JSONResponse(detail)


# ---------------------------------------------------------------- /removeFile

@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(...)) -> Dict[str, Any]:
    """删除任务目录；对已删除或不存在的任务同样返回成功（幂等）。"""
    try:
        value = io_utils.validate_unique_id(uniqueId)
    except ValueError as exc:
        return fail(str(exc))
    try:
        io_utils.remove_task(output_root(), value)
    except io_utils.TaskBusyError as exc:
        return fail("任务正被占用：%s" % exc)
    except OSError as exc:
        return fail("清理失败：%s" % exc)
    return envelope(0, "success", {})


# ---------------------------------------------------------------- 健康检查

@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """[D1] 存活探针：算子标识/状态（统一信封，与已有算子 /health 风格一致）。"""
    return JSONResponse(content=envelope(0, "success", {
        "status": "ok", "operator": inference.OPERATOR_ENGLISH_ID}))


# ---------------------------------------------------------------- 本地启动

def main(argv: Optional[list] = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="算子 7 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8107)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
