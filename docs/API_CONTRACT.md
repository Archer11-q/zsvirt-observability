# ZSvirt Observability — API 契约

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 未经三方评审，不得作为实现依据** |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B |
| 最后更新 | 2026-09-16 |
| 评审人 | 成员 A（A→B 输入侧）、成员 C（B→C 输出侧） |

> **本文件是三方之间的契约。任何字段的新增、改名、删除或类型变化，都必须先修改本文件并通知受影响成员，禁止只改代码不改契约。**
> `【待确认】` = 需三方决策；`【提案】` = 成员 B 推荐方案。

---

## 1. 契约总览

```
┌──────────┐   A → B：上报契约（§3）    ┌──────────┐   B → C：查询契约（§4）  ┌──────────┐
│  成员 A   │ ─────────────────────────▶│  成员 B   │ ────────────────────────▶│  成员 C   │
│ VM 内探针 │                           │ 后端平台  │                          │  前端展示 │
└──────────┘                           └──────────┘                          └──────────┘
                                             ▲
                                             │ B → ZSvirt：平台适配契约（§5）
                                        [ZSvirt API]
```

**原则**
1. **API 先于实现**：先冻结契约，再写实现。
2. **C 不直连数据库**：C 只能通过本契约获取数据，不得依赖 B 的表结构。
3. **A ↔ C 不直连**：A 与 C 的交互原则上经 B 的统一契约完成。
4. **所有对象有稳定 ID 与明确状态语义**（见 `DATA_MODEL.md`）。
5. **错误可诊断**：任何失败都必须给出结构化错误，**禁止只返回 500**。

---

## 2. 通用约定

### 2.1 版本策略（【提案】）

- 路径版本：`/api/v1/...`。
- `/api/health` 不带版本（运维探针，永远可用）。
- 破坏性变更 → 升 `v2`，且 `v1` 至少保留一个完整阶段。
- 非破坏性变更（**仅新增可选字段**）→ 不升版本，但必须更新本文件。
- 响应头携带 `X-API-Version`，便于联调定位。

> **【待确认】** 当前职责文档给出的路径是 `/api/topology` 等**无版本号**形式。本提案改为 `/api/v1/topology`。若团队决定不引入版本前缀，则本文所有路径需同步去掉 `/v1`，但破坏性变更将失去隔离手段。

### 2.2 响应包裹格式（【提案】）

成功：

```json
{
  "data": { },
  "meta": { "generatedAt": "2026-09-16T13:40:00Z", "traceId": "..." }
}
```

列表额外带分页：

```json
{
  "data": [ ],
  "meta": {
    "generatedAt": "...",
    "traceId": "...",
    "page": { "limit": 100, "nextCursor": "..." }
  }
}
```

**分页采用游标（cursor）而非 offset** —— 事件是持续追加的，offset 分页会漏数据或重复。

### 2.3 错误模型（强制）

```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "resource not found",
    "details": { "resourceId": "vm:zsvirt:..." },
    "traceId": "01J..."
  }
}
```

| HTTP | `code` | 含义 |
|---|---|---|
| 400 | `INVALID_ARGUMENT` | 参数不合法（时间窗倒置、limit 越界等） |
| 401 | `UNAUTHENTICATED` | 【待确认】是否引入认证 |
| 403 | `PERMISSION_DENIED` | 【待确认】 |
| 404 | `RESOURCE_NOT_FOUND` / `DIAGNOSIS_NOT_FOUND` | 对象不存在 |
| 409 | `CONFLICT` | 状态冲突（如重复 ack） |
| 422 | `SCHEMA_VALIDATION_FAILED` | 上报体不满足 Schema（**仅 A→B**） |
| 429 | `RATE_LIMITED` | 触发限流，带 `Retry-After` |
| 502 | `UPSTREAM_UNAVAILABLE` | ZSvirt 不可达（**B 的降级信号**） |
| 503 | `SERVICE_DEGRADED` | B 自身降级（如数据库不可用） |
| 500 | `INTERNAL_ERROR` | 兜底，必须带 `traceId` |

**约定**：`502 UPSTREAM_UNAVAILABLE` 用于"ZSvirt 挂了"，`503 SERVICE_DEGRADED` 用于"B 自己能力受损"。C 必须能区分这两种。

