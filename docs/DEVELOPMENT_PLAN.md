# Crosslayer — 开发路线图与剩余任务

| 字段 | 值 |
|---|---|
| 文档状态 | **ACTIVE** —— 随进度更新 |
| 归属 | 全项目真源（产品文档） |
| 负责人 | 成员 B（后端）；A / C 部分见「接口边界」 |
| 最后更新 | 2026-09-17 |

> **本文档解决什么问题**：把「还剩什么、依赖什么、怎么算做完」写成可核对的清单，
> 避免"感觉快好了"这种无法验收的状态。每项任务都有**可执行的验收标准**。
>
> 决策状态查 [`DECISIONS.md`](DECISIONS.md)，契约查 [`API_CONTRACT.md`](API_CONTRACT.md)，
> 待确认项查 [`CONTRACT_FREEZE.md`](CONTRACT_FREEZE.md)。

---

## 1. 当前状态

### 1.1 已完成

| 层面 | 状态 |
|---|---|
| S0 只读审查 / S1 初始化 / S2 设计 | ✅ 完成（契约已冻结，见 `CONTRACT_FREEZE.md`） |
| 环境 | ✅ Python 3.12.11（源码编译）+ PostgreSQL 18.6 + venv（13 依赖锁定） |
| 数据模型 | ✅ 6 张表 + Alembic 迁移（在全新库上验证 upgrade/downgrade） |
| 资源图 | ✅ 遍历算法（纯函数）+ 持久化 + 拓扑裁剪 |
| 归一化 | ✅ 资源 ID 拼装 / 脱敏管道（与探针侧共享测试向量） |
| 诊断引擎 | ✅ 五步流程、规则求值、置信复算、影响范围三集分离（**已接入数据库与 API**） |
| 数据接入 | ✅ `POST /api/v1/ingest/batch`（A 的 Q1–Q7 全部落地） |
| 诊断与工作负载端点 | ✅ **任务 1 完成**（见 §1.2；C 的接口依赖已全部解除） |
| 告警引擎 | ✅ **任务 2 完成**：14 条声明式规则 + 聚合去重 + 证据绑定 + 静默 + 自动恢复 |
| 诊断联动 | 🔶 手动触发已通（`POST /diagnoses`）；**告警产生时自动诊断未做**（任务 3） |
| 测试 | ✅ **449 个**（单元 + 集成 + 契约），真实库上跑；ruff 全绿 |

### 1.2 已实现端点（16 个）

```
GET  /api/health                            健康与降级（无版本前缀）
POST /api/v1/ingest/batch                   接收 A 的探针上报
GET  /api/v1/topology                       资源拓扑（可裁剪）
GET  /api/v1/dict                           枚举字典（含中文文案）
GET  /api/v1/events                         事件查询（时间窗/资源/类型/严重级别）
GET  /api/v1/events/{id}                    事件详情
GET  /api/v1/events/{id}/related-alerts     反向链路：事件 → 采纳它的告警
GET  /api/v1/alerts                         告警列表 + 状态/严重级别计数
GET  /api/v1/alerts/{id}                    告警详情
GET  /api/v1/alerts/{id}/evidence           展开证据事件（含缺失报告）
POST /api/v1/alerts/{id}/actions            单条 ack/resolve/silence/unsilence
POST /api/v1/alerts/actions                 批量状态操作（逐条报告）
GET  /api/v1/workloads                      AI 服务负载总览（C 的 Q10 聚合视图）
GET  /api/v1/diagnoses                      诊断列表（游标分页 + 根因计数）
GET  /api/v1/diagnosis/{id}                 诊断详情（?includeEvidence=true 内联证据事件）
POST /api/v1/diagnoses                      手动触发诊断（{alertId} 或 {anchorResourceId, window}）
```

### 1.3 契约承诺但尚未实现的端点

**无。** `API_CONTRACT.md` §4.1 清单里的 16 个端点全部落地，
成员 C 的 9 项接口需求已满足。

---

## 2. 剩余任务

### 任务 1：诊断与工作负载端点 —— ✅ 已完成（2026-09-17）

**交付物**：`app/diagnosis/service.py`、`app/api/diagnoses.py`、
`app/api/workloads.py`、`app/api/schemas/diagnosis.py`、
`tests/test_diagnosis_api.py`、`tests/test_workloads_api.py`、`tests/_factories.py`。

