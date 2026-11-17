#!/usr/bin/env python3
"""算子 5 三接口服务：/uploadFile、/predict、/removeFile。

按 DEVELOPMENT_CONTRACT §2：所有响应统一信封 ``{"code","msg","data"}``；
不实现 ``/downloadFile``（显式 404 信封）；阶段末 ``data.outputFile`` 是
文件内容对象，不是本地路径。

框架自身产生的错误（未知路径 404、方法不符 405、请求校验 422）也经全局处理器
转成同一信封，不让 ``{"detail": ...}`` 漏给调用方。

启动::

    python -m uvicorn app:app --host 127.0.0.1 --port 8105
"""
from __future__ import annotations

import hashlib
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

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inference as core  # noqa: E402
from utils import form1_package  # noqa: E402
from utils.io import (TaskBusyError, TaskLock, commit_directory, remove_task,  # noqa: E402
                      resolve_in_task, resolve_root, safe_extract_zip, task_dir,
                      validate_unique_id)
from utils.schemas import OperatorConfig, PredictRequest, RemoveFileRequest  # noqa: E402
from utils.transport import TransportError, resolve_entrypoint  # noqa: E402

#: 服务端配置路径（HTTP 不接收请求指定）
CONFIG_PATH = os.environ.get("VIDEO_KEYFRAMES_CONFIG",
                             str(core.DIR / "configs" / "config.yaml"))
CHUNK_SIZE = 1 << 20

app = FastAPI(title="video_keyframes", version="1.0.0",
              description="算子 5 视频关键帧与片段处理（demo 三接口）")


def _output_root(cfg: OperatorConfig) -> Path:
    """共享部署约定：OUTPUT_DIR 环境变量优先于配置文件默认值。"""
    return resolve_root(os.environ.get("OUTPUT_DIR") or cfg.io.output_root)


def _envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None,
              status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"code": code, "msg": msg, "data": data or {}})


def _service_config() -> Tuple[OperatorConfig, Dict[str, Any]]:
    return core.load_config(CONFIG_PATH)


def _failure(message: str, kind: str, status_code: int,
             data: Optional[Dict[str, Any]] = None) -> JSONResponse:
    return _envelope(1, "%s: %s" % (kind, message), data or {}, status_code)


# --------------------------------------------------------------------------- #
# [v4.2] detection/operatorId 别名空间（M06/06/6/英文ID/目录名，大小写不敏感）
# --------------------------------------------------------------------------- #

DETECTION_ALIASES = frozenset({
    "m06", "06", "6", "extract_video_keyframe", "cma_extract_video_keyframe",
    # [D2] 追加 map.json 登记表英文 operatorId（只追加，不重排既有条目）
    "video_keyframes",
})
OPERATOR_ENGLISH_ID = core.OPERATOR_ENGLISH_ID


def _check_operator_alias(request: Request) -> Optional[JSONResponse]:
    """detection/operatorId 双查询参数兼容校验（[v4.2] §2.7 公共约定）。

    值缺失或与算子别名一致即放行；两者同时出现取值必须一致，否则参数错误。
    """
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return _failure("detection 与 operatorId 取值不一致：%s vs %s"
                        % (detection, operator_id), "invalid_request", 422)
    value = detection or operator_id
    if value is not None and value.strip().lower() not in DETECTION_ALIASES:
        return _failure("detection/operatorId 与本算子（M06/extract_video_keyframe）不符：%s"
                        % value, "invalid_request", 422)
    return None


