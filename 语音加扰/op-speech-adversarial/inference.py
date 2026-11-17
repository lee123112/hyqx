#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语音加扰（语音对抗平台）算子包 · 主推理服务入口。

一个进程承载 11 个算子，按 ``operator`` 字段路由到各算子目录下的 ``processor.py``。
对外提供 6 个接口，包括按服务返回路径下载文件：

===========================  ============================================================
方法 / 路径                   说明
===========================  ============================================================
``GET  /health``             容器探活
``GET  /operators``          11 个算子注册表 + 每个算子的 ``paramsSpec``（前端可据此生成表单）
``POST /uploadFile``         上传 zip / 单文件，返回容器内路径
``POST /predict``            调用指定算子
``GET/POST /downloadFile``   按 path 下载 /data 内的单个文件（兼容 filePath）
``POST /removeFile``         删除本任务的容器内目录
===========================  ============================================================

响应统一 ``{code, msg, data, logs}``：``code=0`` 成功 / ``code=1`` 失败。
**HTTP 状态码**：业务结果一律 200；只有「请求信封本身坏了」（未知算子、缺 uniqueId、
缺 file 字段）才回 400。未预期异常也会被转成 200 + ``code=1`` + ``internal_error:``，
不会漏出 500 堆栈。

命令行
------
``python inference.py --serve --host 0.0.0.0 --port 8000``   起服务（Docker CMD 用）
``python inference.py --list``                                列出 11 个算子
``python inference.py --operator 1 --input a.zip --params '{...}'``  单算子冒烟（打印 JSON）
``python inference.py --operator 语料入库 --input a.zip``      支持用 tag / operatorId / 编号
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import PACKAGE_VERSION, SERVICE_NAME  # noqa: E402
from common import config as cfgmod  # noqa: E402
from common import iofs  # noqa: E402
from common import lineage as lin  # noqa: E402
from common import schemas  # noqa: E402
from common.context import OperatorContext  # noqa: E402
from common.logger import RunLog  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
LOGGER = logging.getLogger("avp.inference")

MAP_PATH = ROOT / "map.json"
DATA_ROOT = Path(os.environ.get("AVP_DATA_ROOT", str(ROOT / "data")))
API_REVISION = "2026-11-13-download-v1"
INLINE_CONTENT_MAX = 512 * 1024
INLINE_SUFFIXES = (".json", ".jsonl", ".md", ".html", ".txt", ".csv", ".tsv")


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
def load_map(path: Optional[Path] = None) -> dict:
    payload = iofs.read_json(path or MAP_PATH)
    if "operators" not in payload or not isinstance(payload["operators"], dict):
        raise RuntimeError("map.json 缺少 operators 字典")
    return payload


REG = load_map()
OPERATORS: Dict[str, dict] = REG["operators"]
_SERVICE_PORT = int(REG.get("server", {}).get("listenPort", 17023))


def _alias_index() -> Dict[str, str]:
    """把目录名 / operatorId / englishName / tag / 编号 全部映射到目录名 key。"""
    index: Dict[str, str] = {}
    for key, meta in OPERATORS.items():
        index[key.casefold()] = key
        for field in ("operatorId", "englishName", "tag", "directory"):
            value = meta.get(field)
            if value:
                index[str(value).casefold()] = key
        num = meta.get("formalOperatorNumber")
        if num is not None:
            index[str(num)] = key
            index["%02d" % int(num)] = key
            index["0%02d" % int(num)] = key
    for alias, key in (REG.get("aliases") or {}).items():
        if key in OPERATORS:
            index[str(alias).casefold()] = key
    return index


ALIAS_INDEX = _alias_index()


def resolve_operator(value) -> str:
    """把任意写法解析成算子目录名；未知抛 ``unknown_operator``。"""
    if value is None or str(value).strip() == "":
        raise schemas.OpError("unknown_operator: 未提供 operator 字段")
    key = ALIAS_INDEX.get(str(value).strip().casefold())
    if key is None:
        raise schemas.OpError(
            "unknown_operator: %r，允许：%s"
            % (value, " / ".join(sorted(OPERATORS.keys())))
        )
    return key


