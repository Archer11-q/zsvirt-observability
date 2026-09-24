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
| D-077 | 影响范围三集是可达集合的**划分**：`affected` = 证据 + 其下游后代 **减去**过渡层；过渡层归 `on_chain`；离链祖先归 `potentiallyAffected`。**同一资源不得出现在两个集合里** | ✅ FROZEN | 成员 B 2026-09-17（实现任务 1 时发现并修正重叠缺陷） | `graph/algorithms.py` `impact_scope`、C 的 D-075 |
| D-078 | 工作负载归属是**一对多**：共享资源（同一容器下的两个 AI 服务）归入**全部**相关服务；共享 GPU 的指标在每张卡片上**都**显示 | ✅ FROZEN | 成员 B 2026-09-17 | `api/workloads.py` `_resolve_owners` |
| D-079 | 诊断与工作负载的关联按**链路归属**判定，不要求诊断点名该工作负载 —— 否则链路下层的诊断在业务视图里全部不可见 | ✅ FROZEN | 成员 B 2026-09-17 | `diagnosis/service.py` `latest_diagnoses_per_owner` |
| D-080 | `POST /api/v1/diagnoses` **同步返回**，不实现契约预留的 `202 + pending` 分支（无真异步执行器时返回"已受理"会让前端拿到查不到的 id） | ✅ FROZEN | 成员 B 2026-09-17 | `API_CONTRACT.md` §4.6、`api/diagnoses.py` |
| D-081 | 告警规则是**声明式数据**且规则集带版本号（`rs-alert-1.0.0`）；每条告警把 `ruleSetVersion` 写进 `labels`，规则改动后可回答"当时按什么规则报的" | ✅ FROZEN | 成员 B 2026-09-17（任务 2） | `alerts/rules.py`、`DIAGNOSIS_DESIGN.md` §7 |
| D-082 | 告警严重级别 = **规则级别与事件级别中更严重的一方**（规则 warning + 事件 critical → critical），不得降级显示 | ✅ FROZEN | 成员 B 2026-09-17 | `alerts/engine.py` `_create_alert` |
| D-083 | **静默 ≠ 已恢复**：静默期内没有新证据是因为"我们没在听"，不构成条件解除；释放静默时把 `lastFiredAt` **重新基准化到释放时刻**，恢复窗口从那时重新计时 | ✅ FROZEN | 成员 B 2026-09-17 | `alerts/engine.py` `release_expired_silences` |
| D-084 | 告警**自动恢复**：超过 `recovery_after`（默认 10 分钟）无新证据 → `resolved` + `resolvedAt`；`silenced` 不参与恢复；**复发新建告警**而不复活旧记录（保留旧的 `resolvedAt`） | ✅ FROZEN | 成员 B 2026-09-17 | `alerts/engine.py` `reconcile` |
| D-085 | 告警求值**与事件写入同事务**，且只对本批**新写入**的事件求值（重复批次走幂等回放，不得让 `count` 灌水） | ✅ FROZEN | 成员 B 2026-09-17 | `ingest/service.py`、A 的 Q2 |
| D-086 | 聚合去重键 = `{ruleId}:{resourceId}`，**只支持 `{rule}` / `{resource}` 两个占位符**，不在规则数据里放求值表达式 | ✅ FROZEN | 成员 B 2026-09-17 | `alerts/rules.py` `AlertRule.aggregation_key` |
| D-087 | 不支持 `durationSec` 这类**持续时间条件**：那需要跨批次状态记忆（流式窗口），本版不做半套实现 —— 声明了却不生效的条件比没有更危险 | ✅ FROZEN | 成员 B 2026-09-17 | `alerts/rules.py` `AlertCondition` |
| D-088 | 增加**平台自监控告警规则**（时钟漂移 / ZSvirt 同步失败 / GPU 渠道降级）—— 平台自己坏了却安静无声，会让所有下游结论失去可信度 | ✅ FROZEN | 成员 B 2026-09-17 | `alerts/rules.py` |
| D-089 | 自动诊断**在上报事务提交之后**执行，失败被吞掉并逐条记录 —— 数据采集是主流程，根因分析是增值能力，增值能力不得毁掉主流程 | ✅ FROZEN | 成员 B 2026-09-17（任务 3） | `api/ingest.py`、`diagnosis/auto.py` |
| D-090 | 自动诊断**只覆盖 `error` 以上**的活动告警；提示级告警由用户按需触发 —— 自动跑根因分析只会灌出一堆 `UNKNOWN`，而 `UNKNOWN` 太多会让运维忽略整个诊断列表 | ✅ FROZEN | 成员 B 2026-09-17 | `diagnosis/auto.py` |
| D-091 | 自动诊断按 `alertId` **幂等**（已有 `diagnosisId` 则跳过），单次封顶 10 条，超出置 `truncated` 并留待手动补 | ✅ FROZEN | 成员 B 2026-09-17 | `diagnosis/auto.py` |
| D-092 | `upsert_resource` **必须同步维护邻接表**：`parent_id` 与 `resource_edge` 是同一关系的两个视图，不能只写一个。父资源尚不存在时暂不写边（下一次 upsert 补齐），而不是为内部顺序问题让整批上报失败 | ✅ FROZEN | 成员 B 2026-09-17（任务 4，修正致命缺陷） | `graph/repository.py` |
| D-093 | 诊断规则集存放于 `app/diagnosis/rules.py`（Python 数据字面量），版本 `rs-diag-1.0.0`；`assert_rule_set_is_consistent` 在**导入时**校验全部引用 | ✅ FROZEN | 成员 B 2026-09-17 | `diagnosis/rules.py` |
| D-094 | 实现 `Rule.contradicts`：反证命中记到**被反对的结论**名下（而不是规则自己的 `root_cause`），且必须为负贡献；反证证据**不出现在**获胜结论的 `evidence` 里（否则解释自我否证） | ✅ FROZEN | 成员 B 2026-09-17 | `diagnosis/engine.py` |
| D-095 | 事件证据展开为**多条**：事件类型一条 + 每个数值型指标各一条，共用同一组 `eventIds`。非数值型指标不参与比较（不猜、不默认为 0） | ✅ FROZEN | 成员 B 2026-09-17 | `diagnosis/service.py` |
| D-096 | 告警作为 `EvidenceKind.ALERT` 参与诊断，`name` 是**告警规则 id**、`count` 是聚合次数 —— 告警已聚合分级，比零散事件更能支撑结论 | ✅ FROZEN | 成员 B 2026-09-17 | `diagnosis/service.py` |
| D-098 | 父资源类型**从本批声明的资源里查**，不按子类型推断。`process` 的父可能是 `container` 或 `vm`，按子类型猜必然错一半，而错误代价是外键违约导致**整批 503**。父资源不存在时 `upsert_resource` **不写** `parent_id`（与边表取舍一致） | ✅ FROZEN | 成员 B 2026-09-20（成员 A 的样例暴露） | `ingest/service.py`、`graph/repository.py` |
| D-099 | 诊断/告警规则**只能引用 F-01 已冻结的事件类型**。曾有一条匹配 `vm.memory.exhausted` 的死规则（F-01 里没有该类型，永不命中）。新增测试：所有规则的事件类型引用必须是枚举成员 | ✅ FROZEN | 成员 B 2026-09-20 | `diagnosis/rules.py`、`alerts/rules.py` |
| D-100 | 成员 A 已实现的事件类型**必须有对应告警规则**（交叉校验）：A 在采集而 B 不告警 = 数据白采。据此补 `R-PROC-IOWAIT-013` | ✅ FROZEN | 成员 B 2026-09-20 | `tests/test_cross_layer.py` |
| D-101 | ✅ **平台层命名空间桥接已落地（原 🔴 关键路径阻塞，2026-09-23 关闭）**：`app/zsvirt/harvest.py` 把 ZWatch 读数映射成 `host:zsvirt:{uuid}` / `gpu:zsvirt:{serial}` 并落图（`zsvirt_resource_id` 现已有真实调用方）。VM 资源仍需 ZSvirt VM UUID，阻塞于 X-09 | ✅ FROZEN | 成员 B 2026-09-23（命题方答复关闭 X-07 后） | `zsvirt/harvest.py`、`tests/test_harvest.py` |
| D-102 | 跨层场景 = **平台层 + A 的探针症状 + B 的 GPU 渠道**三份批次叠加。演示与集成测试共用 `build_cross_layer_batches()`，避免"演示能跑但测试没覆盖" | ✅ FROZEN | 成员 B 2026-09-20 | `zsvirt/scenarios.py`、`app/demo.py --cross-layer` |（宿主有压力 + 本机占用低），因为 F-01 的事件类型里没有邻居争用事件，探针也不上报它。宿主满且本机也满时两名候选并列，**如实报告冲突降权，不调权重让它看起来果断** | ✅ FROZEN | 成员 B 2026-09-17 | `diagnosis/rules.py` |

