// Crosslayer 前端类型 —— 与 docs/API_CONTRACT.md §4 及后端 schemas 对齐。
// 资源 ID 是不透明字符串：只能整体比对与传递，不得解析结构（DATA_MODEL §3.3）。

export type Severity = 'info' | 'warning' | 'error' | 'critical'
export type AlertState = 'firing' | 'acked' | 'resolved' | 'silenced'
export type ResourceStatus = 'running' | 'stopped' | 'error' | 'unknown'
export type Observability = 'active' | 'stale' | 'gone'
/** 根因归属：是不是**这个工作负载自己**的问题（契约 §4.3）。 */
export type RootCauseScope = 'own' | 'shared'

// ---- 通用包裹 ----
export interface ResponseMeta {
  generatedAt: string
  traceId: string
  page: PageMeta | null
}
export interface PageMeta {
  limit: number
  nextCursor: string | null
  count: number
}
export interface Envelope<T> {
  data: T
  meta: ResponseMeta
}
export interface ErrorBody {
  code: string
  message: string
  traceId?: string
  details?: Record<string, unknown>
}

// ---- /api/health ----
export interface ComponentStatus {
  status: string
  detail: Record<string, unknown> | null
}
/**
 * `/api/health` 里 `components.gpuProvider.detail` 的形状（docs/DECISIONS.md D-104）。
 *
 * `available` / `attribution` 都是**可选**字段，且语义各不相同：
 * - `available` **在模拟渠道下不返回**（后端有意为之）。「字段缺失」≠「报告不可用」，
 *   把它读成 false 会报出假的「GPU 指标读不到」。
 * - `attribution` 只在配置了 ZSVIRT 端点、真正调用过探针时才有值。
 */
export interface GpuProviderDetail {
  mode?: string
  configuredMode?: string
  simulated?: boolean
  available?: boolean
  /** 归因口径：`passthrough` = 卡级读数，平台侧无法按 VM 拆分（当前环境） */
  attribution?: 'partitioned' | 'passthrough'
  error?: string | null
  note?: string | null
  endpoint?: string | null
  authStyle?: string
  credentialsConfigured?: boolean
}
export interface HealthResponse {
  status: 'ok' | 'degraded' | 'down'
  components: Record<string, ComponentStatus>
  version: Record<string, string>
  generatedAt: string
}

// ---- /api/v1/dict ----
/** 键与后端 `enums.build_dict_payload()` 一一对应（契约 §4.6.1，共 8 段）。 */
export interface Dict {
  severity: Record<string, string>
  alertState: Record<string, string>
  resourceKind: Record<string, string>
  resourceStatus: Record<string, string>
  observability: Record<string, string>
  eventType: Record<string, string>
  rootCause: Record<string, string>
  recommendation: Record<string, string>
}

// ---- /api/v1/topology ----
export interface TopologyNode {
  id: string
  kind: string
  name: string | null
  parentId: string | null
  status: ResourceStatus
  observability: Observability
  staleness: string | null
  /**
   * `staleness` 是否**已超阈值**（后端 `TOPOLOGY_STALENESS_WARN_SEC`，默认 300s）。
   *
   * 用这个布尔值，而不是去解析 `staleness` 字符串再比大小：`"13d"` 与 `"5m"`
   * 的大小不在字面上，解析是易碎逻辑。后端既已把判断结果给出来，前端不该重算一遍。
   *
   * **与 `observability` 是两回事**（契约 §4.2.1）：这里是「多久没听到心跳」这个
   * **时间事实**，`observability` 是 B 的生命周期**判决**。早先两者耦合，而陈旧标记器
   * 默认关闭 ⇒ `observability` 恒为 `active` ⇒ 承诺给前端的字段永久为空。
   */
  isStale: boolean
  lastSeenAt: string
  isPlaceholder: boolean
  attributes: Record<string, unknown>
  labels: Record<string, unknown>
}
export interface TopologyEdge {
  parentId: string
  childId: string
  relation: string
}
export interface TopologyData {
  nodes: TopologyNode[]
  edges: TopologyEdge[]
  truncated: boolean
  truncationReason: string | null
  droppedNodes: number
}
export interface TopologyParams {
  rootId?: string
  depth?: number
  kinds?: string
  includeStale?: boolean
}