_PROC_CACHE: Dict[str, Any] = {}


def load_processor(op_key: str):
    """按算子目录加载其 ``processor.py``（带缓存）。"""
    if op_key in _PROC_CACHE:
        return _PROC_CACHE[op_key]
    meta = OPERATORS[op_key]
    rel = meta.get("processor") or "%s/processor.py" % op_key
    path = (ROOT / rel).resolve()
    if not path.is_file():
        raise schemas.OpError("internal_error: 算子实现缺失 %s" % rel)
    mod_name = "avp_proc_%s" % op_key.lower()
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover
        raise schemas.OpError("internal_error: 无法加载 %s" % rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise schemas.OpError("internal_error: %s 缺少 run(ctx)" % rel)
    _PROC_CACHE[op_key] = module
    return module


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _safe_unique_id(unique_id: str) -> str:
    text = str(unique_id or "").strip()
    if not text:
        raise schemas.OpError("invalid_params: uniqueId 不能为空")
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in text)
    cleaned = cleaned.strip("._") or "default"
    return cleaned[:128]


def work_dir_of(unique_id: str) -> Path:
    return iofs.ensure_dir(DATA_ROOT / _safe_unique_id(unique_id))


def input_dir_of(unique_id: str) -> Path:
    return iofs.ensure_dir(work_dir_of(unique_id) / "input")