| D-103 | **接入 ZWatch 指标渠道**（`app/zsvirt/watch.py`）：HTTP 与响应解析独立成模块，`GpuMetricsProvider` 只做渠道语义映射。响应形状的假设**集中在一处**，不符时抛 `ZWatchSchemaError` 并带上实际键名 —— 一个"解析不出来就返回空"的适配器在演示现场表现为"GPU 指标一直是 —"，会把排查方向误导到权限或网络 | ✅ FROZEN | 成员 B 2026-09-23（命题方答复提供路由与指标名） | `zsvirt/watch.py`、`zsvirt/__init__.py` |
| D-104 | **归因口径显式化**：`GpuMetricReading.attribution` ∈ {`partitioned`, `passthrough`}，`can_attribute` 由它派生。命题方确认当前环境为 **GPU 直通**（无 MDEV 实例），平台侧**没有**按虚拟机维度的显存占用 ⇒ 直通下 `host_mem_usage_pct == self_vgpu_mem_usage_pct` 且 `can_attribute=False`。"自己超配 vs 邻居干扰"在直通下**不可得**，如实标注而不是调权重让它看起来果断 | ✅ FROZEN | 成员 B 2026-09-23 | `zsvirt/__init__.py`、`zsvirt/harvest.py` |
| D-105 | **`GpuMemoryUtilization` 不得换算成字节数**：命题方原话"不应直接写成'已用显存字节数'"。`mem_used_bytes` 在 ZWatch 渠道保持 `None` —— 由百分比 × 总容量反推出的数字**不是观测值**，进证据链会让"置信度可复算"变成空话。`temperature_c` 同样可空（该指标可能缺采样）。缺失时**不写属性键**，避免"缺失"与"值为 None"在库里长得一样 | ✅ FROZEN | 成员 B 2026-09-23 | `zsvirt/watch.py`、`zsvirt/harvest.py` |
| D-106 | **`/api/health` 的 `gpuProvider` 必须真实探活**（早先硬编码 `status="ok"` 只回显配置字符串）。读不到的渠道报 ok 违反项目自己的诚实性红线。探活失败**不降级整体状态** —— 模拟兜底是有意设计，降级会让演示期间 `/api/health` 恒为 degraded 而失去信号价值 —— 但 `available` / `authStyle` / `credentialsConfigured` 与缺口说明必须如实给出 | ✅ FROZEN | 成员 B 2026-09-23 | `api/health.py` |