// ---- /api/v1/workloads ----
export interface WorkloadResourceUsage {
  gpuMemoryUsedBytes: number | null
  gpuMemoryTotalBytes: number | null
  gpuUtilizationPct: number | null
  note: string | null
}
export interface Workload {
  id: string
  name: string | null
  status: ResourceStatus
  observability: Observability
  staleness: string | null
  attributes: Record<string, unknown>
  resourceUsage: WorkloadResourceUsage
  eventCount: number
  alertCount: number
  firingAlertCount: number
  diagnosisId: string | null
  /**
   * 关联诊断的根因码。**可能为 `null`** —— 那表示「没有与该工作负载相关的结论」，
   * 是一个**正确的结果**，不是加载失败。
   *
   * 语义在 2026-09-30 收紧：以前几乎总有值，于是健康场景也会挂上
   * `GPU_MEMORY_EXHAUSTED`，与真正的容器 OOM 在列表里长得一模一样
   * （见下方 `rootCauseScope`）。
   */
  rootCause: string | null
  /**
   * 这条根因**是不是它自己的问题**。`own` / `shared` 必须**用不同视觉**呈现：
   * 一张 GPU 卡满时，卡上每个工作负载都受影响，但**没有一个是"自己配错了"** ——
   * 不区分的话，界面会把共享基础设施的故障显示成每个服务各自的故障，
   * 运维会去改一个没问题的服务。
   */
  rootCauseScope: RootCauseScope | null
  parentId: string | null
}
export interface WorkloadListData {
  items: Workload[]
  /**
   * 当前 GPU 数据渠道（`zsvirt-zwatch` / `guest-smi` / `simulated` / `auto`）。
   * 与 `/api/health` 的 `gpuProvider.mode` 同源（`Settings.gpu_provider_mode`），
   * 用来在**数字旁边**区分真实指标与模拟数据（诚实性要求）。
   */
  gpuProviderMode: string
  /**
   * 事件 / 告警计数的统计窗口起点（UTC ISO8601，默认近 24 小时）。
   *
   * **必须在界面上展示。** 窗口不可见时会出现自相矛盾的现象：某个工作负载
   * `eventCount: 0`、`alertCount: 0`，**同时** `rootCause` 非空 ——
   * 因为事件全在窗口之外。用户看到"0 条告警 + 一个确定根因"只会认为界面坏了。
   */
  windowFrom: string
  /** 统计窗口终点（UTC ISO8601）。 */
  windowTo: string
}
export interface WorkloadParams {
  since?: string
  to?: string
  kind?: string
  limit?: number
}

// ---- /api/v1/events ----
export interface EventItem {
  id: string
  occurredAt: string
  receivedAt: string
  source: string
  resourceId: string
  type: string
  severity: string
  message: string | null
  metrics: Record<string, unknown>
  raw: Record<string, unknown>
  traceId: string | null
  batchId: string | null
}
export interface EventListData {
  items: EventItem[]
  newestOccurredAt: string | null
  oldestOccurredAt: string | null
}
export interface EventParams {
  from?: string
  to?: string
  resourceId?: string[]
  type?: string[]
  minSeverity?: Severity
  limit?: number
  cursor?: string
  /** 向前追新游标（轮询用），与 cursor 互斥 */
  after?: string
}

// ---- /api/v1/alerts ----
export interface AlertItem {
  id: string
  ruleId: string
  resourceId: string
  severity: string
  state: string
  firstFiredAt: string
  lastFiredAt: string
  resolvedAt: string | null
  count: number
  aggregationKey: string
  evidenceEventIds: string[]
  diagnosisId: string | null
  title: string | null
  labels: Record<string, unknown>
}
export interface AlertListData {
  items: AlertItem[]
  stateCounts: Record<string, number>
  activeSeverityCounts: Record<string, number>
}
export interface AlertParams {
  state?: AlertState[]
  minSeverity?: Severity
  resourceId?: string[]
  ruleId?: string[]
  from?: string
  to?: string
  evidenceEventId?: string
  limit?: number
  cursor?: string
}
export type AlertAction = 'ack' | 'resolve' | 'silence' | 'unsilence'
export interface AlertActionRequest {
  action: AlertAction
  silenceSeconds?: number
  comment?: string
}
export interface AlertActionData {
  alert: AlertItem
  changed: boolean
  message: string
}
export interface BulkAlertActionData {
  requested: number
  changed: number
  unchanged: number
  failed: number
  results: Record<string, unknown>[]
}
export interface AlertEvidenceData {
  alertId: string
  evidenceCount: number
  events: EventItem[]
  missingEventIds: string[]
}

