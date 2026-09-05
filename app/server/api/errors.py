"""Application errors and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable


def error_body(request: Request, error: AppError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
        "request_id": request.state.request_id,
    }
    if error.details is not None:
        body["details"] = error.details
    return body


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(request, exc),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = []
        for error in exc.errors():
            normalized = dict(error)
            normalized.pop("url", None)
            normalized.pop("ctx", None)
            details.append(normalized)
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "details": details,
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request.app.state.logger.exception(
            "Unhandled request error", extra={"request_id": request.state.request_id}
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                "request_id": request.state.request_id,
            },
        )
