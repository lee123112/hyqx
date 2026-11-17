from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from .api import include_routers
from .core.config import settings
from .core.envelope import install_error_handlers
from .store import db
from .store.seed import ensure_seed


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    db.init_db()
    ensure_seed()
    yield


app = FastAPI(
    title="小模型测评（数据集评测）服务",
    description="MTY 小模型测评算子与数据集评测业务 API",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_error_handlers(app)
include_routers(app)


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """Optional API key for write/operator endpoints.

    Read-only browsing, health, docs and the console stay open.  When
    SME_API_KEY is empty (default) this guard is a no-op.
    """
    if settings.api_key:
        path = request.url.path
        protected = (
            path in {"/uploadFile", "/predict", "/removeFile"}
            or (path.startswith("/api/v1/") and request.method not in
                {"GET", "HEAD", "OPTIONS"})
        )
        if protected and request.headers.get("X-API-Key") != settings.api_key:
            return JSONResponse(
                status_code=401,
                content={"code": 401, "msg": "缺少或错误的 X-API-Key",
                         "data": None},
            )
    return await call_next(request)


@app.get("/", include_in_schema=False)
def root() -> JSONResponse:
    return JSONResponse({
        "service": "small-model-evaluation",
        "version": "1.0.0",
        "health": "/health",
        "docs": "/docs",
        "console": "/console",
        "api": "/api/v1/eval/templates",
    })


@app.get("/console", include_in_schema=False)
def console() -> Response:
    path = settings.project_root / "web" / "console.html"
    if not path.exists():
        return JSONResponse({"code": 404, "msg": "console.html 尚未生成"}, status_code=404)
    return FileResponse(path, media_type="text/html; charset=utf-8")
