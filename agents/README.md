# agents/ — ZSvirt 虚拟机内探针与数据采集

**负责人：成员 A**

本目录是 VM 内探针 / 采集组件的实现位置。

## 对 B 的接口（需要 A 确认的部分）

B 端接收 A 上报的端点与载荷格式定义在：

- [../docs/API_CONTRACT.md](../docs/API_CONTRACT.md) **§3 A → B 上报契约**

其中最关键的几条约定：

1. **`vmId` 必填**：载荷中必须携带 ZSvirt 虚拟机标识，它是把 VM 内部资源（容器、进程、AI 服务、Agent）挂到平台资源树上的**唯一锚点**（见 [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) §4.1）。
2. **资源用 `sourceId` 上报**：探针侧只需给出自己命名空间内稳定的 `sourceId`，全局 ID 由 B 负责拼装（见 [../docs/DATA_MODEL.md](../docs/DATA_MODEL.md) §3）。
3. **时间戳来源必须明确**：跨层关联依赖时间窗口，时钟漂移会直接导致漏关联。

## 待 A 确认的问题（阻塞 B 的接入实现）

见 [../docs/API_CONTRACT.md](../docs/API_CONTRACT.md) §3.3 的 **Q1–Q7**：

| # | 问题 |
|---|---|
| Q1 | `resourceRef` 引用的资源是否保证在同批次或之前批次出现过？ |
| Q2 | 上报是至少一次还是恰好一次？是否会重复投递？ |
| Q3 | 探针时间戳来源与时钟同步方式？ |
| Q4 | 批量大小上限与上报频率？ |
| Q5 | 探针如何发现 B 的地址？ |
| Q6 | 断网期间数据如何处理（本地缓冲还是丢弃）？ |
| Q7 | 是否需要鉴权（token / mTLS）？ |

## 环境注意事项（B 侧勘测到的本机事实，供 A 参考）

- 本机 WSL 内 `unprivileged_bpf_disabled=2`，且 `sudo` 需要交互密码 → **eBPF 类采集在本机不可行**。
- 本机 WSL 内不存在 `/dev/nvidia*` → **GPU 指标无法在开发机上直接采集**，需明确数据来源（见下方）。
- VM 内采集不受上述限制，但需要在 ZSvirt 侧确认权限与内核能力。

> **待确认**：GPU / vGPU 指标由 ZSvirt API 提供，还是由 A 在 VM 内采集上报？这决定 `docs/DATA_MODEL.md` 中 `gpu` / `vgpu` 字段的来源与归属。

> **状态：本目录尚未开始实现。** 探针技术选型、语言、采集手段属成员 A 的实现范围，B 不预设、不干预。
