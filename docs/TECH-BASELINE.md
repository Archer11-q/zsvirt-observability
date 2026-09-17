# Crosslayer — 技术基线与兼容性

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 部分条目已由成员确认，其余待确认** |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B |
| 最后更新 | 2026-09-16 |

> **纪律**：版本先固定、接口先固定、能力后扩展。**没有确认的版本，不得自行选择"最新版本"。**
> 任何新增依赖必须说明：用途、版本、体积/运行时影响、许可证、兼容性、替代方案、删除成本。

---

## 1. 已确认的基线决策

| 项 | 结论 | 确认人 | 日期 |
|---|---|---|---|
| 后端语言 | **Python 3.12.x**（锁定，不使用 3.14） | 成员（项目负责人） | 2026-09-16 |
| 后端框架 | **FastAPI** | 成员 | 2026-09-16 |
| 数据库 | **PostgreSQL** | 成员 | 2026-09-16 |
| ORM | **SQLAlchemy** | 成员 | 2026-09-16 |
| 运行时依赖策略 | **最小依赖**：不引入消息队列、不引入搜索引擎（Elasticsearch）、不引入分布式追踪后端 | 成员 | 2026-09-16 |
| 部署拓扑 | 本机（WSL Ubuntu）为成员 B 主开发机；**ZSvirt 运行于远程被管理主机 / 测试集群**，通过 API 接入 | 成员 | 2026-09-16 |

---

## 2. 版本矩阵

### 2.1 后端运行时

| 组件 | 锁定版本 | 本机现状 | 差异 / 动作 |
|---|---|---|---|
| Python | **3.12.x**（锁定 minor） | 本机默认 `python3` 为 **3.14.4** | ✅ 已决策：**锁定 3.12**，本机需另装 3.12 并与其并存 |
| pip | 随 venv | `pip3` **缺失** | 需 `python3 -m ensurepip` 或安装 `python3-pip` |
| venv | 标准库 `venv` | 可用 | 项目内 `.venv/`，已加入 `.gitignore` |

> **已决策（2026-09-16，成员确认）：Python 版本锁定 3.12.x，不采用 3.14。**
>
> 理由：3.12 是当前第三方生态兼容性最稳的版本；3.14 发布较新，依赖库（尤其 ORM 驱动与
> ASGI 中间件）存在兼容性风险，而本项目「可复现」是赛题评分项，不应承担该风险。
>
> 本机执行方式（与系统默认 3.14 并存，**不替换系统 Python，不使用 sudo 改系统环境**）：
>
> ```bash
> # 方式一：独立用户级安装（推荐，无需 sudo）
> #   通过 pyenv 或源码编译安装到 ~/.local，然后用它创建项目 venv
> # 方式二：deadsnakes PPA（需要 sudo，本机 sudo 需交互密码）
> ```
>
> **【待确认】** 具体安装方式（pyenv / 源码编译 / PPA）在 B2 开工前确定，
> 并记录到 `DEPLOYMENT.md` §3，确保其他成员可复现。

### 2.2 后端依赖（【提案】，版本待锁定）

| 用途 | 包 | 候选版本 | 状态 |
|---|---|---|---|
| ASGI 框架 | `fastapi` | `0.11x` | 【待确认】 |
| ASGI 服务器 | `uvicorn[standard]` | `0.3x` | 【待确认】 |
| ORM | `sqlalchemy` | `2.0.x` | 【待确认】 |
| PG 驱动 | `psycopg[binary]`（psycopg3） | `3.2.x` | 【待确认】 |
| 配置管理 | `pydantic-settings` | `2.x` | 【待确认】 |
| 数据校验 | `pydantic` | `2.x` | 【待确认】 |
| 数据库迁移 | `alembic` | `1.1x` | 【待确认】 |
| HTTP 客户端（调 ZSvirt） | `httpx` | `0.2x` | 【待确认】 |
| 测试框架 | `pytest` + `pytest-asyncio` | `8.x` | 【待确认】 |
| API 测试客户端 | `httpx`（ASGI transport） | — | 【待确认】 |
| 代码规范 | `ruff` | `0.6+` | 【待确认】 |

**待办**：以上版本必须在 B2 开发开工前**逐个锁定到 `requirements.txt` 并记录许可证**。

### 2.3 基础设施

