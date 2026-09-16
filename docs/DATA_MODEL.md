# ZSvirt Observability — 数据模型

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 未经团队评审，不得作为实现依据** |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B |
| 最后更新 | 2026-09-16 |
| 评审人 | 成员 A、成员 C（待评审） |

> 约定：`【待确认】` 表示需团队决策；`【提案】` 表示成员 B 的推荐方案；`TBD` 表示字段语义或取值尚未确定。
> **本文件中的字段在未确认前一律标记为待定，不得在代码中当作既定事实实现。**

---

## 1. 设计目标

1. **可追溯**：从宿主机一路下钻到 AI Agent，任意两层之间可双向导航。
2. **稳定 ID**：任何对象在生命周期内 ID 不变，且能反查到来源系统。
3. **不编造**：来源系统没给的信息，一律 `null` + 显式标记，不用默认值猜测。
4. **可解释**：每个诊断结论都能指回具体证据记录。

---

## 2. 资源对象与层级

```
Host (宿主机)
 └── GPU (物理 GPU)
      └── vGPU (虚拟 GPU / MIG 实例)        【待确认】是否为独立实体
           └── VM (ZSvirt 虚拟机)  ← 赛题边界
                ├── Container (容器)
                │    └── Process (进程)
                └── Process (VM 内直接运行的进程)
                     └── AIService (AI 服务)
                          └── Agent (智能体) / Task (任务)
```

> **待确认**：`vGPU` 是否作为独立资源实体，还是仅作为 GPU 的一个属性（如 `gpu.partitionProfile`）。ZSvirt 对 vGPU 的建模方式决定此选择（R1）。

### 2.1 资源类型枚举

| kind | 说明 | ID 归属（权威来源） |
|---|---|---|
| `host` | 物理宿主机 | ZSvirt |
| `gpu` | 物理 GPU | ZSvirt |
| `vgpu` | 虚拟 GPU / 分区 | ZSvirt【待确认】 |
| `vm` | ZSvirt 虚拟机（**边界对象**） | ZSvirt |
| `container` | VM 内容器 | A 探针 |
| `process` | 进程 | A 探针 |
| `ai_service` | AI 服务（如推理服务） | A 探针 |
| `agent` | 智能体 | A 探针 |
| `task` | 一次任务 / 请求批次 | A 探针【待确认】 |

---

## 3. 资源 ID 语义（【提案】，关键契约）

### 3.1 格式

```
{kind}:{source}:{sourceId}
```

| 段 | 含义 | 示例 |
|---|---|---|
| `kind` | 资源类型枚举（§2.1） | `vm` |
| `source` | 权威来源系统：`zsvirt` \| `probe` \| `manual` | `zsvirt` |
| `sourceId` | 来源系统内的原生标识 | ZSvirt VM UUID |

示例：

```
vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44
container:probe:9d1c...:web-0        # 探针侧原生 ID 由 A 定义【待确认】
ai_service:probe:9d1c...:vllm
```

### 3.2 为什么这样设计

- **可追溯**：一眼看出这个资源是谁定义的，避免"这个 ID 是谁生成的"这类联调扯皮。
- **不冲突**：ZSvirt 与探针各管一段，不存在两个系统同时claiming 同一个 ID。
- **可降级**：`source=manual` 允许演示期手工登记资源，不阻塞联调。
- **代价**：ID 较长；需在 API 层保证不把它当主键暴露给 C 的展示逻辑（C 只应把它当不透明字符串）。

### 3.3 约束

- ID **不透明**：消费方（C）不得解析 ID 结构，只能整体比对与传递。
- ID **不可变**：资源重建（例如 VM 重建）视为新资源，不复用旧 ID。
- ID **不承载语义**：不得从 ID 推断状态、时间或健康度。

> **【待确认】** 是否采纳本方案；探针侧 `sourceId` 的生成规则需成员 A 确认。

---

## 4. 资源对象字段

