"""API 层共用小工具（响应元信息、traceId、错误响应）。

抽出来的原因：拓扑 / 字典 / 事件 / 告警 / 诊断五个端点都要构造
`{data, meta}` 与统一错误体，若各写一份必然漂移 —— 而 `traceId`
是排障时的关键线索，格式不一致会让日志串联失败。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse

from app.api.schemas.common import (
    ErrorBody,
    ErrorEnvelope,
    PageMeta,
    ResponseMeta,
)


def new_trace_id(prefix: str = "req") -> str:
    """生成可读的 traceId。

    格式：`{prefix}_{UTC时间戳}_{随机后缀}`。带时间戳便于在日志里按时间定位，
    带随机后缀避免同一微秒内的并发请求撞号。
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"{prefix}_{stamp}_{secrets.token_hex(3)}"


def build_meta(*, trace_id: str | None = None, page: PageMeta | None = None) -> ResponseMeta:
    """构造响应元信息。"""
    return ResponseMeta(
        generatedAt=datetime.now(UTC).isoformat(),
        traceId=trace_id or new_trace_id(),
        page=page,
    )


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """统一错误响应（`docs/API_CONTRACT.md` §2.3）。

    必须带 `traceId` ——「错误响应要可诊断，不能只返回 500」是契约要求。
    """
    body = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            traceId=new_trace_id("err"),
            details=details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


def invalid_argument(message: str, **details: Any) -> JSONResponse:
    """400 INVALID_ARGUMENT 的快捷构造（参数非法、超上限等）。"""
    return error_response(400, "INVALID_ARGUMENT", message, details=details or None)


def not_found(code: str, message: str, **details: Any) -> JSONResponse:
    """404 的快捷构造。"""
    return error_response(404, code, message, details=details or None)
