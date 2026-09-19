"""探针侧数据模型：Resource / Event。

字段对齐 `docs/API_CONTRACT.md` §3.2 上报体。探针只负责给出 `sourceId`，
全局 ID（`{kind}:{source}:{sourceId}`）由 B 的 `normalize` 拼装。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def utc_now_ms() -> str:
    """当前时间的 ISO 8601 UTC 毫秒格式（带 Z）。契约要求的时间戳格式。"""
    ms = int(time.time() * 1000)
    sec, milli = divmod(ms, 1000)
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec))
        + f".{milli:03d}Z"
    )


@dataclass
class Resource:
    """探针侧资源（对应上报体 `resources[]`）。"""

    kind: str  # container / process / ai_service / agent / task
    source_id: str  # 探针侧原生标识，规则见 DATA_MODEL D-031
    name: str
    parent_source_id: str | None = None  # 上级资源的 sourceId
    status: str = "running"  # running / stopped / error / unknown
    attributes: dict[str, Any] = field(default_factory=dict)
    # 资源生命周期时间（契约 §3.2）。采集器暂未填充时保持 None（B 侧可空）。
    first_seen_at: str | None = None
    last_seen_at: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sourceId": self.source_id,
            "name": self.name,
            "parentSourceId": self.parent_source_id,
            "status": self.status,
            "attributes": self.attributes,
            "firstSeenAt": self.first_seen_at,
            "lastSeenAt": self.last_seen_at,
        }


@dataclass
class Event:
    """探针侧事件（对应上报体 `events[]`）。"""

    type: str  # 事件类型，遵循 {domain}.{subject}.{qualifier}
    severity: str  # info / warning / error / critical
    message: str
    resource_kind: str  # 被引用资源的 kind
    resource_source_id: str  # 被引用资源的 sourceId
    occurred_at: str = field(default_factory=utc_now_ms)
    metrics: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "occurredAt": self.occurred_at,
            "resourceRef": {
                "kind": self.resource_kind,
                "sourceId": self.resource_source_id,
            },
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
            "metrics": self.metrics,
            "raw": self.raw,
        }
