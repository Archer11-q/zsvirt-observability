# Crosslayer — API 契约

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

### 3.1 传输方式（【已确认】）

| 方案 | 说明 | 取舍 |
|---|---|---|
| **HTTP POST（已采纳）** | 探针主动 POST 到 B 的接入端点，批量化 | 实现简单、易调试、无额外中间件；符合"最小依赖"已确认策略 |
| 消息队列 | Kafka / NATS | **已确认排除**（属重大组件，且增加演示复现难度） |
| B 主动拉取 | B 去 VM 内拉 | 需要 A 暴露端点，VM 网络可达性差 |

**已确认**：不引入消息队列，采用 HTTP 批量上报。端点：

```
POST /api/v1/ingest/batch
```

### 3.2 上报体（【已确认】，成员 A 2026-09-16 答复）

> 本节字段经成员 A 逐条确认；A 的完整决策依据见 `agents/docs/REPORTING_CONTRACT.md`。

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

### 3.3 上报语义要求（【已确认】—— 成员 A 2026-09-16 答复）

| # | 结论 | 对 B 的落地要求 |
|---|---|---|
| Q1 | **主保证 + 兜底**：探针维护「已确认资源」缓存；事件引用的资源要么同批次上报，要么此前已被 B 接受。极端竞态允许孤儿引用 | 上报响应须**逐条**返回每个 resource 的接受结果（A 据此更新缓存）；孤儿事件挂 `unresolved` 并**计数暴露为健康指标** |
| Q2 | **至少一次（at-least-once）**，允许重复投递。`batchId` 用 **ULID**，断网重试**复用同一 `batchId`** | 幂等去重以 `batchId` 为唯一键；**重复批次必须返回与首次一致的结果（200 + 已处理标记），不得判定为脏数据** |
| Q3 | ISO 8601 UTC 毫秒 + `Z`；时钟基准优先 **NTP**，退化用宿主机/VM 时钟。每批携带 `sentAt` | B 计算 `receivedAt − sentAt` 作为时钟漂移并**暴露为健康指标**；漂移告警阈值 **\|offset\| > 5s**；关联容差窗口维持 **±30s** |
| Q4 | **默认 5s 一批**；事件实时优先（最多缓冲 1s 即发）。硬上限：`events ≤ 1000`/批、`resources ≤ 500`/批、payload ≤ 1MB。达上限或超时即发（先到先发） | 明确 `429 RATE_LIMITED` 的触发阈值与 `Retry-After` 语义；A 按 `Retry-After`（或默认指数退避）重试，不无脑重放 |
| Q5 | **环境变量 + 配置文件**，不引入服务发现。主通道 `ZSVIRT_OBS_BACKEND_URL`；优先级：环境变量 > 配置文件 > 默认值 | 记入 `DEPLOYMENT.md` §4 |
| Q6 | **本地磁盘缓冲 + FIFO 断点续传**；实现可选嵌入式 SQLite 或 append-only JSONL；缓冲上限默认 100MB 或 10000 条，**超限丢最旧并计数**（丢弃数随后续批次上报）；补传复用原 `batchId` | 对迟到/乱序的历史批次**按 `occurredAt` 追加写入**，不得因 `occurredAt` 早于当前时间而丢弃或误告警 |
| Q7 | **可选共享 token**（`Authorization: Bearer <token>`），**默认关闭**；不引入 mTLS。token 仅从环境变量 `ZSVIRT_OBS_PROBE_TOKEN` 注入 | 强制鉴权时未带/带错 token 返回 `401 UNAUTHENTICATED`；A 收到 401 立即报错停止重试（**不把鉴权失败当网络故障无限重放**） |

### 3.3.1 探针侧 `sourceId` 生成规则（【已确认】，关闭 ADR-0002 阻塞点）

| kind | `sourceId` 规则 | 稳定性来源 |
|---|---|---|
| `container` | 容器 ID 前 12 位（或容器名，若唯一） | 容器生命周期内不变 |
| `process` | `starttime + pid`（取自 `/proc/<pid>/stat`） | 避免 pid 复用冲突 |
| `ai_service` | `服务名 + 监听端口` | 服务实例稳定标识 |
| `agent` | 探针分配的 ULID | 全局唯一 |
| `task` | 探针分配的 ULID | 全局唯一 |

统一要求：**同一 `agentId` 内唯一、生命周期内不变**。`agentId` 形如 `probe-<vm-uuid 前 8 位>`。

