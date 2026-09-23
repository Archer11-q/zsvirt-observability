// GPU 数据渠道的诚实标注：把 `/api/health` 里 `components.gpuProvider.detail` 的
// `available` / `attribution` 解析成**可以直接内联展示**的文案。
//
// 为什么单独抽一个模块：这两个字段决定了用户该怎么读页面上的 GPU 数字
// （读得到还是读不到、是卡级读数还是按 VM 拆分），所以必须在界面上看得见，
// 不能只躺在 tooltip 里。D-104 把归因口径冻结为 partitioned | passthrough，
// 当前环境是 **GPU 直通**：平台侧没有按虚拟机维度的显存占用，
// 「自身超配 vs 邻居干扰」不可得 —— 如实标注，而不是调权重让它看起来果断。

import { useQuery } from '@tanstack/react-query'
import { api } from '../api/endpoints'
import type { ComponentStatus, GpuProviderDetail } from '../types'

export type AttributionBasis = 'partitioned' | 'passthrough'

export interface GpuProviderInfo {
  /** 具体渠道名；字段缺失为 null（不猜） */
  mode: string | null
  /**
   * **字段缺失时为 null，与「明确报告不可用（false）」是两回事。**
   * 模拟渠道后端有意不返回该字段，把它当 false 会报出假的「GPU 指标读不到」。
   */
  available: boolean | null
  /** 归因口径；非这两个取值或字段缺失时为 null */
  attribution: AttributionBasis | null
  error: string | null
  note: string | null
  simulated: boolean
}

/** 后端错误码 → 人话。错误码是契约（README 约定 5），未知码原样透出而不是吞掉。 */
const ERROR_TEXT: Record<string, string> = {
  ZSVIRT_ENDPOINT_NOT_CONFIGURED: '未配置 ZSVIRT 端点',
  ZWATCH_UNREACHABLE_OR_UNAUTHORIZED: 'ZWatch 不可达或未授权',
}

export function errorText(code: string | null | undefined): string {
  if (!code) return '探针不可用'
  return ERROR_TEXT[code] ?? code
}

export function parseGpuProvider(
  components: Record<string, ComponentStatus> | undefined,
): GpuProviderInfo | null {
  const c = components?.gpuProvider
  if (!c) return null

  const d = (c.detail ?? {}) as GpuProviderDetail
  return {
    mode: typeof d.mode === 'string' ? d.mode : null,
    available: typeof d.available === 'boolean' ? d.available : null,
    attribution:
      d.attribution === 'partitioned' || d.attribution === 'passthrough' ? d.attribution : null,
    error: typeof d.error === 'string' ? d.error : null,
    note: typeof d.note === 'string' ? d.note : null,
    simulated: d.mode === 'simulated' || d.simulated === true,
  }
}

/**
 * 读取当前 GPU 渠道状态。与 StatusBar / HealthBanner 共用同一个 `['health']`
 * 查询（键与 refetchInterval 一致，TanStack 自动去重，不会多打请求）。
 */
export function useGpuProvider(): GpuProviderInfo | null {
  const q = useQuery({
    queryKey: ['health'],
    queryFn: () => api.health(),
    refetchInterval: 3000,
  })
  return parseGpuProvider(q.data?.components)
}
