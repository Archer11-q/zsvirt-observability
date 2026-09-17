"""告警引擎与状态机（L4）。

契约：`docs/API_CONTRACT.md` §4.5；对象定义：`docs/DATA_MODEL.md` §5.2。

当前已落地：**状态机与运营能力**（查询、分级、聚合计数、静默、批量操作）。
待落地：**规则求值**（阈值/事件触发 → 产生告警）与跨层关联，
依赖三类故障场景的事件夹具（见 `docs/backend/DIAGNOSIS_DESIGN.md` §5）。

红线（`DATA_MODEL.md` §5.2）：**没有证据的告警不允许存在**。
本层与数据库 CHECK 约束双重保证。
"""

from app.alerts.repository import (
    IllegalTransition,
    active_severity_counts,
    alert_state_counts,
    bulk_transition,
    count_alerts,
    get_alert,
    query_alerts,
    silence_expired,
    transition,
)

__all__ = [
    "IllegalTransition",
    "active_severity_counts",
    "alert_state_counts",
    "bulk_transition",
    "count_alerts",
    "get_alert",
    "query_alerts",
    "silence_expired",
    "transition",
]