#: HTTP 状态码 → 信封里的错误类别
_STATUS_KINDS = {400: "bad_request", 404: "not_found", 405: "method_not_allowed",
                 415: "unsupported_media_type", 422: "invalid_request",
                 500: "internal_error"}


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """框架抛出的 404/405 等也走统一信封，避免漏出 ``{"detail": ...}``。"""
    kind = _STATUS_KINDS.get(exc.status_code, "http_error")
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail.strip() else "http error"
    return _failure(detail, kind, exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(_request: Request,
                                        exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors() or []
    first = errors[0] if errors else {}
    location = ".".join(str(item) for item in first.get("loc", ()) if item != "body")
    message = "%s: %s" % (location or "request", first.get("msg", "invalid request"))
    return _failure(message.strip(": "), "invalid_request", 422)


def _locate_stage_input(task: Path) -> Optional[Path]:
    """阶段首：按上传 manifest 定位入口，绝不读取本算子旧运行的产物。"""
    for candidate in (task / core.INPUT_DIR_NAME, task):
        if not candidate.is_dir():
            continue
        try:
            entry, _relative = resolve_entrypoint(candidate)
        except TransportError:
            continue
        return entry
    return None


# --------------------------------------------------------------------------- #
# 三接口
# --------------------------------------------------------------------------- #

@app.post("/uploadFile")
async def upload_file(uniqueId: Optional[str] = None, file: Optional[UploadFile] = File(None)):
    """上传素材包（zip 或单个文件）：安全解压校验后原子提交到任务 input 目录。

    参数缺失也返回统一信封（422），不落到框架默认的 ``{"detail": ...}``。
    """
    if file is None:
        return _failure("缺少上传文件 file", "invalid_upload", 422)
    if not uniqueId:
        return _failure("缺少 uniqueId", "invalid_unique_id", 422)
    try:
        validate_unique_id(uniqueId)
    except ValueError as exc:
        return _failure(str(exc), "invalid_unique_id", 422)
    try:
        cfg, _snapshot = _service_config()
    except core.ConfigError as exc:
        return _failure(str(exc), "invalid_config", 500)

    root = _output_root(cfg)
    tmp_path: Optional[Path] = None
    staging: Optional[Path] = None
    try:
        with TaskLock(root, uniqueId):
            task = task_dir(root, uniqueId)
            tmp_path = task / (".upload-%s%s" % (uuid.uuid4().hex, Path(file.filename or "").suffix[:10]))
            written = 0
            with open(tmp_path, "wb") as sink:
                while True:
                    chunk = await file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > cfg.server.max_unpacked_bytes:
                        raise ValueError("上传内容超过解压总大小限额 %d 字节"
                                         % cfg.server.max_unpacked_bytes)
                    sink.write(chunk)
            if written == 0:
                raise ValueError("上传内容为空")

            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(task)))
            if zipfile.is_zipfile(tmp_path):
                safe_extract_zip(tmp_path, staging, max_files=cfg.server.max_files,
                                 max_unpacked_bytes=cfg.server.max_unpacked_bytes)
            else:
                name = Path(file.filename or "upload.bin").name
                if not name or name in {".", ".."}:
                    raise ValueError("上传文件名非法")
                shutil.copy2(tmp_path, staging / name)
            # 入口必须可唯一定位，否则不提交
            _entry, entry_relative = resolve_entrypoint(staging)
            target = task / core.INPUT_DIR_NAME
            if target.exists():
                shutil.rmtree(target)
            commit_directory(staging, target)
            staging = None
    except (ValueError, TransportError) as exc:
        return _failure(str(exc), "invalid_upload", 422)
    except TaskBusyError as exc:
        return _failure(str(exc), "task_busy", 400)
    except OSError as exc:
        return _failure("%s" % type(exc).__name__, "upload_failed", 500)
    finally:
        for path in (tmp_path, staging):
            if path is not None:
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except OSError:
                    pass
        await file.close()
    # 回显定位到的入口，便于调用方确认上游清单/媒体包已被正确提交
    return _envelope(0, "success", {"entrypoint": entry_relative})


