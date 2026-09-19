// 15 个 B→C 端点的一一封装。路径与参数与 docs/API_CONTRACT.md §4 对齐。

import { apiGet, apiGetDirect, apiPost } from './client'
import type {
  AlertActionData,
  AlertActionRequest,
  AlertEvidenceData,
  AlertItem,
  AlertListData,
  AlertParams,
  BulkAlertActionData,
  Diagnosis,
  DiagnosisListData,
  DiagnosisParams,
  Dict,
  EventItem,
  EventListData,
  EventParams,
  HealthResponse,
  TopologyData,
  TopologyParams,
  TriggerDiagnosisRequest,
  TriggerDiagnosisResponse,
  WorkloadListData,
  WorkloadParams,
} from '../types'

export const api = {
  // ---- 健康（直返，非信封）----
  health: () => apiGetDirect<HealthResponse>('/api/health'),

  // ---- 字典（可缓存，ETag + 304）----
  dict: () => apiGet<Dict>('/api/v1/dict'),

  // ---- 拓扑 ----
  topology: (p?: TopologyParams) => apiGet<TopologyData>('/api/v1/topology', p),

  // ---- 工作负载（limit 截断，非游标）----
  workloads: (p?: WorkloadParams) => apiGet<WorkloadListData>('/api/v1/workloads', p),

  // ---- 事件 ----
  events: (p?: EventParams) => apiGet<EventListData>('/api/v1/events', p),
  event: (id: string) => apiGet<EventItem>(`/api/v1/events/${id}`),
  eventRelatedAlerts: (id: string) => apiGet<AlertListData>(`/api/v1/events/${id}/related-alerts`),

  // ---- 告警 ----
  alerts: (p?: AlertParams) => apiGet<AlertListData>('/api/v1/alerts', p),
  alert: (id: string) => apiGet<AlertItem>(`/api/v1/alerts/${id}`),
  alertEvidence: (id: string) => apiGet<AlertEvidenceData>(`/api/v1/alerts/${id}/evidence`),
  alertAction: (id: string, body: AlertActionRequest) =>
    apiPost<AlertActionData>(`/api/v1/alerts/${id}/actions`, body),
  alertsBulkAction: (ids: string[], body: AlertActionRequest) =>
    apiPost<BulkAlertActionData>('/api/v1/alerts/actions', body, { ids }),

  // ---- 诊断 ----
  diagnoses: (p?: DiagnosisParams) => apiGet<DiagnosisListData>('/api/v1/diagnoses', p),
  diagnosis: (id: string, includeEvidence = true) =>
    apiGet<Diagnosis>(`/api/v1/diagnosis/${id}`, { includeEvidence }),
  triggerDiagnosis: (body: TriggerDiagnosisRequest) =>
    apiPost<TriggerDiagnosisResponse>('/api/v1/diagnoses', body),
}