// ---- /api/v1/diagnoses ----
export interface ConfidenceContribution {
  ruleId: string
  contribution: number
  observed: string
}
export interface DiagnosisEvidence {
  type: string
  name: string
  resourceId?: string | null
  value?: string | null
  count?: number | null
  at?: string | null
  eventIds?: string[]
  source?: string | null
}
export interface DiagnosisRecommendation {
  code: string
  text: string
}
export interface Diagnosis {
  id: string
  createdAt: string
  trigger: Record<string, unknown>
  rootCause: string
  confidence: number
  confidenceBreakdown: ConfidenceContribution[]
  affectedResources: string[]
  potentiallyAffected: string[]
  onChain: string[]
  evidence: DiagnosisEvidence[]
  recommendation: DiagnosisRecommendation[]
  ruleSetVersion: string
  notes: string[]
  durationMs: number | null
  evidenceEvents?: EventItem[]
}
export interface DiagnosisListData {
  items: Diagnosis[]
  rootCauseCounts: Record<string, number>
}
export interface DiagnosisParams {
  rootCause?: string[]
  resourceId?: string
  from?: string
  to?: string
  limit?: number
  cursor?: string
}
export interface TriggerDiagnosisRequest {
  alertId?: string
  window?: { from: string; to: string }
  anchorResourceId?: string
  linkToAlert?: boolean
}
export type TriggerDiagnosisResponse = Diagnosis & { linkedToAlert?: boolean }

// ---- /api/v1/metrics（契约 §4.11）----
export interface MetricPoint {
  /** 事件的发生时间（UTC ISO8601） */
  at: string
  value: number
}
export interface MetricSeries {
  resourceId: string
  name: string
  /**
   * **恒为 `null`** —— 事件契约里 `metrics` 只有 `{名称: 数值}`，没有单位。
   * 从名称猜（`_pct` → `%`）在 `io_wait_pct` 上碰巧对、在 `value` 上就是编造。
   * 界面显示**裸数值**，不要补单位。
   */
  unit: string | null
  /** 按时间**升序** —— 曲线从左到右。 */
  points: MetricPoint[]
  /**
   * 该序列是否因点数上限被裁剪。裁剪时保留的是**最新**的点，
   * 因此曲线左端缺口不代表"那时没数据"，而是"被截掉了"。
   */
  truncated: boolean
}
export interface MetricListData {
  series: MetricSeries[]
  /** 实际扫描的事件数 —— 调用方据此判断 `truncated` 是否触发了。 */
  eventsScanned: number
  /**
   * `true` 时曲线可能不完整，**必须在界面上体现**：
   * 一条"看起来完整其实被截断"的曲线会让人误判趋势平稳。
   */
  truncated: boolean
  /** 本次结果里出现的全部指标名。 */
  names: string[]
}
export interface MetricParams {
  /** 资源 ID 过滤，可重复传 */
  resourceId?: string[]
  /** 指标名过滤，可重复传 */
  name?: string[]
  /** 发生时间窗（闭区间）。非法或倒置后端返回 400，不静默忽略。 */
  from?: string
  to?: string
  scanEvents?: number
}

// ---- /api/v1/diagnoses/{id}/ticket（契约 §4.12）----
export interface DiagnosisTicket {
  diagnosisId: string
  title: string
  /** 多行纯文本，可直接粘贴进工单系统。由**纯函数**生成：同一诊断必然同一文本。 */
  text: string
  rootCause: string
  confidence: number
  /**
   * 该结论是否**可据此行动**。为 `false` 时（`UNKNOWN` 或置信度 < 0.5）
   * 工单正文已写明"不可据此行动"，界面须用**不同样式**区分 ——
   * 不要让"待确认"的结论看起来和确定结论一样。
   *
   * 另：该端点**只读**，不写库、不写回 ZSvirt、不执行任何处置动作。
   * 不要把「复制为工单」做成"一键修复"入口。
   */
  actionable: boolean
}
