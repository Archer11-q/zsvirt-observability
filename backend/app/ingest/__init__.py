"""接入层（L1，入站）。

契约：`docs/API_CONTRACT.md` §3。

职责边界（`docs/backend/BACKEND_DESIGN.md` §2.1）：

  - 负责：Schema 校验、限流、幂等去重、脏数据隔离、逐条返回结果
  - **不负责**：不做字段语义改写（那是 `app.normalize`）、
    不决定成员 A 的探针实现

关键行为（成员 A 的 Q1–Q7 已确认）：

1. `batchId` 幂等 —— 重复批次返回**与首次一致**的结果，HTTP 仍为 200
2. 逐条返回 resource 接受结果与 `globalId`，供 A 维护「已确认资源」缓存
3. 部分接受 —— 被拒条目带 `index` + `reason`，便于定位探针 bug
4. 超限返回 `429` + `Retry-After`，不静默截断
5. 孤儿引用挂占位节点，不丢数据、不报错
6. 迟到批次按 `occurredAt` 追加，不因迟到丢弃
7. 可选 Bearer Token，默认关闭
"""

from app.ingest.limits import (
    BatchRateLimiter,
    IngestMetrics,
    metrics,
    rate_limiter,
    reset_metrics,
)
from app.ingest.schemas import (
    AcceptedCounts,
    EventPayload,
    IngestBatch,
    IngestResponse,
    IngestResult,
    RejectedItem,
    ResourcePayload,
    ResourceRef,
    ResourceResult,
)
from app.ingest.service import (
    BatchTooLarge,
    ingest_batch,
    validate_batch_size,
    validate_payload_size,
)

__all__ = [
    "AcceptedCounts",
    "BatchRateLimiter",
    "BatchTooLarge",
    "EventPayload",
    "IngestBatch",
    "IngestMetrics",
    "IngestResponse",
    "IngestResult",
    "RejectedItem",
    "ResourcePayload",
    "ResourceRef",
    "ResourceResult",
    "ingest_batch",
    "metrics",
    "rate_limiter",
    "reset_metrics",
    "validate_batch_size",
    "validate_payload_size",
]
