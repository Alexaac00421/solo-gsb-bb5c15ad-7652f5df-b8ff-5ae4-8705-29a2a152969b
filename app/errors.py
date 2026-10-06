"""统一错误：业务错误携带稳定 code，便于调用方分支处理。"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .settlement import SettlementError


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def api_error_response(exc: ApiError | SettlementError, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": getattr(exc, "details", None),
            }
        },
    )


def register_exception_handlers(app) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError):
        return api_error_response(exc, exc.status_code)

    @app.exception_handler(SettlementError)
    async def _handle_settlement_error(_: Request, exc: SettlementError):
        # 结算类错误（无费率、顺序关账、期间格式）统一 422
        return api_error_response(exc, 422)