| 组件 | 版本 | 本机现状 | 动作 |
|---|---|---|---|
| PostgreSQL | 【待确认】建议 16.x | **未安装** | 需安装（本机或远程） |
| ZSvirt | **上游 `VERSION` = 1.0.0**（GPLv3，Java/Maven）；目标集群版本【阻塞】未知 | 远程，不在本机 | 需向**命题方**索取 API/SDK 文档与测试环境账号 |
| Prometheus / Grafana / OTel / Jaeger | **不引入** | — | **【已确认】2026-09-17**，理由见 `ADR-0003` |
| 容器运行时 | 【待确认】 | **Docker 在本 WSL 不可用** | 见 §4 R2（已降级：B 自身不强制容器化） |
| 操作系统 | Ubuntu 26.04 LTS | 一致 | — |
| Git | 2.53.0 | 一致 | — |

### 2.4 ZSvirt 平台能力边界（2026-09-17 由源码实测得出）

> 来源：`github.com/ZSvirt/zsvirt`（clone 后阅读源码，非推测）。版本 `1.0.0`，许可证 **GPLv3**。

| 能力 | 是否可得 | 依据 | 用途 |
|---|---|---|---|
| 宿主机 / VM 资源清单 | ✅ 预计可得 | `QueryVmInstance`、`QueryHost` 等标准查询 API | Pull 路径的资源图 |
| **GPU 资产**（序列号、显存容量、功耗、驱动状态） | ✅ 可得 | `premium/mevoco/.../gpu/GpuDeviceVO.java` —— 字段仅 `serialNumber` / `memory` / `power` / `isDriverLoaded` | GPU 归属与资产层 |
| **vGPU / MDEV 切分与 VM 绑定** | ✅ 可得 | `QueryGpuDevice`、`QueryMdevDevice`、`QueryVmInstanceMdevDeviceSpecRef` | vGPU 归属 |
| **GPU 利用率 / 显存占用 / 温度** | ⚠️ **不在资产 API 中** | `GpuDeviceVO` **没有** `utilization` / `memUsed` / `temperature` 字段 | 需 ZWatch 或探针，见下 |
| 指标（metric） | ⚠️ **依赖 premium `zwatch`** | `zwatch` 目录 604 个 Java 文件，含 `APIGetMetricDataMsg`、`APIGetAllMetricMetadataMsg` | GPU 性能层 |
| 告警（alarm） | ⚠️ **依赖 premium `zwatch`** | `APIQueryAlarmMsg`、`APIGetAlarmDataMsg`、`APIQueryActiveAlarmMsg` | 赛题所说的"ZSvirt 告警" |
| 事件（event） | ⚠️ **依赖 premium `zwatch`** | `APIQueryEventSubscriptionMsg`、`APIGetEventDataMsg` | 跨层关联的事件源 |
| Prometheus 集成 | ⚠️ premium | `APIGetPrometheusMetricLabelValueMsg`、`APICreateMetricDataHttpReceiverMsg` | 备选指标通道 |

**两条关键结论**：

1. **GPU 资产可见，GPU 性能不可见（通过资产 API）** —— `QueryGpuDevice` 只回答"谁的卡、切给谁、多大显存"，
   **不回答**"现在用了多少"。这直接决定了 `DATA_MODEL.md` §4.2.1 的三层 Provider 设计。
2. **监控告警子系统 `zwatch` 位于 `premium/` 目录** —— 赛题禁止「不可验证的闭源服务作为核心能力」，
   因此**必须向命题方确认测试环境是否启用 ZWatch 及其权限**（风险 R9）。

**认证方式**：ZSvirt / ZStack 系使用 OAuth Token（请求头 `Authorization: OAuth <token>`）。

---

## 3. 协议与规范

| 项 | 结论 | 状态 |
|---|---|---|
| 对外 API 风格 | REST + JSON over HTTP/1.1 | 【提案】 |
| API 版本 | 路径前缀 `/api/v1` | 【待确认】见 `API_CONTRACT.md` §2.1 |
| 时间格式 | ISO 8601 UTC，带 `Z`；禁止本地时间裸传 | 【提案】 |
| 时间精度 | 毫秒 | 【提案】 |
| ID 格式 | `{kind}:{source}:{sourceId}`，不透明 | 【提案】见 `DATA_MODEL.md` §3 |
| 分页 | 游标（cursor） | 【提案】 |
| 认证 | 【待确认】 | 未知 |
| 字符编码 | UTF-8 | 已定 |

---

## 4. 本机环境勘测结果与风险

勘测日期：2026-09-16，环境：WSL2 `Ubuntu 26.04 LTS`，16 vCPU，内核 `6.18.33.2-microsoft-standard-WSL2`。

