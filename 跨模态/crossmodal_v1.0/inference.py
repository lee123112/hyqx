"""跨模态算子包 · 上层主推理服务入口（多算子打包）

上传结构（对照甲方《开发规范》「算子上层目录」）：

    crossmodal_v1.0/                      # 算子上层目录（正式版：权重随算子目录内置）
    ├── Dockerfile                        # 容器化打包文件
    ├── inference.py                      # 主推理服务入口（本文件）
    ├── weight/                           # 仅保留说明/清单；实际权重已内置到各算子目录 weight/
    ├── map.json                          # 算子映射表（编号 → 目录/端口/入口/权重内置关系）
    └── CMA_xxx/                          # 26 个算子目录：dataset/ utils/ configs/ tests/ output/ README.md

本文件职责（与示例 `pretrain_operator_demo_image/inference.py` 同构）：
1. 读 ``map.json`` 注册表，得到 26 个算子的编号 / 目录 / 端口 / 入口；
2. 进程管理：按需（``POOL_AUTOSTART=1`` 时预启动）拉起**各算子自己的服务进程**
   （``cwd=<算子目录>``，``python -m uvicorn app:app --port <map.json 中的端口>``）；
3. 统一入口：``/uploadFile``、``/predict``、``/removeFile``、``/health``、``/ready``、``/operators``
   按 ``operatorId``（或 [v4.2] ``detection``）路由到对应算子进程，原样转发请求与响应
   （含 multipart 上传与阶段末大响应体）。

这样 26 个算子**代码零改动**、三接口契约与各自单测口径完全不变；
对甲方只暴露一个镜像、一个端口、一个入口。

operatorId 三种写法等价（大小写不敏感）：

    M12 / 12 / 012                        # 表1 编号
    image_text_semantic_scoring           # 登记表算子英文 id（map.json.operatorId）
    CMA_score_image_text_consistency      # 目录名（map.json.package）

[v4.2] 另支持 MTY V2 单文件流协议的 ``?detection=`` 查询参数，与 ``operatorId`` 同别名空间：
``/predict?detection=score_image_text_consistency``（multipart/form-data + file）经池化层
按同一张注册表路由到对应算子进程。

取值优先级：``?operatorId=`` / ``?detection=`` → 请求头 ``X-Operator-Id`` / ``X-Detection``
→ ``/predict`` 请求体 JSON 的 ``operatorId`` / ``detection``
（body 里的该字段会在转发前剔除，避免子算子 pydantic 因未知字段拒收）。

环境变量：

    PORT                  池化入口端口（默认 8100，由 Dockerfile 的 CMD 传给 uvicorn）
    OUTPUT_DIR            结果落盘根目录（透传子进程；阶段间共享）
    MODEL_ROOT            可选：覆盖各算子的权重根目录（默认由各算子加载自身 weight/；
                           仅在需要外挂统一模型盘时设置）
    POOL_AUTOSTART        1=启动即拉起全部算子进程；默认 0=首次调用时惰性拉起
    POOL_ONLY             逗号分隔的编号（如 `12,13`），仅暴露/启动这些算子（裁剪镜像用）
    POOL_STARTUP_TIMEOUT  单个算子进程就绪等待秒数（默认 60）
    HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE / YOLO_OFFLINE  1=强制离线，只用各算子内置权重

本地运行：

    python inference.py --port 8100 --autostart 1
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import mimetypes
import os
import platform
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

PKG_ROOT = Path(__file__).resolve().parent
MAP_PATH = PKG_ROOT / "map.json"
SERVICE_NAME = "crossmodal-operator-pool"
VERSION = "v1.1.0"

AUTOSTART = os.getenv("POOL_AUTOSTART", "0").strip() in {"1", "true", "yes", "on"}
STARTUP_TIMEOUT = float(os.getenv("POOL_STARTUP_TIMEOUT", "60"))
ONLY = {item.strip() for item in os.getenv("POOL_ONLY", "").replace("，", ",").split(",") if item.strip()}

FORWARD_PATHS = ("/uploadFile", "/predict", "/removeFile")
# [v1.1] 路径输入：/predict?detection=... 允许用 filePath 指定本机文件，由池化入口读成文件流再转发。
# 兼容旧的 multipart file 上传，两种方式二选一；filePath 仅支持池进程可读的绝对路径。
PATH_INPUT_FIELDS = ("filePath", "file_path", "filepath")
# 转发时剔除的请求头（由 httpx 重算）
_HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
                "accept-encoding", "upgrade"}


# ------------------------------------------------------------------ 注册表

def load_registry() -> Dict[str, Dict[str, Any]]:
    """读 ``map.json``：编号 → 算子条目（含 package/port/entrypoint/operatorId）。"""
    data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    operators = data.get("operators") or {}
    if not isinstance(operators, dict) or not operators:
        raise RuntimeError("map.json 未提供 operators 注册表")
    return operators


def _candidate_aliases(key: str, entry: Dict[str, Any]) -> List[str]:
    """一个算子的全部可识别别名。"""
    aliases = [key]
    number = entry.get("formalOperatorNumber")
    if number is not None:
        try:
            num = int(number)
            aliases += [str(num), "%02d" % num, "%03d" % num, "M%02d" % num, "m%02d" % num]
        except (TypeError, ValueError):
            pass
    for field in ("operatorId", "package", "englishName"):
        value = entry.get(field)
        if not value:
            continue
        text = str(value).strip()
        if not text:
            continue
        aliases.append(text)
        # [v4.2] MTY V2 单文件流的 detection 标识 = map.json englishName 去掉 CMA_ 前缀
        # （例：M01 CMA_receive_multimodal_batch -> receive_multimodal_batch）。
        # 池化层必须识别该标识，否则平台 ?detection= 调用在路由层就被拒（工艺步 0 失败）。
        for prefix in ("CMA_", "cma_"):
            if text.startswith(prefix):
                aliases.append(text[len(prefix):])
                break
    return aliases


def build_aliases(operators: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    table: Dict[str, str] = {}
    for key, entry in operators.items():
        if ONLY:
            number = str(entry.get("formalOperatorNumber", ""))
            if key not in ONLY and number not in ONLY:
                continue
        for alias in _candidate_aliases(key, entry):
            table[alias.lower()] = key
    return table


REGISTRY: Dict[str, Dict[str, Any]] = load_registry()
ALIASES: Dict[str, str] = build_aliases(REGISTRY)


def resolve_operator_id(raw: Optional[str]) -> Optional[str]:
    """把用户给的 operatorId 归一化为 map.json 的键（M01…M26）。"""
    if raw is None:
        return None
    text = str(raw).strip().strip("/")
    if not text:
        return None
    if text in REGISTRY:
        return text
    return ALIASES.get(text.lower())


# ------------------------------------------------------------------ 子进程

class OperatorProcess:
    """一个算子的服务进程（= 该算子目录下的 app:app）。"""

    def __init__(self, key: str, entry: Dict[str, Any]) -> None:
        self.key = key
        self.entry = entry
        self.package = str(entry.get("package") or "")
        self.port = int(entry.get("port"))
        self.entrypoint = str(entry.get("entrypoint") or "app:app")
        self.operator_id = str(entry.get("operatorId") or key)
        self.name = str(entry.get("name") or self.package)
        self.proc: Optional[subprocess.Popen] = None
        self.state = "stopped"          # stopped / starting / ready / failed
        self.started_at: Optional[float] = None
        self.calls = 0
        self.failures = 0
        self.last_error: Optional[str] = None
        self._lock = asyncio.Lock()

    # ---------- 路径与环境 ----------
    @property
    def cwd(self) -> Path:
        return PKG_ROOT / self.package

    def _child_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["MULTIMODAL_POOL_CHILD"] = self.key
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(self.cwd) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    # ---------- 生命周期 ----------
    def _is_listening(self) -> bool:
        with contextlib.closing(socket.socket()) as sock:
            sock.settimeout(0.4)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    async def ensure_started(self) -> None:
        """确保算子进程在跑；失败时状态置 failed 并抛出 RuntimeError。"""
        async with self._lock:
            if self.proc is not None and self.proc.poll() is None and self._is_listening():
                self.state = "ready"
                return
            if not self.cwd.is_dir():
                self.state = "failed"
                self.last_error = "算子目录不存在：%s" % self.cwd
                raise RuntimeError(self.last_error)
            if self._is_listening():                      # 外部已起（POOL_AUTOSTART=0 的联调场景）
                self.state = "ready"
                return
            self.state = "starting"
            command = [sys.executable, "-m", "uvicorn", self.entrypoint,
                       "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "info"]
            try:
                self.proc = subprocess.Popen(command, cwd=str(self.cwd), env=self._child_env())
            except OSError as exc:
                self.state = "failed"
                self.last_error = "拉起算子进程失败：%s" % exc
                raise RuntimeError(self.last_error) from exc
            self.started_at = time.time()
            await self._wait_ready()

    async def _wait_ready(self) -> None:
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                self.state = "failed"
                self.last_error = "算子进程提前退出（exit=%s）" % self.proc.returncode
                raise RuntimeError(self.last_error)
            if self._is_listening():
                self.state = "ready"
                return
            await asyncio.sleep(0.2)
        self.state = "failed"
        self.last_error = "算子进程 %.0fs 内未就绪（端口 %d）" % (STARTUP_TIMEOUT, self.port)
        raise RuntimeError(self.last_error)

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.state = "stopped"
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.state = "stopped"

    # ---------- 状态 ----------
    def status(self) -> Dict[str, Any]:
        alive = self.proc is not None and self.proc.poll() is None
        if not alive and self.state == "ready":
            self.state = "stopped"
        return {"operatorId": self.operator_id, "number": self.entry.get("formalOperatorNumber"),
                "key": self.key, "name": self.name, "package": self.package, "port": self.port,
                "entrypoint": self.entrypoint, "state": self.state, "pid": self.proc.pid if alive else None,
                "calls": self.calls, "failures": self.failures, "lastError": self.last_error,
                "uptimeSec": round(time.time() - self.started_at, 1) if alive and self.started_at else None}

    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port


PROCESSES: Dict[str, OperatorProcess] = {key: OperatorProcess(key, entry)
                                        for key, entry in REGISTRY.items() if key in ALIASES.values()}
_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    """共享 httpx 客户端（转发用；单进程内复用连接池）。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
    return _client


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _client
    await get_client()
    if AUTOSTART:
        await asyncio.gather(*(proc.ensure_started() for proc in PROCESSES.values()),
                             return_exceptions=True)
    try:
        yield
    finally:
        for proc in PROCESSES.values():
            proc.stop()
        if _client is not None:
            await _client.aclose()
            _client = None


