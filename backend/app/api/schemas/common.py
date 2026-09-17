"""API 通用响应包裹与分页（`docs/API_CONTRACT.md` §2.2）。

契约要求所有成功响应为 `{data, meta}`，列表额外带分页信息。

**分页为什么用游标而不是 offset**：事件是持续追加的，offset 分页在追加
场景下会漏数据或重复（第 2 页请求时前页插入了新数据，offset 偏移就错位）。
游标锚定在**具体记录**上，因此不受并发追加影响。

**游标编码**：`(occurred_at, id)` 的 base64。事件按 `(occurred_at DESC, id DESC)`
排序 —— 时间可能相同，用 `id` 兜底保证全序，否则同一时刻的记录顺序不确定、
分页会漏。`id` 用 `evt_` + ULID，本身时间单调有序，是很合适的次级键。
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

#: 单页上限。与 `docs/API_CONTRACT.md` §4.4 一致。
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000


class PageMeta(BaseModel):
    """分页元信息。"""

    limit: int
    #: 下一页游标；`None` 表示没有更多数据
    nextCursor: str | None = None
    #: 本次返回条数，便于前端判断是否空页
    count: int = 0


class ResponseMeta(BaseModel):
    """响应元信息。"""

    generatedAt: str
    traceId: str
    page: PageMeta | None = None


class Envelope[T](BaseModel):
    """成功响应包裹 `{data, meta}`。

    用 PEP 695 泛型语法（Python 3.12+），比 `Generic[T]` + `TypeVar` 更简洁，
    且 `T` 的绑定范围天然限定在本类内。
    """

    data: T
    meta: ResponseMeta


class ErrorBody(BaseModel):
    code: str
    message: str
    traceId: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """错误响应包裹（`docs/API_CONTRACT.md` §2.3）。"""

    error: ErrorBody


# ---------------------------------------------------------------- 游标


class CursorError(ValueError):
    """游标无法解析 —— 应返回 400 INVALID_ARGUMENT。"""


def encode_cursor(occurred_at: datetime, item_id: str) -> str:
    """把 `(occurred_at, id)` 编码为不透明游标。

    刻意 encode 成不透明字符串：消费方（成员 C）不应解析它，
    只应原样回传。这样内部排序键将来可以改动而不破坏契约。
    """
    raw = f"{occurred_at.isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解析游标。非法输入抛 `CursorError`（映射为 400）。"""
    if not cursor:
        raise CursorError("cursor is empty")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise CursorError(f"cursor is not valid base64: {exc}") from exc

    if "|" not in raw:
        raise CursorError("cursor missing separator")
    ts_part, _, item_id = raw.partition("|")
    if not item_id:
        raise CursorError("cursor missing id part")
    try:
        occurred_at = datetime.fromisoformat(ts_part)
    except ValueError as exc:
        raise CursorError(f"cursor timestamp is not ISO8601: {exc}") from exc
    return occurred_at, item_id


def clamp_limit(limit: int | None, *, default: int = DEFAULT_PAGE_LIMIT) -> int:
    """把 `limit` 收进合法区间。

    **超上限不静默截断** —— 由调用方判断并返回 400，避免消费方以为
    拿到了全部数据却只拿到一部分。本函数只做区间收敛，不做拒绝判断。
    """
    if limit is None:
        return default
    if limit < 1:
        raise CursorError("limit must be >= 1")
    return min(limit, MAX_PAGE_LIMIT)


class ListParams(BaseModel):
    """列表查询的公共参数。"""

    limit: int = Field(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = None