| 子项 | 结果 |
|---|---|
| `Diagnosis` 序列化 | ✅ `confidenceBreakdown` 可复算、`ruleSetVersion` 必带、三集独立字段 |
| `GET /diagnoses` | ✅ 游标分页（`createdAt DESC, id DESC`）+ 根因计数 + 三集任一资源过滤 |
| `GET /diagnosis/{id}` | ✅ 详情 + `?includeEvidence=true` 内联证据事件（默认不内联，避免撑爆列表页） |
| `POST /diagnoses` | ✅ 两种请求体都支持；**同步返回**；`linkToAlert` 可关；参数非法**不落库** |
| `GET /workloads` | ✅ `ai_service` 聚合；事件/告警计数按**链路**归并；GPU 指标缺失时 `null` + `note` |
| 诊断落库 | ✅ 引擎产出的结论写入 `diagnosis` 表，含落库前的"置信可复算"自检 |

**验收标准对照**：6 条全部满足，均有测试断言
（`tests/test_diagnosis_api.py` 30 项、`tests/test_workloads_api.py` 25 项）。

**过程中的两处实现缺陷（已修正并加回归测试，见 `DECISIONS.md` D-077 / v0.3）**：

1. `chain_intermediates` 只处理"证据在锚点**下游**"一个方向 —— 而本项目最常见的
   场景恰恰相反（证据在 GPU/容器、锚点是链路末端的 AI 服务），于是 `onChain`
   **恒为空**，过渡层被错并进 `potentiallyAffected`。
2. 修好 ① 之后暴露出 `impact_scope` 的 `affected` 未剔除过渡层，同一资源会
   **同时出现在两个集合里** —— 违反 D-075 的"集合必须分开"。①之前这个缺陷
   是隐藏的，因为 `onChain` 一直是空的。

> 教训（值得记住）：断言"集合互斥"的测试在其中一个集合恒为空时会**永远通过**。
> 这类测试必须同时断言集合**非空**，否则它保护不了任何东西。

---

### 任务 2：告警引擎（规则求值 → 产生告警）—— ✅ 已完成（2026-09-17）

**交付物**：`app/alerts/rules.py`（14 条声明式规则 + 一致性自检）、
`app/alerts/engine.py`（求值 / 聚合 / 证据绑定 / 静默 / 恢复）、
`app/ingest/service.py`（接入点）、`tests/_scenarios.py`（三类场景夹具）、
`tests/test_alert_engine.py`（43 项）。

| 子项 | 结果 |
|---|---|
| 规则集 | ✅ 覆盖三类赛题场景 + 平台自监控；规则是**数据**，`assert_rules_are_consistent` 校验所有引用 |
| 求值 | ✅ 事件写入后求值，**与事件同事务**（告警和它的证据一起可见或一起不可见） |
| 聚合去重 | ✅ 按 `aggregationKey` 累加 `count` 与证据，**不新建**告警 |
| 证据绑定 | ✅ `evidenceEventIds` 非空 —— 引擎与数据库 CHECK 双重强制 |
| 分级 | ✅ 取规则与事件中**更严重**的一方，不降级显示 |
| 静默 | ✅ 静默期内只更新证据、不动状态；到期释放时**重新基准化** `lastFiredAt` |
| 恢复 | ✅ 超恢复窗口无新证据 → `resolved` + `resolvedAt`；复发新建告警 |
| 上报响应 | ✅ 带上告警摘要（`created`/`updated`/`skippedSilenced`），探针不必轮询 |

**验收标准对照**：6 条全部满足。

**过程中发现并修正的 5 个缺陷**（详见 `DECISIONS.md` v0.4）：

1. **测试隔离缺口**（影响所有后续任务）：`TRUNCATE` 只挂在 `db_session` 夹具上，
   只请求 `client` 的用例写库后不清理，数据泄漏给后续用例 —— 症状是
   "单独跑通过、一起跑失败"。清理逻辑已抽为 `_truncate_all` 并由两个夹具共用。
2. **`count` 与证据自相矛盾**：新建告警时 `count` 硬编码 1，但一批可能命中多条
   事件（`evidenceEventIds` 有 3 条而 `count=1`）。运维靠 `count` 判断频率。
