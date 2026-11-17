"""统一响应包络与错误处理。

契约见 spec/03-api-contract.md §0。
所有端点都返回 {ok, data/error, meta}，客户端不需要区分 HTTP 状态码与业务状态。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import APP_VERSION

_REQUEST_ID = uuid.uuid4().hex[:12]


def request_meta(request: Request | None = None, elapsed_ms: float | None = None) -> dict:
    meta = {"request_id": _REQUEST_ID, "version": APP_VERSION}
    if elapsed_ms is not None:
        meta["elapsed_ms"] = round(elapsed_ms, 1)
    return meta


def ok(data: Any = None, meta: dict | None = None) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data if data is not None else {},
                         "meta": meta or request_meta()})


def fail(code: str, message: str, *, status: int = 400, hint: str = "",
         retryable: bool = False, detail: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "hint": hint,
                "detail": detail or {},
                "retryable": retryable,
            },
            "meta": request_meta(),
        },
        status_code=status,
    )


def from_exception(exc) -> JSONResponse:
    """把内部异常映射为统一错误包络。"""
    code = getattr(exc, "code", None)
    if code:
        return fail(code, getattr(exc, "message", str(exc)),
                    hint=getattr(exc, "hint", ""),
                    retryable=getattr(exc, "retryable", False))
    return fail("CHANNEL_INTERNAL_ERROR", str(exc), status=500,
                hint="这是服务端异常，请查看日志。", retryable=True)


class ApiError(Exception):
    """业务异常，由全局处理器转成统一包络。"""

    def __init__(self, code: str, message: str, *, status: int = 400,
                 hint: str = "", retryable: bool = False, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint
        self.retryable = retryable
        self.detail = detail or {}