> 拼装方式由 B 负责：`{kind}:probe:{agentId}:{sourceId}`（见 `DATA_MODEL.md` §3）。

### 3.4 上报响应（字段按 Q1 诉求扩展）

```json
{
  "data": {
    "batchId": "01J...",
    "duplicate": false,
    "accepted": { "resources": 3, "events": 12 },
    "resources": [
      { "index": 0, "sourceId": "web-0", "kind": "container",
        "globalId": "container:probe:probe-3f2a9c10:web-0", "result": "accepted" },
      { "index": 1, "sourceId": "vllm", "kind": "ai_service",
        "globalId": "ai_service:probe:probe-3f2a9c10:vllm", "result": "accepted" }
    ],
    "rejected": [ { "index": 4, "reason": "SCHEMA_VALIDATION_FAILED", "detail": "occurredAt missing" } ]
  },
  "meta": { "traceId": "...", "serverTime": "2026-09-16T13:40:00.123Z" }
}
```

**约定**：
1. **部分接受允许**（207 语义用 200 + `rejected` 表达）。被拒条目必须给出 `reason` + `detail` 与原始 `index`，便于 A 定位探针 bug。
2. `resources[]` **逐条**返回接受结果与拼装后的 `globalId` —— 这是 A 维护「已确认资源」缓存的依据（Q1 诉求）。
3. 重复批次（相同 `batchId`）返回 `duplicate: true` 与**与首次一致**的 `accepted`/`resources`，HTTP 仍为 `200`（Q2 诉求）。
4. `serverTime` 供 A 侧计算时钟漂移（Q3 诉求）。

---

## 4. B → C 查询契约

### 4.1 端点清单（【已确认】，含成员 C 2026-09-17 评审结论）

| 方法 | 路径 | 用途 | 状态 |
|---|---|---|---|
| GET | `/api/health` | 健康 / 降级状态（**无版本前缀**） | 【已确认】 |
| GET | `/api/v1/topology` | 资源拓扑（可指定根节点与深度） | 【已确认】 |
| GET | `/api/v1/workloads` | **`ai_service` 层的业务聚合视图**（见 §4.4.1） | 【已确认】语义 |
| GET | `/api/v1/events` | 事件查询（时间 / 资源 / 类型 / 严重级别过滤） | 【已确认】 |
| GET | `/api/v1/alerts` | 告警列表（按状态 / 严重级别过滤） | 【已确认】 |
| GET | `/api/v1/diagnosis/{id}` | 单个诊断详情 | 【已确认】 |
| GET | `/api/v1/diagnoses` | 诊断列表（C 要求，否则无法发现无告警关联的诊断） | **【已确认】新增** |
| POST | `/api/v1/diagnoses` | 手动触发一次诊断（C 要求，演示与联调需要） | **【已确认】新增** |
| GET | `/api/v1/dict` | 枚举字典（含 rootCause / recommendation 可读文案） | **【已确认】新增** |

**已确认变更说明**（依据 C 的 `frontend/FRONTEND_DESIGN.md`）：
1. 采纳 `/api/v1` 版本前缀（Q9），`/api/health` 保持无前缀。
2. 新增诊断列表与手动触发端点（Q13）。
3. 新增字典端点（Q14），避免前后端枚举与文案漂移。
4. `/api/workloads` 语义定为 `ai_service` 聚合（Q10）。
5. 首版**纯轮询**，不引入 SSE / WebSocket；SSE 列为可选演进项（Q11）。
6. 拓扑单次响应上限 **节点 ≤ 200、边 ≤ 400**（Q12），超限必须 `truncated: true`。
7. 首版**不引入用户级认证**；可选**单一只读 Bearer Token**（Q8），见 §4.8。

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

**规模上限（【已确认】，C 提案）**：单次响应 **节点 ≤ 200、边 ≤ 400**。超限时由服务端通过 `rootId` / `depth` / `kinds` 裁剪，并置 `truncated: true`。

### 4.2.1 `GET /api/v1/topology` 的资源状态语义（【已确认】，修正 C 指出的字段歧义）

C 指出 `DATA_MODEL.md` §4.1 的 `status` 与 §7 生命周期是两套词汇。**已确认拆分**：

| 字段 | 取值 | 含义 | 用途 |
|---|---|---|---|
| `status` | `running` \| `stopped` \| `error` \| `unknown` | **业务状态**（来源系统给出） | 前端状态标签与颜色 |
| `lastSeenAt` | timestamp | B 最近一次观测到该资源的时间 | "数据新鲜度"展示 |
| `staleness` | string \| null | 距上次观测的时长（如 `"10m"`） | 前端提示"数据陈旧" |
| `observability` | `active` \| `stale` \| `gone` | **B 的观测状态**（内部生命周期） | 排障；`includeStale` 参数据此过滤 |