app = FastAPI(
    title="跨模态算子包 · 多算子池化入口",
    version=VERSION,
    lifespan=lifespan,
)


# ------------------------------------------------------------------ 请求解析

def _operator_from_query(request: Request) -> Optional[str]:
    # [v4.2] 新增 detection（MTY V2 单文件流协议）：detection 与 operatorId 同一别名空间
    for name in ("operatorId", "operator_id", "toolId", "operator", "detection"):
        value = request.query_params.get(name)
        if value:
            return value
    return (request.headers.get("X-Operator-Id") or request.headers.get("x-operator-id")
            or request.headers.get("X-Detection") or request.headers.get("x-detection"))


def _strip_operator_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    # [v4.2] detection 视同 operatorId：已在池化层完成路由，不向子算子转发
    for field in ("operatorId", "operator_id", "toolId", "detection"):
        payload.pop(field, None)
    return payload


# ------------------------------------------------------------ 路径输入（v1.1）
def _path_input(request: Request, parsed: Optional[Dict[str, Any]]) -> Optional[str]:
    """从 query（优先）或 JSON body 中取 filePath/file_path/filepath。"""
    for name in PATH_INPUT_FIELDS:
        value = request.query_params.get(name)
        if value and value.strip():
            return value.strip()
    if isinstance(parsed, dict):
        for name in PATH_INPUT_FIELDS:
            value = parsed.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _max_path_bytes() -> int:
    """路径输入的大小上限；0 表示不限制（默认 2 GiB，与 compose 的 MAX_UPLOAD_BYTES 对齐）。"""
    raw = os.getenv("MAX_UPLOAD_BYTES", "").strip()
    try:
        return int(raw) if raw else 2 * 1024 ** 3
    except ValueError:
        return 2 * 1024 ** 3