### 4.1 公共字段（所有资源）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 见 §3，不透明稳定 ID |
| `kind` | enum | ✅ | 资源类型 |
| `name` | string | ⬜ | 展示名，可能重复，**不得用作标识** |
| `parentId` | string \| null | ⬜ | 上级资源 ID，构成层级 |
| `status` | enum | ✅ | `running` \| `stopped` \| `error` \| `unknown` |
| `firstSeenAt` | timestamp | ✅ | B 首次观测到该资源的时间 |
| `lastSeenAt` | timestamp | ✅ | B 最近一次观测到该资源的时间 |
| `attributes` | object | ⬜ | 类型特有属性（见下） |
| `labels` | object | ⬜ | 键值标签 |

> `status = unknown` 是一等公民：远程系统不可达时必须显式降级，不得伪造 `running`。

### 4.2 类型特有属性（`attributes`）

| kind | 关键属性 | 状态 |
|---|---|---|
| `host` | `cpuCores`, `memTotalBytes`, `agentVersion`, `clusterId` | 【待确认】以 ZSvirt 实际字段为准 |
| `gpu` | `vendor`, `model`, `memTotalBytes`, `memUsedBytes`, `utilizationPct`, `temperatureC` | 【待确认】 |
| `vgpu` | `profile`, `memQuotaBytes`, `ownerVmId` | 【待确认】 |
| `vm` | `uuid`, `vcpu`, `memBytes`, `hostId`, `state`, `ipAddresses[]` | 【待确认】 |
| `container` | `image`, `runtime`, `restartCount`, `cpuLimit`, `memLimitBytes` | 【待确认】 |
| `process` | `pid`, `ppid`, `cmdline`, `rssBytes`, `cpuPct` | 【待确认】 |
| `ai_service` | `framework`, `modelName`, `endpoint`, `concurrency`, `qps` | 【待确认】 |
| `agent` | `agentType`, `sessionId`, `taskRef` | 【待确认】 |

**所有 `attributes` 中的字段均为 TBD**：须以 ZSvirt API 实际返回与 A 探针实际采集能力为准，成员 B 不预设。

---

## 5. 事件与告警对象

### 5.1 `Event` — 不可变观测记录

事件是**只追加、不修改**的事实记录，是诊断的证据来源。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 事件唯一 ID（建议 `evt_` + ULID，单调时间有序） |
| `occurredAt` | timestamp | ✅ | 事件实际发生时间（产生方给出） |
| `receivedAt` | timestamp | ✅ | B 接收时间 |
| `source` | enum | ✅ | `zsvirt` \| `probe` \| `derived` |
| `resourceId` | string | ✅ | 关联资源 ID（可能是占位 `unresolved`） |
| `type` | string | ✅ | 事件类型，如 `gpu.memory.high`、`inference.timeout` |
| `severity` | enum | ✅ | `info` \| `warning` \| `error` \| `critical` |
| `message` | string | ⬜ | 人类可读描述 |
| `metrics` | object | ⬜ | 随事件携带的数值观测 |
| `raw` | object | ⬜ | 原始上报载荷（保留可追溯性） |
| `traceId` | string | ⬜ | 若来自分布式追踪 |

> **`type` 命名规范【提案】**：`{domain}.{subject}.{qualifier}`，全小写点分，如 `gpu.memory.exhausted`、`vm.disk.io_saturated`、`agent.network.timeout`。**类型名属于契约，改名等于破坏性变更。**

### 5.2 `Alert` — 有状态的判断

告警由事件/指标经规则产生，**有生命周期**。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 告警 ID |
| `ruleId` | string | ✅ | 触发它的规则 ID（保证可解释） |
| `resourceId` | string | ✅ | 告警对象 |
| `severity` | enum | ✅ | 同 `Event.severity` |
| `state` | enum | ✅ | `firing` \| `acked` \| `resolved` \| `silenced` |
| `firstFiredAt` | timestamp | ✅ | 首次触发 |
| `lastFiredAt` | timestamp | ✅ | 最近触发 |
| `resolvedAt` | timestamp \| null | ⬜ | 恢复时间 |
| `count` | integer | ✅ | 聚合计数（去重后的触发次数） |
| `evidenceEventIds` | string[] | ✅ | **指向触发本告警的证据事件** |
| `aggregationKey` | string | ✅ | 聚合/去重键 |
| `diagnosisId` | string \| null | ⬜ | 关联诊断（若有） |

