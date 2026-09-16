# ZSvirt Observability — 后端模块设计

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 骨架 + 模块契约，待评审** |
| 归属 | 成员 B 模块设计（产品文档） |
| 负责人 | 成员 B |
| 最后更新 | 2026-09-16 |
| 上游文档 | [`../ARCHITECTURE.md`](../ARCHITECTURE.md)、[`../DATA_MODEL.md`](../DATA_MODEL.md)、[`../API_CONTRACT.md`](../API_CONTRACT.md) |

> 本文档是 `docs/ARCHITECTURE.md` 在后端模块层的细化。总体分层与数据流见 ARCHITECTURE，
> 诊断引擎的完整设计见 [`DIAGNOSIS_DESIGN.md`](DIAGNOSIS_DESIGN.md)。

---

## 1. 代码组织（【提案】，待技术基线锁定后定稿）

```text
services/observability-api/          # 后端服务根目录【待确认命名】
├── app/
│   ├── main.py                      # 应用装配与生命周期
│   ├── config.py                    # 配置加载（pydantic-settings）
│   ├── ingest/                      # L1  接入适配器（接收 A）
│   ├── zsvirt/                      # L1  平台适配层（对接 ZSvirt）
│   ├── normalize/                   # L1  标准化与 ID 拼装
│   ├── graph/                       # L3  资源图
│   ├── events/                      # L2  事件存储
│   ├── alerts/                      # L4  告警引擎
│   ├── diagnosis/                   # L4  诊断引擎（纯函数）
│   ├── api/                         # L5  对外契约层（routers）
│   └── models/                      # 数据模型（SQLAlchemy + Pydantic）
├── rules/                           # 诊断规则集（声明式数据，随版本发布）
├── tests/
└── requirements.txt
```

**命名与位置为【提案】**：在 B2 开工前需与 A、C 对齐仓库整体目录约定（例如 A 的探针代码放哪、C 的前端放哪）。

---

## 2. 模块契约（内部接口）

> 这些是 B 内部的模块边界。跨成员的契约在 `../API_CONTRACT.md`，不在本文档。

### 2.1 `ingest`（L1，入站）

```python
def accept_batch(payload: IngestBatch) -> IngestResult: ...
```

| 职责 | 不负责 |
|---|---|
| Schema 校验、鉴权（【待确认】）、限流、幂等去重（按 `batchId`）、脏数据隔离 | 不做字段语义改写；不决定 A 的探针实现 |

**关键行为**：部分接受。被拒条目必须返回 `reason` + `detail`，**不得静默丢弃**。

### 2.2 `zsvirt`（L1，出站）

| 职责 | 不负责 |
|---|---|
| 查询 ZSvirt 资源清单与平台事件；认证与重试退避；失败时保留上次快照并标记陈旧度 | **不把 ZSvirt SDK 类型泄漏到 L2 以上**（必须在边界处转成内部模型） |

**这是全系统唯一的 ZSvirt 耦合点。** 若 ZSvirt 升级波及 L2 以上，即为分层破坏。

### 2.3 `normalize`（L1）

```python
def to_resource(kind, source, source_id, raw) -> Resource: ...
def to_event(raw, resource_ref) -> Event: ...
```

| 职责 | 不负责 |
|---|---|
| 拼装全局 ID（`DATA_MODEL.md` §3）、统一时间语义、统一严重级别枚举、缺失字段留 `null` | **不填补缺失字段**；不猜测来源没给的信息 |

### 2.4 `graph`（L3）

| 职责 | 不负责 |
|---|---|
| 节点与边的增删改、`parentId` 关系维护、上下游检索、`stale`/`gone` 生命周期、影响范围传播 | 不自行编造资源关系；不物理删除节点 |

### 2.5 `events`（L2）

| 职责 | 不负责 |
|---|---|
| 只追加写入、按资源/时间/类型检索、游标分页 | 不修改既有事件；不承担告警状态机 |

### 2.6 `alerts`（L4）

```python
def evaluate(event_or_metric, ruleset) -> list[Alert]: ...
def transition(alert_id, action) -> Alert: ...   # ack / silence / resolve
```

| 职责 | 不负责 |
|---|---|
| 规则求值、聚合去重、状态机、证据关联（`evidenceEventIds` 必填） | 不输出无证据的告警；不为单场景写一次性硬编码 |

### 2.7 `diagnosis`（L4）

```python
def diagnose(ctx: DiagnosisContext) -> Diagnosis: ...
```

| 职责 | 不负责 |
|---|---|
| 跨层关联、规则匹配、置信评分、证据组装、影响范围计算、建议生成 | **不访问数据库、不发 HTTP**；不输出黑盒结论 |

完整设计见 [`DIAGNOSIS_DESIGN.md`](DIAGNOSIS_DESIGN.md)。

### 2.8 `api`（L5）

| 职责 | 不负责 |
|---|---|
| 请求校验、调用 L3/L4、组装响应包裹、错误模型映射、`traceId` | 不暴露数据库结构；不在 Handler 里写业务/诊断逻辑 |

---

## 3. 依赖方向（强制）

```
api ──▶ alerts ──▶ graph ──▶ events ──▶ normalize ──▶ ingest
 │        │                    │                        zsvirt
 └────────┴──▶ diagnosis ─────┘
```

规则：

- 依赖只能向下，**不得反向**。
- `diagnosis` 接收已装配的纯数据对象，因此它可以被单测而不需要数据库。
- 外部系统（ZSvirt、A 探针）只出现在 L1。

---

## 4. 错误处理

统一遵循 `../API_CONTRACT.md` §2.3 的错误模型。

| 场景 | 行为 |
|---|---|
| 上报 Schema 失败 | `422 SCHEMA_VALIDATION_FAILED` + 字段定位；条目进隔离区 |
| ZSvirt 不可达 | 退避重试 → 熔断；服务状态转 `degraded`；保留上次快照 + `staleness` |
| 孤儿资源引用 | 挂 `unresolved` 占位节点 + 计数暴露为健康指标 |
| 诊断无匹配规则 | 返回 `UNKNOWN` + 已收集证据，**不编造** |
| 未预期异常 | `500 INTERNAL_ERROR` + `traceId`，**禁止裸奔异常** |

---

## 5. 测试

分层测试策略见 [`../TEST_PLAN.md`](TEST_PLAN.md)。模块级要求：

- `diagnosis`：必须可在**无数据库、无网络**的条件下完成全部单元测试。
- `normalize`：ID 拼装与时间语义必须有边界用例。
- `ingest`：幂等、部分接受、脏数据隔离必须有专门用例。
- 契约测试：A→B 与 B→C 双向，用 golden file 固定。

---

## 6. 未决问题

1. 服务目录命名与仓库整体结构（需与 A、C 对齐）。
2. 后台任务（周期同步、诊断触发）是同进程调度还是独立 worker。
3. 是否引入认证，以及认证放在哪一层。
4. 事件表的索引与保留策略（见 ADR-0003 的后续跟进项）。
5. 规则集的存放形式：文件 / 数据库表。
6. 日志与追踪方案（最小依赖约束下倾向于结构化日志 + `traceId` 贯穿）。

---

## 7. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 首轮骨架：代码组织、模块契约、依赖方向、错误处理、测试要求 | DRAFT，待评审 |
