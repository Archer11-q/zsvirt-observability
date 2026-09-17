"""`GET /api/v1/dict` —— 枚举字典端点。

契约：`docs/API_CONTRACT.md` §4.6.1；成员 C 的 Q14 明确要求：

> 需要字典端点（建议 `GET /api/v1/dict` 或 `/meta`），**含可读文案**。
> 前端筛选下拉、图例颜色、状态标签、根因/建议文案都依赖枚举映射；
> 硬编码会导致前后端漂移，破坏「可复现」。

因此本端点是**枚举的唯一对外真源**：B 变更枚举必须同步这里并通知 C。

**响应可缓存**：字典内容只在发版时变化，而 C 会按 10–15s 的节奏轮询
（他的 Q11）。因此返回 `Cache-Control` 与 `ETag`，让前端与中间层可以
用条件请求省掉重复传输 —— 这不是过早优化，而是明确知道调用频率下的
低成本收益。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Header, Response

from app.api.common import build_meta
from app.enums import build_dict_payload

router = APIRouter(tags=["dict"])


def _dict_etag(payload: dict[str, Any]) -> str:
    """按内容计算 ETag。

    用内容哈希而不是版本号：这样只要枚举有实质变化，ETag 一定变；
    若只是重排键顺序（无实质变化），ETag 不变 —— 对前端更友好。
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    digest = hashlib.sha256(canonical).hexdigest()[:16]
    return f'W/"{digest}"'


@router.get("/api/v1/dict")
def get_dict(
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> Any:
    """返回全部枚举与中文文案。"""
    payload = build_dict_payload()
    etag = _dict_etag(payload)

    response.headers["ETag"] = etag
    # 允许客户端与中间层缓存，但要求每次复用前校验 ——
    # 枚举变化必须能被前端及时看到，不能靠 TTL 猜。
    response.headers["Cache-Control"] = "public, max-age=60, must-revalidate"

    if if_none_match and if_none_match == etag:
        # 内容未变：304 让前端继续用本地缓存
        response.status_code = 304
        return None

    return {
        "data": payload,
        "meta": build_meta().model_dump(mode="json"),
    }
