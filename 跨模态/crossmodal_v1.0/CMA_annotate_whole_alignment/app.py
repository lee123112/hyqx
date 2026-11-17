"""算子 #16 HTTP 接口层：/uploadFile、/predict、/removeFile（不含 /downloadFile）。

契约（DEVELOPMENT_CONTRACT §2/§3、通用提示词 §三、V4.2 §2.7 双协议）：
- 外层统一 ``{"code": 0|1, "msg": "...", "data": {...}}``；
- ``/predict`` 形态二（application/json）必传 ``uniqueId`` 与严格布尔 ``isLastOperator``
  （[v4.2] isLastOperator 保留严格校验，但不再改变响应形态）；
- [v4.2] 形态二响应去 lineageFile，统一返回 outputFile（crossmodal-files-v1）+ result；
- [v4.2] 新增形态一（multipart/form-data 单文件流）：``POST /predict?detection=annotate_whole_alignment``，
  返回 JSON 首字段 int ``status``（处理型：1 成功/0 失败）+ 业务字段；
- 只读写 ``OUTPUT_DIR/<uniqueId>/``，按 uniqueId 隔离，拒绝目录穿越；
- 人工回写事件文件随素材包一起上传（``16_human_events.jsonl`` / ``human_events.jsonl``）。
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
from utils import alignment as al  # noqa: E402
from utils import form1_package  # noqa: E402
from utils import io as op_io  # noqa: E402
from utils import transport  # noqa: E402
# [v4.2] mty_compliance 已停用：不再导入（血缘/阶段归档/运输输出停用）
# from utils import mty_compliance
from utils.logger import log  # noqa: E402
from utils.schemas import ALLOWED_CONFIG_KEYS, validate_predict_payload  # noqa: E402

OUTPUT_ROOT = Path(os.environ.get("OUTPUT_DIR", str(BASE / "output"))).expanduser().resolve()
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="算子 #16 标注整体对齐关系", version=inference.VERSION)

#: 阶段末需要打包的文件（入口在主清单之外的全部产物 + 控制文件）
PACK_EXTRA = (inference.REVIEW_NAME, inference.ISOLATION_NAME,
              inference.NOT_APPLICABLE_NAME, inference.REJECTS_NAME,
              inference.AUDIT_NAME, "manifest.json",
              # [v4.2] 血缘文件已停用：不再打包 lineage.json / stage_lineage.json
              "config.snapshot.json")


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


def _extract_upload(filename: str, content: bytes, input_dir: Path) -> str:
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
        suffix = Path(filename or "candidate_pairs.jsonl").suffix or ".jsonl"
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
                relative = _extract_upload(file.filename or "", content, input_dir)
            except ValueError:
                # 素材非法时整任务回滚，避免留下半份包
                shutil.rmtree(str(task), ignore_errors=True)
                raise
            meta = {"uniqueId": uid, "filename": file.filename, "entry": relative,
                    "bytes": len(content),
                    "humanEventsFile": None,
                    "uploadedAt": __import__("datetime").datetime.now().isoformat(
                        timespec="seconds")}
            events = inference.resolve_human_events_path(input_dir, None)
            if events is not None:
                meta["humanEventsFile"] = events.name
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
               "bytes": len(content), "humanEventsFile": meta["humanEventsFile"]})


def _reuse_own_run(task: Path, entry: Path):
    """当 inputFile 指向本算子上次运行的产物时直接复用，避免重复计算。

    只接受 ``OUTPUT_DIR/<uniqueId>/16_whole_alignment/runNNN/16_whole_alignment.jsonl``
    这种"自己的输出"；其他路径一律按新输入处理。
    [v4.2] 血缘输出已停用：新运行不再产出 lineage.json，改用 manifest.json 识别既有运行。
    """
    parent = entry.parent
    if entry.name != inference.MAIN_NAME or parent.parent.name != inference.OPERATOR:
        return None
    if not (parent.name.startswith("run") and (parent / "manifest.json").is_file()):
        return None
    # [v4.2] 血缘输出已停用：不再读取 lineage.json / stage_lineage.json（代码保留注释存档）
    # node = json.loads((parent / "lineage.json").read_text(encoding="utf-8"))
    # stage_file = parent / "stage_lineage.json"
    # stage = json.loads(stage_file.read_text(encoding="utf-8")) if stage_file.is_file() else []
    manifest_file = parent / "manifest.json"
    manifest = (json.loads(manifest_file.read_text(encoding="utf-8"))
                if manifest_file.is_file() else {})
    return {"runDir": parent, "manifest": manifest}


def _pack_stage_object(run_dir: Path, manifest: dict) -> dict:
    media_files = [item for item in manifest.get("media", {}).get("carried", [])
                   if isinstance(item, str)]
    return transport.pack_file_object(
        run_dir, inference.MAIN_NAME, [inference.MAIN_NAME, *PACK_EXTRA, *media_files])


@app.post("/predict")
async def predict(request: Request) -> JSONResponse:
    """[v4.2] 双协议入口：multipart/form-data → 形态一（单文件流）；否则形态二（阶段 JSON）。"""
    ctype = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in ctype:
        return await _predict_single_file(request)
    try:
        _check_detection(request)
        payload = await request.json()
    except ValueError as exc:
        return fail(str(exc))
    except Exception:  # noqa: BLE001
        return fail("请求体必须是 JSON 对象")
    return await _predict_stage(payload)


async def _predict_stage(payload: Any) -> JSONResponse:
    """形态二：阶段文件传递协议。[v4.2] 响应去 lineageFile，统一返回 outputFile。"""
    try:
        request = validate_predict_payload(payload)
    except ValueError as exc:
        return fail(str(exc))

    try:
        uid = op_io.validate_unique_id(request["uniqueId"])
        task = op_io.task_dir(OUTPUT_ROOT, uid)
    except ValueError as exc:
        return fail(str(exc))

    # 注意：运行期锁由 inference.run 统一持有，这里不能再嵌套同一任务的锁
    # [v4.2] isLastOperator 保留严格校验（契约兼容），但不再改变响应形态
    input_dir = task / "input"
    reuse = None
    if request.get("inputFile"):
        try:
            entry = op_io.resolve_in_task(task, request["inputFile"])
        except ValueError as exc:
            return fail(str(exc))
        if not entry.is_file():
            return fail("inputFile 不存在：%s" % request["inputFile"])
        reuse = _reuse_own_run(task, entry)
    else:
        if not input_dir.is_dir():
            return fail("任务尚未上传素材，请先调用 /uploadFile")
        try:
            entry, _relative = transport.resolve_entrypoint(input_dir)
        except ValueError as exc:
            return fail(str(exc))

    if reuse is not None:
        try:
            files = _pack_stage_object(reuse["runDir"], reuse["manifest"])
        except ValueError as exc:
            return fail("打包阶段末文件对象失败：%s" % exc)
        return ok({"operator": inference.OPERATOR, "status": "success",
                   "outputFile": files,
                   "result": {"uniqueId": uid, "reused": True,
                              "statistics": reuse["manifest"].get("statistics", {})}})

    events = inference.resolve_human_events_path(Path(entry).parent, None)
    result = inference.run(str(entry), uid, output_root=str(OUTPUT_ROOT),
                           overrides=request.get("config"),
                           human_events_path=str(events) if events else None)
    # [v4.2] mty_compliance 已停用：注释 prepare_stage_result 调用（代码保留注释存档）
    # from utils import mty_compliance as _mty_compliance
    # _mty_compliance.prepare_stage_result(result, request["isLastOperator"])
    if result["status"] == "failed":
        return fail(result.get("reason") or "运行失败",
                    data={"status": "failed", "uniqueId": uid,
                          "statistics": result.get("statistics", {})})

    try:
        files = _pack_stage_object(Path(result["runDir"]), result["manifest"])
    except ValueError as exc:
        return fail("打包阶段末文件对象失败：%s" % exc)
    return ok({"operator": inference.OPERATOR,
               "status": result["status"],
               "outputFile": files,
               "result": {"uniqueId": uid, "runId": result.get("runId"),
                          "statistics": result.get("statistics", {})}})


# ------------------------------------------------------------------ 形态一：单文件流（MTY V2）

#: detection/operatorId 别名空间（大小写不敏感）：M16/16/英文ID/目录名
DETECTION_ALIASES = {"m16", "16", "annotate_whole_alignment",
                     "cma_annotate_whole_alignment",
                     # [D2] 追加 map.json 登记表英文 operatorId（只追加，不重排既有条目）
                     "whole_alignment_annotation"}


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


def _form_overrides(form: Any) -> Dict[str, Any]:
    """形态一宽松参数：只取 config 白名单标量（字符串转数字/布尔），未知字段忽略。"""
    overrides: Dict[str, Any] = {}
    for key in ALLOWED_CONFIG_KEYS:
        raw = form.get(key)
        if raw is None or hasattr(raw, "filename"):
            continue
        text = str(raw).strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in ("true", "false"):
            overrides[key] = lowered == "true"
            continue
        try:
            overrides[key] = float(text) if ("." in text or "e" in lowered) else int(text)
        except ValueError:
            continue  # 形态一宽松：无法转换的标量忽略
    return overrides


def _ensure_native_input(uid: str, entry: Path) -> Path:
    """JSONL 原样使用；单个 JSON（对象或数组）转成 JSONL；其他文件一律拒绝。"""
    suffix = entry.suffix.lower()
    if suffix == ".jsonl":
        return entry
    if suffix == ".json":
        payload = json.loads(entry.read_text(encoding="utf-8-sig"))
        items = payload if isinstance(payload, list) else [payload]
        target = entry.with_name(entry.stem + "__native.jsonl")
        with open(str(target), "w", encoding="utf-8", newline="\n") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return target
    raise ValueError("该算子需要 JSON/JSONL 输入")


def _form1_audit(uid: str, detection: str, filename: str, size: int,
                 status: int, elapsed_ms: int) -> None:
    """[v4.2] 形态一最小调用审计：detection、文件摘要、status、耗时、时间戳。"""
    record = {"uniqueId": uid, "detection": detection,
              "file": {"name": filename, "bytes": size},
              "status": status, "elapsed_ms": elapsed_ms,
              "time": datetime.now().isoformat(timespec="seconds")}
    try:
        task = op_io.task_dir(OUTPUT_ROOT, uid)
        with open(str(task / "form1_audit.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  # 审计写盘失败不影响主流程
        pass


#: 整体结论 → 形态一 alignment_label（V4.2 §4.16：aligned/not_aligned/uncertain）
_ALIGNMENT_LABELS = {
    al.DECISION_AUTO_ALIGNED: "aligned",
    al.DECISION_MANUAL_ALIGNED: "aligned",
    al.DECISION_NOT_ALIGNED: "not_aligned",
    al.DECISION_UNCERTAIN: "uncertain",
    al.DECISION_PENDING_MANUAL_REVIEW: "uncertain",
}


def _iter_jsonl(path: Optional[str]):
    if not path or not Path(path).is_file():
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


def _first_whole_record(result: dict):
    """取本次运行首条带整体结论的记录（主清单 → 隔离台账内嵌记录）。"""
    for record in _iter_jsonl(result.get("outputFile")):
        whole = (record.get("alignment") or {}).get("whole")
        if isinstance(whole, dict) and whole.get("decision"):
            return record, whole
    for entry in _iter_jsonl(result.get("isolationFile")):
        record = entry.get("record") or {}
        whole = (record.get("alignment") or {}).get("whole")
        if isinstance(whole, dict) and whole.get("decision"):
            return record, whole
    return None, None


def _single_file_response(uid: str, result: dict) -> dict:
    """形态一业务字段（V4.2 §4.16）：alignment_label/decision_source/score/review_id。"""
    record, whole = _first_whole_record(result)
    payload: Dict[str, Any] = {"status": 1, "uniqueId": uid}
    if whole is None:
        payload.update({"process_status": "not_applicable", "pair_id": None,
                        "alignment_label": None, "decision_source": None,
                        "score": None, "review_id": None})
        return payload
    decision = whole.get("decision")
    payload.update({
        "process_status": decision,
        "pair_id": record.get("pair_id"),
        "alignment_label": _ALIGNMENT_LABELS.get(decision, "uncertain"),
        "decision_source": ("human" if whole.get("confirmation_method") == "human"
                            else "automatic"),
        "score": whole.get("clip_score"),
        "review_id": whole.get("review_task_id"),
    })
    return payload


#: [v4.3] 形态一压缩包响应的算子英文标识（与登记表/形态二口径一致）
FORM1_OPERATOR = "annotate_whole_alignment"


def _package_response(uid: str, result: Dict[str, Any],
                      detail: Dict[str, Any]) -> Dict[str, Any]:
    """[v4.3] 压缩包模式响应：status + 业务指标 + outputFile 容器内路径引用。"""
    run_root = Path(str(result.get("runDir") or result.get("run_dir") or ""))
    entry_hint = result.get("entrypoint") or Path(str(result.get("outputFile") or "")).name
    reference = form1_package.build_reference(run_root, entry_hint)
    business = {key: value for key, value in detail.items() if key != "status"}
    body = form1_package.package_payload(
        result, reference, operator=FORM1_OPERATOR, unique_id=uid, business=business)
    # package_payload 的 process_status 取运行状态；形态一的业务流程态以业务字段为准
    if "process_status" in business:
        body["process_status"] = business["process_status"]
    return body


async def _predict_single_file(request: Request) -> JSONResponse:
    """形态一：单文件流协议（multipart/form-data），处理型 status（1 成功/0 失败）。

    [v4.3] 上传内容是压缩包时按「单文件压缩包输入」处理：安全解压到 ``task/input``，
    用与形态二完全相同的 ``inference.run`` 跑阶段推理，返回 ``status`` + 业务指标 +
    ``outputFile`` 容器内路径引用，并把运行输出目录打成可直传下一算子的
    ``*_form1.zip``；非压缩包仍走原单文件单记录逻辑。
    """
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
        uid = op_io.validate_unique_id(str(uid_raw))
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    content = await upload.read()
    if not content:
        return JSONResponse({"status": 0, "msg": "上传内容为空"})

    filename = upload.filename or "upload.jsonl"
    is_package = zipfile.is_zipfile(io.BytesIO(content))
    try:
        with op_io.task_lock(OUTPUT_ROOT, uid):
            task = op_io.task_dir(OUTPUT_ROOT, uid)
            input_dir = task / "input"
            if input_dir.exists():
                shutil.rmtree(str(input_dir))
            input_dir.mkdir(parents=True, exist_ok=True)
            if is_package:
                # [v4.3] 压缩包模式：helper 安全解包，入口优先 manifest.json
                form1_package.extract_zip_into(content, input_dir)
                try:
                    entry = form1_package.resolve_entrypoint(input_dir)
                except ValueError:
                    try:
                        _entry, relative = transport.resolve_entrypoint(input_dir)
                        entry = input_dir / relative
                    except ValueError:
                        # 兜底：允许唯一的 *.json 作为入口（manifest.json 除外）
                        jsons = sorted(path for path in input_dir.rglob("*.json")
                                       if path.name != "manifest.json")
                        if len(jsons) != 1:
                            raise
                        entry = jsons[0]
            else:
                entry = input_dir / Path(filename.replace("\\", "/")).name
                entry.write_bytes(content)
    except (ValueError, zipfile.BadZipFile, op_io.TaskBusy) as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except OSError as exc:
        return JSONResponse({"status": 0, "msg": "写入任务目录失败：%s" % type(exc).__name__})

    try:
        native = _ensure_native_input(uid, entry)
    except ValueError as exc:
        return JSONResponse({"status": 0, "msg": str(exc)})
    except json.JSONDecodeError:
        return JSONResponse({"status": 0, "msg": "JSON 文件解析失败"})

    overrides = _form_overrides(form)
    events = inference.resolve_human_events_path(native.parent, None)
    result = inference.run(str(native), uid, output_root=str(OUTPUT_ROOT),
                           overrides=overrides or None,
                           human_events_path=str(events) if events else None)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    detection = request.query_params.get("detection") \
        or request.query_params.get("operatorId") or ""
    if result.get("status") == "failed":
        _form1_audit(uid, detection, filename, len(content), 0, elapsed_ms)
        return JSONResponse({"status": 0, "msg": result.get("reason") or "处理失败"})
    payload = _single_file_response(uid, result)
    if is_package:
        # [v4.3] 压缩包模式：返回容器内路径引用 + 可直传下一算子的 *_form1.zip
        try:
            body = _package_response(uid, result, payload)
        except (OSError, ValueError) as exc:
            _form1_audit(uid, detection, filename, len(content), 0, elapsed_ms)
            return JSONResponse({"status": 0, "msg": "输出打包失败：%s" % str(exc)[:200]})
        _form1_audit(uid, detection, filename, len(content),
                     int(body.get("status", 1)), elapsed_ms)
        return JSONResponse(body)
    payload.setdefault("uniqueId", uid)
    _form1_audit(uid, detection, filename, len(content),
                 int(payload.get("status", 1)), elapsed_ms)
    return JSONResponse(payload)


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

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8116")))