def _resolve_local_file(raw: str) -> Path:
    """校验并归一化本地文件路径；只接受存在、可读的普通文件，不限制目录（按部署方要求）。"""
    if not raw or "\x00" in raw:
        raise ValueError("filePath 为空或包含非法字符")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("filePath 必须是绝对路径（收到 %r，如 /data/output/xxx.zip）" % raw[:300])
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("filePath 不可访问：%s" % exc) from exc
    if not candidate.is_file():
        raise ValueError("filePath 不是普通文件（不支持目录）：%s" % raw[:300])
    return candidate


def _path_forward_form(parsed: Optional[Dict[str, Any]],
                       operator_key: Optional[str] = None) -> Optional[Dict[str, str]]:
    """将路径请求的标量配置转成子算子的表单字段，禁止静默丢弃配置。"""
    if not isinstance(parsed, dict):
        return None
    form: Dict[str, str] = {}

    def add(name: str, value: Any, source: str) -> None:
        if value is None:
            return
        if operator_key == "M08" and name == "transcription_enabled":
            name = "transcribe"
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            encoded = str(value)
        else:
            raise ValueError("%s.%s 必须是标量；深层配置请使用上传后 JSON 调用" % (source, name))
        if name in form and form[name] != encoded:
            raise ValueError("filePath 业务参数 %s 重复且值冲突" % name)
        form[name] = encoded

    for name, value in parsed.items():
        if name in PATH_INPUT_FIELDS or name in {"config", "overrides"}:
            continue
        add(name, value, "body")
    for wrapper in ("config", "overrides"):
        if wrapper not in parsed:
            continue
        values = parsed[wrapper]
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except (ValueError, TypeError) as exc:
                raise ValueError("%s 必须是 JSON 对象" % wrapper) from exc
        if not isinstance(values, dict):
            raise ValueError("%s 必须是 JSON 对象" % wrapper)
        for name, value in values.items():
            if name in PATH_INPUT_FIELDS:
                raise ValueError("%s.%s 不是业务配置字段" % (wrapper, name))
            add(name, value, wrapper)
    return form or None


