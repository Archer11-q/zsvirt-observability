"""Crosslayer 后端应用包。

分层结构见 docs/backend/BACKEND_DESIGN.md：

    app.ingest       L1  接入适配器（接收成员 A 的上报）
    app.zsvirt       L1  ZSvirt 平台适配层（唯一外部耦合点）
    app.normalize    L1  标准化与资源 ID 拼装、敏感字段脱敏
    app.events       L2  事件存储（只追加）
    app.graph        L3  资源图与影响范围传播
    app.alerts       L4  告警引擎
    app.diagnosis    L4  诊断引擎（纯函数，不访问数据库 / 不发 HTTP）
    app.api          L5  对外契约层
    app.models       数据模型
"""

__version__ = "0.1.0"