---

## 7. 契约冻结（第一轮）—— ✅ 已生效

> **2026-09-17：成员 A 与成员 C 均已签字确认，F-01～F-07 全部 FROZEN。**
>
> | 回执文件 | 结论 |
> |---|---|
> | `agents/docs/FREEZE_ACK.md`（成员 A） | F-01～F-07 **全部同意**，附 3 条不阻塞意见 |
> | `frontend/CONTRACT_FREEZE_CONFIRMATION.md`（成员 C） | F-01～F-05 **全部同意** |
>
> 回执已合入 `develop`（合并提交 `b6997d4` / `36d408a`）。
> 详细码表见 [`CONTRACT_FREEZE.md`](CONTRACT_FREEZE.md)，**已具有约束力**。

| ID | 项 | 状态 | 确认人 |
|---|---|---|---|
| F-01 | **事件 `type` 完整枚举**（17 项） | ✅ FROZEN | A、C 2026-09-17 |
| F-02 | **`rootCause` 码表**（11 项）+ 可读文案 | ✅ FROZEN | A、C 2026-09-17 |
| F-03 | **`recommendation` 码表**（14 项）+ 可读文案 | ✅ FROZEN | A、C 2026-09-17 |
| F-04 | `severity` / `alert.state` / `resource.kind` / `resource.status` / `resource.observability` | ✅ FROZEN | A、C 2026-09-17 |
| F-04.1 | `vgpu` 独立 kind = **是**；`task` 纳入首版 = **是**；输出 `potentiallyAffected` = **是** | ✅ FROZEN | A、C 2026-09-17 |
| F-05 | 敏感字段清单；**内网 IP 保留** | ✅ FROZEN | A、C 2026-09-17 |
| F-06 | 前端技术栈（D-016） | ✅ FROZEN | A 无异议、C 提案 |
| F-07 | 探针技术选型：**Python 3.12 / 零第三方依赖 / `/proc`+Docker API+`nvidia-smi` / 不用 eBPF / SQLite 缓冲** | ✅ FROZEN | A 2026-09-17 |

