# ZSvirt Observability

面向 ZSvirt 虚拟机的全栈可观测与根因诊断平台。

以 ZSvirt 虚拟机为边界、以 AI 工作负载为对象、以事件关联为核心，建立

```
宿主机 → GPU / vGPU → VM → 容器 / 进程 → AI 服务 / Agent / Task
```

之间的可追溯关系，并提供指标 / 日志 / 事件的接入、告警、跨层关联、根因候选、
影响范围分析与处置建议。

> **状态：设计阶段。** 本仓库当前只有设计文档，尚未开始实现。
> 所有设计文档均为 DRAFT，需团队评审后才能作为实现依据。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构、分层、数据流、模块边界、设计优先级 |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | 资源 / 事件 / 告警 / 诊断对象与 ID 语义 |
| [docs/API_CONTRACT.md](docs/API_CONTRACT.md) | A→B 上报契约、B→C 查询契约、错误模型 |
| [docs/TECH-BASELINE.md](docs/TECH-BASELINE.md) | 语言 / 框架 / 数据库 / 版本基线 |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | 兼容性与演进规则 |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | 环境要求、部署步骤、配置项、模拟模式 |
| [docs/SENSITIVE_DATA.md](docs/SENSITIVE_DATA.md) | 敏感信息保护：脱敏、字段过滤、访问控制 |
| [docs/TEST_PLAN.md](docs/TEST_PLAN.md) | 测试分层、三类故障场景与验收闭环 |
| [docs/backend/BACKEND_DESIGN.md](docs/backend/BACKEND_DESIGN.md) | 后端模块设计 |
| [docs/backend/DIAGNOSIS_DESIGN.md](docs/backend/DIAGNOSIS_DESIGN.md) | 诊断引擎与故障场景设计 |
| [docs/ADR/](docs/ADR/) | 架构决策记录 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 协作流程、分支规则、契约纪律 |

---

## ZSvirt 集成声明

> 依据赛题要求：「参赛者应在 README 中**声明实际使用的接口与权限**」。
> **当前状态：待补充。** 需在拿到 ZSvirt API/SDK 文档与测试环境后填写实际使用的接口清单与所需权限。

已确定的集成设计（详见 [docs/DATA_MODEL.md](docs/DATA_MODEL.md) §4.2.1）：

| 用途 | ZSvirt 接口 | 权限 | 状态 |
|---|---|---|---|
| 宿主机 / VM 资源清单 | `QueryHost` / `QueryVmInstance` 等 | 只读 | 【待确认接口名与权限】 |
| GPU 资产（型号、显存容量、序列号、功耗、驱动状态） | `QueryGpuDevice` | 只读 | 已从源码确认存在 |
| vGPU / MDEV 切分与 VM 绑定 | `QueryMdevDevice` / `QueryVmInstanceMdevDeviceSpecRef` | 只读 | 已从源码确认存在 |
| GPU 性能指标（利用率 / 显存占用 / 温度） | `zwatch` metric API | 只读 | ⚠️ **premium 模块，待确认测试环境是否启用** |
| 平台告警 | `zwatch` alarm API（`QueryAlarm` / `GetAlarmData`） | 只读 | ⚠️ 同上 |

**认证方式**：ZSvirt 使用 OAuth Token（`Authorization: OAuth <token>`）。**凭据仅从环境变量注入，不入仓库。**

**最小权限原则**：本项目只需要**只读**权限，不申请任何写权限。

---

## 降级与模拟模式

赛题要求「作品需支持模拟数据或最小化降级模式，以便在缺少特定 GPU 硬件时复现实验流程」。

```bash
# 无 GPU 环境（如仅 CPU 的开发机）标准启动方式
export GPU_PROVIDER=simulated
export SIMULATED_DATA_ENABLED=true
```

启动后 `GET /api/health` 的 `gpuProvider.mode` 显示当前数据渠道（`zsvirt-zwatch` / `guest-smi` / `simulated`）。
**模拟数据可识别，不会伪装成真实采集数据。** 详见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) §4.1。

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