3. **`lastFiredAt` 用接收时间**：迟到批次会让告警排到列表最前，而它旁边的证据
   时间戳是十分钟前。改为跟随事件的 `occurredAt`。
4. **静默到期被当成"已恢复"**：静默期内没有新证据，是因为**我们没在听**，
   不能推断"问题消失"。释放时把 `lastFiredAt` 重新基准化到释放时刻。
5. **`reconcile_all` 没有 flush**：`reconcile` 用同一 session 发 SELECT，而本项目
   关闭了 autoflush，未 flush 的状态改动对它不可见 —— 刚释放的静默告警会被立刻
   判为已恢复，正是第 4 条要防的行为。

> 教训：第 4、5 两条都是"合理推断在特定时序下变成错误结论"。负向用例必须断言
> **具体状态**，只断言"没有异常"会写出假通过。

---

## 3. 待办任务（任务 3 起）

### 任务 3：告警 → 诊断自动联动

**依赖**：任务 2（要有告警才谈联动）。

| 子项 | 要点 |
|---|---|
| 触发 | 告警进入 `firing` 时触发一次诊断 |
| 关联 | 诊断结果写回 `alert.diagnosisId` |
| 幂等 | 同一告警不重复触发无限诊断（按 `aggregationKey` + 时间窗去重） |
| 降级 | 诊断失败不影响告警本身（告警是主流程，诊断是增值） |

**验收标准**：

1. 告警产生后，`diagnosisId` 被填充
2. `GET /api/v1/alerts/{id}` 能看到关联诊断
3. 诊断失败时告警仍正常存在（不因诊断异常而丢失告警）
4. 同一告警的重复触发不产生无限诊断记录

---

### 任务 4：ZSvirt 适配层（**被外部阻塞**）

| 子项 | 要点 |
|---|---|
| `QueryGpuDevice` / `QueryMdevDevice` | GPU 与 vGPU **资产**清单（源码已确认字段：序列号/显存容量/功耗/驱动状态） |
| 资源清单同步 | 宿主机 / VM |
| `GpuMetricsProvider` | 三个实现：`ZWatchProvider` / `GuestSmiProvider` / `SimulatedProvider` |
| 权限与安全说明 | 赛题要求 README 声明实际使用的接口与权限 |

**阻塞项**：命题方需提供 API/SDK 文档、测试环境账号（见 §5 X-01~X-10）。

**可先做的部分**：`SimulatedProvider`（赛题明文要求的降级模式）**不依赖任何外部条件**，
可以先写出来，这样无 GPU 环境也能跑通全链路演示。

**验收标准**：

1. `/api/health` 的 `gpuProvider.mode` 反映真实渠道
2. 模拟模式下三类场景仍能完整走通（注入 → 告警 → 诊断）
3. **模拟数据可识别**，不伪装成真实采集进入证据链
4. ZSvirt 不可达时服务降级而非崩溃（`degraded` + `staleness`）

---

### 任务 5：契约与集成测试补强

| 子项 | 要点 |
|---|---|
| 契约 golden file | 按 `API_CONTRACT.md` 的示例响应固定，字段变更必须同步 |
| A→B 契约测试 | 用 A 的**真实样例载荷**做 fixture |
| B→C 契约测试 | 全部 12 个端点的响应形态断言 |
| 场景端到端 | 三类故障场景各一条（依赖任务 2） |

**验收标准**：契约测试失败 = 契约被破坏，**不允许改测试绕过**。

---

### 任务 6：演示与可复现材料

| 子项 | 要点 |
|---|---|
| 一键启动脚本 | `deploy/` 下；赛题要求"可复现" |
| 故障注入脚本 | 三场景各一条命令 |
| 演示数据夹具 | 每场景一份可回放数据 |
| 3–5 分钟演示脚本 | 含注入 → 告警 → 诊断 → 定位 |

---

## 4. 依赖关系

```
任务1 诊断/工作负载端点 ──┐
                          ├──▶ C 的页面开发（B5 联调收尾）
任务2 告警引擎 ───────────┤
      │                   │
      └──▶ 任务3 自动诊断联动
                          │
任务1 + 2 + 3 ────────────┴──▶ 任务5 场景端到端测试
                                    │
                                    └──▶ 任务6 演示材料

任务4 ZSvirt 适配层 ── ⛔ 阻塞于命题方（除 SimulatedProvider 外）
```