**结论：`status` 与 `observability` 是两个独立字段，不得混用。** `stale` / `gone` 永远不出现在 `status` 中。

### 4.3 `GET /api/v1/workloads`（【已确认】语义）

**定义：`ai_service` 层级的业务聚合视图** —— 不是容器，也不是 Agent/Task。

每个 workload 至少包含：

| 字段 | 说明 |
|---|---|
| `id` | `ai_service` 资源 ID（不透明） |
| `name` | 展示名 |
| `status` | 业务状态（见 §4.2.1） |
| `attributes` | `framework` / `modelName` / `endpoint` / `concurrency` / `qps` |
| `resourceUsage` | 底层资源占用：GPU 利用率、显存、内存 |
| `alertCount` / `eventCount` | 关联告警 / 事件计数 |
| `diagnosisId` | 关联诊断（若有，否则 `null`） |

> 容器属基础设施细节（看拓扑），Agent/Task 属更细粒度（看服务详情）。

### 4.4 `GET /api/v1/events`

| 参数 | 说明 |
|---|---|
| `from` / `to` | ISO8601 时间窗（按 `occurredAt`） |
| `resourceId` | 精确匹配；可重复传 |
| `type` | 事件类型；可重复传 |
| `minSeverity` | `info`\|`warning`\|`error`\|`critical` |
| `limit` | 默认 100，上限 1000 |
| `cursor` | 分页游标 |

**轮询约定（【已确认】）**：接口必须**幂等**且**游标分页稳定**，避免 C 轮询时重复或漏数据。

### 4.5 `GET /api/v1/alerts`

| 参数 | 说明 |
|---|---|
| `state` | `firing`\|`acked`\|`resolved`\|`silenced` |
| `minSeverity` | 同上 |
| `resourceId` | 受影响资源 |
| `limit` / `cursor` | 分页 |

### 4.6 `GET /api/v1/diagnoses` 与 `POST /api/v1/diagnoses`（【已确认】新增）

**C 的诉求**：只有 `GET /diagnosis/{id}` 时前端无法发现"有哪些诊断"，且演示需要可控的「一键诊断」时机。

```
GET  /api/v1/diagnoses?state=&from=&to=&limit=&cursor=
POST /api/v1/diagnoses
```

`POST` 请求体（与 `DATA_MODEL.md` 的 `Diagnosis.trigger` 对齐）：

```json
{ "alertId": "alert_01J..." }
```
或
```json
{ "window": { "from": "...", "to": "..." }, "anchorResourceId": "vm:zsvirt:..." }
```

响应：
- **同步**（推荐，诊断是规则计算，通常 <1s）：`200` + §4.7 的 `Diagnosis` 对象。
- 若超时：`202 Accepted` + `{ "diagnosisId": "...", "status": "pending" }`，C 轮询 `GET /api/v1/diagnosis/{id}`。

### 4.6.1 `GET /api/v1/dict`（【已确认】新增）

前端启动时拉取并缓存。**含可读文案，避免前端硬编码中文映射导致前后端漂移。**

```json
{
  "data": {
    "severity": { "info": "提示", "warning": "警告", "error": "错误", "critical": "严重" },
    "alertState": { "firing": "触发中", "acked": "已确认", "resolved": "已恢复", "silenced": "已静默" },
    "resourceKind": { "host": "宿主机", "gpu": "GPU", "vgpu": "vGPU", "vm": "虚拟机",
                      "container": "容器", "process": "进程", "ai_service": "AI 服务",
                      "agent": "智能体", "task": "任务" },
    "resourceStatus": { "running": "运行中", "stopped": "已停止", "error": "异常", "unknown": "未知" },
    "eventType": { },
    "rootCause": { "GPU_MEMORY_EXHAUSTED": "GPU 显存耗尽", "UNKNOWN": "未识别" },
    "recommendation": { "REDUCE_CONCURRENCY": "降低推理并发" }
  },
  "meta": { "generatedAt": "...", "traceId": "..." }
}
```

> `eventType` 与 `rootCause` / `recommendation` 的**完整枚举需三方共同定义**（见 §8）。
> 字典端点是枚举的**唯一对外真源**，B 变更枚举必须同步该端点并通知 C。

### 4.7 `GET /api/v1/diagnosis/{id}`

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