@app.post("/predict")
async def predict(request: Request):
    """[v4.2] 双协议入口：multipart/form-data → 形态一单文件流；JSON → 形态二阶段文件传递。

    形态二统一响应：去除血缘文件字段、始终返回 outputFile（crossmodal-files-v1）。
    """
    alias_error = _check_operator_alias(request)
    if alias_error is not None:
        return alias_error
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 请求体不是 JSON
        return _failure("请求体必须是 JSON 对象", "invalid_request", 422)
    try:
        body = PredictRequest.model_validate(payload)
    except ValidationError as exc:
        return _failure(core._format_validation_error(exc), "invalid_request", 422)
    try:
        validate_unique_id(body.uniqueId)
    except ValueError as exc:
        return _failure(str(exc), "invalid_unique_id", 422)
    try:
        cfg, _snapshot = core.load_config(CONFIG_PATH, overrides=body.config)
    except core.ConfigError as exc:
        return _failure(str(exc), "invalid_config", 422)

    root = _output_root(cfg)
    task = task_dir(root, body.uniqueId)
    if body.inputFile:
        try:
            input_path = resolve_in_task(task, body.inputFile)
        except ValueError as exc:
            return _failure(str(exc), "invalid_input_file", 422)
        input_file = str(input_path)
    else:
        located = _locate_stage_input(task)
        if located is None:
            return _failure("未找到可定位的上传入口（input/ 内缺少 manifest 或唯一 JSONL）",
                            "input_not_found", 400)
        input_file = str(located)

    result = core.run(input_file, body.uniqueId, config_path=CONFIG_PATH,
                      output_root=str(root), overrides=body.config)
    # [v4.2] 血缘/阶段归档已停用：mty_compliance 调用注释存档（模块保留，不再调用）
    # from utils import mty_compliance as _mty_compliance
    # _mty_compliance.prepare_stage_result(result, body.isLastOperator)
    code, message, data = core.build_response(
        result, body.isLastOperator, cfg.server.max_response_bytes)
    if code != 0:
        # [v4.2] 失败路径信封：data={"status": "failed"}
        if message.startswith("output_too_large") or message.startswith("transport_error"):
            return _failure(message, "output_pack_failed", 400, {"status": "failed"})
        error_kind = result.get("statistics", {}).get("error") or "run_failed"
        if error_kind == "task_busy":
            return _failure(message, "task_busy", 400, {"status": "failed"})
        return _failure(message, str(error_kind), 400, {"status": "failed"})

    # [v4.2] 响应不再区分阶段末/非阶段末：媒体自检始终执行（isLastOperator 仅契约兼容，
    # 保留严格布尔校验但不再改变响应形态）
    entry = Path(result["outputFile"])
    check = core.verify_media_references(entry, Path(result["runDir"]))
    if not check["ok"]:
        return _failure("输出媒体校验失败：missing=%d empty=%d"
                        % (len(check["missing"]), len(check["empty"])),
                        "media_verify_failed", 400, {"status": "failed"})
    return _envelope(0, message, data)


# --------------------------------------------------------------------------- #
# [v4.2] 形态一：单文件流协议（MTY V2，POST /predict?detection=extract_video_keyframe）
# --------------------------------------------------------------------------- #

#: 形态一表单白名单：config 业务标量 + fps（sample_fps 的表单别名）；未知字段忽略（宽松）
_FORM_SCALAR_KEYS = frozenset({"enabled", "sample_fps", "max_frames", "fps"})
#: 本算子形态一接受的单媒体扩展名（ffmpeg 可解码的常见视频容器）。
#: v1.1 放宽：除 mp4/mov/avi 外，补 mkv/m4v/webm 等，避免平台上传合法视频被
#: 扩展名白名单误拒；是否可解码仍由 ffmpeg/ffprobe 按文件内容判断。
_VIDEO_SUFFIXES = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm",
    ".flv", ".wmv", ".mpeg", ".mpg", ".ts", ".3gp", ".ogv",
})
#: 错误提示中的人读格式列表（与 _VIDEO_SUFFIXES 保持同步）
_VIDEO_SUFFIX_HINT = "mp4/mov/avi/mkv/m4v/webm/flv/wmv/mpeg/mpg/ts/3gp/ogv"


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
    """白名单标量转换；未知字段忽略（[v4.2] 形态一宽松）。"""
    overrides: Dict[str, Any] = {}
    for key, value in form.items():
        if key in ("file", "uniqueId"):
            continue
        if key not in _FORM_SCALAR_KEYS or not isinstance(value, str):
            continue
        target = "sample_fps" if key == "fps" else key
        overrides[target] = _coerce_scalar(value)
    return overrides


