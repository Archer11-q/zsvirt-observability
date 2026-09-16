# backend/ — 后端平台、资源关联、告警与根因诊断

**负责人：成员 B**

本目录是后端服务的实现位置。详细设计见：

| 文档 | 内容 |
|---|---|
| [../docs/backend/BACKEND_DESIGN.md](../docs/backend/BACKEND_DESIGN.md) | 模块划分、依赖方向、错误处理 |
| [../docs/backend/DIAGNOSIS_DESIGN.md](../docs/backend/DIAGNOSIS_DESIGN.md) | 诊断引擎、规则集、三类故障场景 |
| [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) | 全项目分层与数据流 |
| [../docs/TECH-BASELINE.md](../docs/TECH-BASELINE.md) | 语言与依赖基线 |

## 规划中的结构（见 BACKEND_DESIGN.md §1）

```text
backend/
├── app/
│   ├── main.py          # 应用装配
│   ├── config.py        # 配置加载
│   ├── ingest/          # L1 接入适配器（接收 A 上报）
│   ├── zsvirt/          # L1 ZSvirt 平台适配层（唯一耦合点）
│   ├── normalize/       # L1 标准化与资源 ID 拼装
│   ├── graph/           # L3 资源图
│   ├── events/          # L2 事件存储
│   ├── alerts/          # L4 告警引擎
│   ├── diagnosis/       # L4 诊断引擎（纯函数，可脱离数据库单测）
│   ├── api/             # L5 对外契约层
│   └── models/          # 数据模型
├── rules/               # 诊断规则集（声明式数据）
└── tests/
```

> **状态：尚未开始实现。** 技术基线已确认（Python / FastAPI / PostgreSQL / SQLAlchemy），
> 但设计文档仍为 DRAFT，需团队评审后再动代码。

## 对外接口

| 方向 | 契约位置 |
|---|---|
| A → B 上报 | [../docs/API_CONTRACT.md](../docs/API_CONTRACT.md) §3 |
| B → C 查询 | [../docs/API_CONTRACT.md](../docs/API_CONTRACT.md) §4 |
