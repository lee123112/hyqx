"""算子 #12 HTTP 接口层：/uploadFile、/predict、/removeFile（不含 /downloadFile）。

契约（DEVELOPMENT_CONTRACT §2/§3、通用提示词 §三）：
- 外层统一 ``{"code": 0|1, "msg": "...", "data": {...}}``；
- ``/predict`` 必传 ``uniqueId`` 与严格布尔 ``isLastOperator``；
- ``isLastOperator=false``：``data`` 只含当前阶段中间血缘节点（带临时 outputFile 路径）；
- ``isLastOperator=true``：``data`` 返回最终 outputFile 文件内容对象 + 完整阶段血缘；
- 只读写 ``OUTPUT_DIR/<uniqueId>/``，按 uniqueId 隔离，拒绝目录穿越。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fastapi import FastAPI, File, Query, Request, UploadFile  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import inference  # noqa: E402
from utils import form1_package  # noqa: E402  [v4.3] 形态一压缩包输出引用
from utils import io as op_io  # noqa: E402
from utils import scoring as op_scoring  # noqa: E402
from utils import transport  # noqa: E402
from utils.logger import log  # noqa: E402
from utils.schemas import validate_predict_payload  # noqa: E402

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_DIR", str(BASE / "output"))).expanduser().resolve()
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="算子 #12 评分图文语义一致性", version=inference.VERSION)

#: 阶段末需要打包的文件（入口在主清单之外的全部产物）
# [v4.2] 血缘停用：lineage.json / stage_lineage.json 不再落盘，从打包清单移除
PACK_EXTRA = (inference.ISOLATION_NAME, inference.NOT_APPLICABLE_NAME,
              inference.PENDING_MODEL_NAME, inference.REJECTS_NAME,
              "manifest.json", "config.snapshot.json")


def ok(data: Any, msg: str = "success") -> JSONResponse:
    return JSONResponse(status_code=200, content={"code": 0, "msg": msg, "data": data})


def fail(msg: str, data: Optional[Any] = None, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code,
                        content={"code": 1, "msg": msg, "data": data or {}})


@app.exception_handler(RequestValidationError)
async def on_validation_error(_request, exc: RequestValidationError) -> JSONResponse:
    return fail("请求参数不合法：%s" % json.dumps(exc.errors(), ensure_ascii=False)[:300])


@app.exception_handler(Exception)
async def on_unexpected_error(_request, exc: Exception) -> JSONResponse:
    # 不回传完整堆栈，只给可定位的类型与短信息
    return fail("内部错误：%s: %s" % (type(exc).__name__, str(exc)[:200]))


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    return ok({"operator": inference.OPERATOR, "operatorVersion": inference.VERSION,
               "stageId": inference.STAGE_ID, "outputRoot": str(OUTPUT_ROOT)})


def _extract_upload(uid: str, filename: str, content: bytes, input_dir: Path) -> str:
    """安全解包素材：先整体校验成员，再落盘，避免留下半份异常素材。"""
    def safe_member(name: str) -> Path:
        relative = Path(str(name).replace("\\", "/"))
        if not str(name).strip() or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("ZIP 成员路径非法：%s" % name)
        return relative

    if content[:2] == b"PK":
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise ValueError("ZIP 包损坏：%s" % exc) from exc
        with archive:
            members = []
            seen = set()
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = safe_member(info.filename)
                key = relative.as_posix().lower()
                if key in seen:
                    raise ValueError("ZIP 内存在重复文件：%s" % info.filename)
                seen.add(key)
                members.append((relative, info))
            for relative, info in members:
                target = input_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
    else:
        # 允许直接上传单文件 JSONL（便于命令行联调）
        suffix = Path(filename or "pairs.jsonl").suffix or ".jsonl"
        target = input_dir / ("candidate_pairs" + suffix)
        target.write_bytes(content)

    _entry, relative = transport.resolve_entrypoint(input_dir)
    return relative


# ------------------------------------------------------------------ 接口

@app.post("/uploadFile")
async def upload_file(uniqueId: str = Query(default=""),
                      file: Optional[UploadFile] = File(default=None)) -> JSONResponse:
    try:
        uid = op_io.validate_unique_id(uniqueId)
    except ValueError as exc:
        return fail(str(exc))
    if file is None:
        return fail("缺少上传文件")
    try:
        content = await file.read()
    except Exception as exc:  # noqa: BLE001
        return fail("读取上传内容失败：%s" % type(exc).__name__)
    if not content:
        return fail("上传内容为空")

    try:
        with op_io.task_lock(OUTPUT_ROOT, uid):
            task = op_io.task_dir(OUTPUT_ROOT, uid)
            input_dir = task / "input"
            if input_dir.exists():
                shutil.rmtree(str(input_dir))
            input_dir.mkdir(parents=True, exist_ok=True)
            try:
                relative = _extract_upload(uid, file.filename or "", content, input_dir)
            except ValueError:
                # 素材非法时整任务回滚，避免留下半份包
                shutil.rmtree(str(task), ignore_errors=True)
                raise
            meta = {"uniqueId": uid, "filename": file.filename, "entry": relative,
                    "bytes": len(content),
                    "uploadedAt": __import__("datetime").datetime.now().isoformat(
                        timespec="seconds")}
            (input_dir / "upload_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except ValueError as exc:
        return fail(str(exc))
    except op_io.TaskBusy as exc:
        return fail(str(exc))
    except OSError as exc:
        return fail("写入任务目录失败：%s" % type(exc).__name__)

    entry_path = op_io.task_dir(OUTPUT_ROOT, uid) / "input" / relative
    log("upload", "uniqueId=%s entry=%s bytes=%d" % (uid, relative, len(content)))
    return ok({"uniqueId": uid, "inputFile": str(entry_path), "entry": relative,
               "bytes": len(content)})


def _reuse_own_run(task: Path, entry: Path):
    """当 inputFile 指向本算子上次运行的中间产物时，直接复用该运行，避免重复算。

    只接受 ``OUTPUT_DIR/<uniqueId>/image_text_semantic_scoring/runNNN/12_image_text_scores.jsonl``
    这种"自己的输出"；其他路径一律按新输入处理。
    [v4.2] 血缘停用：复用判定改以 manifest.json 为准（lineage.json 不再落盘）。
    """
    parent = entry.parent
    if entry.name != inference.MAIN_NAME or parent.parent.name != inference.OPERATOR:
        return None
    if not (parent.name.startswith("run") and (parent / "manifest.json").is_file()):
        return None
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    return {"runDir": parent, "manifest": manifest}


def _pack_stage_object(run_dir: Path, manifest: dict) -> dict:
    media_files = [item for item in manifest.get("media", {}).get("carried", [])
                   if isinstance(item, str)]
    return transport.pack_file_object(
        run_dir, inference.MAIN_NAME, [inference.MAIN_NAME, *PACK_EXTRA, *media_files])


# ------------------------------------------------------------------ [v4.2] 双协议

# [v4.2] detection/operatorId 别名空间：M12/12/英文ID/目录名（大小写不敏感）
_DETECTION_ALIASES = {"m12", "12", "score_image_text_consistency",
                      "cma_score_image_text_consistency",
                      # [D2] 追加 map.json 登记表英文 operatorId（只追加，不重排既有条目）
                      "image_text_semantic_scoring"}

# [v4.2] tier → 形态一分档名（V4.2 §4.12：auto_align/human_review/quarantine）
_BAND_BY_TIER = {op_scoring.TIER_AUTO_ALIGNED: "auto_align",
                 op_scoring.TIER_PENDING_MANUAL: "human_review",
                 op_scoring.TIER_ISOLATED: "quarantine"}


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
        raise ValueError("detection 与本算子不匹配：%s（本算子为 M12 score_image_text_consistency）"
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
    for key in sorted(op_scoring.ALLOWED_OVERRIDES):
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
    for key in ("outputFile", "isolationFile", "notApplicableFile",
                "pendingModelFile", "rejectsFile"):
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
    """[v4.2] 形态一响应（判定型）：status=1 通过/0 过滤；业务字段 score/band/model_version/thresholds。"""
    stats = result.get("statistics", {})
    cfg = result.get("config") or {}
    section = cfg.get("image_text_scoring") or {}
    judge_mode = section.get("judge_mode", "operator")
    thresholds = stats.get("thresholds") or {
        "isolation": section.get("isolation_threshold"),
        "auto_align": section.get("auto_align_threshold"),
    }
    identity = stats.get("model") or result.get("model") or {}
    model_version = identity.get("name") or "CLIP-ViT/B-32"

    entries = []

    def _collect(record: Any, pair_id: Optional[str] = None) -> None:
        score_node = record.get("image_text_score") if isinstance(record, dict) else None
        if not isinstance(score_node, dict):
            return
        entries.append({
            "pair_id": pair_id or record.get("pair_id"),
            "score": score_node.get("clip_score"),
            "band": _BAND_BY_TIER.get(score_node.get("tier")),
            "thresholds": score_node.get("thresholds") or thresholds,
        })

    if result.get("outputFile"):
        for record in _read_records(result["outputFile"]):
            _collect(record)
    if result.get("isolationFile"):
        for ledger in _read_records(result["isolationFile"]):
            _collect(ledger.get("record") or {}, pair_id=ledger.get("pair_id"))

    if stats.get("degraded"):
        process_status = "pending_model"
    elif not stats.get("route_enabled", True):
        process_status = "not_applicable"
    else:
        process_status = "success"

    quarantined = any(entry["band"] == "quarantine" for entry in entries)
    if judge_mode == "system":
        # [v4.2] §2.8：judge_mode=system 按处理型语义（1 成功/0 失败），分档由平台按任务配置执行
        status = 1
    else:
        status = 0 if quarantined else 1

    single = entries[0] if len(entries) == 1 else {}
    return {
        "status": status,
        "process_status": process_status,
        "uniqueId": uid,
        "judge_mode": judge_mode,
        "pair_id": single.get("pair_id"),
        "score": single.get("score"),
        "band": single.get("band"),
        "results": entries,
        "model_version": model_version,
        "thresholds": thresholds,
    }


def _audit_single_file(uid: str, detection: Optional[str], filename: Optional[str],
                       status: int, started: float) -> None:
    """[v4.2] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    try:
        task = op_io.task_dir(OUTPUT_ROOT, uid)
        record = {
            "detection": detection,
            "filename": filename,
            "status": status,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "event_time": datetime.now().isoformat(timespec="seconds"),
        }
        with open(str(task / "single_file_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 审计失败不影响主流程
        pass


async def _predict_single_file(request: Request) -> JSONResponse:
    """[v4.2] 形态一（单文件流）：multipart/form-data + file 字段。"""
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
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": 0, "msg": "读取上传内容失败"})
    if not content:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})
    # [v4.3] 形态一压缩包输入识别：压缩包 → 阶段推理 + 输出路径引用；单文件 → 原判定响应
    is_package = zipfile.is_zipfile(io.BytesIO(content))
    try:
        with op_io.task_lock(OUTPUT_ROOT, uid):
            task = op_io.task_dir(OUTPUT_ROOT, uid)
            input_dir = task / "input"
            if input_dir.exists():
                shutil.rmtree(str(input_dir))
            input_dir.mkdir(parents=True, exist_ok=True)
            entry = None
            try:
                if is_package:
                    # [v4.3] 压缩包：解压到 task/input，按 manifest/唯一 JSONL 定位入口
                    form1_package.extract_zip_into(content, input_dir)
                    try:
                        entry = form1_package.resolve_entrypoint(input_dir)
                    except ValueError:
                        entry, _relative = transport.resolve_entrypoint(input_dir)
                else:
                    _extract_upload(uid, upload.filename or "", content, input_dir)
            except ValueError as exc:
                shutil.rmtree(str(task), ignore_errors=True)
                # 上传媒体/非 JSON 素材：本算子只收图文候选对 JSON/JSONL/ZIP
                if "入口" in str(exc) or "manifest" in str(exc):
                    _audit_single_file(uid, detection, upload.filename, 0, started)
                    return JSONResponse({"status": 0, "msg": "该算子需要 JSON/JSONL 输入"})
                return JSONResponse({"status": 0, "msg": str(exc)})
        # 注意：运行期锁由 inference.run 统一持有，这里不能再嵌套同一任务的锁
        if entry is None:
            entry, _relative = transport.resolve_entrypoint(
                op_io.task_dir(OUTPUT_ROOT, uid) / "input")
        result = inference.run(str(entry), uid, output_root=str(OUTPUT_ROOT),
                               overrides=_form_overrides(form) or None)
        if result.get("status") == "failed":
            _audit_single_file(uid, detection, upload.filename, 0, started)
            return JSONResponse({"status": 0, "msg": result.get("reason", "处理失败")})
        _audit_single_file(uid, detection, upload.filename, 1, started)
        if is_package:
            # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
            try:
                run_root = Path(result.get("runDir") or result.get("run_dir"))
                entry_hint = result.get("flowEntrypoint") or result.get("entrypoint") or Path(
                    result.get("outputFile") or "").name
                reference = form1_package.build_reference(run_root, entry_hint)
            except Exception as exc:  # noqa: BLE001 - 打包失败降级为仅返回判定结果
                detail = _single_file_response(uid, result)
                detail["uniqueId"] = uid
                detail["package_error"] = str(exc)[:200]
                detail["status"] = 0
                return JSONResponse(detail)
            detail = _single_file_response(uid, result)
            business = {k: v for k, v in detail.items() if k != "status"}
            return JSONResponse(form1_package.package_payload(
                result, reference, operator="score_image_text_consistency",
                unique_id=uid, business=business))
        return JSONResponse(_single_file_response(uid, result))
    except op_io.TaskBusy as exc:
        return JSONResponse({"status": 0, "msg": str(exc)}, status_code=409)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": 0, "msg": str(exc)})


