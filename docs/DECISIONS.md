# Crosslayer — 决策登记表

| 字段 | 值 |
|---|---|
| 文档状态 | **唯一决策真源（Single Source of Truth for Decisions）** |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B（维护）；所有决策由三方共同确认 |
| 最后更新 | 2026-09-17 |

> **这份文档解决什么问题**
>
> 本项目的决策原本散落在 `TECH-BASELINE.md`、`API_CONTRACT.md`、`DATA_MODEL.md`、
> `ARCHITECTURE.md`、`ADR/`、`TEST_PLAN.md` 六份文档中。任何成员想确认"某个问题定了没有"，
> 都得翻六份文件。
>
> 本表把这些决策**集中、编号、标注证据与状态**，作为唯一查阅入口。
> 各专题文档仍是技术细节的真源，**决策是否成立以本表为准**。

---

## 状态图例

| 标记 | 含义 |
|---|---|
| ✅ **FROZEN** | 已确认，具有约束力。变更需走 `CONTRIBUTING.md` §5 的契约变更流程 |
| 🟡 **PROPOSED** | 已提案，等待三方确认（见 [`CONTRACT_FREEZE.md`](CONTRACT_FREEZE.md)） |
| ⛔ **BLOCKED** | 依赖外部条件（命题方），无法自行决定 |

---

## 1. 项目与流程

| ID | 决策 | 状态 | 确认人 / 日期 | 依据 |
|---|---|---|---|---|
| D-001 | **项目名：Crosslayer**；仓库名保持 `zsvirt-observability`（避免打断协作者 remote） | ✅ FROZEN | 项目负责人 2026-09-17 | 本表 §1 说明 |
| D-002 | 分支模型：`main` / `develop` / `feature/member-{a,b,c}/*`；禁止直接推 `main`/`develop`；禁止 force push | ✅ FROZEN | 项目负责人 2026-09-16 | `CONTRIBUTING.md` §2 |
| D-003 | `develop` 为仓库默认分支 | ✅ FROZEN | 项目负责人 2026-09-16 | 便于协作者克隆即得完整框架 |
| D-004 | 契约变更流程：先改契约文档 → 标 `BREAKING`/`NON-BREAKING` → 通知受影响成员 → 再改代码 | ✅ FROZEN | 三方 2026-09-16 | `CONTRIBUTING.md` §5 |
| D-005 | 内部材料（职责文档、AI 提示词、对话记录、比赛材料）放 `.project-internal/`，`.gitignore` 排除，不进交付仓库 | ✅ FROZEN | 项目负责人 2026-09-16 | `CONTRIBUTING.md` §8 |

---

## 2. 技术基线

| ID | 决策 | 状态 | 确认人 / 日期 | 依据 |
|---|---|---|---|---|
| D-010 | 后端语言 **Python 3.12.x**（锁定，不使用系统默认的 3.14） | ✅ FROZEN | 项目负责人 2026-09-16 | `TECH-BASELINE.md` §2.1、ADR-0001 |
| D-011 | 后端框架 **FastAPI** | ✅ FROZEN | 项目负责人 2026-09-16 | ADR-0001 |
| D-012 | 数据库 **PostgreSQL**；ORM **SQLAlchemy** | ✅ FROZEN | 项目负责人 2026-09-16 | ADR-0001 |
| D-013 | **运行时依赖最小化**：不引入消息队列、搜索引擎、独立缓存 | ✅ FROZEN | 项目负责人 2026-09-16 | ADR-0003 |
| D-014 | **不引入 Prometheus / Grafana / OpenTelemetry / Jaeger**（赛题为"可使用"而非"必须"；可复现性为评分项 15%） | ✅ FROZEN | 项目负责人 2026-09-17 | ADR-0003「范围扩展」 |
| D-015 | `.gitignore` **保留 C/C++ + CMake 块**（成员 A 的探针可能用 C/C++），并补充 Node/npm 规则 | ✅ FROZEN | 项目负责人 2026-09-16 | `TECH-BASELINE.md` §4 R6 |
| D-016 | 前端：TypeScript + React 18 + Vite + TanStack Query + Ant Design 5 + ECharts 5 + Zustand | 🟡 PROPOSED | 成员 C 提案 2026-09-17 | `frontend/FRONTEND_DESIGN.md` §1 |
| D-017 | 探针语言与采集手段（**可能使用 C/C++**） | 🟡 PROPOSED | 成员 A 决定 | 待 A 明确 |
| D-018 | Python 3.12 的**安装方式**（pyenv / 源码编译 / PPA） | 🟡 PROPOSED | 待定 | `DEPLOYMENT.md` §2 |
| D-019 | PostgreSQL 部署位置（本机 vs 远程）与版本 | 🟡 PROPOSED | 待定 | `TECH-BASELINE.md` §2.3 |

