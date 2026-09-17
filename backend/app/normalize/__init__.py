"""标准化层（L1）。

职责（docs/backend/BACKEND_DESIGN.md §2.3）：

  - 拼装全局资源 ID（`{kind}:{source}:{sourceId}`，见 docs/DATA_MODEL.md §3）
  - 统一时间语义、统一严重级别枚举
  - **敏感字段过滤与脱敏**（见 `app.normalize.sensitive`）
  - 缺失字段留 `null`

明确不做：

  - **不填补缺失字段**，不猜测来源没给的信息
  - 不改变成员 A 的采集语义
  - 不做 Schema 校验（那是 `app.ingest` 的职责）
"""

from app.normalize.ids import ResourceRef, build_resource_id, parse_resource_id
from app.normalize.sensitive import (
    filter_sensitive,
    is_sensitive_key,
    mask_cmdline,
    mask_secrets_in_text,
    redact_event,
)

__all__ = [
    "ResourceRef",
    "build_resource_id",
    "filter_sensitive",
    "is_sensitive_key",
    "mask_cmdline",
    "mask_secrets_in_text",
    "parse_resource_id",
    "redact_event",
]