#: 常见媒体扩展名 → Content-Type；filePath 转发媒体流时不能只靠系统 mimetypes
#: （Windows 上 .mkv/.m4v/.webm 等可能没有注册，容易退化成 octet-stream）。
_MEDIA_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska", ".m4v": "video/x-m4v", ".webm": "video/webm",
    ".flv": "video/x-flv", ".wmv": "video/x-ms-wmv", ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg", ".ts": "video/mp2t", ".3gp": "video/3gpp",
    ".ogv": "video/ogg",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
    ".aiff": "audio/aiff", ".aif": "audio/aiff", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
}


def _path_content_type(filename: str) -> str:
    """为 filePath 转发的 multipart 文件补准确的 Content-Type。"""
    suffix = Path(filename).suffix.lower()
    explicit = _MEDIA_CONTENT_TYPES.get(suffix)
    if explicit:
        return explicit
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _response_content_type(upstream: httpx.Response) -> str:
    """透传子算子响应的 Content-Type；媒体流缺头时按 Content-Disposition 文件名兜底。"""
    header = upstream.headers.get("content-type")
    if header:
        return header
    disposition = upstream.headers.get("content-disposition", "")
    marker = "filename="
    if marker in disposition.lower():
        filename = disposition[disposition.lower().rfind(marker) + len(marker):]
        filename = filename.strip(" \"';")
        if filename:
            return _path_content_type(filename)
    return "application/json"


async def pick_operator(request: Request) -> tuple[Optional[str], Optional[bytes], Optional[Dict[str, Any]]]:
    """决定本次请求交给哪个算子；同时返回（可能被改写过的）请求体。"""
    raw = _operator_from_query(request)
    body = await request.body()
    parsed: Optional[Dict[str, Any]] = None
    if body:
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            try:
                candidate = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                candidate = None
            if isinstance(candidate, dict):
                if not raw:
                    raw = (candidate.get("operatorId") or candidate.get("operator_id")
                           or candidate.get("toolId") or candidate.get("detection"))
                parsed = _strip_operator_fields(candidate)
                body = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    return resolve_operator_id(raw), body, parsed