**红线**：`evidenceEventIds` 为必填。没有证据的告警不允许存在。

### 5.3 `Diagnosis` — 根因结论

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 诊断 ID |
| `createdAt` | timestamp | ✅ | 生成时间 |
| `trigger` | object | ✅ | 触发方式：`{ alertId }` 或 `{ window, anchorResourceId }` |
| `rootCause` | string | ✅ | 根因候选码，如 `GPU_MEMORY_EXHAUSTED`；无匹配时 `UNKNOWN` |
| `confidence` | number | ✅ | **规则评分**，`0.0–1.0`，非统计学概率；必须可解释 |
| `confidenceBreakdown` | object[] | ✅ | 各规则的得分贡献，**保证 confidence 可追溯** |
| `affectedResources` | string[] | ✅ | 影响范围（按资源图传播得出） |
| `evidence` | object[] | ✅ | 证据列表，见下 |
| `recommendation` | string[] | ✅ | 处置建议（枚举码 + 可读文案） |
| `ruleSetVersion` | string | ✅ | 产出该结论的规则集版本（结论可复现的前提） |

`evidence` 条目：

```json
{ "type": "metric", "name": "gpu_memory_usage", "value": "98%", "resourceId": "gpu:zsvirt:...", "at": "..." }
{ "type": "event",  "name": "inference_timeout", "count": 12, "eventIds": ["evt_..."] }
```

**红线**：
- `evidence` 不得为空。空证据的诊断只能返回 `rootCause = UNKNOWN`。
- `confidence` 必须能由 `confidenceBreakdown` 复算出来。
- 必须携带 `ruleSetVersion`，否则结论不可复现。

---

## 6. 实体关系

```
Host 1──* GPU 1──* vGPU? 1──* VM 1──* Container 1──* Process
                                  │                      │
                                  └──────* Process ──────┘
                                              │
                                              *── AIService 1──* Agent 1──* Task?
Resource 1──* Event          （resourceId）
Resource 1──* Alert          （resourceId）
Alert    1──0..1 Diagnosis    （diagnosisId）
Alert    *──* Event          （evidenceEventIds）
Diagnosis *──* Event / 指标观测（evidence）
```

关系存储：**邻接表**（`resource_edge(parent_id, child_id, relation_type)`）。
【待确认】是否需要支持多重关系类型（如 `hosts`、`part-of`、`depends-on`），还是单一父子层级即可。

---

## 7. 生命周期与状态语义

| 对象 | 状态机 |
|---|---|
| Resource | `discovered → active → stale → gone`（`stale` 由 `lastSeenAt` 超时判定，**不立即删除**） |
| Alert | `firing → acked → resolved`；旁路 `firing → silenced` |
| Event | 无状态，只追加，永不修改 |
| Diagnosis | 一次性生成，可被新诊断取代；**不修改既有记录** |

**删除策略**：资源不物理删除，只标记 `gone` 并保留历史，否则历史事件会变成孤儿，破坏可追溯性。【提案】

---

## 8. 未决问题（阻塞契约冻结）

1. `vGPU` 是否为独立实体（R1）。
2. ZSvirt 对各资源类型实际暴露的字段清单（R4）。
3. A 探针生成 `container` / `process` / `ai_service` / `agent` 原生 ID 的规则（R5）。
4. `task` 是否纳入首版模型。
5. 事件 `type` 的完整枚举——**必须三方共同定义**，因为它是跨层关联的锚点。
6. 时间容差窗口取值。
7. 是否需要多租户 / 权限维度。

---

## 9. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 首轮草案：资源层级、ID 方案提案、Event/Alert/Diagnosis 字段、生命周期 | DRAFT，待评审 |
