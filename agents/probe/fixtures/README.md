# fixtures/ — 稳定样例载荷（成员 A → 成员 B 契约测试用）

交付 `docs/DEVELOPMENT_PLAN.md` §5.4.1 的 ⬜ **「稳定样例数据」**。
B 的任务 5/6（A→B 契约测试、集成测试补强）可直接把下列 JSON 作为
`POST /api/v1/ingest/batch` 的请求体使用。

## 文件清单

| 文件 | 场景（D-070） | 资源 | 事件 | 覆盖的事件类型 |
|---|---|---|---|---|
| `normal.json` | 正常态 | 7（5 种 kind 全覆盖） | 0（**空批**，测 B 的空 events 处理） | — |
| `container_oom_killed.json` | 场景二 · 容器/进程异常（赛题点名） | 5 | 3 | `container.oom_killed` / `process.crash` / `container.restart` |
| `gpu_memory_exhausted.json` | 场景一 · GPU 瓶颈（探针症状侧） | 4 | 2 | `process.io_wait.high` ×2 |
| `agent_network_failure.json` | 场景三 · Agent/网络异常 | 4（含 `agent`/`task`） | 5 | `container.network.unreachable` / `inference.timeout` / `inference.error` / `agent.network.timeout` / `agent.task.failed` |

资源 kind 覆盖探针全部 5 种：`container` / `process` / `ai_service` / `agent` / `task`。

## 稳定性保证

- **时间戳固定**在 `2026-09-17T08:00:00.000Z` 附近（事件按场景时间线递增）。
- **batchId 与 agent/task 的 sourceId 为固定 ULID**（时间位取自固定基准，随机位写死）。
- 由探针**同一套管道**生成（`model.Resource`/`Event` → `to_payload()` →
  `reporter.build_batch()`），与探针运行时上报格式一致。
- 生成器内置契约自校验：字段完整性、`sourceId`/`agentId` 无冒号、`vmId` 形态、
  F-01 事件枚举、severity 枚举、ULID/时间格式、Q4 上限（≤1000 事件 / ≤500 资源 / ≤1MB）、
  **resourceRef 引用自洽**（指向本批已上报资源）。重复运行输出 byte 级一致。

重新生成（在仓库根）：

```bash
python agents/probe/fixtures/generate_samples.py
```

## 关键 ID（全部样例一致）

| 项 | 值 |
|---|---|
| `vmId` | `vm:zsvirt:3f2a9c10-4b7e-4d21-9a55-0c8e1f2b3d44` |
| `agentId` | `probe-3f2a9c10` |
| `agentVersion` | `0.1.0` |
| process sourceId | `{starttime}.{pid}`（如 `1847263.1827`，`.` 连接，见 `SENSITIVE_DATA.md` §8.4） |
| ai_service sourceId | `{服务名}.{端口}`（如 `vllm.8000`） |
| agent/task sourceId | 固定 ULID |

## 样例与探针当前实现的差距（诚实声明）

样例展示的是**契约的目标形态**；以下字段探针采集器尚未填充，但均为 B 侧可空字段，
不影响契约测试（B 的 `ResourcePayload` 均允许 None）：

1. `firstSeenAt` / `lastSeenAt`：`model.Resource` 已支持，采集器填充待增强。
2. `parentSourceId`：样例展示目标层级（container → process / ai_service，
   agent → task）；采集器填充需 pid→容器 cgroup 映射，列为下一步增强。
3. **场景三的 5 个事件**是 X-08（AI 工作负载日志规范）的**目标格式**：
   字段与 severity 遵循 F-01，真实采集待日志规范落地（见
   `agents/docs/EVENT_COVERAGE.md`）。
4. **场景一**探针只含 VM 内症状（`process.io_wait.high`）；根因事件
   `gpu.memory.exhausted` 由 B 的 zsvirt-adapter 产生（D-028 GPU 三层分工）。
   B 可把本样例与 GPU 渠道数据做跨层关联测试。
