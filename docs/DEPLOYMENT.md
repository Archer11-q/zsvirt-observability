# Crosslayer — 部署与运行

| 字段 | 值 |
|---|---|
| 文档状态 | **v1.0 — 已在开发机实测跑通** |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B |
| 最后更新 | 2026-09-23 |

> 本文档的目标：**任何人按本文档操作，都能在最小环境里把系统跑起来**，不依赖个人机器私有配置。
>
> 后端（FastAPI + PostgreSQL）、探针（纯标准库）、前端（React + Vite）三者均已可运行，
> 测试 **722 通过 / 1 跳过**。ZSvirt 平台侧已接入（ZWatch 指标渠道 + 平台命名空间桥接）。
> 依赖清单见 [`DEPENDENCIES.md`](DEPENDENCIES.md)。
>
> ZSvirt 测试环境的内网地址、账号与口令**留在团队内部，不写入仓库**
> （依据 [`SENSITIVE_DATA.md`](SENSITIVE_DATA.md)）；本文档只描述**配置项名称与注入方式**。

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

> 以下命令已在开发机实测跑通（Python 3.12.11 为**源码编译**到 `~/.local`，
> 非系统包；PostgreSQL 18.6）。依赖清单见 [`DEPENDENCIES.md`](DEPENDENCIES.md)。

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

## 4. 配置项

> 标注 `[A已确认]` 的项来自成员 A 的 `agents/docs/REPORTING_CONTRACT.md`。

| 变量 | 用途 | 必填 | 示例 |
|---|---|---|---|
| `DATABASE_URL` | PostgreSQL 连接串 | ✅ | `postgresql+psycopg://user:pass@localhost:5432/zsvirt_obs` |
| `API_HOST` / `API_PORT` | 监听地址与端口 | ✅ | `0.0.0.0` / `8080` |
| `ZSVIRT_ENDPOINT` | ZSvirt API 地址（测试环境由命题方提供） | ⬜ | 未配置时 GPU 渠道降级，`/api/health` 报 `ZSVIRT_ENDPOINT_NOT_CONFIGURED` |
| `ZSVIRT_AUTH_STYLE` | 认证方式：`oauth` \| `accesskey` | ⬜ | `oauth` |
| `ZSVIRT_AUTH_TOKEN` | OAuth Token（`Authorization: OAuth <token>`） | ⬜ | **禁止提交真实凭据** |
| `ZSVIRT_ACCESS_KEY` / `ZSVIRT_SECRET_KEY` | `accesskey` 方式登录换会话的凭据 | ⬜ | **禁止提交真实凭据** |
| `ZSVIRT_VERIFY_TLS` | 是否校验 TLS 证书 | ⬜ | `false`（测试环境为自签证书；如需严格校验改为 `true`） |
| `ZSVIRT_TIMEOUT_SEC` | ZWatch 请求超时 | ⬜ | `8` |
| `ZWATCH_WINDOW_MINUTES` | 指标查询窗口（分钟） | ⬜ | `15` |
| `ZWATCH_CACHE_TTL_SEC` | 读数缓存（秒）。前端每 15s 轮询 workloads，缓存挡住重复查询 | ⬜ | `10` |
| `ZSVIRT_SYNC_INTERVAL_SEC` | 平台资源清单同步周期 | ⬜ | `30` |
| **`GPU_PROVIDER`** | **GPU 指标渠道：`zsvirt-zwatch` \| `guest-smi` \| `simulated` \| `auto`** | ⬜ | 默认 `auto`；**无 GPU 环境必须能跑 `simulated`** |
| **`SIMULATED_DATA_ENABLED`** | 是否启用模拟/降级数据（赛题明文要求） | ⬜ | `false`；演示兜底时 `true` |
| `INGEST_BATCH_MAX` | 单批事件上限 `[A已确认]` | ⬜ | `1000` |
| `INGEST_RESOURCE_MAX` | 单批资源上限 `[A已确认]` | ⬜ | `500` |
| `INGEST_PAYLOAD_MAX_BYTES` | 单批载荷上限 `[A已确认]` | ⬜ | `1048576`（1MB） |
| `INGEST_RATE_LIMIT_*` | 限流阈值（触发 `429`） `[A已确认]` | ⬜ | 【待定，A 需要明确值】 |
| `CLOCK_DRIFT_WARN_MS` | 探针时钟漂移告警阈值 `[A已确认]` | ⬜ | `5000` |
| `DIAG_WINDOW_TOLERANCE_SEC` | 跨层关联容差 `[已确认]` | ⬜ | `30` |
| `API_AUTH_TOKEN` | 可选只读 Bearer Token（**默认空 = 关闭**） | ⬜ | 环境变量注入，不入仓库 |
| `SENSITIVE_FILTER_ENABLED` | 敏感字段过滤开关（见 `SENSITIVE_DATA.md`） | ⬜ | `true` |
| `LOG_LEVEL` | 日志级别 | ⬜ | `INFO` |

**安全约定**：`.env` 必须加入 `.gitignore`；仓库内只提供 `.env.example`。**任何 token、密码、密钥、局域网地址不得进入仓库**（完整规则见 [`SENSITIVE_DATA.md`](SENSITIVE_DATA.md)）。

### 4.2 ZWatch 指标渠道（ZSvirt 平台侧）

