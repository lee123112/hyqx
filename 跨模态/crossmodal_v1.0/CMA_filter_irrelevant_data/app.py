"""算子 2 数据初筛服务，严格遵循跨模态 demo 三接口契约。

[v4.2] ``/predict`` 升级为双协议：
- 形态二（阶段文件传递）：``application/json`` 请求体，响应统一为
  ``data{operator, status, outputFile, result}``（不再返回 lineageFile）；
- 形态一（单文件流）：``multipart/form-data`` + 查询参数 ``detection``/``operatorId``
  （别名 M03/03/3/filter_irrelevant_data/data_prescreen/目录名，大小写不敏感），
  判定型响应：``status`` 1=保留 0=过滤 + ``filter_status``/``reason_code``。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference
from utils import form1_package
from utils.io import resolve_in_task, task_dir

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
app = FastAPI(title="data_prescreen", version=inference.VERSION)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException):
    return envelope(1, str(exc.detail), {}, exc.status_code)


def envelope(code: int, msg: str, data: Optional[Dict[str, Any]] = None,
             status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "msg": msg,
                                                     "data": data or {}})


def _uid(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("uniqueId 不能为空")
    value = value.strip()
    task_dir(OUTPUT_ROOT, value)
    return value


def _entry(directory: Path) -> Path:
    manifest = directory / "manifest.json"
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        name = payload.get("entrypoint")
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("manifest.json entrypoint 非法")
        target = (directory / name).resolve()
        target.relative_to(directory.resolve())
        if not target.is_file():
            raise ValueError("manifest.json entrypoint 不存在")
        return target
    candidates = sorted(directory.rglob("*.jsonl"))
    if len(candidates) != 1:
        raise ValueError("素材包必须包含 manifest.json 或唯一 JSONL 入口")
    return candidates[0]


def _safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        seen = set()
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            p = Path(name)
            if not name or p.is_absolute() or ".." in p.parts:
                raise ValueError("zip 成员路径非法")
            if name.lower() in seen:
                raise ValueError("zip 存在重复成员")
            seen.add(name.lower())
            target = (destination / name).resolve()
            target.relative_to(destination.resolve())
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
        entry = _entry(staging)
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
        value["inputFile"] = str(resolve_in_task(task_dir(OUTPUT_ROOT, value["uniqueId"]),
                                                  value["inputFile"]))
    return value


def _pack(task: Path, result: Dict[str, Any]) -> "tuple":
    output = Path(result["outputFile"])
    rejects = Path(result["rejectsFile"])
    run_dir = task / "data_prescreen" / "run001"
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output, run_dir / output.name)
    shutil.copy2(rejects, run_dir / rejects.name)
    media_paths = set()
    for line in output.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        for key in ("image", "video", "audio"):
            value = record.get(key)
            if isinstance(value, str):
                media_paths.add(value.replace("\\", "/"))
    input_dir = task / "input"
    for relative in sorted(media_paths):
        source = (input_dir / relative).resolve()
        source.relative_to(input_dir.resolve())
        if source.is_file():
            target = run_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    manifest = {"format": "crossmodal-manifest-v1", "entrypoint": output.name,
                "artifacts": [{"path": output.name, "role": "main"},
                              {"path": rejects.name, "role": "review"}]}
    for relative in sorted(media_paths):
        if (run_dir / relative).is_file():
            manifest["artifacts"].append({"path": relative, "role": "media"})
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    inference.publish_stage_output(run_dir)
    # [v4.2] 血缘输出已停用：不再基于 result["lineage"] 构造血缘节点。
    # node = dict(result["lineage"])
    # node["stageId"] = "S1"
    # node["runId"] = "run001"
    # node["outputFile"] = str(run_dir / output.name)
    return run_dir, manifest


def _file_object(run_dir: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    files = []
    paths = [manifest["entrypoint"]] + [a["path"] for a in manifest["artifacts"]] + ["manifest.json"]
    for relative in dict.fromkeys(paths):
        raw = (run_dir / relative).read_bytes()
        if Path(relative).suffix.lower() in {".json", ".jsonl", ".txt", ".md"}:
            files.append({"path": relative, "encoding": "utf-8", "content": raw.decode("utf-8")})
        else:
            files.append({"path": relative, "encoding": "base64",
                          "content": base64.b64encode(raw).decode("ascii")})
    return {"format": "crossmodal-files-v1", "entrypoint": manifest["entrypoint"], "files": files}


@app.post("/uploadFile")
async def upload_file(uniqueId: Optional[str] = None, file: Optional[UploadFile] = File(None)):
    try:
        uid = _uid(uniqueId)
        if file is None:
            raise ValueError("缺少上传文件 file")
        entry = await _upload(uid, file)
        return envelope(0, "success", {"entrypoint": str(entry.relative_to(task_dir(OUTPUT_ROOT, uid))).replace("\\", "/")})
    except Exception as exc:
        return envelope(1, str(exc), {}, 422)


# --------------------------------------------------------------------------- #
# [v4.2] /predict 双协议：detection 别名校验 + 形态一（单文件流）
# --------------------------------------------------------------------------- #

#: detection/operatorId 别名空间（大小写不敏感）：M03/03/3/英文ID/目录名
DETECTION_ALIASES = {"filter_irrelevant_data", "data_prescreen", "m03", "03", "3",
                     "cma_filter_irrelevant_data"}

#: 媒体/非 JSON 上传类型（本算子只收 JSON/JSONL 清单）
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
                  ".wav", ".mp3", ".flac", ".aiff",
                  ".mp4", ".mov", ".avi", ".txt", ".csv"}


def _check_detection(request: Request) -> Optional[str]:
    """detection/operatorId 别名校验（宽松）：缺失或与算子别名一致即放行，不一致报错。"""
    detection = request.query_params.get("detection")
    operator_id = request.query_params.get("operatorId")
    if detection and operator_id \
            and detection.strip().lower() != operator_id.strip().lower():
        return "detection 与 operatorId 取值不一致"
    value = (detection or operator_id or "").strip().lower()
    if value and value not in DETECTION_ALIASES:
        return "detection 参数与算子标识不符：%s" % value
    return None


def _form_overrides(form) -> Dict[str, Any]:
    """[v4.2] 形态一表单标量 → config 覆盖：白名单内字符串转数字/布尔，未知字段忽略。"""
    overrides: Dict[str, Any] = {}
    enabled = form.get("enabled")
    if enabled is not None:
        overrides["enabled"] = str(enabled).strip().lower() in ("1", "true", "yes", "on")
    seconds = form.get("duration_max_diff_sec")
    if seconds is not None:
        try:
            value = float(str(seconds).strip())
        except ValueError:
            raise ValueError("duration_max_diff_sec 必须是数字")
        overrides["prescreen"] = {"video_audio_max_duration_diff_seconds": value}
    return overrides


def _read_jsonl_safe(path: Optional[str]) -> list:
    if not path or not Path(path).is_file():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _ensure_native_input(entry: Path) -> Path:
    """[v4.2] JSONL 直接用；单条样本 JSON 包装为单记录 JSONL；其余拒绝。"""
    suffix = entry.suffix.lower()
    if suffix == ".jsonl":
        return entry
    if suffix == ".json":
        payload = json.loads(entry.read_text(encoding="utf-8"))
        records = payload if isinstance(payload, list) else [payload]
        target = entry.with_suffix(".jsonl")
        with open(target, "w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return target
    raise ValueError("该算子需要 JSON/JSONL 输入")


def _single_file_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.2] 形态一响应：判定型 status（1=保留 0=过滤）+ filter_status/reason_code。"""
    kept = _read_jsonl_safe(result.get("outputFile"))
    rejects = _read_jsonl_safe(result.get("rejectsFile"))
    if not kept and not rejects:
        return {"status": 0, "msg": "输入无有效记录"}
    if rejects:
        entry = rejects[0]
        reason = str(entry.get("reason") or "rejected")
        return {"status": 0, "process_status": "failed",
                "filter_status": "quarantine", "reason_code": reason,
                "sample_id": entry.get("sample_id"),
                "msg": "初筛过滤：%s" % reason}
    record = kept[0]
    text = record.get("text")
    has_text = bool(text) if isinstance(text, str) else bool(
        isinstance(text, dict) and (text.get("content") or text.get("original")))
    process_status = record.get("process_status", "success")
    if has_text:
        return {"status": 1, "process_status": process_status,
                "filter_status": "pass",
                "reason_code": "keyword_or_duration_check_passed",
                "sample_id": record.get("sample_id"), "msg": "success"}
    return {"status": 1, "process_status": process_status,
            "filter_status": "need_description", "reason_code": "no_text_evidence",
            "sample_id": record.get("sample_id"), "msg": "success"}


