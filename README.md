# Crosslayer

**面向 ZSvirt 虚拟机内部 AI 工作负载的跨层可观测与根因诊断平台。**

以 ZSvirt 虚拟机为边界、以 AI 工作负载为对象、以事件关联为核心，建立

```
宿主机 → GPU / vGPU → VM → 容器 / 进程 → AI 服务 / Agent / Task
```

之间的可追溯关系，并提供指标 / 日志 / 事件的接入、告警、跨层关联、根因候选、
影响范围分析与处置建议。

> **名称说明**：产品名为 **Crosslayer**（跨层）—— 对应本项目的核心能力：
> 把宿主机、GPU/vGPU、虚拟机、容器、AI 服务与 Agent 的分散信号关联成一条可解释的证据链。
> 仓库名保持 `zsvirt-observability`，以便按赛道关键词检索。
>
> **状态：实现阶段 —— 后端、探针、前端均已可运行。** 设计已定稿并完成**契约冻结**
> （A、C 均签字，见 [docs/CONTRACT_FREEZE.md](docs/CONTRACT_FREEZE.md)）；后端 **16 个契约端点
> 全部落地**，测试 **722 通过 / 1 跳过**（真实 PostgreSQL 上运行）。
>
> **ZSvirt 平台侧已接入**：命题方确认测试环境的 ZWatch 查询能力已启用，GPU 指标渠道
> （`app/zsvirt/watch.py`）与平台层命名空间桥接（`app/zsvirt/harvest.py`）已落地，
> 平台资源以 `host:zsvirt:{uuid}` / `gpu:zsvirt:{serial}` 写入资源图。
>
> ⚠️ **尚未完成的交付项**：样例容器化 AI 工作负载、真实环境故障注入验证、3–5 分钟
> 真实环境演示视频。清单与验收标准见
> **[docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)**；
> 与命题方的往来问题见 [docs/ZSVIRT_QA_ROUND2.md](docs/ZSVIRT_QA_ROUND2.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md) | **开发路线图** —— 剩余任务、依赖关系与可执行的验收标准 |
| [docs/DECISIONS.md](docs/DECISIONS.md) | **决策登记表** —— 全部已冻结决策的唯一查阅入口 |
| [docs/CONTRACT_FREEZE.md](docs/CONTRACT_FREEZE.md) | 契约冻结提案：待三方确认的枚举与码表 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构、分层、数据流、模块边界、设计优先级 |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | 资源 / 事件 / 告警 / 诊断对象与 ID 语义 |
| [docs/API_CONTRACT.md](docs/API_CONTRACT.md) | A→B 上报契约、B→C 查询契约、错误模型 |
| [docs/TECH-BASELINE.md](docs/TECH-BASELINE.md) | 语言 / 框架 / 数据库 / 版本基线、ZSvirt 能力边界 |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | 兼容性与演进规则 |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | 环境要求、部署步骤、配置项、模拟模式 |
| [docs/SENSITIVE_DATA.md](docs/SENSITIVE_DATA.md) | 敏感信息保护：脱敏、字段过滤、访问控制 |
| [docs/TEST_PLAN.md](docs/TEST_PLAN.md) | 测试分层、三类故障场景与验收闭环 |
| [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md) | **第三方依赖清单**（赛题交付要求 f） |
| [docs/ZSVIRT_QA_ROUND2.md](docs/ZSVIRT_QA_ROUND2.md) | 与命题方的技术问答（第二轮，含未关闭的外部阻塞） |
| [docs/backend/BACKEND_DESIGN.md](docs/backend/BACKEND_DESIGN.md) | 后端模块设计 |
| [docs/backend/DIAGNOSIS_DESIGN.md](docs/backend/DIAGNOSIS_DESIGN.md) | 诊断引擎与故障场景设计 |
| [docs/ADR/](docs/ADR/) | 架构决策记录 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 协作流程、分支规则、契约纪律 |