---

## 3. A → B 上报契约

### 3.1 传输方式（【待确认】）

| 方案 | 说明 | 取舍 |
|---|---|---|
| **HTTP POST（推荐）** | 探针主动 POST 到 B 的接入端点，批量化 | 实现简单、易调试、无额外中间件；符合"最小依赖"已确认策略 |
| 消息队列 | Kafka / NATS | **已确认排除**（属重大组件，且增加演示复现难度） |
| B 主动拉取 | B 去 VM 内拉 | 需要 A 暴露端点，VM 网络可达性差 |

**已确认**：不引入消息队列。默认采用 HTTP 批量上报。【提案】端点：

```
POST /api/v1/ingest/batch
```

### 3.2 上报体（【提案】，字段需 A 确认）

```json
{
  "agentId": "probe-<vm-uuid>-01",
  "vmId": "vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44",
  "agentVersion": "0.1.0",
  "batchId": "01J...",
  "sentAt": "2026-09-16T13:40:00Z",
  "resources": [
    {
      "kind": "container",
      "sourceId": "web-0",
      "name": "web-0",
      "parentSourceId": null,
      "status": "running",
      "attributes": { "image": "vllm/vllm-openai:latest", "runtime": "docker" },
      "firstSeenAt": "2026-09-16T13:00:00Z",
      "lastSeenAt": "2026-09-16T13:40:00Z"
    }
  ],
  "events": [
    {
      "occurredAt": "2026-09-16T13:39:58Z",
      "resourceRef": { "kind": "container", "sourceId": "web-0" },
      "type": "inference.timeout",
      "severity": "error",
      "message": "request exceeded 30s",
      "metrics": { "latency_ms": 30120 },
      "raw": { }
    }
  ]
}
```

**关键点**
- `vmId` **必填**：它是 Push 数据挂到 Pull 资源的唯一锚点（见 `ARCHITECTURE.md` §4.1）。
- 探针侧资源用 `sourceId` 标识，B 负责拼装成全局 ID `container:probe:<agentId>:<sourceId>`。
- 事件用 `resourceRef` 指向同批次资源，或指向历史已上报资源。
- `raw` 允许 A 附带原始载荷，B 原样保存、不解析。

### 3.3 上报语义要求（需 A 确认的关键问题）

| # | 问题 | 为什么重要 |
|---|---|---|
| Q1 | `resourceRef` 引用的资源是否保证在**同一批次**或**之前批次**出现过？ | 决定 B 是否需要缓冲/占位机制，否则产生孤儿事件 |
| Q2 | 上报是**至少一次**还是**恰好一次**？是否可能重复投递？ | B 需要按 `batchId` 幂等去重 |
| Q3 | 探针时间戳来源与时钟同步方式（NTP？宿主机时间？） | 跨层关联依赖时间窗口，时钟漂移会直接导致漏关联 |
| Q4 | 批量大小上限与上报频率 | 决定 B 侧限流与批次大小上限 |
| Q5 | 探针如何发现 B 的地址（配置 / 环境变量 / 服务发现） | 部署配置项 |
| Q6 | 断网期间数据如何处理（本地缓冲？丢弃？） | 影响事件完整性与"可复现"评分 |
| Q7 | 是否需要鉴权（token / mTLS） | 【待确认】安全边界 |

### 3.4 上报响应

```json
{
  "data": {
    "batchId": "01J...",
    "accepted": { "resources": 3, "events": 12 },
    "rejected": [ { "index": 4, "reason": "SCHEMA_VALIDATION_FAILED", "detail": "occurredAt missing" } ]
  },
  "meta": { "traceId": "..." }
}
```

**约定**：部分接受是允许的（207 语义用 200 + `rejected` 表达）。被拒条目必须给出 `reason` 与 `detail`，便于 A 定位探针 bug。

---

## 4. B → C 查询契约

### 4.1 端点清单（推荐最小集合）