async def _stage_upload(task: Path, upload: Any, cfg: OperatorConfig) -> Tuple[Path, str, bool]:
    """把 multipart 文件写入任务 input 目录（zip 安全解压 / 单文件直存）。

    返回 ``(input 目录, 文件摘要, 是否为压缩包)``；[v4.3] 压缩包走列表推理。
    """
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(task)))
    tmp_path: Optional[Path] = None
    try:
        name = Path(upload.filename or "upload.bin").name
        if not name or name in {".", ".."}:
            raise ValueError("上传文件名非法")
        tmp_path = task / (".upload-%s" % uuid.uuid4().hex)
        digest = hashlib.sha256()
        written = 0
        with open(tmp_path, "wb") as sink:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > cfg.server.max_unpacked_bytes:
                    raise ValueError("上传内容超过解压总大小限额 %d 字节"
                                     % cfg.server.max_unpacked_bytes)
                digest.update(chunk)
                sink.write(chunk)
        if written == 0:
            raise ValueError("上传内容为空")
        is_package = zipfile.is_zipfile(tmp_path)
        if is_package:
            safe_extract_zip(tmp_path, staging, max_files=cfg.server.max_files,
                             max_unpacked_bytes=cfg.server.max_unpacked_bytes)
        else:
            shutil.copy2(tmp_path, staging / name)
        target = task / core.INPUT_DIR_NAME
        if target.exists():
            shutil.rmtree(target)
        commit_directory(staging, target)
        staging = None
        return target, digest.hexdigest(), is_package
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _ensure_native_input(uid: str, input_dir: Path) -> Path:
    """定位或合成形态一入口：JSON/JSONL（含唯一 *.json）原样；单个视频合成单记录 JSONL。"""
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
             and path.name != MANIFEST_FALLBACK]
    if not any(path.suffix.lower() == ".jsonl" for path in files) and len(jsons) == 1 \
            and len(files) == 1:
        return jsons[0]
    media = [path for path in files if path.suffix.lower() in _VIDEO_SUFFIXES]
    if len(media) == 1 and len(files) == 1:
        video = media[0]
        # [v4.2] 媒体文件合成单记录 JSONL：{"sample_id": <文件名去扩展名>, "video": "<文件名>"}
        record = {"sample_id": video.stem, "video": video.name}
        entry = input_dir / ("%s.jsonl" % uid)
        entry.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        return entry
    raise ValueError("该算子需要 JSON/JSONL 输入或单个视频文件（%s）" % _VIDEO_SUFFIX_HINT)


#: manifest 文件名（合成入口判断时排除）
MANIFEST_FALLBACK = "manifest.json"