async def _predict_single_file(request: Request) -> JSONResponse:
    """[v4.2] 形态一（单文件流）：multipart/form-data + file 必传。"""
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        return JSONResponse({"status": 0, "msg": "缺少上传文件 file"})
    uid_raw = (request.query_params.get("uniqueId") or form.get("uniqueId")
               or "upload_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    try:
        uid = _uid(str(uid_raw))
        filename = Path(upload.filename or "input.jsonl").name
        if Path(filename).suffix.lower() in MEDIA_SUFFIXES:
            return JSONResponse({"status": 0, "msg": "该算子需要 JSON/JSONL 输入"})
        task = task_dir(OUTPUT_ROOT, uid)
        input_dir = task / "input"
        if input_dir.exists():
            shutil.rmtree(input_dir)
        input_dir.mkdir(parents=True)
        payload = await upload.read()
        if not payload:
            return JSONResponse({"status": 0, "msg": "上传内容为空"})
        target = input_dir / filename
        target.write_bytes(payload)
        is_package = False
        if zipfile.is_zipfile(target):
            is_package = True
            staging = input_dir / ".zip_staging"
            _safe_extract(target, staging)
            target.unlink()
            for child in sorted(staging.rglob("*")):
                if child.is_file():
                    relative = child.relative_to(staging)
                    destination = input_dir / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(child), str(destination))
            shutil.rmtree(staging, ignore_errors=True)
            try:
                entry = form1_package.resolve_entrypoint(input_dir)
            except ValueError:
                try:
                    entry = _entry(input_dir)
                except ValueError:
                    # [v4.2] 找不到 *.jsonl 时允许唯一的 *.json 作为入口
                    jsons = sorted(p for p in input_dir.rglob("*.json")
                                   if p.name != "manifest.json")
                    if len(jsons) != 1:
                        raise
                    entry = jsons[0]
        else:
            entry = target
        native = _ensure_native_input(entry)
        overrides = _form_overrides(form)
        result = inference.run(str(native), uid, output_root=str(OUTPUT_ROOT),
                               overrides=overrides or None)
    except Exception as exc:
        return JSONResponse({"status": 0, "msg": str(exc)[:200]})
    if result.get("status") == "failed":
        return JSONResponse({"status": 0, "msg": result.get("reason", "处理失败")})
    if is_package:
        # [v4.3] 压缩包模式：复用形态二的打包逻辑，返回容器内路径引用 + 可直传压缩包
        try:
            run_dir, manifest = _pack(task, result)
            reference = form1_package.build_reference(
                run_dir, manifest.get("entrypoint"))
        except Exception as exc:  # noqa: BLE001 - 打包失败不影响主结果，降级为仅返回状态
            detail = _single_file_response(result)
            detail["uniqueId"] = uid
            detail["package_error"] = str(exc)[:200]
            detail["status"] = 0
            return JSONResponse(detail)
        detail = _single_file_response(result)
        business = {k: v for k, v in detail.items() if k != "status"}
        return JSONResponse(form1_package.package_payload(
            result, reference, operator="filter_irrelevant_data",
            unique_id=uid, business=business))
    detail = _single_file_response(result)
    detail.setdefault("uniqueId", uid)
    return JSONResponse(detail)