### 7.1 由签字附带的新增约束

| # | 来源 | 内容 | 归属 |
|---|---|---|---|
| 1 | A 意见 1 | `inference.*` / `agent.*` 事件依赖被观测 AI 服务/Agent 框架暴露日志或指标接口；**需三方定义「AI 工作负载日志规范」（路径 + 格式）**，否则只能靠进程/端口启发式，精度有限 | 三方 |
| 2 | A 意见 2 | **探针 `vmId` 来源未定**（建议 cloud-init/环境变量注入，或读 `/sys/class/dmi/id/product_uuid`）—— 锚定 Pull 资源的先决条件，**建议并入 X-04 向命题方确认** | 命题方 |
| 3 | A 意见 3 | `container.*` 事件依赖 VM 内 `/var/run/docker.sock` 可读；若受限则退化为 cgroup+`/proc`，**会丢失 OOM/重启类关键事件** | 命题方 |
| 4 | C 要求 | 每个 `Event` **必须携带 `severity`**（前端按事件自身 severity 渲染，字典默认值仅作兜底） | B |
| 5 | C 要求 | `Diagnosis` 新增独立字段 `potentiallyAffected`，**不得混入** `affectedResources`（D-075） | B |
| 6 | C 要求 | 字典端点须覆盖 F-01/F-02/F-03 全部码 + 中文文案 | B |
| 7 | A 承诺 | 探针侧已实现「第一道」脱敏（`agents/probe/sensitive.py`）；**B 侧 `normalize` 为兜底**，两侧规则须一致 | A / B |
| 8 | C 承诺 | 前端**不原样渲染** `Event.raw` 与 `process.cmdline`，展示层再收敛一次 | C |

> **第 1、2、3 条已登记为外部阻塞 X-08 / X-09 / X-10**（见 §8）。

---

## 8. 外部阻塞（依赖命题方）

> **2026-09-23 状态更新**：命题方第一轮答复（16 问全部作答）已收到，X-01～X-07
> **全部关闭**。第二轮提问（已发出，含第一轮遗漏的采集侧三问）
> 其中 X-08/X-10 需命题方答复，X-09 升级为**当前最关键阻塞**。

