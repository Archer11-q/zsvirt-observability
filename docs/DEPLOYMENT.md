# ZSvirt Observability — 部署与运行

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 骨架，待技术基线锁定后补全** |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B |
| 最后更新 | 2026-09-16 |

> 本文档的目标：**任何人按本文档操作，都能在最小环境里把系统跑起来**，不依赖个人机器私有配置。
> 当前仓库尚无代码，本文档为骨架 + 已勘测的环境事实。标 `【待确认】` 的部分需团队决策后补全。

---

## 1. 目标环境

| 项 | 值 | 状态 |
|---|---|---|
| 操作系统 | Ubuntu 26.04 LTS（WSL2 或原生 Linux 均可） | 已勘测 |
| CPU | ≥ 4 核（勘测机 16 核） | — |
| 内存 | 【待确认】建议 ≥ 8 GB | — |
| GPU | **B 侧不需要本地 GPU**；GPU 数据来自 ZSvirt API 或 A 上报 | 已确认 |
| 网络 | 需能访问 GitHub（SSH 443）与被管理的 ZSvirt 集群 | — |
| 容器运行时 | 【待确认】勘测机 Docker 不可用 | ⚠️ |

---

## 2. B 侧依赖

| 依赖 | 版本 | 安装方式 | 状态 |
|---|---|---|---|
| Git | ≥ 2.40（勘测机 2.53.0） | 系统包 | ✅ |
| Python | **3.12.x（已锁定）** | pyenv / 源码编译到 `~/.local`，或 deadsnakes PPA | ⚠️ 勘测机默认 3.14.4，**需另装 3.12 并存**【安装方式待确认】 |
| pip / venv | 随 Python | `python3.12 -m venv` | ⚠️ 勘测机 `pip3` 缺失 |
| PostgreSQL | 【待确认】建议 16.x | 系统包 / 容器 | ❌ 未安装 |
| ZSvirt 集群访问 | 【阻塞】版本与凭据未知 | 成员提供 | ❌ 未知 |

---

## 3. 首次搭建步骤（骨架）

> **【待确认】** 项目尚未初始化代码与依赖清单，以下命令为**预期形态**，待 B2 阶段实测后定稿。

```bash
# 1. 获取代码
git clone git@github.com:Archer11-q/zsvirt-observability.git
cd zsvirt-observability

# 2. 创建虚拟环境（Python 已锁定 3.12.x，见 docs/TECH-BASELINE.md §2.1）
#    注意：勘测机默认 python3 是 3.14，必须显式使用 3.12
python3.12 -m venv .venv
source .venv/bin/activate
python -V   # 必须是 3.12.x

# 3. 安装依赖 【待确认：requirements.txt 尚未生成】
pip install -r requirements.txt

# 4. 准备数据库 【待确认：是否使用迁移工具 alembic】
createdb zsvirt_obs

# 5. 配置 【待确认：配置项清单见 §4】
cp .env.example .env
$EDITOR .env

# 6. 启动 【待确认：入口模块路径】
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 4. 配置项（【待确认】草案）

| 变量 | 用途 | 必填 | 示例 |
|---|---|---|---|
| `DATABASE_URL` | PostgreSQL 连接串 | ✅ | `postgresql+psycopg://user:pass@localhost:5432/zsvirt_obs` |
| `API_HOST` / `API_PORT` | 监听地址与端口 | ✅ | `0.0.0.0` / `8080` |
| `ZSVIRT_ENDPOINT` | ZSvirt API 地址 | ✅ | 【待确认】 |
| `ZSVIRT_AUTH_*` | ZSvirt 凭据 | ✅ | **禁止提交真实凭据** |
| `ZSVIRT_SYNC_INTERVAL_SEC` | 平台清单同步周期 | ⬜ | `30` |
| `INGEST_BATCH_MAX` | 单批上报上限 | ⬜ | `1000` |
| `DIAG_WINDOW_TOLERANCE_SEC` | 跨层关联容差 | ⬜ | `30` |
| `LOG_LEVEL` | 日志级别 | ⬜ | `INFO` |

**安全约定**：`.env` 必须加入 `.gitignore`；仓库内只提供 `.env.example`。**任何 token、密码、密钥、局域网地址不得进入仓库。**

---

## 5. 端口与网络

| 服务 | 默认端口 | 说明 |
|---|---|---|
| B 的 API | `8080`【待确认】 | C 访问；A 上报也走此端口 |
| PostgreSQL | `5432`【待确认】 | 仅本机可见即可 |
| ZSvirt API | 【待确认】 | 出站访问 |

---

## 6. 健康检查与可观测性

| 端点 | 用途 |
|---|---|
| `GET /api/health` | 整体状态 + 各上游（数据库 / ZSvirt / 接入）分项状态，见 `API_CONTRACT.md` §4.6 |

**部署验收**：`curl -s localhost:8080/api/health` 返回 `status` 且能区分 `ok` / `degraded` / `down`；当 ZSvirt 不可达时状态应为 `degraded` 且给出 `staleness`，**不得假装正常**。

---

## 7. 演示环境（3–5 分钟演示所需）

| 项 | 状态 |
|---|---|
| ZSvirt 集群（远程） | 【待确认】访问方式 |
| 至少一种容器化 AI 工作负载（跑在 ZSvirt VM 内） | 【待确认】由谁提供 |
| VM 内探针（成员 A） | 【待确认】 |
| 三类故障注入脚本 | 【待确认】见 `TEST_PLAN.md` |
| 前端展示（成员 C） | 【待确认】 |
| **一键启动 / 一键复现脚本** | **【待补充】交付要求之一** |

---

## 8. 已知环境阻塞

1. 本机 Docker 不可用（Docker Desktop WSL 集成未开启）——是否影响交付待定。
2. 本机 PostgreSQL 未安装。
3. 本机默认 Python 为 3.14.4，**项目已锁定 3.12**，需另装 3.12 并保持并存（安装方式待确认）。
4. 本机 `pip3` 缺失。
5. ZSvirt 集群接入方式与凭据未知——**阻塞 B1 设计落地**。

---

## 9. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 骨架：环境要求、搭建步骤草案、配置项草案、阻塞清单 | DRAFT |
| v0.2 | 2026-09-16 | 按成员决策更新：Python 明确锁定 3.12.x，搭建步骤改为显式使用 `python3.12` | DRAFT |
