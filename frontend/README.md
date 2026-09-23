# frontend/ — 可视化展示

**负责人：成员 C**

消费 B 实现的 15 个 B→C 查询端点（`docs/API_CONTRACT.md` §4），提供拓扑 / 工作负载 / 事件 / 告警 / 诊断五个页面 + 顶部健康状态条。

## 技术栈（冻结 F-06）

React 18 + TypeScript + Vite 5 + TanStack Query 5 + Ant Design 5 + ECharts 5 + Zustand

## 运行

```bash
cd frontend
npm install
npm run dev          # 开发服务器 http://localhost:5173，/api 代理到后端
npm run build        # 类型检查 + 生产构建 → dist/
npm run typecheck    # 仅类型检查（src + vite.config）
```

### 环境变量

复制 `.env.example` 为 `.env.local`（**勿提交真实凭据**）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `VITE_PROXY_TARGET` | `http://localhost:8000` | Vite 开发代理目标（B 的后端地址） |
| `VITE_API_TOKEN` | 空 | 可选 Bearer Token；留空 = 不发送（对应后端认证默认关闭） |

生产环境由反向代理（Nginx / B 静态挂载）承担同样的 `/api` 转发，前端代码只用相对路径。

## 目录

```
src/
  api/client.ts       统一 fetch 客户端（{data,meta} 信封 + 错误模型，区分 502/503）
  api/endpoints.ts    15 个端点封装
  types.ts            与后端 schemas 字段逐一对齐的 TS 类型
  lib/dict.tsx        字典上下文（GET /api/v1/dict 一次拉取、全局缓存）
  lib/format.ts       时间 / 字节 / 百分比格式化
  lib/tags.tsx        枚举 → 颜色 / 标签
  components/         状态条、降级横幅、查询错误、ECharts 封装
  pages/              拓扑 / 工作负载 / 事件 / 告警 / 诊断
```

## 对 B 的接口

C **必须通过 API 获取数据，不得直接连接 B 的数据库**（团队约定）。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康 / 降级状态（无版本前缀，直返非信封） |
| GET | `/api/v1/dict` | 枚举字典（含中文文案，可缓存 ETag） |
| GET | `/api/v1/topology` | 资源拓扑（rootId / depth / includeStale） |
| GET | `/api/v1/workloads` | AI 工作负载聚合视图（kind / since / to） |
| GET | `/api/v1/events` | 事件查询（游标分页） |
| GET | `/api/v1/events/{id}` | 事件详情 |
| GET | `/api/v1/events/{id}/related-alerts` | 事件关联告警 |
| GET | `/api/v1/alerts` | 告警列表（状态/严重级过滤 + 全量计数） |
| GET | `/api/v1/alerts/{id}` | 告警详情 |
| GET | `/api/v1/alerts/{id}/evidence` | 证据事件展开（含缺失报告） |
| POST | `/api/v1/alerts/{id}/actions` | 单条操作（ack/resolve/silence/unsilence） |
| POST | `/api/v1/alerts/actions` | 批量操作 |
| GET | `/api/v1/diagnoses` | 诊断列表（游标分页） |
| GET | `/api/v1/diagnosis/{id}` | 诊断详情（?includeEvidence） |
| POST | `/api/v1/diagnoses` | 手动触发诊断 |

## 几个必须遵守的契约约定

1. **资源 ID 是不透明的**：只能整体比对与传递，**不得解析其结构**（`docs/DATA_MODEL.md` §3.3）。
2. **必须处理降级状态**：`/api/health` 返回 `ok` / `degraded` / `down`，`degraded` 指明哪个上游坏了。前端用状态条 + 内容区横幅展示，不假装一切正常。
3. **必须处理裁剪标记**：拓扑 `truncated: true` 提示用户（深度/节点上限裁剪）。
4. **必须区分上游故障与自身故障**：`502 UPSTREAM_UNAVAILABLE` 与 `503 SERVICE_DEGRADED` 语义不同，`QueryError` 分别给出不同文案。
5. **错误码是契约**：结构化错误码分支；B 变更错误码属破坏性变更。
6. **模拟数据必须可见**：`gpuProvider.mode=simulated` 与证据 `source=simulated` 在 UI 内联标注（诚实性），不伪装成真实采集。

## 前后端对账

B 提供 `deploy/reconcile_frontend.py`（任务 6.4）做静态 + 实测对账：端点探活、`types.ts` 字段双向核对、枚举对账。前端侧需保持：

- `src/api/endpoints.ts` 的静态路径可被脚本识别（带 id 的路径用模板字符串 `` `/api/v1/xxx/${id}` ``）
- `types.ts` 的 `Severity` / `AlertState` / `ResourceStatus` 三个类型别名与 `/api/v1/dict` 一致
- 列表条目类型名用 `EventItem` / `AlertItem` / `Workload` / `Diagnosis` / `TopologyNode`
