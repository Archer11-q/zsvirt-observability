# 前端设计 & B→C 契约评审（成员 C）

| 字段 | 值 |
|---|---|
| 文档状态 | **DRAFT v0.1 — 待团队评审** |
| 归属 | `frontend/`（成员 C） |
| 负责人 | 成员 C |
| 最后更新 | 2026-09-17 |
| 关联契约 | `docs/API_CONTRACT.md` §4、§4.7（Q8–Q14） |

> 本文档是成员 C 对 B→C 查询契约待确认项（Q8–Q14）的评审意见与前端技术方案。
> 最终结论以团队评审后回写的 `docs/API_CONTRACT.md` 为准；本文档**不改动契约真源**。

---

## 1. 前端技术栈（C 提案，待评审）

> `TECH-BASELINE.md` §7 将前端选型留归成员 C。以下方案与 Q11/Q12 的回答强相关。

| 层 | 选型 | 理由 |
|---|---|---|
| 语言 / 框架 | TypeScript + React 18 + Vite | 生态成熟、构建快、演示可复现 |
| 数据请求 / 轮询 | TanStack Query（React Query） | 自带缓存、`refetchInterval` 轮询、游标分页，直接支撑 Q11 |
| UI 组件 | Ant Design 5 | 中后台表格 / 表单 / 状态标签 / 抽屉齐全，中文文档完善 |
| 拓扑 + 图表 | ECharts 5（graph + 折线 / 柱状） | 单库同时覆盖拓扑图与指标图，最小依赖 |
| 状态管理 | Zustand（轻量，仅全局 UI 态） | 数据态交给 React Query 缓存，不引入 Redux |
| 测试 | Vitest + React Testing Library（单测）；Playwright（E2E，可选） | 与 C 的测试职责对齐 |
| 构建 / 部署 | `vite build` → 静态产物，Nginx 托管或 B 后端静态挂载 | 详见后续 `DEPLOYMENT.md` |

> C 提案遵循「最小依赖、可复现」原则：不引入消息队列、不引入重型状态层。

---

## 2. B→C 契约评审：Q8–Q14

> 每条给出 C 的**推荐结论 + 理由 + 对 B 的要求**。

### Q8 是否需要认证 / 授权？C 如何拿凭据？

- **C 结论**：首版**不引入用户级认证 / 授权**；如需防护，采用**单一只读 Bearer Token**。
- **理由**：演示 / 比赛环境为内部信任网络，B→C 全是只读查询；OAuth / mTLS / RBAC 超出赛题范围，违反「最小依赖」。单一静态 token 成本极低，可防误操作且不影响演示。
- **对 B 的要求**：
  - 若采纳 token：B 校验 `Authorization: Bearer <token>`，token 经环境变量注入，仓库只留 `.env.example`（遵守仓库清洁红线）。
  - C 通过 `VITE_API_TOKEN` 构建时注入，前端统一加请求头。
  - 若不引入：明确 401 `UNAUTHENTICATED` / 403 `PERMISSION_DENIED` **本阶段不启用**，在错误模型标注为「预留」。

### Q9 路径是否加 /v1 版本前缀？

- **C 结论**：**赞成加 `/v1`**（支持 §2.1 提案）。
- **理由**：破坏性变更需要隔离手段；前端统一从 `VITE_API_BASE_URL` 读 baseUrl，加前缀成本为 0。
- **对 B 的要求**：职责文档中的无版本路径统一升为 `/api/v1/*`；`/api/health` 保持无前缀。

### Q10 `/api/workloads` 的确切语义？

- **C 结论**：定义为 **`ai_service` 层级的业务聚合视图**，不是容器，也不是 Agent/Task。
- **理由**：前端「工作负载总览」面向「哪个 AI 服务在跑、健不健康」；容器是基础设施细节（放拓扑看）、Agent/Task 是更细粒度（放服务详情看）。
- **对 B 的要求**：
  - 每个 workload 至少含：`id`、`name`、`status`、`attributes`（framework / modelName / endpoint / concurrency / qps，见 `DATA_MODEL.md` §4.2）、底层资源占用（GPU 利用率 / 显存、内存）、关联告警 / 事件计数、`diagnosisId`（若有）。
  - 将 §4.1 中 `/workloads` 的「【待确认】语义」回写为 `ai_service` 聚合，并补示例响应。

### Q11 实时推送 vs 纯轮询？