---

## 3. 架构

| ID | 决策 | 状态 | 确认人 / 日期 | 依据 |
|---|---|---|---|---|
| D-020 | 五层架构：L1 接入/适配 → L2 事件存储 → L3 资源图 → L4 告警/诊断 → L5 对外契约；**依赖只能向下** | ✅ FROZEN | 成员 B 2026-09-16 | `ARCHITECTURE.md` §3 |
| D-021 | 两路数据输入：**Pull**（ZSvirt 平台资源）+ **Push**（VM 内探针），**以 VM 为汇合点** | ✅ FROZEN | 成员 B 2026-09-16 | `ARCHITECTURE.md` §4.1 |
| D-022 | ZSvirt 集成集中在 `zsvirt-adapter` 单一模块；**SDK 类型不得泄漏到 L2 以上** | ✅ FROZEN | 成员 B 2026-09-16 | `BACKEND_DESIGN.md` §2.2 |
| D-023 | 诊断引擎**不访问数据库、不发 HTTP**，接收装配好的纯数据对象 → 可脱离基础设施单测 | ✅ FROZEN | 成员 B 2026-09-16 | `DIAGNOSIS_DESIGN.md` §2.1 |
| D-024 | 诊断规则为**声明式数据**（非代码分支）；新场景只加数据不加代码路径 | ✅ FROZEN | 成员 B 2026-09-16 | `DIAGNOSIS_DESIGN.md` §7 |
| D-025 | **必须实现模拟 / 降级模式**（赛题明文要求）；`/api/health` 必须暴露当前数据渠道 | ✅ FROZEN | 项目负责人 2026-09-17 | `ARCHITECTURE.md` §7.3 |
| D-026 | **必须实现敏感信息保护**（脱敏 + 字段过滤 + 访问控制说明） | ✅ FROZEN | 项目负责人 2026-09-17 | [`SENSITIVE_DATA.md`](SENSITIVE_DATA.md) |
| D-027 | B **自身不强制容器化**；赛题的"容器化 AI 负载"是被观测对象（跑在 ZSvirt VM 内） | ✅ FROZEN | 项目负责人 2026-09-17 | `ARCHITECTURE.md` §7 |
| D-028 | **GPU 数据三层分工 + 可插拔 Provider**（ZSvirt 资产 / ZWatch / 探针 / 模拟） | ✅ FROZEN | 项目负责人 2026-09-17 | `DATA_MODEL.md` §4.2.1 |

---

## 4. 数据模型

| ID | 决策 | 状态 | 确认人 / 日期 | 依据 |
|---|---|---|---|---|
| D-030 | 资源 ID 格式 `{kind}:{source}:{sourceId}`；ID **不透明、不可变、不承载语义** | ✅ FROZEN | 成员 A、C 确认 2026-09-17 | ADR-0002（Accepted） |
| D-031 | 探针侧 `sourceId` 规则：container=容器 ID 前 12 位；process=`starttime+pid`；ai_service=服务名+端口；agent/task=ULID | ✅ FROZEN | 成员 A 2026-09-16 | `API_CONTRACT.md` §3.3.1 |
| D-032 | `status`（**业务状态**，来源系统给出）与 `observability`（**B 的观测状态** `active`/`stale`/`gone`）是**两个独立字段**，禁止混用 | ✅ FROZEN | 成员 C 提出、B 采纳 2026-09-17 | `DATA_MODEL.md` §4.1 |
| D-033 | 资源不物理删除，只标记 `gone` 并保留历史（否则历史事件变孤儿） | 🟡 PROPOSED | 成员 B 提案 | `DATA_MODEL.md` §7 |
| D-034 | `Event` 只追加不修改；`Diagnosis` 一次性生成不被改写 | ✅ FROZEN | 成员 B 2026-09-16 | `DATA_MODEL.md` §5、§7 |
| D-035 | `Alert.evidenceEventIds` 为**必填** —— 没有证据的告警不允许存在 | ✅ FROZEN | 成员 B 2026-09-16 | `DATA_MODEL.md` §5.2 |
| D-036 | `Diagnosis.evidence` 不得为空；`confidence` 必须可由 `confidenceBreakdown` 复算；必须携带 `ruleSetVersion` | ✅ FROZEN | 成员 C 确认 2026-09-17 | `API_CONTRACT.md` §4.7 |
| D-037 | 事件时间双记：`occurredAt`（产生方给出）+ `receivedAt`（B 记录）；关联容差窗口 **±30s** | ✅ FROZEN | 成员 A 无异议 2026-09-16 | `ARCHITECTURE.md` §4.3 |
| D-038 | `vGPU` 作为独立资源实体（ZSvirt 源码存在 `MdevDevice` 概念） | 🟡 PROPOSED | 成员 B 提案 | `DATA_MODEL.md` §9 |
| D-039 | `task` 是否纳入首版模型 | 🟡 PROPOSED | 待定 | `DATA_MODEL.md` §9 |
| D-040 | 是否支持多租户 / 权限维度（赛题提到"租户或项目"关联） | 🟡 PROPOSED | 待定 | `DATA_MODEL.md` §9 |