| 方法 | 路径 | 用途 | 状态 |
|---|---|---|---|
| GET | `/api/health` | 健康 / 降级状态（**无版本前缀**） | 【提案】 |
| GET | `/api/v1/topology` | 资源拓扑（可指定根节点与深度） | 【提案】 |
| GET | `/api/v1/workloads` | AI 工作负载视图（跨层聚合的"业务视图"） | 【待确认】语义 |
| GET | `/api/v1/events` | 事件查询（时间 / 资源 / 类型 / 严重级别过滤） | 【提案】 |
| GET | `/api/v1/alerts` | 告警列表（按状态 / 严重级别过滤） | 【提案】 |
| GET | `/api/v1/diagnosis/{id}` | 单个诊断详情 | 【提案】 |
| GET | `/api/v1/diagnoses` | 诊断列表 | **【待确认】职责文档未列出，但 §4.5 流程需要** |

> 潜在缺口：若只有 `GET /diagnosis/{id}`，C 无法发现"有哪些诊断"。建议补列表端点，或让告警响应内嵌 `diagnosisId` 由 C 直接跳转。【待确认】

### 4.2 `GET /api/v1/topology`

查询参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `rootId` | string | 无 | 从该资源开始；省略则返回全部根节点（host） |
| `depth` | int | `3` | 下钻层数，上限 `8` |
| `kinds` | csv | 无 | 只要这些类型 |
| `includeStale` | bool | `false` | 是否包含 `stale` 资源 |

响应（节点 + 边分离，便于前端布局）：

```json
{
  "data": {
    "nodes": [
      { "id": "host:zsvirt:h1", "kind": "host", "name": "node-01", "status": "running",
        "lastSeenAt": "...", "attributes": { } }
    ],
    "edges": [ { "parentId": "host:zsvirt:h1", "childId": "gpu:zsvirt:g0", "relation": "hosts" } ],
    "truncated": false
  },
  "meta": { "generatedAt": "...", "traceId": "..." }
}
```

**约定**：`truncated: true` 表示因 `depth` 或节点上限被裁剪，C 必须据此提示用户，而不是静默显示不完整拓扑。

### 4.3 `GET /api/v1/events`

| 参数 | 说明 |
|---|---|
| `from` / `to` | ISO8601 时间窗（按 `occurredAt`） |
| `resourceId` | 精确匹配；可重复传 |
| `type` | 事件类型；可重复传 |
| `minSeverity` | `info`\|`warning`\|`error`\|`critical` |
| `limit` | 默认 100，上限 1000 |
| `cursor` | 分页游标 |

### 4.4 `GET /api/v1/alerts`

| 参数 | 说明 |
|---|---|
| `state` | `firing`\|`acked`\|`resolved`\|`silenced` |
| `minSeverity` | 同上 |
| `resourceId` | 受影响资源 |
| `limit` / `cursor` | 分页 |

### 4.5 `GET /api/v1/diagnosis/{id}`

响应即 `DATA_MODEL.md` §5.3 的 `Diagnosis` 对象：

```json
{
  "data": {
    "id": "diag_01J...",
    "createdAt": "2026-09-16T13:41:02Z",
    "trigger": { "alertId": "alert_01J..." },
    "rootCause": "GPU_MEMORY_EXHAUSTED",
    "confidence": 0.92,
    "confidenceBreakdown": [
      { "ruleId": "R-GPU-MEM-001", "contribution": 0.55, "observed": "gpu_memory_usage=98%" },
      { "ruleId": "R-INFER-TIMEOUT-002", "contribution": 0.37, "observed": "inference.timeout x12" }
    ],
    "affectedResources": [ "vm:zsvirt:...", "container:probe:...", "ai_service:probe:..." ],
    "evidence": [
      { "type": "metric", "name": "gpu_memory_usage", "value": "98%", "resourceId": "gpu:zsvirt:...", "at": "..." },
      { "type": "event", "name": "inference.timeout", "count": 12, "eventIds": ["evt_..."] }
    ],
    "recommendation": [ { "code": "REDUCE_CONCURRENCY", "text": "降低推理并发" },
                        { "code": "CHECK_GPU_ALLOCATION", "text": "检查 vGPU 分配" } ],
    "ruleSetVersion": "rs-0.1.0"
  },
  "meta": { "traceId": "..." }
}
```