| 项 | 结果 |
|---|---|
| Git | 2.53.0 ✅ |
| Git 身份 | `Archer11-q / 1933676390@qq.com` ✅ |
| GitHub SSH | `ssh -T git@github.com` 认证成功 ✅ |
| python3 | 3.14.4（默认版本；**项目锁定 3.12**，需另装并并存 ⚠️） |
| pip3 | **缺失** ❌ |
| npm | 11.12.1 ✅ |
| gcc / cmake / make | 15.2.0 / 4.2.3 / 4.4.1 ✅ |
| protoc | 3.21.12 ✅ |
| go / java / mvn / kubectl | **缺失**（当前基线不需要） |
| nvidia-smi | 570.133.07 存在，但 **`/dev/nvidia*` 不存在** ⚠️ |
| `/dev/kvm` | **存在**，CPU 支持虚拟化 ✅ |
| Docker | **本发行版未启用 Docker Desktop 集成** ❌ |
| eBPF | `unprivileged_bpf_disabled=2`，`sudo` 需交互密码 ❌ |
| PostgreSQL | **未安装** ❌ |

### 风险与处置

| # | 风险 | 影响 | 处置 |
|---|---|---|---|
| R2 | Docker 不可用 | 无法在本机跑容器化负载；B 自身容器化交付受限 | 【待确认】是否需要开启 Docker Desktop WSL 集成 |
| R3 | eBPF / sudo 受限 | 本机做不了内核级采集 | **属成员 A 职责**；B 不依赖该能力，只要 A 能上报即可 |
| R1 | 无 GPU 设备 | 无法直接验证 GPU/vGPU 关联 | GPU 数据须来自 ZSvirt API 或 A 上报（见 `ARCHITECTURE.md` §2.1） |
| R8 | PostgreSQL 未安装 | B2 无法落库 | 需安装或使用远程实例【待确认】 |
| R6 | `.gitignore` 含 **C/C++ + CMake + vcpkg** 块 | 曾与 Python 基线看似不一致 | ✅ **已决策：保留，不删除**（成员 A 可能使用 C/C++）。已在该块上方加注释说明用途 |
| R9 | **`zwatch` 监控模块位于 ZSvirt 的 `premium/` 目录** | GPU 性能指标与 ZSvirt 告警可能不可得；赛题禁止"不可验证的闭源服务作为核心能力" | ❌ **需向命题方确认**测试环境是否启用 ZWatch 及最小权限 |
| R10 | ZSvirt 上游 `VERSION` 为 **1.0.0**，但目标集群实际版本未知 | API 兼容性无法预先验证 | ❌ 需命题方提供集群版本与 API 文档 |

---

## 5. 兼容性纪律

1. **API/Schema 变更**必须同步更新契约文档与测试，并通知 A / C。
2. **优先向后兼容**：新增字段尽量可选；删除或改名必须版本化或经团队确认。
3. **本地可用 ≠ 项目可用**：所有依赖必须记录安装/构建方式与最小环境。
4. **依赖引入流程**：说明用途 → 版本 → 体积/运行时影响 → 许可证 → 兼容性 → 替代方案 → 删除成本 → 成员确认 → 引入。
5. **禁止**未经确认升级/降级核心语言、框架、数据库或基础镜像。

---

## 6. 待确认清单

1. ~~Python 版本最终定为 3.12 还是 3.14（§2.1）~~ → ✅ **已决策：锁定 3.12.x**；剩余待确认项为「安装方式」（pyenv / 源码编译 / PPA）。
2. 所有第三方依赖的精确版本与许可证（§2.2）。
3. PostgreSQL 版本、部署位置（本机安装 vs 远程实例）。
4. ZSvirt 集群版本与 API 兼容矩阵。
5. 是否引入 API 认证及其方式。
6. 是否引入 `/api/v1` 版本前缀。
7. 是否需要容器化交付，以及 Docker 环境如何准备。
8. ~~`.gitignore` 基线是否重写为 Python 版本~~ → ✅ **已决策：保留 C/C++ 块**（成员 A 可能使用 C/C++），并已补充 Node/npm 规则以覆盖成员 C 的前端。

---

## 7. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-16 | 首轮草案：确认项、版本矩阵、环境勘测、风险清单 | DRAFT，待评审 |
| v0.2 | 2026-09-16 | 成员决策：**Python 锁定 3.12.x**（不用 3.14）；**`.gitignore` 保留 C/C++ 块**；补充 Node/npm 规则 | DRAFT，待评审 |
| v0.3 | 2026-09-17 | 新增 **§2.4 ZSvirt 平台能力边界**（源码实测）：确认 GPU **资产** API 存在但**性能**指标不在其中；确认 `zwatch` 监控告警为 **premium** 模块；确认 ZSvirt 上游版本 1.0.0 / GPLv3；确认不引入 Prometheus/Grafana/OTel；风险表新增 R9 / R10 | 已确认 |
