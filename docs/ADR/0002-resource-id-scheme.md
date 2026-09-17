# ADR-0002：资源 ID 采用 `{kind}:{source}:{sourceId}` 方案

| 字段 | 值 |
|---|---|
| 状态 | **Accepted（2026-09-17 由 Proposed 转正）** |
| 日期 | 2026-09-16 |
| 决策人 | 成员 B 提案，需成员 A、C 确认 |
| 影响范围 | `DATA_MODEL.md`、`API_CONTRACT.md`、A 探针侧 ID 生成、C 前端 ID 处理 |
| 关联文档 | `DATA_MODEL.md` §3 |

---

## 背景

系统需要跨层关联 `宿主机 → GPU/vGPU → VM → 容器/进程 → AI 服务/Agent`。

这些资源由**两个不同来源**定义：

- **ZSvirt 平台**：权威定义宿主机、GPU、vGPU、VM（平台侧 UUID）。
- **成员 A 的 VM 内探针**：权威定义容器、进程、AI 服务、Agent（ZSvirt 看不到 VM 内部）。

如果不由规则约束 ID，会出现三个具体问题：

1. **ID 冲突**：ZSvirt 与探针各自生成 ID，可能碰撞。
2. **语义不明**：联调时看不出来某个 ID 是谁生成的，排障靠猜。
3. **契约脆弱**：一旦两个来源的 ID 规则改变，跨层关联全部断裂。

## 决策

资源 ID 采用统一格式，由 B 在标准化层统一拼装：

```
{kind}:{source}:{sourceId}
```

| 段 | 取值 |
|---|---|
| `kind` | `host` \| `gpu` \| `vgpu` \| `vm` \| `container` \| `process` \| `ai_service` \| `agent` \| `task` |
| `source` | `zsvirt` \| `probe` \| `manual` |
| `sourceId` | 来源系统内的原生标识（ZSvirt 为平台 UUID；探针侧由 A 定义） |

示例：

```
vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44
container:probe:<agentId>:web-0
ai_service:probe:<agentId>:vllm
```

配套约束：

- ID 对消费方**不透明**：C 不得解析其结构，只能整体比对与传递。
- ID **不可变**：资源重建视为新资源，不复用旧 ID。
- ID **不承载语义**：不得从中推断状态、时间或健康度。

## 备选方案

| 方案 | 优点 | 缺点 | 为何未选 |
|---|---|---|---|
| `{kind}:{source}:{sourceId}` | 可追溯来源；两来源互不冲突；允许 `manual` 手工登记资源不阻塞演示 | ID 较长；需要 B 做拼装层 | **提案** |
| 纯 UUID（`uuid4`） | 简单、定长、无歧义 | **丢失来源信息**，排障时无法判断 ID 归属；手工登记的资源无自然来源 | 可追溯性不满足诊断需求 |
| 直接用 ZSvirt UUID | 与平台一致 | 无法覆盖容器/进程/AI 服务（平台无此概念）；探针侧被迫伪造 UUID | 覆盖不了资源层级 |
| 直接用探针生成的 ID | 探针侧简单 | 与平台 ID 体系冲突；VM 边界无法对齐 | 破坏跨层关联 |
| 自增整数 | 短 | 跨来源无法协调；分布式下不可用；重建后语义混乱 | 不适用 |

## 后果

### 正面

- 跨层关联有唯一锚点：Push 数据用 `vmId` 挂到 Pull 建立的 VM 节点下。
- 排障时可立即判断某个 ID 由谁定义。
- `source=manual` 提供演示期的兜底手段，避免阻塞联调。

### 负面 / 代价

- ID 字符串较长，索引体积略增。
- 需要成员 A 明确探针侧 `sourceId` 的生成规则 —— ✅ **已解决**（2026-09-16，见下方「决策更新」）。
- 需要 C 遵守"ID 不透明"约定，不能在前端做字符串切割。

### 需要后续跟进

1. ~~成员 A 确认探针侧 `sourceId` 生成规则~~ → ✅ **A 已给出规则**，见下方「决策更新」。
2. 成员 C 确认接受不透明 ID 约定 —— ✅ **C 已遵守**（`frontend/FRONTEND_DESIGN.md` §2 Q14 明确"遵守不解析 ID"）。
3. 确认 `vgpu` 是否作为独立 kind —— ⚠️ ZSvirt 源码表明确有 `MdevDevice` 概念，倾向**是**；仍需测试环境字段确认。
4. 确认 `task` 是否纳入首版 —— ❌ 待定。

---

## 决策更新（2026-09-16 / 2026-09-17）

### 状态变更

| 项 | 变更 |
|---|---|
| 状态 | `Proposed` → **`Accepted`** |
| 依据 | ① 成员 A 在 `agents/docs/REPORTING_CONTRACT.md` §3 给出 `sourceId` 生成规则，**关闭本 ADR 的唯一阻塞点**；② 成员 C 明确遵守"ID 不透明"约定 |
| 生效 | 已回写至 `API_CONTRACT.md` §3.3.1 与 `DATA_MODEL.md` §3 |

### 探针侧 `sourceId` 生成规则（成员 A 确认）

| kind | 规则 | 稳定性来源 |
|---|---|---|
| `container` | 容器 ID 前 12 位（或容器名，若唯一） | 容器生命周期内不变 |
| `process` | `starttime + pid`（取自 `/proc/<pid>/stat`） | **避免 pid 复用冲突** |
| `ai_service` | 服务名 + 监听端口 | 服务实例稳定标识 |
| `agent` | 探针分配的 ULID | 全局唯一 |
| `task` | 探针分配的 ULID | 全局唯一 |

统一要求：**同一 `agentId` 内唯一、生命周期内不变**。`agentId` 形如 `probe-<vm-uuid 前 8 位>`。

**B 侧拼装方式**：`{kind}:probe:{agentId}:{sourceId}`
例：`container:probe:probe-3f2a9c10:web-0`

> **设计确认点**：A 选择 `starttime + pid` 而非裸 `pid`，正确规避了容器内 pid 复用导致的
> ID 漂移 —— 这是本方案能成立的关键细节，已在 `DATA_MODEL.md` §3.3 的"ID 不可变"约束下验证通过。

## 回滚方案

若本方案被否决，需要修改 `DATA_MODEL.md` §3、`API_CONTRACT.md` 中的 ID 示例与 `vmId` 字段语义，并同步 A 与 C。**必须在 B2 数据接入开工前确定**——一旦有数据落库，更换 ID 方案需要数据重写，属严重破坏性变更（见 `COMPATIBILITY.md` §3）。

## 验证方式

- 集成测试：A 上报容器事件 → B 正确拼装为 `container:probe:<agentId>:web-0` → 拓扑查询能返回该容器挂在正确 VM 下。
- 负向测试：上报不存在的 `vmId` → 资源挂 `unresolved` 占位节点，不丢数据、不报 500。