@app.post("/predict")
async def predict(request: Request):
    alias_error = _check_detection(request)
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        # [v4.2] 形态一：单文件流协议
        if alias_error:
            return JSONResponse({"status": 0, "msg": alias_error})
        return await _predict_single_file(request)
    if alias_error:
        return envelope(1, alias_error, {}, 400)
    try:
        model = _parse_predict(await request.json())
        uid = model["uniqueId"]
        task = task_dir(OUTPUT_ROOT, uid)
        input_path = Path(model["inputFile"]) if model.get("inputFile") else _entry(task / "input")
        if not input_path.is_file():
            raise ValueError("输入文件不存在")
        result = inference.run(str(input_path), uid, output_root=str(OUTPUT_ROOT),
                               overrides=model.get('config'))
        if result.get("status") == "failed":
            return envelope(1, result.get("reason", "处理失败"), {"status": "failed"})
        run_dir, manifest = _pack(task, result)
        # [v4.2] 血缘/阶段归档已停用：mty_compliance 不再调用。
        # from utils import mty_compliance as _mty_compliance
        # _mty_compliance.annotate_node(node, model["isLastOperator"])
        # _mty_compliance.prepare_stage_result(result, model["isLastOperator"], run_dir=run_dir, manifest=manifest)
        # [v4.2] 形态二响应统一：不再区分阶段末/非阶段末，始终返回 outputFile，去除 lineageFile。
        return envelope(0, "success", {
            "operator": "filter_irrelevant_data",
            "status": "success",
            "outputFile": _file_object(run_dir, manifest),
            "result": {
                "process_status": result.get("status"),
                "statistics": result.get("statistics"),
            },
        })
    except Exception as exc:
        return envelope(1, str(exc), {}, 400)


@app.post("/removeFile")
async def remove_file(uniqueId: Optional[str] = None):
    try:
        uid = _uid(uniqueId)
        task = task_dir(OUTPUT_ROOT, uid, create=False)
        if task.exists():
            shutil.rmtree(task)
            return envelope(0, "success", {"deleted": True})
        return envelope(1, "任务不存在或已删除", {"deleted": False})
    except Exception as exc:
        return envelope(1, str(exc), {}, 400)


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """[D1] 单算子存活探针：算子标识/状态（与已有算子 /health 风格一致）。"""
    return envelope(0, "ok", {"operator": inference.OPERATOR,
                              "operatorVersion": inference.VERSION,
                              "outputRoot": str(OUTPUT_ROOT)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8102)