---

## ZSvirt 集成声明

> 依据赛题要求：「参赛者应在 README 中**声明实际使用的接口与权限**」。
> 下表为本项目**实际调用**的 ZSvirt 接口。环境访问地址与账号口令**不入仓库**
> （见 [docs/SENSITIVE_DATA.md](docs/SENSITIVE_DATA.md)），仅从环境变量注入。

### 实际使用的接口

| 用途 | 接口 | 路由后缀 | 方法 | 权限 | 实现位置 |
|---|---|---|---|---|---|
| GPU 指标元数据（判断环境有哪些指标） | `GetAllMetricMetadata` | `/zwatch/metrics/meta-data` | GET | 只读 | `app/zsvirt/watch.py` |
| GPU 性能指标时间序列 | `GetMetricData` | `/zwatch/metrics` | GET | 只读 | `app/zsvirt/watch.py` |
| GPU 静态资产（型号 / 显存容量 / 功耗 / 驱动状态） | `QueryGpuDevice` | — | GET | 只读 | `app/zsvirt/watch.py` |
| 认证（AccessKey 换会话，可选） | 账号登录 | — | POST | 只读 | `app/zsvirt/watch.py` |

采集的指标（namespace `ZStack/Host`）：`GpuUtilization`、`GpuMemoryUtilization`、
`GpuTemperature`、`GpuStatus`、`GpuPowerDraw`；关联标签：`HostUuid`、
`PciDeviceAddress`、`GpuSerialNumber`。

### 已确认可用但**本项目尚未接入**的接口

命题方第一轮答复确认下列接口在测试环境实测可用。本项目当前未调用它们，
列出以便评审核对集成边界：

| 接口 | 路由后缀 | 未接入原因 |
|---|---|---|
| `QueryAlarm` | `/zwatch/alarms` | 返回**告警配置**；平台侧是否需要接入告警规则待与命题方确认 |
| `GetAlarmData` | `/zwatch/alarm-histories` | 命题方实测「调用成功但**未取得告警记录**」；无真实告警可关联（见 [docs/ZSVIRT_QA_ROUND2.md](docs/ZSVIRT_QA_ROUND2.md) §2） |
| `QueryActiveAlarm` | `/zwatch/activealarms/alarms` | 同上 |
| `GetEventData` | `/zwatch/events` | 主机事件；作为平台侧信号来源的备选（同上 §2.3） |
| `QueryEventSubscription` | `/zwatch/events/subscriptions` | 事件订阅配置，本项目用轮询 |
| `GetPrometheusMetricLabelValue` | `/zwatch/metrics/prometheus/label-values` | 仅标签值查询，不是 PromQL；本项目指标走 ZWatch |
| `CreateMetricDataHttpReceiver` | `/zwatch/metrics/httpreceivers` | **写操作**，超出只读原则，不使用 |

**认证方式**：`Authorization: OAuth <token>`。适配器支持两种凭据注入
（`ZSVIRT_AUTH_STYLE`）：`oauth`（直接给 token）与 `accesskey`（用 AccessKey/SecretKey
登录换取会话）。**会话非永久有效**（约 2 小时），适配器实现**提前续期**与 401/403 失效重试。

**最小权限原则**：本项目只需要**只读**权限，不申请任何写权限，测试环境中不做破坏性操作。

### 数据来源的诚实标注

赛题要求区分真实采集与模拟。本项目用 `origin` 字段在**类型层面**强制这一点
（`docs/DATA_MODEL.md` §4.2.1），并通过 `/api/health` 的 `gpuProvider` 暴露：

| `origin` | 含义 |
|---|---|
| `zsvirt-zwatch` | ZWatch 平台指标（真实采集） |
| `probe` | 虚拟机内探针采集（真实采集） |
| `simulated` | 模拟数据（**可识别，绝不伪装成真实采集**） |