---

## 5. 接口契约

| ID | 决策 | 状态 | 确认人 / 日期 | 依据 |
|---|---|---|---|---|
| D-050 | 版本策略：路径前缀 `/api/v1`；`/api/health` 无前缀 | ✅ FROZEN | 成员 C 支持 2026-09-17 | `API_CONTRACT.md` §2.1 |
| D-051 | 响应包裹 `{data, meta}`；列表用**游标分页**（非 offset） | ✅ FROZEN | 成员 B 提案、C 无异议 | `API_CONTRACT.md` §2.2 |
| D-052 | 错误模型：结构化 `{error:{code,message,details,traceId}}`；**禁止裸奔 500** | ✅ FROZEN | 成员 B 2026-09-16 | `API_CONTRACT.md` §2.3 |
| D-053 | `502 UPSTREAM_UNAVAILABLE`（ZSvirt 挂了）与 `503 SERVICE_DEGRADED`（B 自己受损）语义分离 | ✅ FROZEN | 成员 B 2026-09-16 | `API_CONTRACT.md` §2.3 |
| D-054 | A → B 传输：**HTTP 批量上报** `POST /api/v1/ingest/batch`，不引入消息队列 | ✅ FROZEN | 成员 A 2026-09-16 | `API_CONTRACT.md` §3.1 |
| D-055 | 上报语义**至少一次**；`batchId`（ULID）幂等；重复批次返回与首次一致的结果 | ✅ FROZEN | 成员 A 2026-09-16 | `API_CONTRACT.md` §3.3 Q2 |
| D-056 | 批量上限：`events ≤ 1000` / `resources ≤ 500` / payload ≤ 1MB；默认 5s 一批 | ✅ FROZEN | 成员 A 2026-09-16 | `API_CONTRACT.md` §3.3 Q4 |
| D-057 | 探针配置：环境变量 `ZSVIRT_OBS_BACKEND_URL`，优先级 环境变量 > 配置文件 > 默认值；不引入服务发现 | ✅ FROZEN | 成员 A 2026-09-16 | `API_CONTRACT.md` §3.3 Q5 |
| D-058 | 断网处理：本地磁盘缓冲（SQLite 或 JSONL）+ FIFO 断点续传；超限丢最旧并计数 | ✅ FROZEN | 成员 A 2026-09-16 | `API_CONTRACT.md` §3.3 Q6 |
| D-059 | 鉴权：**可选单一 Bearer Token，默认关闭**；不引入 mTLS；不入库不入仓库 | ✅ FROZEN | 成员 A Q7 + 成员 C Q8 2026-09-17 | `API_CONTRACT.md` §3.3 Q7、§4.9 |
| D-060 | 时钟漂移监测：按 `receivedAt − sentAt` 计算，告警阈值 **\|offset\| > 5s** | ✅ FROZEN | 成员 A 提案 2026-09-16 | `API_CONTRACT.md` §3.3 Q3 |
| D-061 | `/api/v1/workloads` 语义 = **`ai_service` 层级的业务聚合视图** | ✅ FROZEN | 成员 C 定义 2026-09-17 | `API_CONTRACT.md` §4.3 |
| D-062 | 首版**纯轮询**，不引入 SSE / WebSocket；SSE 列为可选演进项 | ✅ FROZEN | 成员 C 2026-09-17 | `API_CONTRACT.md` §4.10 Q11 |
| D-063 | 拓扑单次响应上限 **节点 ≤ 200、边 ≤ 400**；超限必须 `truncated: true` | ✅ FROZEN | 成员 C 提案 2026-09-17 | `API_CONTRACT.md` §4.2 |
| D-064 | 新增 `GET` / `POST /api/v1/diagnoses`（诊断列表 + 手动触发） | ✅ FROZEN | 成员 C 要求 2026-09-17 | `API_CONTRACT.md` §4.6 |
| D-065 | 新增 `GET /api/v1/dict` 字典端点，**含枚举的可读文案** | ✅ FROZEN | 成员 C 要求 2026-09-17 | `API_CONTRACT.md` §4.6.1 |
| D-066 | 诊断触发方式：**自动（告警联动）+ 手动（API）都要** | ✅ FROZEN | 成员 C 2026-09-17 | `API_CONTRACT.md` §4.6 |

---

## 6. 故障场景与诊断

