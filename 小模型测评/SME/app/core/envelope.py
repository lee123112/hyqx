"""Unified response envelope and error handling."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def ok(data: Any = None, msg: str = "success") -> dict[str, Any]:
    return {"code": 0, "msg": msg, "data": data}


class ApiError(Exception):
    def __init__(self, status_code: int, error_code: str, message: str,
                 details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.status_code,
            "msg": self.message,
            "data": {"errorCode": self.error_code, "details": self.details},
        }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": 422, "msg": "请求参数校验失败",
                     "data": {"errorCode": "REQUEST_INVALID",
                              "details": exc.errors()}},
        )

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"code": 500, "msg": "服务内部错误",
                     "data": {"errorCode": "INTERNAL_ERROR",
                              "details": str(exc)[:300]}},
        )