def _pack_frames_zip(run_dir: Path, uid: str) -> Optional[Path]:
    """[v4.2] M06 形态一成功返回：帧图片压缩包文件流（含帧索引 JSONL）。"""
    frames_root = run_dir / "05_frames"
    frames = sorted(path for path in frames_root.rglob("*") if path.is_file()) \
        if frames_root.is_dir() else []
    if not frames:
        return None
    zip_path = run_dir / ("%s_frames.zip" % uid)
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as archive:
        # 主清单即帧索引（含 frame_index/timestamp_ms/路径），随包提供
        archive.write(str(run_dir / core.MAIN_NAME), arcname=core.MAIN_NAME)
        for frame in frames:
            archive.write(str(frame), arcname=frame.relative_to(run_dir).as_posix())
    return zip_path


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
    """[v4.2] 形态一：单文件流。M06 成功返回帧 zip 文件流；失败返回 {"status": 0, "msg"}。

    [v4.3] 上传内容为压缩包时改走列表/阶段推理，返回容器内路径引用 JSON。
    """
    started = time.time()
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    uid_raw = (request.query_params.get("uniqueId") or form.get("uniqueId")
               or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    try:
        uid = validate_unique_id(str(uid_raw))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": "uniqueId 非法：%s" % exc})
    try:
        cfg, _snapshot = _service_config()
    except core.ConfigError as exc:
        return JSONResponse({"status": 0, "msg": "配置错误：%s" % exc})

    root = _output_root(cfg)
    sha256 = ""
    try:
        with TaskLock(root, uid):
            task = task_dir(root, uid)
            input_dir, sha256, is_package = await _stage_upload(task, upload, cfg)
    except TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except (ValueError, TransportError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except OSError as exc:
        return JSONResponse({"status": 0, "msg": "上传失败：%s" % type(exc).__name__})
    finally:
        await upload.close()

    try:
        if is_package:
            try:
                # [v4.3] 压缩包：入口优先 manifest.json，否则唯一顶层 JSONL
                entry = form1_package.resolve_entrypoint(input_dir)
            except ValueError:
                # 兼容压缩包内仅含单个视频的旧用法：合成单记录 JSONL
                entry = _ensure_native_input(uid, input_dir)
        else:
            entry = _ensure_native_input(uid, input_dir)
    except (ValueError, TransportError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    # [v4.2] 形态一直传视频即显式请求抽帧：默认开启本算子（表单 enabled=false 可显式关闭）；
    # config 默认关闭仅约束形态二产线编排，不影响单文件流调用。M06 为规则型，无模型加载。
    overrides.setdefault("enabled", True)
    result = core.run(str(entry), uid, config_path=CONFIG_PATH,
                      output_root=str(root), overrides=overrides)
    _audit_single_file(uid, getattr(upload, "filename", None), sha256, result, started)
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})

    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            run_root = Path(result.get("runDir") or "")
            entry_hint = Path(result.get("outputFile") or "").name or None
            reference = form1_package.build_reference(run_root, entry_hint)
        except (OSError, ValueError) as exc:
            return JSONResponse({"status": 0, "msg": "输出打包失败：%s" % str(exc)[:200]})
        statistics = result.get("statistics") or {}
        business = {"frame_index": core.MAIN_NAME,
                    "frames_saved": statistics.get("frames_saved"),
                    "frames_total": statistics.get("frames_total")}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator="extract_video_keyframe",
            unique_id=uid, business=business))

    zip_path = _pack_frames_zip(Path(result["runDir"]), uid)
    if zip_path is None:
        return JSONResponse({"status": 0, "msg": "未产出任何帧（输入无视频或记录不适用）"})
    return FileResponse(str(zip_path), media_type="application/zip",
                        filename="%s_frames.zip" % uid)


@app.post("/removeFile")
async def remove_file(uniqueId: Optional[str] = None):
    """按 uniqueId 删除任务目录；不影响其他任务，锁释放后才清理。"""
    if not uniqueId:
        return _failure("缺少 uniqueId", "invalid_unique_id", 422)
    try:
        validate_unique_id(uniqueId)
    except ValueError as exc:
        return _failure(str(exc), "invalid_unique_id", 422)
    try:
        cfg, _snapshot = _service_config()
    except core.ConfigError as exc:
        return _failure(str(exc), "invalid_config", 500)
    root = _output_root(cfg)
    try:
        remove_task(root, uniqueId)
    except TaskBusyError as exc:
        return _failure(str(exc), "task_busy", 400)
    except OSError as exc:
        return _failure("%s" % type(exc).__name__, "remove_failed", 500)
    return _envelope(0, "success", {})


# --------------------------------------------------------------------------- #
# [D1] 健康检查（与已有算子 /health 风格一致）
# --------------------------------------------------------------------------- #

@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """存活探针：算子标识/状态（统一信封）。"""
    return _envelope(0, "ok", {"status": "ok", "operator": core.OPERATOR_DIR_NAME})