- **C 结论**：**首版纯轮询**，不引入 SSE / WebSocket。
- **理由**：最小依赖、易复现、无长连接状态管理；TanStack Query 的 `refetchInterval` 即可覆盖。
- **C 的轮询节奏（提案）**：`/health` 与顶部状态 3s；拓扑 / 告警 / 事件列表 10–15s；诊断详情 30s。
- **对 B 的要求**：
  - 轮询接口必须**幂等 + 游标分页稳定**，避免重复 / 漏数据。
  - 若后续要实时：**SSE 优先于 WebSocket**（单向、纯 HTTP、易调试），建议列为可选演进项，本轮不交付。

### Q12 拓扑规模上限与前端渲染匹配？

- **C 结论**：上限提案 **节点 ≤ 200、边 ≤ 400**（单次响应）。
- **理由**：ECharts graph 在 200 节点内可流畅交互；超过则必须靠服务端裁剪，前端不画全图。
- **对 B 的要求**：
  - 超限时用 `depth` / 根节点 / `kinds` 服务端裁剪，并返回 `truncated: true`；C 据此提示「结果被裁剪，请下钻」，而不是静默显示不全。
  - 确认后端查询能力能否匹配该上限；若不能，请回一个可行数字。

### Q13 诊断自动触发还是 C 手动触发？

- **C 结论**：**两者都要**。自动（告警 → 诊断）是主流程，但需要**手动触发端点**。
- **理由**：演示需要可控的「一键诊断」时机，联调也需要手动构造。
- **对 B 的要求**：
  - 补 `POST /api/v1/diagnoses`：请求体 `{ alertId }` 或 `{ window, anchorResourceId }`（与 `DATA_MODEL.md` `Diagnosis.trigger` 对齐），响应即 §4.5 的 Diagnosis 对象（同步返回或 202 + 轮询）。
  - 补 `GET /api/v1/diagnoses` 列表端点（C 需要「有哪些诊断」，否则只能从告警的 `diagnosisId` 跳转，无法发现无告警关联的诊断）。

### Q14 C 需要哪些枚举 / 字典？需要 B 提供字典端点吗？

- **C 结论**：**需要字典端点**（建议 `GET /api/v1/dict` 或 `/meta`），前端启动时拉取并缓存。
- **C 需要的枚举清单**：
  1. `severity`（info | warning | error | critical）
  2. `alert.state`（firing | acked | resolved | silenced）
  3. `resource.kind`（host | gpu | vgpu | vm | container | process | ai_service | agent | task）
  4. `resource.status`（running | stopped | error | unknown）
  5. `event.type` 完整枚举（三方共同定义，见 `DATA_MODEL.md` §8.5）
  6. `diagnosis.rootCause` 码 + `recommendation` 码（**含可读文案**，用于前端渲染根因 / 建议，不硬编码中文映射）
- **理由**：前端筛选下拉、图例颜色、状态标签、根因 / 建议文案都依赖枚举映射；硬编码会导致前后端漂移，破坏「可复现」。C 遵守 `DATA_MODEL.md` §3.3 不解析 ID，故需要 B 提供枚举与文案。
- **提醒 B**：`DATA_MODEL.md` §4.1 的 `resource.status`（running / stopped / error / unknown）与 §7 生命周期（discovered → active → stale → gone）是两套词汇，请明确查询接口对外暴露哪一套，避免 C 前端标签与后端不一致。

---

## 3. 对 B / 团队的行动项汇总

| # | 行动项 | 关联 |
|---|---|---|
| 1 | 确认是否引入单一只读 Bearer Token，并定义注入方式 | Q8 |
| 2 | 确认 `/api/v1` 前缀（§2.1） | Q9 |
| 3 | 回写 `/workloads` 语义为 `ai_service` 聚合 + 示例 | Q10 |
| 4 | 确认首版纯轮询，SSE 列为可选演进 | Q11 |
| 5 | 确认拓扑上限（节点 ≤ 200 / 边 ≤ 400）与服务端裁剪能力 | Q12 |
| 6 | 补 `POST /api/v1/diagnoses` 与 `GET /api/v1/diagnoses` | Q13 |
| 7 | 提供字典端点 `GET /api/v1/dict`（含 rootCause / recommendation 文案） | Q14 |
| 8 | 三方共同定义 `event.type` 完整枚举 | Q14 / DATA_MODEL §8.5 |

---

## 4. 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-17 | 首版：技术栈提案 + Q8–Q14 评审意见 | DRAFT，待评审 |