def _envelope(code: int, msg: str, data: Optional[Any] = None) -> Dict[str, Any]:
    return {"code": int(code), "msg": msg, "data": data if data is not None else {}}


# ------------------------------------------------------------------ 转发

@app.api_route("/uploadFile", methods=["POST"])
@app.api_route("/predict", methods=["POST"])
@app.api_route("/removeFile", methods=["POST"])
async def forward(request: Request) -> Response:
    """按 operatorId 把三接口请求转发给对应算子进程。"""
    path = request.url.path
    if path not in FORWARD_PATHS:
        return JSONResponse(status_code=404, content=_envelope(1, "未提供该接口：%s" % path))

    key, body, parsed = await pick_operator(request)
    if not key:
        return JSONResponse(status_code=400, content=_envelope(
            1, "缺少或无法识别的 operatorId（可用 M12 / 12 / image_text_semantic_scoring / "
               "CMA_score_image_text_consistency），也可用请求头 X-Operator-Id"))

    proc = PROCESSES[key]
    try:
        await proc.ensure_started()
    except RuntimeError as exc:
        proc.failures += 1
        return JSONResponse(status_code=503, content=_envelope(1, "算子 %s 不可用：%s" % (key, exc)))

    # [v4.2] detection 与 operatorId 一样：路由解析后从转发查询串剔除
    params = [(name, value) for name, value in request.query_params.multi_items()
              if name not in {"operatorId", "operator_id", "toolId", "operator", "detection"}]
    headers = {name: value for name, value in request.headers.items() if name.lower() not in _HOP_HEADERS}

    # [v1.1] /predict?detection=... 支持 filePath：池化入口读成文件流，再按 multipart 转发给子算子。
    # 第一批 #3–#21 链路用 filePath 直传上一步 outputFile.zip，无需平台下载/解压。
    path_value = _path_input(request, parsed) if path == "/predict" else None
    request_content_type = request.headers.get("content-type", "").lower()
    if path_value is None and path == "/predict" and \
            "application/x-www-form-urlencoded" in request_content_type:
        # 兼容平台把 filePath 放在 urlencoded 表单里的调用方式。
        try:
            urlencoded_form = await request.form()
        except Exception:  # noqa: BLE001 - 解析失败时按原有的无 filePath 逻辑转发
            urlencoded_form = None
        if urlencoded_form:
            for name in PATH_INPUT_FIELDS:
                value = urlencoded_form.get(name)
                if isinstance(value, str) and value.strip():
                    path_value = value.strip()
                    break
    local_file: Optional[Path] = None
    if path_value:
        if "multipart/form-data" in request_content_type:
            proc.failures += 1
            return JSONResponse(status_code=400, content=_envelope(
                1, "filePath 与 multipart/file 不能混用：请改用 query 或 JSON 传 filePath"))
        try:
            local_file = _resolve_local_file(path_value)
            file_size = local_file.stat().st_size
        except (ValueError, OSError) as exc:
            proc.failures += 1
            return JSONResponse(status_code=400, content=_envelope(
                1, "filePath 参数错误：%s" % exc))
        limit = _max_path_bytes()
        if limit and file_size > limit:
            proc.failures += 1
            return JSONResponse(status_code=413, content=_envelope(
                1, "filePath 文件过大：%d 字节 > MAX_UPLOAD_BYTES=%d" % (file_size, limit)))
        params = [(name, value) for name, value in params
                  if name not in PATH_INPUT_FIELDS and name not in {"file", "file_path", "filepath"}]
        headers = {name: value for name, value in headers.items() if name.lower() != "content-type"}
        content_type = request.headers.get("content-type", "").lower()
        if "application/x-www-form-urlencoded" in content_type:
            try:
                raw_form = await request.form()
            except Exception as exc:  # noqa: BLE001 - 表单解析失败按参数错误返回
                proc.failures += 1
                return JSONResponse(status_code=400, content=_envelope(
                    1, "filePath 伴随的表单参数解析失败：%s" % exc))
            parsed_form = dict(raw_form)
        else:
            parsed_form = parsed
        try:
            input_form = _path_forward_form(parsed_form, operator_key=key)
        except ValueError as exc:
            proc.failures += 1
            return JSONResponse(status_code=400, content=_envelope(
                1, "filePath 业务参数错误：%s" % exc))
    else:
        input_form = None

    proc.calls += 1
    try:
        client = await get_client()
        if local_file is not None:
            with local_file.open("rb") as handle:
                upstream = await client.request(
                    request.method, proc.base_url() + path, params=params,
                    files={"file": (local_file.name, handle, _path_content_type(local_file.name))},
                    data=input_form, headers=headers)
        else:
            upstream = await client.request(request.method, proc.base_url() + path, params=params,
                                            content=body if body else None, headers=headers)
    except OSError as exc:
        proc.failures += 1
        return JSONResponse(status_code=400, content=_envelope(
            1, "读取 filePath 文件失败：%s" % exc))
    except httpx.HTTPError as exc:
        proc.failures += 1
        return JSONResponse(status_code=502, content=_envelope(
            1, "算子 %s 调用失败：%s: %s" % (key, type(exc).__name__, exc)))

    passthrough = {name: value for name, value in upstream.headers.items()
                   if name.lower() not in _HOP_HEADERS | {"content-type"}}
    response_headers = {**passthrough, "X-Pool-Operator": proc.operator_id}
    if local_file is not None:
        response_headers["X-Pool-Input"] = "filePath"
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=_response_content_type(upstream),
                    headers=response_headers)