| ID | 阻塞项 | 影响 | 状态 |
|---|---|---|---|
| X-01 | ZSvirt API/SDK 开发文档 | `zsvirt-adapter` 落地 | ✅ **已关闭**（2026-09-23）：答复给出公开文档入口 + 9 个 ZWatch 路由后缀 |
| X-02 | 测试环境访问方式 / 试用账号 | 无法联调 | ✅ **已关闭**：答复给出内网地址、控制台、OAuth/AccessKey 说明（**信息留内部，不入仓库**） |
| X-03 | 基础镜像与样例资源 | 无法部署容器化 AI 负载 | 🔶 **部分关闭**：已 Ready 的有 CentOS 7.9（qcow2）与 Windows Server 2019（ISO），**无预装 AI 工作负载的镜像**；能否自定义镜像已列入第二轮提问 §4.4 |
| X-04 | 测试环境 GPU/vGPU 规格 | 决定场景一能否用真实数据 | 🔶 **部分关闭**：GPU 为**直通**（非 MDEV），NVIDIA Quadro RTX 6000 / 24 GiB / 驱动 535.183.04。**但直通下无按虚拟机的显存占用**（见 D-104），归因能力受此限制，已在第二轮提问 §1.2 追问 |
| X-05 | ZWatch 是否启用 | GPU 指标与平台告警能否取到 | ✅ **已关闭**：**已启用在用**，8 个 GET 接口实测可用；合规性亦确认（"是不同的架构情形"，不构成闭源核心能力） |
| X-06 | 目标集群 ZSvirt 版本 | API 兼容性对齐 | ✅ **已关闭**：实测 `ZStack-ZSvirt 1.0.0.93`，主版本与开源仓库 `VERSION` 一致 |
| X-07 | 模拟数据做法是否符合赛题要求 | 降级模式合法性 | ✅ **已关闭**：符合；但要求**一致标明来源并区分真实采集 / 真实回放 / 人工模拟**，且**全模拟不能替代最终环境演示** |
| X-08 | **AI 工作负载日志规范**（路径 + 格式）| 决定 `inference.*` / `agent.*` 事件能否准确采集（A 意见 1） | ⛔ **仍阻塞**：第一轮提问遗漏，已列入第二轮提问 §5.1 |
| X-09 | **探针 `vmId` 注入方式** | 锚定平台 VM 资源、打通跨层链的先决条件 | 🔴 **最关键阻塞**：第一轮遗漏，已列入第二轮提问 §5.2。D-101 的 VM 部分依赖它 |
| X-10 | **VM 内 `/var/run/docker.sock` 挂载方式** | 决定 `container.*` 事件（OOM/重启）能否采集（A 意见 3） | ⛔ **仍阻塞**：第一轮遗漏，已列入第二轮提问 §5.3 |
| X-11 | **平台告警记录为空** | 赛题要求关联「ZSvirt 告警」，但答复实测 `GetAlarmData` / `QueryActiveAlarm` **查不到记录** | 🆕 **新登记**：已列入第二轮提问 §2，追问是"环境无告警"还是"接口无数据"，并请求配置一条演示告警 |
| X-12 | **ZWatch 接口的请求/响应字段名** | 适配器当前的响应形状是**单一假设**（集中在一处，不符即抛 `ZWatchSchemaError`） | 🆕 **新登记**：已列入第二轮提问 §1，索取响应样例 |
| X-13 | **GPU 直通虚拟机的创建** | 样例负载、故障注入、演示视频三项交付全部依赖它 | 🆕 **新登记**：宿主机上尚无虚拟机，已列入第二轮提问 §4.1 请求协助 |

**当前关键路径**：X-13（建 VM）→ 样例 AI 负载（交付 d）→ 真实故障注入（交付 e）→ 演示视频（交付 g）。
X-09 决定跨层链能否真正闭合。

---

## 9. 决策统计

> 2026-09-17 更新：契约冻结生效后，原 7 项待冻结全部转为 FROZEN；
> F-04.1 三个待定项与 D-016/D-017 也随之关闭。

| 类别 | ✅ FROZEN | 🟡 PROPOSED | ⛔ BLOCKED |
|---|---|---|---|
| 项目与流程 | 5 | 0 | 0 |
| 技术基线 | **10**（+D-016 前端栈、D-017 探针栈已定） | 2（D-018 Python 安装方式已实测解决→见下、D-019） | 0 |
| 架构 | 9 | 0 | 0 |
| 数据模型 | 8 | 3 | 0 |
| 接口契约 | 17 | 0 | 0 |
| 故障场景与诊断 | 33（+D-081～D-102） | 1 | 0 |
| **契约冻结（F-01～F-07）** | **7** | 0 | 0 |
| 外部依赖 | 6 | 0 | **7**（X-01/02/05/06/07 关闭，X-03/04 部分关闭；X-08/09/10 仍阻塞，新增 X-11/12/13） |
| **合计** | **94** | **6** | **7**（D-101 关键路径阻塞**已关闭**） |

