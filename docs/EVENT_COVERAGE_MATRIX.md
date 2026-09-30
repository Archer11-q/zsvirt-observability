# 事件覆盖对账矩阵

| 字段 | 值 |
|---|---|
| 用途 | 赛题「任务定义与通用性 15%」与「数据与关联模型质量 20%」的**申报材料** |
| 生成方式 | **由代码生成**（`backend/scripts/gen_event_coverage.py`），不是手工维护 |
| 事件类型总数 | 17 项（F-01 已冻结枚举） |

## 为什么要这张表

赛题要求「统一组织指标、日志、链路、配置与告警证据」，并要求关联模型**可追溯**。
一张「事件类型 → 告警规则 → 诊断规则」的对照表，是证明这件事最直接的材料：

| 情况 | 含义 |
|---|---|
| 有事件类型但**没有告警规则** | 数据采了但没人看（"数据白采"） |
| 有告警但**没有诊断规则** | 能发现异常，但无法定因 |
| 探针在采而**枚举里没有** | 契约漂移 |

第一种在本项目里**真实发生过**：成员 A 的探针已在采集 `process.io_wait.high`，而 B 的
规则集里没有任何规则引用它 —— 数据进来了却没人看。补上 `R-PROC-IOWAIT-013` 之后，这一点由
`tests/test_cross_layer.py` 的交叉校验用例**永久守住**：A 采集的每个事件类型都必须有对应的
告警规则，否则测试失败。

## 三列对照表

| 事件类型 | 可读文案 | 告警规则 | 诊断规则 |
|---|---|---|---|
| `agent.network.timeout` | 智能体网络超时 | R-AGENT-NET-024 | R-NET-201 |
| `agent.task.failed` | 智能体任务失败 | R-AGENT-TASK-022 | R-AGENT-210 |
| `container.network.unreachable` | 容器网络不可达 | R-NET-UNREACH-023 | R-CONTRA-AGENT-920,R-NET-200 |
| `container.oom_killed` | 容器内存溢出被终止 | R-CTR-OOM-010 | R-CTR-OOM-101 |
| `container.restart` | 容器重启 | R-CTR-RESTART-011 | R-CTR-RESTART-110 |
| `gpu.memory.exhausted` | GPU 显存耗尽 | R-GPU-MEM-001 | R-CONTRA-MODEL-930,R-GPU-SELF-001 |
| `gpu.provider.degraded` | GPU 数据渠道降级 | R-PLATFORM-GPU-092 | — |
| `gpu.utilization.high` | GPU 利用率过高 | R-GPU-UTIL-002 | R-GPU-NEIGHBOR-011,R-GPU-UTIL-020 |
| `inference.error` | 推理请求错误 | R-AIS-ERROR-021 | R-AIS-ERROR-221 |
| `inference.timeout` | 推理请求超时 | R-AIS-TIMEOUT-020 | R-AIS-TIMEOUT-220 |
| `ingest.clock_drift.high` | 采集时钟漂移过大 | R-PLATFORM-DRIFT-090 | — |
| `ingest.resource.unresolved` | 存在未识别资源 | — | — |
| `process.crash` | 进程异常退出 | R-PROC-CRASH-012 | R-CONTRA-CTR-910,R-CTR-RESTART-111,R-VM-MEM-040 |
| `process.io_wait.high` | 进程 I/O 等待过高 | R-PROC-IOWAIT-013 | R-VM-DISK-031,R-VM-MEM-041 |
| `vgpu.quota.exceeded` | vGPU 配额超出 | R-VGPU-QUOTA-003 | R-GPU-SELF-003 |
| `vm.disk.io_saturated` | 虚拟机磁盘 I/O 饱和 | — | R-VM-DISK-030 |
| `zsvirt.sync.failed` | 平台资源同步失败 | R-PLATFORM-SYNC-091 | — |

## 怎么读

- **告警规则为 `—`**：该类型不直接触发告警。目前是 `ingest.resource.unresolved`
  与 `vm.disk.io_saturated` 两项 —— 前者是引擎的内部可观测信号，后者是赛题非必需的
  可选扩展场景（`D-071`）；
- **诊断规则为 `—`**：该类型目前只用于告警，不参与根因评分；
- **两列都有值**：该类型的信号从"发现"到"定因"是打通的。

## 与探针自动对账

成员 A 的实际采集范围见 [`agents/docs/EVENT_COVERAGE.md`](../agents/docs/EVENT_COVERAGE.md)。
两边的对账由 `tests/test_cross_layer.py` **自动执行**，不靠人工比对：

1. A 采集的每个事件类型必须在冻结枚举里（否则契约漂移）；
2. A 采集的每个事件类型必须有对应告警规则（否则数据白采）；
3. 所有规则引用的类型必须是枚举成员 —— 防"死规则"。本项目出现过一条匹配
   `vm.memory.exhausted` 的规则，而 F-01 里根本没有该类型，它永不命中。

## 重新生成

```bash
cd backend
PYTHONPATH=. .venv/bin/python scripts/gen_event_coverage.py   # 打印表格行
```

> **请勿手工编辑上面的表格**。它由脚本产出；手改会在下次生成时被覆盖，
> 而"手工维护导致漂移"正是这张表要消除的东西。