# ------------------------------------------------------------------ 池化自身接口

@app.get("/health")
async def health() -> Dict[str, Any]:
    """存活探针：进程存活（不判断算子子进程是否就绪）。"""
    return _envelope(0, "success", {"status": "alive", "service": SERVICE_NAME, "version": VERSION,
                                    "operatorCount": len(PROCESSES), "listenPort": _listen_port(),
                                    "autostart": AUTOSTART})


@app.get("/ready")
async def ready() -> Dict[str, Any]:
    """就绪探针：注册表非空 + 已就绪算子数。"""
    ready_count = sum(1 for proc in PROCESSES.values() if proc.status()["state"] == "ready")
    return _envelope(0, "success", {"status": "ready" if PROCESSES else "not_ready",
                                    "operatorCount": len(PROCESSES), "readyCount": ready_count,
                                    "autostart": AUTOSTART, "operators": [proc.status() for proc in PROCESSES.values()]})


@app.get("/operators")
async def operators() -> Dict[str, Any]:
    """算子清单与子进程状态（含别名解析结果），供编排/排障使用。"""
    return _envelope(0, "success", {
        "service": SERVICE_NAME, "version": VERSION,
        "routing": "?operatorId= / ?detection= / 请求头 X-Operator-Id / /predict body.operatorId",
        "operators": [proc.status() for proc in PROCESSES.values()]})


def _listen_port() -> int:
    return int(os.getenv("PORT", "8100"))


# ------------------------------------------------------------------ 本地启动

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="跨模态算子包 · 上层池化入口")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=_listen_port())
    parser.add_argument("--autostart", action="store_true", help="启动即拉起全部算子进程")
    parser.add_argument("--preflight", action="store_true", help="只打印注册表与目录检查结果后退出")
    args = parser.parse_args(argv)

    if args.preflight:
        report = preflight()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["okCount"] == report["operatorCount"] else 1

    import uvicorn

    if args.autostart:
        os.environ["POOL_AUTOSTART"] = "1"
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _weight_bundle_report() -> List[Dict[str, Any]]:
    """按 map.json 的 models.bundledIn 检查每个所需算子是否内置了本地权重。

    v1.0 正式版：模型权重随算子目录交付（``<package>/weight/<relativePath>``），
    包根 ``weight/`` 不再承担运行期共享模型目录职责。
    """
    data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    models = data.get("models") or {}
    items: List[Dict[str, Any]] = []
    for model_name, spec in sorted(models.items()):
        relative = str(spec.get("relativePath") or "").strip()
        if not relative:
            continue
        for key in spec.get("bundledIn") or []:
            entry = REGISTRY.get(str(key)) or {}
            package = str(entry.get("package") or "").strip()
            if not package:
                continue
            path = PKG_ROOT / package / "weight" / relative
            if path.is_dir():
                exists = (path / "config.json").is_file() or any(p.is_file() for p in path.iterdir())
                size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            else:
                exists = path.is_file()
                size = path.stat().st_size if exists else 0
            items.append({"operator": key, "package": package, "model": model_name,
                          "path": str(path), "exists": exists, "bytes": size,
                          "expectedSha256": spec.get("sha256")})
    return items