**已随实测关闭的项**：
- D-018（Python 3.12 安装方式）→ ✅ 已解决：**源码编译到 `~/.local`**（Ubuntu 26.04 无 3.12 包且缺 openssl 头文件）。
- D-019（PostgreSQL 位置与版本）→ ✅ 已解决：**本机 PostgreSQL 18.6**，专用非超级用户角色。
- D-017（探针技术选型）→ ✅ A 拍板：Python 3.12 / 零依赖。
- D-075（是否输出 `potentiallyAffected`）→ ✅ 输出，且 C 会在前端展示。

---

## 10. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-17 | 首版：汇总散落在 6 份文档中的 75 项决策，编号并标注状态、确认人、依据；识别 7 项待冻结与 7 项外部阻塞 | 已确认 |
| v0.2 | 2026-09-17 | **契约冻结 F-01～F-07 生效**（A、C 均签字，回执合入 develop `b6997d4`/`36d408a`）；新增 §7.1 签字附带约束 8 条；新增外部阻塞 X-08/X-09/X-10；统计更新（FROZEN 51→62） | 已确认 |
| v0.3 | 2026-09-17 | 实现任务 1（诊断与工作负载 4 个端点）时新增 D-077～D-080；**修正两处实现缺陷**：① `chain_intermediates` 只处理"证据在锚点下游"一个方向，导致本项目最常见场景下 `onChain` 恒为空；② `impact_scope` 的 `affected` 未剔除过渡层，同一资源会同时出现在两个集合里（D-077 的红线）。两处均有回归测试 | 已确认 |
| v0.8 | 2026-09-20 | 合入成员 A 的分支（`95d9167`），完成任务 6 全部四项，新增 D-098～D-102。**发现并修正 3 个产品缺陷 + 4 个测试期问题**：① 🔴 `_infer_parent_kind("process")` 猜成 `vm` 导致 A 的样例入库整批 503（父类型改为从本批声明中查）；② 🔴 诊断规则匹配不存在的 `vm.memory.exhausted`，是死规则；③ A 已实现的 `process.io_wait.high` 无任何告警规则；④ 🔴 平台层命名空间桥接缺失（`zsvirt_resource_id` 无调用方）升级为关键路径阻塞。测试数 564 → 637 | 已确认 |
| v0.6 | 2026-09-17 | 完成任务 4（诊断规则集），新增 D-092～D-097；**修正 1 个致命缺陷 + 3 个缺陷**：① 🔴 `resource_edge` 从不被真实上报填充（`upsert_resource` 写 `parent_id`，但没人写边表），导致拓扑无连线、工作负载计数恒为 0、诊断图遍历全空 —— 被"所有测试都手工建边"掩盖；② `Rule.contradicts` 声明了但引擎从未求值，反证规则无法表达；③ 指标型规则永不命中（证据 `name` 是事件类型，指标名从不可见）；④ 规则集匹配一个**不存在**的事件类型 `gpu.neighbor.contention`。规则内容落地后诊断不再恒为 UNKNOWN。测试数 466 → 488 | 已确认 |
| v0.5 | 2026-09-17 | 完成任务 3（告警 → 诊断自动联动），新增 D-089～D-091；自动诊断在**事务提交后**执行、只覆盖 `error` 以上活动告警、按 `alertId` 幂等并封顶 10 条；上报响应新增 `data.alerts.autoDiagnosis` 摘要（含跳过原因）。测试数 449 → 466 | 已确认 |
| v0.4 | 2026-09-17 | 完成任务 2（告警引擎），新增 D-081～D-088；**修正 5 处缺陷**：① 测试隔离缺口（`TRUNCATE` 只挂在 `db_session` 上，只请求 `client` 的用例会泄漏数据，症状是"单独跑通过、一起跑失败"）；② 新建告警的 `count` 硬编码 1 与证据条数矛盾；③ `lastFiredAt` 用接收时间而非事件发生时间；④ 静默到期被当作"已恢复"（D-083）；⑤ `reconcile_all` 未 flush 导致状态改动对同 session 的 SELECT 不可见。测试数 406 → 449 | 已确认 |