### 4.8 `GET /api/health`

> 补充字段：`zsvirt.clockDriftMs`、`ingest.unresolvedEvents`、`ingest.discarded`（见 §3.3 Q3/Q1/Q6 的 B 侧落地要求）。

```json
{
  "status": "ok | degraded | down",
  "components": {
    "database": { "status": "ok", "latencyMs": 3 },
    "zsvirt":   { "status": "degraded", "lastSuccessAt": "2026-09-16T13:30:00Z",
                  "error": "UPSTREAM_TIMEOUT", "staleness": "10m" },
    "ingest":   { "status": "ok", "lastBatchAt": "2026-09-16T13:39:59Z",
                  "unresolvedEvents": 0, "clockDriftMs": 120, "discarded": 0 },
    "gpuProvider": { "status": "ok", "mode": "zsvirt-zwatch | guest-smi | simulated",
                     "lastSuccessAt": "..." }
  },
  "version": { "api": "v1", "build": "..." },
  "generatedAt": "..."
}
```

**用途**：C 顶部状态条数据源；运维排障入口。`degraded` 必须给出**具体是哪个上游坏了**，不能只给整体状态。

**`clockDriftMs`**：探针时钟漂移监测值（A 的 Q3 诉求）。超过 `5000ms` 时 `ingest.status` 应为 `degraded`。

**`gpuProvider.mode`**：当前 GPU 数据渠道。**演示时必须能显示是否为模拟模式**，不得把模拟数据伪装成真实数据。

### 4.9 认证（【已确认】，C 的 Q8 结论）

| 项 | 结论 |
|---|---|
| 首版用户级认证 / 授权 | **不引入**（无 OAuth / mTLS / RBAC） |
| 可选防护 | **单一只读 Bearer Token** |
| B 侧 | 校验 `Authorization: Bearer <token>`；token 经环境变量注入；仓库只留 `.env.example` |
| C 侧 | 通过 `VITE_API_TOKEN` 构建时注入，前端统一加请求头 |
| 不引入时 | `401 UNAUTHENTICATED` / `403 PERMISSION_DENIED` 标注为**预留**，本阶段不启用 |
| 默认 | **关闭**（与 A 的 Q7 结论一致） |

### 4.10 B → C 待确认项（【已全部关闭】）

| # | 结论 | 依据 |
|---|---|---|
| Q8 | 不引入用户级认证；可选单一只读 Bearer Token | C §2 Q8 |
| Q9 | **采纳 `/api/v1` 前缀**；`/api/health` 无前缀 | C §2 Q9 |
| Q10 | `/api/workloads` = **`ai_service` 聚合视图** | C §2 Q10 |
| Q11 | **首版纯轮询**；SSE 列为可选演进（SSE 优先于 WebSocket） | C §2 Q11 |
| Q12 | 拓扑上限**节点 ≤ 200、边 ≤ 400**，超限 `truncated: true` | C §2 Q12 |
| Q13 | **自动 + 手动都要** → 新增 `POST` / `GET /api/v1/diagnoses` | C §2 Q13 |
| Q14 | **需要字典端点** `GET /api/v1/dict`，含可读文案 | C §2 Q14 |

**C 的轮询节奏（B 已知悉，实现须支撑）**：`/health` 3s；拓扑 / 告警 / 事件列表 10–15s；诊断详情 30s。

---

## 5. B → ZSvirt 平台适配契约

**这是 B 的内部实现细节，但对外部环境（ZSvirt 版本与权限）有依赖。**

| # | 问题 | 状态 |
|---|---|---|
| Q15 | ZSvirt API / SDK 的接入方式（REST / SDK / CLI / 数据库） | ✅ **已确认**（2026-09-23，命题方）：**REST**，公开 API 文档入口 + 9 个 ZWatch 路由后缀 |
| Q16 | 认证方式与凭据获取流程（谁提供、如何轮换） | ✅ **已确认**：`Authorization: OAuth <token>`；AccessKey 可换会话，**会话约 2 小时**，需自行续期（适配器已实现提前续期） |
| Q17 | 需要的最小权限集合（只读是否足够？） | 🔶 **原则已确认**（本项目只申请只读），**只读账号的配置流程待落实**（第二轮提问 §3.3） |
| Q18 | ZSvirt 集群版本与 API 版本兼容矩阵 | ✅ **已确认**：实测 `ZStack-ZSvirt 1.0.0.93`，主版本与开源仓库 `VERSION` 一致 |
| Q19 | 资源清单的同步方式：全量快照 / 增量 / 事件订阅 | 🔶 **部分确认**：指标走**周期查询**（`GetMetricData`，已实现）；`QueryHost` / `QueryVmInstance` 资源清单尚未接入（不影响当前链路） |
| Q20 | 是否存在资源变更事件推送，还是只能周期轮询 | 🔶 **部分确认**：存在事件查询接口（`GetEventData`）与订阅配置接口（`QueryEventSubscription`），**未见推送机制**；本项目采用轮询（与前端 Q11 的纯轮询决策一致） |