**与职责文档示例的差异（需团队确认）**：职责文档 §15 的示例中 `confidence` 说明为"规则评分或其他可解释量，不得伪装成未经定义的统计学概率"。本契约据此**新增** `confidenceBreakdown` 与 `ruleSetVersion`，目的是让 `confidence` 可复算、结论可复现。若团队认为字段过重，可降级为可选字段。【待确认】

### 4.6 `GET /api/health`

```json
{
  "status": "ok | degraded | down",
  "components": {
    "database": { "status": "ok", "latencyMs": 3 },
    "zsvirt":   { "status": "degraded", "lastSuccessAt": "2026-09-16T13:30:00Z",
                  "error": "UPSTREAM_TIMEOUT", "staleness": "10m" },
    "ingest":   { "status": "ok", "lastBatchAt": "2026-09-16T13:39:59Z", "unresolvedEvents": 0 }
  },
  "version": { "api": "v1", "build": "..." },
  "generatedAt": "..."
}
```

**用途**：C 顶部状态条数据源；运维排障入口。`degraded` 必须给出**具体是哪个上游坏了**，不能只给整体状态。

### 4.7 B → C 待确认项

| # | 问题 |
|---|---|
| Q8 | 是否需要认证 / 授权？C 如何拿凭据？ |
| Q9 | 路径是否加 `/v1` 版本前缀（§2.1）？ |
| Q10 | `/api/workloads` 的确切语义："工作负载"是 AI 服务、容器、还是 Agent/Task？ |
| Q11 | 是否需要实时推送（SSE / WebSocket），还是纯轮询？ |
| Q12 | 拓扑规模上限（节点/边数量）与前端渲染能力的匹配 |
| Q13 | 诊断是自动触发还是 C 手动触发（是否需要一个 POST 诊断端点）？ |
| Q14 | C 需要哪些枚举/字典（严重级别、根因码、建议码）？需要 B 提供字典端点吗？ |

---

## 5. B → ZSvirt 平台适配契约

**这是 B 的内部实现细节，但对外部环境（ZSvirt 版本与权限）有依赖。**

| # | 问题 | 状态 |
|---|---|---|
| Q15 | ZSvirt API / SDK 的接入方式（REST / SDK / CLI / 数据库） | 【阻塞】未知 |
| Q16 | 认证方式与凭据获取流程（谁提供、如何轮换） | 【阻塞】未知 |
| Q17 | 需要的最小权限集合（只读是否足够？） | 【阻塞】未知 |
| Q18 | ZSvirt 集群版本与 API 版本兼容矩阵 | 【阻塞】未知 |
| Q19 | 资源清单的同步方式：全量快照 / 增量 / 事件订阅 | 【阻塞】未知 |
| Q20 | 是否存在资源变更事件推送，还是只能周期轮询 | 【阻塞】未知 |

> 上述 6 项直接决定 `zsvirt-adapter` 的实现形态，是 **B1 设计的硬阻塞**。需要成员提供 ZSvirt 官方文档与测试集群访问方式。

---

## 6. 契约变更流程（强制）

1. 修改本文件（或 `DATA_MODEL.md` / `TECH-BASELINE.md`）。
2. 在 PR / 提交中显式标注 `BREAKING` 或 `NON-BREAKING`。
3. 通知受影响的 A / C 成员。
4. 同步更新测试与示例响应。
5. **禁止先改代码后补契约。**

---

## 7. 待确认项汇总

| 编号 | 归属 | 内容 |
|---|---|---|
| Q1–Q7 | 成员 A | 上报语义、时间、幂等、鉴权、缓冲 |
| Q8–Q14 | 成员 C | 认证、版本前缀、workloads 语义、推送方式、字典 |
| Q15–Q20 | 团队 / 平台 | ZSvirt API 接入方式与权限 |
| §2.1 | 三方 | 是否引入 `/v1` 版本前缀（与职责文档现有路径不同） |
| §4.1 | 三方 | 是否新增诊断列表端点 |
| §4.5 | 三方 | `confidenceBreakdown` / `ruleSetVersion` 是否采纳 |

---

## 8. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 首轮草案：通用约定、A→B 上报契约、B→C 查询契约、ZSvirt 适配待确认项 | DRAFT，待三方评审 |