命题方已确认测试环境的 ZWatch 查询能力**已启用**（关闭外部阻塞 X-07），
GPU 指标位于 namespace `ZStack/Host`。适配器见 `backend/app/zsvirt/watch.py`。

**实际调用**：`GetAllMetricMetadata`（`/zwatch/metrics/meta-data`）与
`GetMetricData`（`/zwatch/metrics`）；指标为 `GpuUtilization` /
`GpuMemoryUtilization` / `GpuTemperature` / `GpuStatus` / `GpuPowerDraw`。
完整接口声明见仓库根 [`README.md`](../README.md)「ZSvirt 集成声明」。

**会话有效期**：命题方答复指出 API 会话约 **2 小时**过期（平台授权"永久有效"
不等于会话永久有效）。适配器实现**提前 5 分钟自动续期**，并在收到 401/403 时
作废本地会话、**重试一次**。

**两条如实标注的限制**（不要在演示中含糊过去）：

1. 当前环境为 **GPU 直通**（非 vGPU 切分，平台未发现 MDEV 实例）。平台侧
   **没有**按虚拟机维度的显存占用，因此卡级使用率同时代表宿主与本机 ——
   读数标注 `attribution=passthrough`、`memUsageScope=card`，
   "自己超配 vs 邻居干扰"的区分**在直通下不可得**。
2. `GpuMemoryUtilization` 是**使用率**，不是已用显存字节数（命题方明确）。
   因此 `memUsedBytes` 在 ZWatch 渠道保持为空 —— 由百分比反推的数字不是观测值，
   精确字节数需由**虚拟机内探针的 `nvidia-smi`** 提供。

**验证方式**：

```bash
curl -s localhost:8080/api/health | python -m json.tool | grep -A 12 gpuProvider
# 关注：mode / available / authStyle / credentialsConfigured / attribution
```

未配置或不可达时 `available: false` 并给出缺口说明 —— 而**不是**一个看起来正常的 `ok`。

### 4.1 模拟 / 降级模式（赛题明文要求）

赛题要求「支持模拟数据或最小化降级模式，以便在缺少特定 GPU 硬件时复现实验流程」。

```bash
# 无 GPU 环境的标准启动方式（开发机自测 / 评委复现）
export GPU_PROVIDER=simulated
export SIMULATED_DATA_ENABLED=true
```

**验收**：启动后 `GET /api/health` 的 `gpuProvider.mode` 必须为 `simulated`，
且三类故障场景仍能完整走通（注入 → 告警 → 诊断 → 证据链）。
**模拟数据必须可识别**，不得在诊断证据中伪装成真实采集（见 `ARCHITECTURE.md` §7.3）。

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
| `GET /api/health` | 整体状态 + 各上游（数据库 / ZSvirt / 接入 / GPU Provider）分项状态，见 `API_CONTRACT.md` §4.8 |

**部署验收**：`curl -s localhost:8080/api/health` 返回 `status` 且能区分 `ok` / `degraded` / `down`；当 ZSvirt 不可达时状态应为 `degraded` 且给出 `staleness`，**不得假装正常**。

---

## 7. 演示环境（3–5 分钟演示所需）

| 项 | 状态 |
|---|---|
| ZSvirt 集群（远程） | 【待确认】访问方式（向命题方索取） |
| 至少一种容器化 AI 工作负载（跑在 ZSvirt VM 内） | 【待确认】由谁提供（成员 A 交付物） |
| VM 内探针（成员 A） | 【待确认】A 的 `agents/` 实现 |
| 三类故障注入脚本 | 【待确认】见 `TEST_PLAN.md` §3 |
| 前端展示（成员 C） | 技术栈已定（React 18 + Vite + AntD 5 + ECharts 5），见 `frontend/FRONTEND_DESIGN.md` |
| **模拟 / 降级模式** | **【已确认】必需**（赛题要求），配置见 §4.1 |
| **一键启动 / 一键复现脚本** | **【待补充】交付要求之一** |

---

## 8. 已知环境阻塞

1. 本机 Docker 不可用（Docker Desktop WSL 集成未开启）——**已降级**：B 自身不强制容器化。
2. 本机 PostgreSQL 未安装。
3. 本机默认 Python 为 3.14.4，**项目已锁定 3.12**，需另装 3.12 并保持并存（安装方式待确认）。
4. 本机 `pip3` 缺失。
5. ❌ **ZSvirt 集群接入方式与凭据未知** —— 阻塞 B1 设计落地，**需向命题方索取 API/SDK 文档与测试环境账号**。
6. ⚠️ **GPU 性能指标渠道未知** —— ZSvirt 的 `zwatch` 监控模块位于 `premium/` 目录，需确认测试环境是否启用及其权限。**已有缓解**：模拟 Provider 保证无 GPU 也能跑通全流程。
7. ⚠️ 本机无 GPU 设备（`/dev/nvidia*` 不存在）—— 真实 GPU 归因需远程环境验证。

---

## 9. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 骨架：环境要求、搭建步骤草案、配置项草案、阻塞清单 | DRAFT |
| v0.2 | 2026-09-16 | 按成员决策更新：Python 明确锁定 3.12.x，搭建步骤改为显式使用 `python3.12` | DRAFT |
| v0.3 | 2026-09-17 | 配置项补齐 A 已确认的上报参数（批次上限 / 限流 / 时钟漂移）与 GPU Provider、认证、敏感过滤开关；新增 §4.1 模拟/降级模式；阻塞清单更新 | 已确认 |
