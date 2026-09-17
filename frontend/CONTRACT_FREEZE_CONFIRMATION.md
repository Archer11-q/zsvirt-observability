# 契约冻结确认（成员 C）

| 字段 | 值 |
|---|---|
| 文档状态 | 待成员 B 回写签字表 |
| 归属 | `frontend/`（成员 C） |
| 负责人 | 成员 C |
| 日期 | 2026-09-17 |
| 关联 | `docs/CONTRACT_FREEZE.md`（F-01～F-05）、`docs/DECISIONS.md` |

> 本文件是成员 C 对第一轮契约冻结提案（F-01～F-05）的确认回复。
> 请成员 B 据此回写 `CONTRACT_FREEZE.md` 签字表，并将相关项标记 FROZEN。

---

## 结论总览

| 项 | C 结论 |
|---|---|
| F-01 事件类型枚举 | ✅ 同意 |
| F-02 `rootCause` 码表 | ✅ 同意 |
| F-03 `recommendation` 码表 | ✅ 同意 |
| F-04 基础枚举 | ✅ 同意 |
| F-04.1 `vgpu` 独立 kind | ✅ 同意（是） |
| F-04.1 `task` 纳入首版 | ✅ 同意（纳入） |
| F-04.1 `potentiallyAffected` | ✅ 同意输出，前端展示（视觉区分，见 §4） |
| F-05 敏感字段 / 内网 IP | ✅ 同意（内网 IP 保留） |

---

## F-01 事件类型枚举 —— 同意

- 命名规范 `{domain}.{subject}.{qualifier}` 与 C 的 Q14 要求一致；17 项覆盖三大故障场景 + 平台侧事件，够用。
- 字典端点 `GET /api/v1/dict` 已含中文可读文案，满足 C「前端不硬编码中文映射」的要求。
- **提醒**：C 前端按**事件自身携带的 `severity`** 渲染；表中「默认 severity」仅作字典兜底。请 B 保证每个 `Event` 都带 `severity`（`DATA_MODEL.md` §5.1 已约定）。

## F-02 `rootCause` 码表 —— 同意

- 11 项覆盖三大场景 + 反例 + `UNKNOWN` 兜底。`GPU_MEMORY_EXHAUSTED` 与 `GPU_NEIGHBOR_CONTENTION` 拆分是「GPU 归因」能力的直接体现，前端据此给出差异化建议文案，无异议。
- **要求**：字典端点返回 `rootCause` 码 + 中文文案（提案表已列），前端不硬编码。

## F-03 `recommendation` 码表 —— 同意

- 14 项；`recommendation` 为数组且**排序即优先级**，前端按顺序渲染建议列表，无异议。
- **要求**：字典端点返回码 + 中文文案。

## F-04 基础枚举 —— 同意

- `severity`(4) / `alert.state`(4) / `resource.kind`(9) / `resource.status`(4) / `resource.observability`(3) 均确认。
- **特别确认**：`status`（业务状态）与 `observability`（观测状态）拆分为两个独立字段是 C 提出的（D-032）。前端据此分别渲染「运行状态」与「数据新鲜度 / 是否陈旧」，不再混淆。

### F-04.1 三个待定项

1. **`vgpu` 独立 kind —— 同意（是）**：前端拓扑需展示 GPU → vGPU → VM 的切分关系，支撑场景一展示。
2. **`task` 纳入首版 —— 同意（纳入）**：Agent → Task 是赛题点名层级，前端服务详情需展示 Task 列表。**提醒**：`task` 字段多 TBD、首版数据可能稀疏，前端把 task 做成「有数据才渲染」的可选层，避免空层干扰。
3. **`potentiallyAffected` 输出 —— 同意输出，且前端展示**（详见 §4）。

## F-05 敏感字段清单 —— 同意，内网 IP 保留

- 同意 B 的「直接丢弃、不入库」清单（`*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `sk-` / `ghp_` / 连接串等）与「保留但掩码」处理（`cmdline` 参数掩码、`raw` 过脱敏管道、身份标识哈希）。
- **内网 IP：同意保留**。拓扑关联与节点标识需要它（VM `ipAddresses`、连线）；前端仅展示、不外泄。
- **C 侧补充承诺（双层防护）**：前端**不原样渲染** `Event.raw` 与 `process.cmdline`，展示层再做一次收敛，避免二次泄露。

---

## 需要 B 回写的动作

| 动作 | 关联 |
|---|---|
| 签字表 F-01～F-05 标记成员 C ✅ | 全部 |
| `Diagnosis` 对象新增独立字段 `potentiallyAffected`，与 `affectedResources` 并列、**不得混入** | D-075 |
| 字典端点 `GET /api/v1/dict` 覆盖 F-01/F-02/F-03 全部码 + 中文文案 | D-065 |

---

## 变更记录

| 版本 | 日期 | 变更 | 状态 |
|---|---|---|---|
| v0.1 | 2026-09-17 | 成员 C 对冻结提案 F-01～F-05 的确认 | 待 B 回写 |