def _runtime_probe() -> Dict[str, Any]:
    """运行设备自检：CPU / CUDA / 昇腾 NPU。

    本包 8 个用模型的算子（#08/#12/#14/#16/#17/#18/#20 等）默认 `device: auto|cpu`：
    `auto` 在无 CUDA 时一律按 CPU 解析，因此在无 GPU 的昇腾节点上可直接 CPU 运行。
    NPU 加速需容器内装 torch_npu + CANN，本包未内置（见 部署说明.md §10）。
    """
    info: Dict[str, Any] = {
        "machine": platform.machine(),
        "system": platform.system(),
        "python": platform.python_version(),
    }
    try:
        import torch  # noqa: WPS433（延迟导入，避免 preflight 之外的路径加载 torch）
        info["torch"] = getattr(torch, "__version__", "")
        info["cudaAvailable"] = bool(torch.cuda.is_available())
        info["cudaDeviceCount"] = int(torch.cuda.device_count()) if info["cudaAvailable"] else 0
        info["torchThreads"] = int(torch.get_num_threads())
    except Exception as exc:  # 未装 torch 也可跑非模型算子
        info["torch"] = "unavailable: %s" % exc
        info["cudaAvailable"] = False
        info["cudaDeviceCount"] = 0
    try:
        info["torchNpuInstalled"] = importlib.util.find_spec("torch_npu") is not None
    except (ImportError, ValueError):
        info["torchNpuInstalled"] = False
    if info.get("cudaAvailable"):
        info["deviceMode"] = "cuda（GPU 可用；模型算子会按 auto 走 GPU）"
    elif info.get("torchNpuInstalled"):
        info["deviceMode"] = "cpu（已装 torch_npu，但本包代码只解析 cpu/cuda，NPU 需另行适配）"
    else:
        info["deviceMode"] = "cpu（无 CUDA、无 torch_npu；8 个模型算子按各 config 的 auto/cpu 走 CPU）"
    return info


def preflight() -> Dict[str, Any]:
    """启动自检：注册表、算子目录、入口文件、逐算子权重内置情况（不拉起进程）。"""
    bundles = _weight_bundle_report()
    bundles_by_operator: Dict[str, List[Dict[str, Any]]] = {}
    for bundle in bundles:
        bundles_by_operator.setdefault(str(bundle.get("operator")), []).append(bundle)
    items = []
    ok = 0
    for key, proc in PROCESSES.items():
        problems = []
        if not proc.cwd.is_dir():
            problems.append("目录缺失")
        else:
            for required in ("app.py", "inference.py", "configs", "utils", "tests", "dataset", "output", "README.md"):
                if not (proc.cwd / required).exists():
                    problems.append("缺少 %s" % required)
            for bundle in bundles_by_operator.get(key, []):
                if not bundle["exists"]:
                    problems.append("缺少内置权重 %s" % bundle["model"])
        if problems:
            items.append({"key": key, "package": proc.package, "ok": False, "problems": problems})
        else:
            ok += 1
            items.append({"key": key, "package": proc.package, "ok": True, "port": proc.port,
                          "weights": [bundle["model"] for bundle in bundles_by_operator.get(key, [])]})
    missing = [bundle for bundle in bundles if not bundle["exists"]]
    return {"service": SERVICE_NAME, "version": VERSION, "map": str(MAP_PATH),
            "runtime": _runtime_probe(),
            "operatorCount": len(PROCESSES), "okCount": ok,
            "weightBundles": {"root": "<算子目录>/weight",
                              "checked": len(bundles),
                              "ok": len(bundles) - len(missing),
                              "missing": missing[:10],
                              "items": bundles},
            "operators": items}


if __name__ == "__main__":
    sys.exit(main())