| ID | 决策 | 状态 | 确认人 / 日期 | 依据 |
|---|---|---|---|---|
| D-070 | 三类故障场景对齐赛题分类：**① GPU 显存耗尽**（GPU/资源瓶颈）**② 容器 OOMKilled**（容器/进程异常）**③ Agent/容器网络异常**（应用/Agent 异常） | ✅ FROZEN | 项目负责人 2026-09-17 | `DIAGNOSIS_DESIGN.md` §5 |
| D-071 | VM 磁盘 I/O 饱和降级为**可选扩展场景**（非赛题必需） | ✅ FROZEN | 项目负责人 2026-09-17 | `DIAGNOSIS_DESIGN.md` §5.4 |
| D-072 | `confidence` 是**规则加权评分**，非统计学概率；`< 0.30` 时强制返回 `UNKNOWN` | ✅ FROZEN | 成员 B 2026-09-16 | `DIAGNOSIS_DESIGN.md` §4 |
| D-073 | 无证据 → 不允许有根因结论（返回 `UNKNOWN`）；反证须扣分；症状早于"根因"须扣分 | ✅ FROZEN | 成员 B 2026-09-16 | `DIAGNOSIS_DESIGN.md` §1、§4 |
| D-074 | 影响范围只包含**有证据支持**的资源，不是"结构上在下游就算受影响" | ✅ FROZEN | 成员 B 2026-09-16 | `DIAGNOSIS_DESIGN.md` §6 |
| D-075 | 是否额外输出 `potentiallyAffected`（结构相关但无证据） | 🟡 PROPOSED | 成员 B 倾向输出 | `DIAGNOSIS_DESIGN.md` §9 |
| D-076 | 规则集存放形式（YAML 文件 / 数据库表） | 🟡 PROPOSED | 成员 B 倾向 YAML 文件 | `DIAGNOSIS_DESIGN.md` §9 |

---

## 7. 待冻结（阻塞实施，需三方确认）

> 详见 [`CONTRACT_FREEZE.md`](CONTRACT_FREEZE.md)。这些项**未冻结前不得开始实现**。

| ID | 待冻结项 | 阻塞谁 |
|---|---|---|
| F-01 | **事件 `type` 完整枚举** | 所有人 —— 它是跨层关联的锚点 |
| F-02 | **`rootCause` 码表 + 可读文案** | B 的字典端点、C 的前端渲染 |
| F-03 | **`recommendation` 码表 + 可读文案** | 同上 |
| F-04 | `severity` / `alert.state` / `resource.kind` / `resource.status` 枚举**确认** | C 的筛选与图例 |
| F-05 | 敏感字段清单 | A（上报哪些字段）、B（脱敏管道） |
| F-06 | 前端技术栈（D-016） | C |
| F-07 | 探针技术选型（D-017） | A |

---

## 8. 外部阻塞（依赖命题方）

| ID | 阻塞项 | 影响 | 状态 |
|---|---|---|---|
| X-01 | ZSvirt API/SDK 开发文档 | `zsvirt-adapter` 无法落地到具体调用 | ⛔ 已发邮件，等待回应（2026-09-17） |
| X-02 | 测试环境访问方式 / 试用账号 | 无法联调 | ⛔ 同上 |
| X-03 | 基础镜像与样例资源 | 无法部署容器化 AI 负载 | ⛔ 同上 |
| X-04 | **测试环境 GPU/vGPU 规格**（vGPU 切分？VM 内能否 `nvidia-smi`？） | 决定场景一能否用真实数据 | ⛔ 同上 |
| X-05 | **ZWatch 是否启用**（源码位于 `premium/`，涉及赛题合规） | 决定 GPU 性能指标与平台告警能否取到 | ⛔ 同上 |
| X-06 | 目标集群 ZSvirt 版本 | API 兼容性对齐 | ⛔ 同上 |
| X-07 | 模拟数据做法是否符合赛题要求 | 降级模式的合法性确认 | ⛔ 同上 |

---

## 9. 决策统计

| 类别 | ✅ FROZEN | 🟡 PROPOSED | ⛔ BLOCKED |
|---|---|---|---|
| 项目与流程 | 5 | 0 | 0 |
| 技术基线 | 6 | 4 | 0 |
| 架构 | 9 | 0 | 0 |
| 数据模型 | 8 | 4 | 0 |
| 接口契约 | 17 | 0 | 0 |
| 故障场景与诊断 | 6 | 2 | 0 |
| 待冻结 | 0 | 7 | 0 |
| 外部依赖 | 0 | 0 | 7 |
| **合计** | **51** | **17** | **7** |

---

## 10. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-17 | 首版：汇总散落在 6 份文档中的 75 项决策，编号并标注状态、确认人、依据；识别 7 项待冻结与 7 项外部阻塞 | 已确认 |
