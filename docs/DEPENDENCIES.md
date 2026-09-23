# 第三方依赖清单

> 赛题交付要求 f：「提供源代码、部署脚本、配置样例、README、开源许可证、**第三方依赖清单**
> 及最小化环境要求」。本文档是该清单的**唯一查阅入口**；精确版本以各模块的锁文件为准。
>
> 设计约束见 [ADR-0003](ADR/0003-minimal-runtime-dependencies.md)：**刻意不引入**
> Prometheus / Grafana / OpenTelemetry / Jaeger / Kafka / Redis / Elasticsearch ——
> 「稳定性与可复现性」是独立评分项（15%），组件越多，评委从零复现的难度越高。

| 模块 | 锁文件 | 说明 |
|---|---|---|
| 后端 | [`backend/requirements.txt`](../backend/requirements.txt) | 精确版本锁定 |
| 后端（开发） | [`backend/requirements-dev.txt`](../backend/requirements-dev.txt) | 与运行时同一锁文件 |
| 前端 | [`frontend/package-lock.json`](../frontend/package-lock.json) | `npm ci` 可复现安装 |
| 探针 | **无** | **纯标准库，零第三方依赖**（见 [agents/docs/PROBE_DESIGN.md](../agents/docs/PROBE_DESIGN.md)） |

---

## 1. 后端运行时依赖

Python **3.12.x**（本项目在 3.12.11 上验证）。全部为精确版本，禁止浮动范围。

| 包 | 版本 | 用途 | 许可证 |
|---|---|---|---|
| fastapi | 0.141.1 | HTTP 框架 | MIT |
| uvicorn[standard] | 0.53.0 | ASGI 服务器 | BSD-3-Clause |
| sqlalchemy | 2.0.54 | ORM / SQL 构建 | MIT |
| psycopg[binary] | 3.3.5 | PostgreSQL 驱动 | LGPL-3.0 |
| pydantic | 2.13.5 | 数据校验与序列化 | MIT |
| pydantic-settings | 2.15.0 | 环境变量配置加载 | MIT |
| alembic | 1.20.0 | 数据库迁移 | MIT |
| httpx | 0.28.1 | HTTP 客户端（ZSvirt / ZWatch 适配层） | BSD-3-Clause |
| python-ulid | 4.0.1 | ULID 标识生成 | MIT |

## 2. 后端开发与测试依赖

生产镜像可不安装。

| 包 | 版本 | 用途 | 许可证 |
|---|---|---|---|
| pytest | 9.1.1 | 测试框架 | MIT |
| pytest-asyncio | 1.4.0 | 异步测试 | Apache-2.0 |
| anyio | 4.15.1 | 异步兼容层（FastAPI 测试客户端） | MIT |
| ruff | 0.16.8 | 代码检查与格式化 | MIT |

## 3. 前端依赖

Node.js **18+**（本项目在 Node 26 上验证）。

### 运行时

| 包 | 版本范围 | 用途 | 许可证 |
|---|---|---|---|
| react / react-dom | ^18.3.1 | UI 框架 | MIT |
| react-router-dom | ^6.26.2 | 路由 | MIT |
| antd | ^5.21.4 | 中后台组件库 | MIT |
| @tanstack/react-query | ^5.59.16 | 数据请求、缓存与轮询 | MIT |
| echarts | ^5.5.1 | 拓扑图与图表 | Apache-2.0 |
| zustand | ^4.5.5 | 轻量全局 UI 状态 | MIT |
| dayjs | ^1.11.13 | 时间格式化 | MIT |

### 开发

| 包 | 版本范围 | 用途 | 许可证 |
|---|---|---|---|
| typescript | ^5.6.3 | 类型系统 | Apache-2.0 |
| vite | ^5.4.9 | 构建与开发服务器 | MIT |
| @vitejs/plugin-react | ^4.3.3 | React 支持 | MIT |
| @types/react / @types/react-dom / @types/node | ^18.3.x / ^26.6.2 | 类型声明 | MIT |

## 4. 探针依赖

| 依赖 | 说明 |
|---|---|
| **无第三方依赖** | 仅使用 Python 标准库（`/proc`、`cgroup`、`socket`、`json` 等） |

部署方式即"复制脚本到虚拟机内运行"，无需 `pip install`。这是**可复现性**与
**低侵入**两项评分要求的直接体现。

## 5. 基础设施依赖

| 组件 | 版本 | 必需 | 说明 |
|---|---|---|---|
| PostgreSQL | 18.6（本项目验证版本） | 是 | 唯一有状态组件；启动脚本见 [DEPLOYMENT.md](DEPLOYMENT.md) §3 |
| ZSvirt | 实测管理节点 `ZStack-ZSvirt 1.0.0.93` | 否 | 缺省时走模拟/降级模式，流程仍可复现 |

**刻意不引入**（见 ADR-0003）：Kafka、Redis、Elasticsearch、Prometheus、Grafana、
OpenTelemetry、Jaeger、Docker/Kubernetes。

## 6. 许可证与合规

- 本项目自有代码：**Apache-2.0**（见仓库根 `LICENSE`）。
- 命题方确认：赛题要求提供开源许可证，**未指定必须使用哪一种**，也未要求与
  ZSvirt 本体（GPLv3）相同；本项目通过 REST API 集成，不修改也不链接 ZSvirt 代码，
  判断不构成衍生作品。
- 上游依赖许可证均为宽松型（MIT / BSD / Apache-2.0），`psycopg` 为 LGPL-3.0
  （以库的形式动态链接使用，未修改其源码）。

## 7. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-09-23 | 首版：按赛题交付要求 f 建立依赖清单；记录 ADR-0003 的排除范围与许可证合规结论 |