**关键路径**：任务 2 → 任务 3 → 任务 5 的场景测试 → 任务 6 演示。
任务 1 与任务 4 的模拟部分可并行。

---

## 5. 接口边界（与 A / C）

### 4.1 A → B（成员 A 提供）

| 项 | 状态 |
|---|---|
| 上报契约（`API_CONTRACT.md` §3） | ✅ 已冻结，A 已确认 Q1–Q7 |
| 脱敏规则一致性 | ✅ 由 `shared/sensitive_vectors.json` 保证；**A 侧有 2 条更松的规则**（见 `SENSITIVE_DATA.md` §8.2） |
| **稳定样例数据** | ⬜ **待 A 提供** —— 任务 5 的 A→B 契约测试需要 |
| 事件 `type` 覆盖确认 | ⬜ 待确认 17 项枚举 A 都能采到（尤其是 `inference.*` / `agent.*`，见 X-08） |

### 4.2 B → C（成员 B 提供）

| 项 | 状态 |
|---|---|
| C 需要的 9 个端点 | 🔶 **12 个已实现，还差 4 个**（§1.3） |
| 字典端点（含中文文案） | ✅ 已完成 |
| 游标分页稳定性 | ✅ 已完成（keyset + 双游标互斥） |
| 轮询节奏支撑 | ✅ 查询接口幂等；`ETag` 用于字典 |
| 错误码契约 | ✅ 已实现（§2.3 全部错误码） |

### 4.3 B 内部

| 项 | 状态 |
|---|---|
| `zsvirt-adapter` 是唯一 ZSvirt 耦合点 | ✅ 设计已定，待实现 |
| 诊断引擎不访问数据库/HTTP | ✅ 已实现（纯函数，可单测） |
| 规则是声明式数据 | ✅ 设计已定，任务 2 落地 |

---

## 6. 外部阻塞（依赖命题方）

| ID | 阻塞项 | 影响的任务 |
|---|---|---|
| X-01 | ZSvirt API/SDK 开发文档 | 任务 4 |
| X-02 | 测试环境访问方式 / 试用账号 | 任务 4 |
| X-03 | 基础镜像与样例资源 | 任务 6 |
| X-04 | 测试环境 GPU/vGPU 规格（vGPU 切分？VM 内能否 `nvidia-smi`？） | 任务 4 |
| X-05 | ZWatch 是否启用（源码位于 `premium/`，涉及赛题合规） | 任务 4 |
| X-06 | 目标集群 ZSvirt 版本 | 任务 4 |
| X-07 | 模拟数据做法是否符合赛题要求 | 任务 4 |
| X-08 | AI 工作负载日志规范（路径 + 格式） | 任务 2 的 `inference.*` / `agent.*` 事件 |
| X-09 | 探针 `vmId` 注入方式 | 任务 4 的 Pull/Push 汇合 |
| X-10 | VM 内 `/var/run/docker.sock` 挂载方式 | 任务 2 的 `container.*` 事件 |

**已发邮件向命题方索取**（2026-09-17）。在收到回应前，任务 4 只能做
`SimulatedProvider`，任务 2 的事件夹具需 B 自行构造。

---

## 7. 不建议做的事（与设计原则冲突）

记录在此以免后续反复讨论：

| 不建议 | 原因 |
|---|---|
| 引入消息队列 / 搜索引擎 | `ADR-0003` 已确认最小依赖；当前规模用不到 |
| 为单个故障场景写一次性硬编码 | `DIAGNOSIS_DESIGN.md` §7.2：规则必须是声明式数据 |
| 让诊断引擎直接查数据库 | 会破坏"可脱离基础设施单测"，见 `BACKEND_DESIGN.md` §2.7 |
| 让 C 直连数据库 | `API_CONTRACT.md` §1 原则 2 |
| 把任务 2 的事件夹具等 A 提供 | 夹具可按已冻结契约自行构造，无谓阻塞 |
| 用 offset 分页 | 事件持续追加会错位，漏读或重读 |

---

## 8. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-17 | 首版：当前状态、6 个剩余任务（含验收标准）、依赖关系、A/C 接口边界、10 项外部阻塞、不建议事项 | ACTIVE |
