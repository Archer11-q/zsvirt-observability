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
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构、分层、数据流、模块边界 |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | 资源 / 事件 / 告警 / 诊断对象与 ID 语义 |
| [docs/API_CONTRACT.md](docs/API_CONTRACT.md) | A→B 上报契约、B→C 查询契约、错误模型 |
| [docs/TECH-BASELINE.md](docs/TECH-BASELINE.md) | 语言 / 框架 / 数据库 / 版本基线 |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | 兼容性与演进规则 |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | 环境要求与部署步骤 |
| [docs/TEST_PLAN.md](docs/TEST_PLAN.md) | 测试分层与验收闭环 |
| [docs/backend/BACKEND_DESIGN.md](docs/backend/BACKEND_DESIGN.md) | 后端模块设计 |
| [docs/backend/DIAGNOSIS_DESIGN.md](docs/backend/DIAGNOSIS_DESIGN.md) | 诊断引擎与故障场景设计 |
| [docs/ADR/](docs/ADR/) | 架构决策记录 |

## 分支约定

```
main                 # 发布 / 稳定版本，只读为主
develop              # 团队集成分支
feature/member-a/*   # 成员 A：VM 内探针与采集
feature/member-b/*   # 成员 B：后端平台 / 资源关联 / 告警 / 根因诊断
feature/member-c/*   # 成员 C：前端展示
```

禁止直接在 `main` / `develop` 上开发，禁止 force push。

## 许可证

见 [LICENSE](LICENSE)。