# --------------------------------------------------------------------------- #
# 核心执行
# --------------------------------------------------------------------------- #
def _rel_to_data(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return "/data/" + Path(path).resolve().relative_to(DATA_ROOT.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _from_data(path: Any) -> Optional[str]:
    """把对外展示路径反解回本机真实路径。

    契约要求下一个算子传上一算子回的 ``data.lineageFile.outputFile``，而那是
    ``/data/{uniqueId}/tmp/001/001_corpus.jsonl`` 这种**容器内约定路径**。
    同一个服务自己必须能把它解回来，否则跨算子串联直接断——这是必须成对出现的映射。

    兼容三类输入：``/data/...``（容器路径）、``/data`` 前缀之外的本机绝对路径、
    以及调用方自己给的相对路径（交给算子按工作目录解析）。
    """
    if path in (None, ""):
        return None
    text = str(path).replace("\\", "/")
    if text == "/data":
        return str(DATA_ROOT)
    if text.startswith("/data/"):
        return str(DATA_ROOT / text[len("/data/"):])
    # Windows 盘符路径不能被当成 URL 处理
    return str(path)


def _download_path(value: Any, unique_id: Any = None) -> Path:
    """Resolve a returned file path without creating directories or escaping DATA_ROOT."""
    if not isinstance(value, str) or not value.strip():
        raise schemas.OpError("invalid_params: path/filePath 必须是非空字符串", http_status=400)
    if unique_id is not None and (not isinstance(unique_id, str) or not unique_id.strip()):
        raise schemas.OpError("invalid_params: uniqueId 必须是非空字符串", http_status=400)
    text = value.strip().replace("\\", "/")
    data_root = DATA_ROOT.resolve()
    if text == "/data":
        candidate = data_root
    elif text.startswith("/data/"):
        candidate = data_root / text[len("/data/"):]
    else:
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = data_root / candidate
    try:
        candidate = candidate.resolve()
        candidate.relative_to(data_root)
        if unique_id is not None:
            candidate.relative_to((data_root / _safe_unique_id(unique_id)).resolve())
    except (ValueError, RuntimeError):
        raise schemas.OpError("path_escape: 只能下载数据根目录及指定任务内的文件", http_status=403)
    if candidate.is_dir():
        raise schemas.OpError("invalid_params: downloadFile 只支持单个文件，请传产物文件路径", http_status=400)
    if not candidate.is_file():
        raise schemas.OpError("missing_file: 文件不存在，请重新上传或检查本次算子的返回路径", http_status=404)
    if not os.access(candidate, os.R_OK):
        raise schemas.OpError("permission_denied: 文件不可读", http_status=403)
    return candidate


def run_operator(
    op_key: str,
    unique_id: str,
    params: Optional[dict] = None,
    input_file: Any = None,
    is_last: bool = False,
    prior_lineage: Any = None,
) -> dict:
    """执行一次算子，返回 ``{code,msg,data,logs}``。所有异常都在这里收口。"""
    meta = OPERATORS[op_key]
    op_id = meta.get("operatorId", op_key)
    op_name = meta.get("name", op_key)
    op_version = meta.get("version") or REG.get("version", PACKAGE_VERSION)
    log = RunLog(op_name)
    started = lin.now_iso()

    try:
        work = work_dir_of(unique_id)
        number = int(meta.get("formalOperatorNumber", 0))
        out_dir = iofs.ensure_dir(work / "tmp" / ("%03d" % number))

        resolved_input: Optional[Path] = None
        if input_file not in (None, ""):
            # 先把 /data/... 这类对外展示路径反解回本机真实路径（跨算子串联的关键）
            candidate = Path(str(_from_data(input_file)))
            if not candidate.is_absolute():
                candidate = (work / candidate).resolve()
            resolved_input = candidate
        else:
            resolved_input = _default_input(work)

        config = cfgmod.load(ROOT / op_key)
        ctx = OperatorContext(
            operator_id=op_id,
            operator_name=op_name,
            operator_version=op_version,
            params=dict(params or {}),
            unique_id=_safe_unique_id(unique_id),
            work_dir=work,
            out_dir=out_dir,
            log=log,
            input_file=resolved_input,
            is_last=bool(is_last),
            config=config,
            prior_lineage=prior_lineage,
        )

        log.start()
        log.route("%s（%s）" % (op_id, op_key))
        if resolved_input is not None:
            log.load_input(resolved_input.name)

        module = load_processor(op_key)
        result = module.run(ctx) or {}
        if not isinstance(result, dict):
            raise schemas.OpError("internal_error: processor.run 必须返回 dict")
        outputs: Dict[str, Path] = result.get("outputs") or {}
        statistics = result.get("statistics") or {}
        status = str(result.get("status") or "success")

        if not outputs:
            raise schemas.OpError("internal_error: processor 未产出任何文件")
        for name, path in outputs.items():
            if not Path(path).exists():
                raise schemas.OpError("internal_error: 产物 %s 未生成" % name)
        log.done()

        finished = lin.now_iso()
        record = lin.make_record(
            operator=op_id,
            operator_version=op_version,
            unique_id=ctx.unique_id,
            input_file=_rel_to_data(resolved_input),
            output_file=_rel_to_data(next(iter(outputs.values()))),
            status=status,
            started_at=started,
            finished_at=finished,
            config=cfgmod.snapshot(config),
            statistics=statistics,
            operatorDirectory=op_key,
            operatorName=op_name,
            outputs={name: _rel_to_data(p) for name, p in outputs.items()},
        )
        records = lin.append_record(lin.load_lineage(prior_lineage), record) if prior_lineage else [record]
        lineage_payload = lin.build_payload(records, is_last=bool(is_last))

        data: Dict[str, Any] = {
            "operator": op_id,
            "operatorDirectory": op_key,
            "name": op_name,
            "version": op_version,
            "status": status,
            "statistics": statistics,
            "outputs": {name: _rel_to_data(p) for name, p in outputs.items()},
            "lineageFile": lineage_payload,
        }
        if "result" in result:
            data["result"] = result["result"]
        if is_last:
            data["outputFile"] = _output_file_bundle(outputs)
        return schemas.ok(data=data, logs=log.lines())

    except schemas.OpError as exc:
        msg = str(exc.msg)
        log.error(op_name, _short(msg))
        body = schemas.fail(msg, data={"logs": log.lines()}, logs=log.lines())
        if msg.startswith(("unknown_operator:", "missing_file: 未找到上传")):
            body["_http_status"] = 400
        return body
    except Exception as exc:  # noqa: BLE001 - 统一收口，不漏 500
        LOGGER.error("operator %s failed: %s", op_key, exc)
        LOGGER.debug(traceback.format_exc())
        log.error(op_name, "内部错误")
        return schemas.fail(
            "internal_error: %s: %s" % (type(exc).__name__, _short(exc)),
            data={"logs": log.lines()},
            logs=log.lines(),
        )


def _default_input(work: Path) -> Optional[Path]:
    """未传 inputFile 时：优先用解压目录，其次工作目录下唯一的输入文件。"""
    extracted = work / "input" / "extracted"
    if extracted.is_dir():
        return extracted
    inp = work / "input"
    if inp.is_dir():
        files = [p for p in sorted(inp.iterdir()) if p.is_file() and not p.name.startswith(".")]
        if len(files) == 1:
            return files[0]
        dirs = [p for p in sorted(inp.iterdir()) if p.is_dir()]
        if dirs:
            return dirs[0]
    return None


def _short(value: Any, limit: int = 120) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _output_file_bundle(outputs: Dict[str, Path]) -> dict:
    """阶段末 ``data.outputFile``：文件对象列表 + 小文本的内联内容。"""
    bundle: Dict[str, Any] = {"main": None, "files": {}, "content": {}}
    for name, path in outputs.items():
        p = Path(path)
        item = {
            "path": _rel_to_data(p),
            "bytes": p.stat().st_size,
            "sha256": None,
        }
        try:
            from common.audio import sha256_file

            if p.suffix.lower() not in (".wav", ".mp3", ".flac", ".aiff"):
                item["sha256"] = sha256_file(p)
        except Exception:  # noqa: BLE001 - 哈希失败不影响交付
            pass
        bundle["files"][name] = item
        if bundle["main"] is None:
            bundle["main"] = name
        if p.suffix.lower() in INLINE_SUFFIXES and p.stat().st_size <= INLINE_CONTENT_MAX:
            try:
                bundle["content"][name] = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                pass
    return bundle


# --------------------------------------------------------------------------- #
# FastAPI 应用
# --------------------------------------------------------------------------- #
def create_app():
    app = FastAPI(
        title=SERVICE_NAME,
        version=PACKAGE_VERSION,
        description="语音加扰（语音对抗平台）算子包：11 个算子统一入口",
    )

    def _respond(payload: dict) -> JSONResponse:
        status = int(payload.pop("_http_status", 200))
        return JSONResponse(status_code=status, content=payload)

    def _fail(message, status: int = 200, data: Optional[dict] = None) -> JSONResponse:
        """统一失败信封。传 ``OpError`` 时沿用它的 http_status。"""
        if isinstance(message, schemas.OpError):
            msg, status = message.msg, message.http_status
        else:
            msg = str(message)
        payload = schemas.fail(msg, data=data or {})
        payload["_http_status"] = status
        return _respond(payload)

    @app.get("/health")
    def health():
        return {
            "status": 1,
            "service": SERVICE_NAME,
            "version": PACKAGE_VERSION,
            "operators": len(OPERATORS),
            "dataRoot": str(DATA_ROOT),
            "apiRevision": API_REVISION,
            "capabilities": {"downloadFile": True},
        }

    @app.get("/operators")
    def list_operators():
        items = []
        for key, meta in sorted(OPERATORS.items(), key=lambda kv: kv[1].get("formalOperatorNumber", 0)):
            items.append(
                {
                    "directory": key,
                    "number": meta.get("formalOperatorNumber"),
                    "operatorId": meta.get("operatorId"),
                    "name": meta.get("name"),
                    "tag": meta.get("tag"),
                    "stage": meta.get("category"),
                    "stageId": meta.get("stageId"),
                    "stagePosition": meta.get("stagePosition"),
                    "isLastOfStage": meta.get("isLastOfStage", False),
                    "summary": meta.get("summary"),
                    "outputs": meta.get("outputs", {}),
                    "paramsSpec": meta.get("paramsSpec", {}),
                }
            )
        return {"code": 0, "msg": "success", "data": {"count": len(items), "operators": items}}

    @app.post("/uploadFile")
    async def upload_file(request: Request, file: UploadFile = File(...)):
        try:
            unique_id = request.query_params.get("uniqueId")
            if not unique_id:
                raise schemas.OpError("invalid_params: 缺少查询参数 uniqueId", http_status=400)
            work = work_dir_of(unique_id)
            inp = input_dir_of(unique_id)
            filename = Path(file.filename or "input.bin").name
            target = iofs.safe_join(inp, filename)
            with target.open("wb") as fh:
                shutil.copyfileobj(file.file, fh)
            size = target.stat().st_size
            if size == 0:
                target.unlink(missing_ok=True)
                raise schemas.OpError("missing_file: 上传内容为空", http_status=400)

            input_path = target
            extracted = None
            if target.suffix.lower() == ".zip":
                extracted = iofs.ensure_dir(inp / "extracted")
                try:
                    files = iofs.safe_extract_zip(target, extracted)
                except ValueError as exc:
                    raise schemas.OpError("missing_file: zip 解压失败：%s" % exc, http_status=400) from exc
                if not files:
                    raise schemas.OpError("missing_file: zip 内没有文件", http_status=400)
                input_path = extracted

            LOGGER.info("uploaded uniqueId=%s file=%s bytes=%d", unique_id, filename, size)
            return {
                "code": 0,
                "msg": "success",
                "data": {
                    "uniqueId": unique_id,
                    "inputFile": _rel_to_data(input_path),
                    "archive": _rel_to_data(target),
                    "bytes": size,
                    "workDir": _rel_to_data(work),
                },
            }
        except schemas.OpError as exc:
            return _fail(exc)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("upload failed: %s", exc)
            return _fail("internal_error: %s" % _short(exc))

    @app.post("/predict")
    async def predict(request: Request):
        try:
            try:
                body = await request.json()
            except Exception as exc:  # noqa: BLE001
                raise schemas.OpError("invalid_params: 请求体必须是 JSON 对象") from exc
            if not isinstance(body, dict):
                raise schemas.OpError("invalid_params: 请求体必须是 JSON 对象")

            raw_op = body.get("operator") or body.get("operatorId") or body.get("tag")
            try:
                op_key = resolve_operator(raw_op)
            except schemas.OpError as exc:
                return _fail(exc.msg, status=400)

            unique_id = body.get("uniqueId")
            if not unique_id:
                return _fail("invalid_params: 缺少 uniqueId", status=400)

            params = body.get("params")
            if params is None:
                params = {
                    k: v
                    for k, v in body.items()
                    if k
                    not in (
                        "operator",
                        "operatorId",
                        "tag",
                        "uniqueId",
                        "isLastOperator",
                        "inputFile",
                        "lineageFile",
                        "params",
                    )
                }
            if not isinstance(params, dict):
                return _fail("invalid_params: params 必须是 JSON 对象", status=400)

            payload = run_operator(
                op_key=op_key,
                unique_id=unique_id,
                params=params,
                input_file=body.get("inputFile"),
                is_last=bool(body.get("isLastOperator", False)),
                prior_lineage=body.get("lineageFile"),
            )
            return _respond(payload)
        except schemas.OpError as exc:
            return _fail(exc)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("predict failed: %s", exc)
            return _fail("internal_error: %s" % _short(exc))

    @app.get("/downloadFile", response_class=FileResponse)
    @app.post("/downloadFile", response_class=FileResponse)
    async def download_file(request: Request, path: Optional[str] = None,
                            filePath: Optional[str] = None, uniqueId: Optional[str] = None):
        try:
            fields = dict(request.query_params)
            if request.method == "POST" and await request.body():
                try:
                    body = await request.json()
                except Exception as exc:
                    raise schemas.OpError("invalid_params: 请求体必须是 JSON 对象", http_status=400) from exc
                if not isinstance(body, dict):
                    raise schemas.OpError("invalid_params: 请求体必须是 JSON 对象", http_status=400)
                fields.update(body)
            values = [fields[key] for key in ("filePath", "path", "inputFile") if key in fields]
            if not values:
                raise schemas.OpError("invalid_params: 缺少 path（兼容 filePath/inputFile）", http_status=400)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise schemas.OpError("invalid_params: path/filePath 必须是非空字符串", http_status=400)
            if len(set(values)) > 1:
                raise schemas.OpError("invalid_params: 文件路径参数互相冲突", http_status=400)
            target = _download_path(values[0], fields.get("uniqueId"))
            media_type = "application/x-ndjson" if target.suffix.lower() == ".jsonl" else None
            return FileResponse(target, media_type=media_type, filename=target.name)
        except schemas.OpError as exc:
            return _fail(exc)
        except OSError as exc:
            LOGGER.error("download failed: %s", exc)
            return _fail("internal_error: 文件读取失败", status=500)

    @app.post("/removeFile")
    async def remove_file(request: Request):
        try:
            unique_id = request.query_params.get("uniqueId")
            if not unique_id:
                raise schemas.OpError("invalid_params: 缺少查询参数 uniqueId", http_status=400)
            work = work_dir_of(unique_id)
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            return {"code": 0, "msg": "success", "data": {"removed": _rel_to_data(work)}}
        except schemas.OpError as exc:
            return _fail(exc)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("removeFile failed: %s", exc)
            return _fail("internal_error: %s" % _short(exc))

    return app


app = create_app()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="语音加扰算子包")
    parser.add_argument("--serve", action="store_true", help="启动 HTTP 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--list", action="store_true", help="列出算子")
    parser.add_argument("--operator", default=None, help="算子目录名 / operatorId / tag / 编号")
    parser.add_argument("--input", default=None, help="输入文件或目录（单算子冒烟）")
    parser.add_argument("--params", default="{}", help="业务参数 JSON 字符串")
    parser.add_argument(
        "--params-file",
        default=None,
        help="业务参数 JSON 文件路径。**Windows/PowerShell 下推荐用这个**，避免引号被 shell 吃掉",
    )
    parser.add_argument("--unique-id", default="cli_smoke", help="单算子冒烟用的 uniqueId")
    parser.add_argument("--is-last", action="store_true", help="标记为阶段末算子")
    args = parser.parse_args(argv)

    if args.list:
        for key, meta in sorted(OPERATORS.items(), key=lambda kv: kv[1].get("formalOperatorNumber", 0)):
            print("%-22s %-2s %-16s %s" % (key, meta.get("formalOperatorNumber"), meta.get("tag"), meta.get("name")))
        return 0

    if args.serve:
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port, log_level=os.environ.get("LOG_LEVEL", "info").lower())
        return 0

    if not args.operator:
        parser.print_help()
        return 2

    try:
        if args.params_file:
            params: dict = json.loads(Path(args.params_file).read_text(encoding="utf-8-sig"))
        else:
            params = json.loads(args.params or "{}")
    except json.JSONDecodeError as exc:
        print(
            json.dumps(
                {
                    "code": 1,
                    "msg": "invalid_params: 参数不是合法 JSON：%s（PowerShell 下请改用 --params-file）" % exc,
                },
                ensure_ascii=False,
            )
        )
        return 2
    except OSError as exc:
        print(json.dumps({"code": 1, "msg": "missing_file: 参数文件读取失败：%s" % exc}, ensure_ascii=False))
        return 2
    if not isinstance(params, dict):
        print(json.dumps({"code": 1, "msg": "invalid_params: 参数必须是 JSON 对象"}, ensure_ascii=False))
        return 2
    input_file = None
    if args.input:
        src = Path(args.input).resolve()
        if not src.exists():
            print(json.dumps({"code": 1, "msg": "missing_file: --input 不存在：%s" % args.input}, ensure_ascii=False))
            return 2
        if src.is_dir():
            # 目录直接用（单算子冒烟最常用）
            input_file = src
        elif src.suffix.lower() == ".zip":
            dest = input_dir_of(args.unique_id) / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            input_file = iofs.ensure_dir(input_dir_of(args.unique_id) / "extracted")
            iofs.safe_extract_zip(dest, input_file)
        else:
            dest = input_dir_of(args.unique_id) / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            input_file = dest

    payload = run_operator(
        op_key=resolve_operator(args.operator),
        unique_id=args.unique_id,
        params=params,
        input_file=input_file,
        is_last=args.is_last,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("code") == 0 else 1


if __name__ == "__main__":
    sys.exit(_cli())
