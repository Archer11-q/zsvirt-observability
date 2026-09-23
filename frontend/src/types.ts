// Crosslayer 前端类型 —— 与 docs/API_CONTRACT.md §4 及后端 schemas 对齐。
// 资源 ID 是不透明字符串：只能整体比对与传递，不得解析结构（DATA_MODEL §3.3）。

export type Severity = 'info' | 'warning' | 'error' | 'critical'
export type AlertState = 'firing' | 'acked' | 'resolved' | 'silenced'
export type ResourceStatus = 'running' | 'stopped' | 'error' | 'unknown'
export type Observability = 'active' | 'stale' | 'gone'

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
  rootCause: string | null
  parentId: string | null
}
export interface WorkloadListData {
  items: Workload[]
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
