"""事件存储（L2）。

契约：`docs/API_CONTRACT.md` §4.4；对象定义：`docs/DATA_MODEL.md` §5.1。

**只追加、不修改** —— 事件是诊断证据的来源，可改写即等于证据链失效。
因此本模块不提供 update / delete 路径。
"""

from app.events.repository import (
    count_events,
    get_events_by_ids,
    latest_event_at,
    query_events,
)

__all__ = [
    "count_events",
    "get_events_by_ids",
    "latest_event_at",
    "query_events",
]