@app.post("/predict")
async def predict(request: Request) -> JSONResponse:
    # [v4.2] 双协议：multipart/form-data 走形态一（单文件流），其余为形态二（阶段文件传递）
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
    except ValueError as exc:
        return fail(str(exc))
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return fail("请求体必须是 JSON 对象")
    try:
        model = validate_predict_payload(payload)
    except ValueError as exc:
        return fail(str(exc))

    try:
        uid = op_io.validate_unique_id(model["uniqueId"])
        task = op_io.task_dir(OUTPUT_ROOT, uid)
    except ValueError as exc:
        return fail(str(exc))

    # 注意：运行期锁由 inference.run 统一持有，这里不能再嵌套同一任务的锁
    input_dir = task / "input"
    reuse = None
    if model.get("inputFile"):
        try:
            entry = op_io.resolve_in_task(task, model["inputFile"])
        except ValueError as exc:
            return fail(str(exc))
        if not entry.is_file():
            return fail("inputFile 不存在：%s" % model["inputFile"])
        reuse = _reuse_own_run(task, entry)
    else:
        if not input_dir.is_dir():
            return fail("任务尚未上传素材，请先调用 /uploadFile")
        try:
            entry, _relative = transport.resolve_entrypoint(input_dir)
        except ValueError as exc:
            return fail(str(exc))

    if reuse is not None:
        # [v4.2] 形态二统一响应：去 lineageFile、始终返回 outputFile
        try:
            files = _pack_stage_object(reuse["runDir"], reuse["manifest"])
        except ValueError as exc:
            return fail("打包阶段文件对象失败：%s" % exc)
        manifest = reuse["manifest"]
        return ok({
            "operator": inference.OPERATOR,
            "status": "success",
            "process_status": manifest.get("status", "success"),
            "outputFile": files,
            "result": {
                "runId": manifest.get("runId"),
                "runDir": reuse["runDir"].relative_to(task).as_posix(),
                "reused": True,
                "statistics": manifest.get("statistics", {}),
                "stageId": inference.STAGE_ID,
            },
        })

    result = inference.run(str(entry), uid, output_root=str(OUTPUT_ROOT),
                           overrides=model.get("config"))
    # [v4.2] mty_compliance（血缘/阶段归档）已停用：prepare_stage_result 调用注释，代码保留存档。
    # from utils import mty_compliance as _mty_compliance
    # _mty_compliance.prepare_stage_result(result, model["isLastOperator"])
    if result["status"] == "failed":
        return fail(result.get("reason") or "运行失败",
                    data={"status": "failed", "uniqueId": uid,
                          "statistics": result.get("statistics", {})})

    # [v4.2] 形态二统一响应：不再区分阶段末/非阶段末，去 lineageFile、始终返回 outputFile；
    # isLastOperator 仅保留严格校验（契约兼容），不再改变响应形态。
    try:
        files = _pack_stage_object(Path(result["runDir"]), result["manifest"])
    except ValueError as exc:
        return fail("打包阶段文件对象失败：%s" % exc)
    return ok({
        "operator": inference.OPERATOR,
        "status": "success",
        "process_status": result.get("status"),
        "outputFile": files,
        "result": _result_summary(task, result),
    })


@app.post("/removeFile")
async def remove_file(uniqueId: str = Query(default="")) -> JSONResponse:
    try:
        uid = op_io.validate_unique_id(uniqueId)
    except ValueError as exc:
        return fail(str(exc))
    with op_io.task_lock(OUTPUT_ROOT, uid):
        task = op_io.task_dir(OUTPUT_ROOT, uid, create=False)
        deleted = task.is_dir()
        if deleted:
            shutil.rmtree(str(task))
    log("remove", "uniqueId=%s deleted=%s" % (uid, deleted))
    return ok({"uniqueId": uid, "deleted": deleted})


if __name__ == "__main__":  # pragma: no cover - 本地直启
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8112")))