此外，当前环境为 **GPU 直通**（非 vGPU 切分），平台侧没有按虚拟机维度的显存占用。
读数因此标注 `attribution=passthrough` / `memUsageScope=card`：落进资源图的
`memUsagePct` 是**卡级**使用率，而不是某个虚拟机的占用。

---

## 降级与模拟模式

赛题要求「作品需支持模拟数据或最小化降级模式，以便在缺少特定 GPU 硬件时复现实验流程」。

```bash
# 无 GPU 环境（如仅 CPU 的开发机）标准启动方式
export GPU_PROVIDER=simulated
export SIMULATED_DATA_ENABLED=true
```

启动后 `GET /api/health` 的 `gpuProvider` 显示当前数据渠道（`zsvirt-zwatch` / `guest-smi` /
`simulated` / `auto`）、**真实探活结果** `available`、认证方式 `authStyle` 与凭据是否就绪
`credentialsConfigured`。读不到指标时它报 `available: false` 并给出缺口说明 ——
而不是报一个看起来正常的 `ok`。

**模拟数据可识别，不会伪装成真实采集数据。** 详见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) §4.1。

## 快速验证（后端）

```bash
cd backend && source .venv/bin/activate
python -m pytest -q                       # 722 passed, 1 skipped
python -m uvicorn app.main:app --port 8080

curl -s localhost:8080/api/health                       # 健康与降级状态
curl -s localhost:8080/api/v1/dict                      # 枚举码表（含中文文案）
curl -s localhost:8080/api/v1/workloads                 # AI 服务负载总览

# 灌入一份"容器 OOM"场景事件 → 告警由规则自动产生（响应里带告警摘要）
curl -s -X POST localhost:8080/api/v1/ingest/batch \
     -H 'content-type: application/json' \
     -d '{"agentId":"demo","vmId":"vm:zsvirt:d0000000-0000-0000-0000-000000000000",
          "agentVersion":"1.0","batchId":"demo-1","sentAt":"2026-09-17T12:00:00Z",
          "resources":[{"kind":"container","sourceId":"web-0","status":"running"}],
          "events":[{"occurredAt":"2026-09-17T12:00:00Z","type":"container.oom_killed",
                     "resourceRef":{"kind":"container","sourceId":"web-0"}}]}'

curl -s 'localhost:8080/api/v1/alerts?state=firing'     # 看自动产生的告警
curl -s -X POST localhost:8080/api/v1/diagnoses \
     -H 'content-type: application/json' \
     -d '{"alertId":"alert_..."}'                       # 一键诊断
```

交互式 API 文档：<http://localhost:8080/docs>

> 环境搭建见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) §3（Python 3.12 为源码编译，非系统包）。

## 仓库结构

多模块单仓库（monorepo），三位成员按目录并行开发。

```text
├── backend/    后端平台、资源关联、告警、根因诊断   （成员 B）
├── agents/     ZSvirt 虚拟机内探针与数据采集        （成员 A）
├── frontend/   可视化展示                          （成员 C）
├── deploy/     部署脚本、配置样例、故障注入脚本      （三方共同）
└── docs/       项目真源文档：架构 / 数据模型 / API 契约 / 兼容性 / 部署 / 测试
```

各模块的实现范围与对外契约见对应目录下的 `README.md`。

## 分支约定

```
main                 # 发布 / 稳定版本，只读为主
develop              # 团队集成分支
feature/member-a/*   # 成员 A：VM 内探针与采集
feature/member-b/*   # 成员 B：后端平台 / 资源关联 / 告警 / 根因诊断
feature/member-c/*   # 成员 C：前端展示
```

禁止直接在 `main` / `develop` 上开发，禁止 force push。

## 协作

三位成员的协作流程、分支规则、接口契约纪律与仓库清洁红线见
[CONTRIBUTING.md](CONTRIBUTING.md)。**首次参与前请先读它。**

---

## 许可证

见 [LICENSE](LICENSE)。
