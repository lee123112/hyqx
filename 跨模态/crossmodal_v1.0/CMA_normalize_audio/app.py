"""算子 6 HTTP 服务：/uploadFile、/predict、/removeFile（不注册 /downloadFile）。

契约（DEVELOPMENT_CONTRACT §2 + [v4.2] 双协议）：
- 上传/删除用 query ``uniqueId``；``/predict`` 双协议：multipart/form-data 为形态一
  单文件流（成功返回标准化 WAV 文件流），application/json 为形态二阶段文件传递；
- 失败统一 ``{"code":1,"msg":...,"data":{}}``；参数错误用 HTTP 422，业务失败用 code=1；
- [v4.2] 形态二统一响应：不再区分 isLastOperator，去除血缘文件字段，始终返回
  ``outputFile``（crossmodal-files-v1 文件内容对象），不返回本地路径充数。

服务端 task/zip/响应字节限额只来自服务配置与环境变量，不接受请求放宽。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

import inference
from utils import form1_package  # noqa: E402
from utils import io as io_utils
# [v4.2] 血缘已停用：to_stage_lineage 不再导入（utils/lineage.py 存档保留）
from utils.schemas import ConfigError, PredictRequest
from utils.transport import (
    OutputTooLargeError,
    TransportError,
    pack_file_object,
    write_manifest,
    build_manifest,
    resolve_entrypoint,
)

#: 服务端限额（demo 约定，不接受请求放宽）
MAX_UPLOAD_BYTES = 67108864
MAX_FILES = 1000
MAX_UNPACKED_BYTES = 67108864
MAX_RESPONSE_BYTES = 67108864

SERVICE_CONFIG_PATH = os.environ.get(
    "OPERATOR_CONFIG", str(Path(__file__).resolve().parent / "configs" / "config.yaml"))
FALLBACK_OPERATOR_ID = "audio_standardization"
FALLBACK_STAGE_ID = "S2"

app = FastAPI(title="跨模态 06 音频标准化", version="v1.0.0")


def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": code, "msg": msg, "data": data if data is not None else {}}


def fail(msg: str, code: int = 1, http_status: int = 200,
         data: Optional[Dict[str, Any]] = None) -> JSONResponse:
    return JSONResponse(status_code=http_status, content=envelope(code, msg, data))


# --------------------------------------------------------------------------- #
# [v4.2] detection/operatorId 别名空间（M07/07/7/英文ID/目录名，大小写不敏感）
# --------------------------------------------------------------------------- #

DETECTION_ALIASES = frozenset({
    "m07", "07", "7", "normalize_audio", "audio_standardization",
    "cma_normalize_audio",
})
OPERATOR_ENGLISH_ID = inference.OPERATOR_ENGLISH_ID


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """存活探针：算子标识/状态（统一信封；按交付契约不进 OpenAPI 文档）。"""
    identity = service_identity()
    return JSONResponse(content=envelope(0, "success",
                                        {"status": "ok", "operator": identity["operator"],
                                         "stageId": identity["stage_id"]}))


def _check_operator_alias(request: Request) -> Optional[JSONResponse]:
    """detection/operatorId 双查询参数兼容校验（[v4.2] §2.7 公共约定）。"""
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return fail("detection 与 operatorId 取值不一致：%s vs %s"
                    % (detection, operator_id), http_status=422)
    value = detection or operator_id
    if value is not None and value.strip().lower() not in DETECTION_ALIASES:
        return fail("detection/operatorId 与本算子（M07/normalize_audio）不符：%s" % value,
                    http_status=422)
    return None


def output_root() -> Path:
    """输出根：OUTPUT_DIR 环境变量 > 本目录 output（不接收请求任意指定）。"""
    env_root = os.environ.get("OUTPUT_DIR")
    if env_root:
        return io_utils.resolve_root(env_root)
    return io_utils.resolve_root(Path(__file__).resolve().parent / "output")


def service_identity() -> Dict[str, str]:
    try:
        cfg = inference.load_config(SERVICE_CONFIG_PATH)
    except ConfigError:
        return {"operator": FALLBACK_OPERATOR_ID, "stage_id": FALLBACK_STAGE_ID}
    return {"operator": cfg.operator.name, "stage_id": cfg.stage_id}


def _first_validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "请求体非法"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "<body>"
    return "%s: %s" % (location, first.get("msg", "非法取值"))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request, exc: RequestValidationError) -> JSONResponse:
    return fail("请求参数非法——%s" % _first_validation_message(exc), http_status=422)


@app.exception_handler(Exception)
async def handle_unexpected_error(_request, exc: Exception) -> JSONResponse:
    # 不回传堆栈；错误类型进 msg，细节由服务端日志承接
    return fail("服务内部错误：%s" % type(exc).__name__, http_status=500)


def _check_unique_id(unique_id: str) -> Optional[JSONResponse]:
    try:
        io_utils.validate_unique_id(unique_id)
    except ValueError as exc:
        return fail("uniqueId 非法：%s" % exc, http_status=422)
    return None


def _is_zip(filename: str, payload: bytes) -> bool:
    if payload[:4] == b"PK\x03\x04":
        return True
    return Path(filename or "").suffix.lower() == ".zip"


def _store_zip(root: Path, unique_id: str, payload: bytes) -> str:
    """安全解压 zip 到临时目录，校验入口后原子提交到 ``<task>/input``。"""
    task = io_utils.task_dir(root, unique_id)
    tmp_root = io_utils.resolved_absolute(root / ".tmp")
    tmp_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(tmp_root), prefix="upload_"))
    try:
        archive = staging / "upload.zip"
        archive.write_bytes(payload)
        extracted = staging / "content"
        io_utils.safe_extract_zip(archive, extracted, MAX_FILES, MAX_UNPACKED_BYTES)
        from utils.transport import resolve_entrypoint

        _entry, relative = resolve_entrypoint(extracted)
        target = task / "input"
        if target.exists():
            shutil.rmtree(target)
        io_utils.commit_directory(extracted, target)
        return relative or "demo.jsonl"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _store_single(root: Path, unique_id: str, filename: Optional[str], payload: bytes) -> str:
    """单文件上传：只接受不引用媒体的 JSONL/JSON，并补写单文件 manifest。"""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in (".jsonl", ".json"):
        raise ValueError("独立上传只支持 .jsonl/.json；含媒体的素材请打包 zip")
    text = payload.decode("utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("第 %d 行不是合法 JSON" % lineno) from exc
        if not isinstance(record, dict):
            raise ValueError("第 %d 行不是 JSON 对象" % lineno)
        for field in ("image", "video", "audio"):
            if inference._media_paths_of(record.get(field)):  # noqa: SLF001 - 同算子内部复用
                raise ValueError(
                    "独立 JSONL 上传不允许引用媒体（第 %d 行 %s）；请使用 zip 素材包"
                    % (lineno, field))

    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        raise ValueError("上传文件名非法")
    task = io_utils.task_dir(root, unique_id)
    target = task / "input"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    io_utils.atomic_write_text(target / safe_name, text)
    write_manifest(target, build_manifest(safe_name, [{"path": safe_name, "role": "main"}]))
    return safe_name


@app.post("/uploadFile")
async def upload_file(file: UploadFile = File(...), uniqueId: str = Query(...)) -> JSONResponse:
    invalid = _check_unique_id(uniqueId)
    if invalid is not None:
        return invalid
    try:
        payload = await file.read(MAX_UPLOAD_BYTES + 1)
    except (OSError, RuntimeError) as exc:
        return fail("读取上传内容失败：%s" % type(exc).__name__, http_status=400)
    if not payload:
        return fail("上传内容为空", http_status=400)
    if len(payload) > MAX_UPLOAD_BYTES:
        return fail("上传内容超过 %d 字节限额" % MAX_UPLOAD_BYTES, http_status=413)

    root = output_root()
    try:
        with io_utils.TaskLock(root, uniqueId):
            if _is_zip(file.filename, payload):
                entrypoint = _store_zip(root, uniqueId, payload)
            else:
                entrypoint = _store_single(root, uniqueId, file.filename, payload)
    except io_utils.TaskBusyError as exc:
        return fail("task_busy：%s" % exc, http_status=409)
    except (ValueError, TransportError) as exc:
        return fail(str(exc), http_status=400)
    # 回显定位到的入口，便于确认上游包被正确识别（失败时 data 仍恒为 {}）
    return JSONResponse(content=envelope(0, "success", {"entrypoint": entrypoint}))


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
        return fail("请求体必须是 JSON 对象", http_status=422)
    try:
        body = PredictRequest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())) or "<body>"
        return fail("请求参数非法——%s: %s" % (location, first.get("msg", "非法取值")),
                    http_status=422)
    invalid = _check_unique_id(body.uniqueId)
    if invalid is not None:
        return invalid
    root = output_root()
    if body.inputFile:
        # 只允许引用当前 uniqueId 任务目录内的上游产物，拒绝跨任务/越界路径
        try:
            io_utils.resolve_in_task(io_utils.task_dir(root, body.uniqueId),
                                     body.inputFile)
        except ValueError as exc:
            return fail("inputFile 非法：%s" % exc, http_status=422)
    try:
        result = inference.run(body.inputFile, body.uniqueId,
                              config_path=SERVICE_CONFIG_PATH,
                              output_root=str(output_root()),
                              overrides=body.config)
        # [v4.2] 血缘/阶段归档已停用：mty_compliance 调用注释存档（模块保留，不再调用）
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.prepare_stage_result(result, body.isLastOperator)
    except io_utils.TaskBusyError as exc:
        return fail("task_busy：%s" % exc, http_status=409, data={"status": "failed"})
    except inference.InputError as exc:
        return fail(str(exc), http_status=400, data={"status": "failed"})
    except ConfigError as exc:
        return fail(str(exc), http_status=400)
    except ValueError as exc:
        return fail(str(exc), http_status=422)
    except OSError as exc:
        return fail("文件系统错误：%s" % type(exc).__name__, http_status=500,
                    data={"status": "failed"})

    if result["status"] == "failed":
        # [v4.2] 失败路径信封：data={"status": "failed"}
        return fail(result["message"], http_status=500, data={"status": "failed"})

    # [v4.2] 统一响应：isLastOperator 保留严格校验（契约兼容）但不再改变响应形态；
    # 去除血缘文件字段，始终返回 outputFile（crossmodal-files-v1）。
    run_dir = Path(result["runDir"])
    paths: List[str] = []
    for relative in [result["entrypoint"]] \
            + [item["path"] for item in result["artifacts"]] + ["manifest.json"]:
        # artifacts 已含 main/review 入口，去重后端才可能通过 restore 的重复检查
        if relative not in paths:
            paths.append(relative)
    try:
        file_object = pack_file_object(
            run_dir, result["entrypoint"], paths, MAX_RESPONSE_BYTES)
    except OutputTooLargeError as exc:
        return fail("output_too_large：%s" % exc, http_status=500, data={"status": "failed"})
    except TransportError as exc:
        return fail("输出打包失败：%s" % exc, http_status=500, data={"status": "failed"})
    return JSONResponse(content=envelope(0, result["message"], {
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
# [v4.2] 形态一：单文件流协议（MTY V2，POST /predict?detection=normalize_audio）
# --------------------------------------------------------------------------- #

#: 形态一表单白名单：config 业务标量；未知字段忽略（宽松）
_FORM_SCALAR_KEYS = frozenset({"enabled", "sample_rate", "channels", "pcm_bits"})
#: 本算子形态一接受的单媒体扩展名（跨模态加工白名单：音频 wav/mp3/flac）
_AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".flac"})


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
        overrides[key] = _coerce_scalar(value)
    return overrides


async def _stage_single_upload(root: Path, uid: str, upload: Any) -> Tuple[Path, str, bool]:
    """形态一上传落盘：zip 安全解压 / 单文件直存到任务 input。

    返回 ``(input 目录, 文件摘要, 是否为压缩包)``；[v4.3] 压缩包走列表推理。
    """
    payload = await upload.read(MAX_UPLOAD_BYTES + 1)
    if not payload:
        raise ValueError("上传内容为空")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError("上传内容超过 %d 字节限额" % MAX_UPLOAD_BYTES)
    digest = hashlib.sha256(payload).hexdigest()
    task = io_utils.task_dir(root, uid)
    tmp_root = io_utils.resolved_absolute(root / ".tmp")
    tmp_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(tmp_root), prefix="single_"))
    try:
        content = staging / "content"
        is_package = _is_zip(upload.filename or "", payload)
        if is_package:
            archive = staging / "upload.zip"
            archive.write_bytes(payload)
            io_utils.safe_extract_zip(archive, content, MAX_FILES, MAX_UNPACKED_BYTES)
        else:
            name = Path(upload.filename or "upload.bin").name
            if not name or name in (".", ".."):
                raise ValueError("上传文件名非法")
            content.mkdir(parents=True, exist_ok=True)
            (content / name).write_bytes(payload)
        target = task / "input"
        if target.exists():
            shutil.rmtree(target)
        io_utils.commit_directory(content, target)
        return target, digest, is_package
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _ensure_native_input(uid: str, input_dir: Path) -> Path:
    """定位或合成形态一入口：JSON/JSONL（含唯一 *.json）原样；单个音频合成单记录 JSONL。"""
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
    media = [path for path in files if path.suffix.lower() in _AUDIO_SUFFIXES]
    if len(media) == 1 and len(files) == 1:
        audio = media[0]
        # [v4.2] 媒体文件合成单记录 JSONL：{"sample_id": <文件名去扩展名>, "audio": "<文件名>"}
        record = {"sample_id": audio.stem, "audio": audio.name}
        entry = input_dir / ("%s.jsonl" % uid)
        entry.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        return entry
    raise ValueError("该算子需要 JSON/JSONL 输入或单个音频文件（wav/mp3/flac）")


def _normalized_wav_path(result: Dict[str, Any]) -> Optional[Path]:
    """从主清单找第一条标准化音频的 run 内路径（[v4.2] M07 形态一成功返回 WAV 文件流）。"""
    run_dir = Path(result["runDir"])
    try:
        for line in Path(result["outputFile"]).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            audio = record.get("audio")
            if isinstance(audio, dict) and isinstance(audio.get("path"), str):
                candidate = run_dir / audio["path"]
                if candidate.is_file():
                    return candidate
    except (OSError, json.JSONDecodeError):
        return None
    return None


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


async def _predict_single_file(request: Request):
    """[v4.2] 形态一：单文件流。M07 成功返回 16kHz 单声道 WAV 文件流；失败 {"status": 0}。

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
        uid = io_utils.validate_unique_id(str(uid_raw))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": "uniqueId 非法：%s" % exc})

    root = output_root()
    sha256 = ""
    try:
        with io_utils.TaskLock(root, uid):
            input_dir, sha256, is_package = await _stage_single_upload(root, uid, upload)
    except io_utils.TaskBusyError as exc:
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
                # 兼容压缩包内仅含单个音频的旧用法：合成单记录 JSONL
                entry = _ensure_native_input(uid, input_dir)
        else:
            entry = _ensure_native_input(uid, input_dir)
    except (ValueError, TransportError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})

    overrides = _form_overrides(form)
    # [v4.2] 形态一直传音频即显式请求标准化：默认开启本算子（表单 enabled=false 可显式关闭）；
    # config 默认关闭仅约束形态二产线编排。M07 为规则型，无模型加载。
    overrides.setdefault("enabled", True)
    try:
        result = inference.run(str(entry), uid, config_path=SERVICE_CONFIG_PATH,
                               output_root=str(root), overrides=overrides)
    except (ConfigError, inference.InputError, ValueError) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except io_utils.TaskBusyError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except OSError as exc:
        return JSONResponse({"status": 0, "msg": "处理失败：%s" % type(exc).__name__})
    _audit_single_file(uid, getattr(upload, "filename", None), sha256, result, started)
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "msg": result.get("message", "处理失败")})

    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            run_root = Path(result.get("runDir") or "")
            entry_hint = result.get("entrypoint")
            reference = form1_package.build_reference(run_root, entry_hint)
        except (OSError, ValueError) as exc:
            return JSONResponse({"status": 0, "msg": "输出打包失败：%s" % str(exc)[:200]})
        statistics = result.get("statistics") or {}
        business = {"audio_index": result.get("entrypoint"),
                    "audio_standardized": statistics.get("success")}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator=OPERATOR_ENGLISH_ID,
            unique_id=uid, business=business))

    wav = _normalized_wav_path(result)
    if wav is None:
        return JSONResponse({"status": 0, "msg": "未产出标准化音频（输入无音频或记录不适用）"})
    return FileResponse(str(wav), media_type="audio/wav", filename=wav.name)


@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(...)) -> JSONResponse:
    invalid = _check_unique_id(uniqueId)
    if invalid is not None:
        return invalid
    try:
        io_utils.remove_task(output_root(), uniqueId)
    except io_utils.TaskBusyError as exc:
        return fail("task_busy：%s" % exc, http_status=409)
    except ValueError as exc:
        return fail(str(exc), http_status=422)
    # 幂等：任务不存在也返回 success，不泄漏其他任务信息
    return JSONResponse(content=envelope(0, "success"))