> **2026-09-23 更新**：命题方已逐条答复，上述问题**不再是硬阻塞**。适配器
> （`app/zsvirt/watch.py`）按已确认的路由与指标名实现；仍待确认的接口字段名见
> 第二轮提问 [`ZSVIRT_QA_ROUND2.md`](ZSVIRT_QA_ROUND2.md) §1（外部阻塞 X-12）。

---

## 6. 契约变更流程（强制）

1. 修改本文件（或 `DATA_MODEL.md` / `TECH-BASELINE.md`）。
2. 在 PR / 提交中显式标注 `BREAKING` 或 `NON-BREAKING`。
3. 通知受影响的 A / C 成员。
4. 同步更新测试与示例响应。
5. **禁止先改代码后补契约。**

---

## 7. 待确认项汇总

| 编号 | 归属 | 内容 | 状态 |
|---|---|---|---|
| Q1–Q7 | 成员 A | 上报语义、时间、幂等、鉴权、缓冲 | ✅ **已确认**（2026-09-16） |
| Q8–Q14 | 成员 C | 认证、版本前缀、workloads 语义、推送方式、字典 | ✅ **已确认**（2026-09-17） |
| Q15–Q20 | 团队 / 命题方 | ZSvirt API 接入方式与权限 | ✅ **已答复**（2026-09-23）；Q17/Q19/Q20 部分落实，见 §5 |
| §2.1 | 三方 | `/v1` 版本前缀 | ✅ **已确认：采纳** |
| §4.1 | 三方 | 诊断列表与手动触发端点 | ✅ **已确认：新增** |
| §4.6.1 | 三方 | 字典端点 | ✅ **已确认：新增** |
| §4.7 | 三方 | `confidenceBreakdown` / `ruleSetVersion` | ✅ **已确认：采纳**（保证 confidence 可复算） |
| §8 | 三方 | `event.type` / `rootCause` / `recommendation` 完整枚举 | ❌ **待三方共同冻结** |
| — | 团队 | GPU/vGPU 指标渠道（ZWatch 是否可用） | ✅ **已确认可用并已接入**：ZWatch 已启用，`GetMetricData` / `GetAllMetricMetadata` 实测可返回 5 个 GPU 指标；实现见 `app/zsvirt/watch.py` |

---

## 8. 待三方共同冻结的枚举（阻塞诊断与字典端点）

`event.type` 是跨层关联的锚点，必须在实现前冻结。

| 枚举 | 用途 | 归属 |
|---|---|---|
| `event.type` | 事件类型（如 `gpu.memory.exhausted`、`container.oom_killed`、`inference.timeout`、`agent.network.timeout`） | 三方 |
| `rootCause` | 根因码 | B 提案，三方确认 |
| `recommendation` | 处置建议码 + 可读文案 | B 提案，三方确认 |

**命名规范**：`{domain}.{subject}.{qualifier}`，全小写点分。**类型名属契约，改名等于破坏性变更。**

---

## 9. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 首轮草案：通用约定、A→B 上报契约、B→C 查询契约、ZSvirt 适配待确认项 | DRAFT，待三方评审 |
| v0.2 | 2026-09-16 | **Q1–Q7 全部关闭**（采纳成员 A 答复）：至少一次投递、`batchId` 幂等、5s/1MB 批量上限、env 配置、断网缓冲、可选 token；响应体新增 `resources[]` 逐条结果、`duplicate`、`serverTime`；新增 `sourceId` 生成规则 | 已确认 |
| v0.3 | 2026-09-17 | **Q8–Q14 全部关闭**（采纳成员 C 评审）：采纳 `/v1` 前缀、`/workloads` 定为 `ai_service` 聚合、首版纯轮询、拓扑上限 200/400、**新增 `POST`/`GET /api/v1/diagnoses` 与 `GET /api/v1/dict`**；修正 `status` 与观测状态字段歧义（§4.2.1）；`/api/health` 增补时钟漂移与 GPU provider 模式 | 已确认 |
