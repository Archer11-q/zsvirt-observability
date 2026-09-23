// 顶部健康状态条：GET /api/health，3s 轮询（C 的轮询节奏契约）。
//
// gpuProvider 的诚实标注必须**内联可见**，不能只藏在 tooltip 里
// （frontend/README.md 契约约定 6 / 7）：
//   - mode=simulated          → 「(模拟)」紫
//   - available=false         → 「GPU 指标读不到：<原因>」红
//   - attribution=passthrough → 「GPU 直通：卡级读数」橙
// tooltip 只作**补充**（承载后端 note 原文），不作为这些信息的唯一载体。

import { useQuery } from '@tanstack/react-query'
import { Tag, Tooltip } from 'antd'
import { ApiError } from '../api/client'
import { api } from '../api/endpoints'
import { errorText, useGpuProvider } from '../lib/gpu'
import type { ComponentStatus } from '../types'

const STATUS_COLOR: Record<string, string> = { ok: 'green', degraded: 'orange', down: 'red' }

function detailText(c: ComponentStatus): string {
  if (!c.detail) return '无详情'
  return Object.entries(c.detail)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join(', ')
}

export function StatusBar() {
  const q = useQuery({
    queryKey: ['health'],
    queryFn: () => api.health(),
    refetchInterval: 3000,
    retry: 1,
  })
  const gpu = useGpuProvider()

  if (q.isError) {
    const msg = q.error instanceof ApiError ? `${q.error.code} ${q.error.message}` : '网络错误'
    return <Tag color="red">后端不可用：{msg}</Tag>
  }
  if (!q.data) return <Tag>连接中…</Tag>

  const h = q.data
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      <Tag color={STATUS_COLOR[h.status] ?? 'default'}>整体 {h.status}</Tag>
      {Object.entries(h.components).map(([name, c]) => {
        const simulated = name === 'gpuProvider' && c.detail?.mode === 'simulated'
        return (
          <Tooltip key={name} title={`${detailText(c)}`}>
            <Tag color={simulated ? 'purple' : STATUS_COLOR[c.status] ?? 'default'}>
              {name}
              {simulated ? ' (模拟)' : ''} {c.status}
            </Tag>
          </Tooltip>
        )
      })}

      {/* GPU 渠道的两条诚实标注：独立常驻，不用展开 tooltip 就能看到。
          `available === null`（字段缺失，如模拟渠道）与 `false` 区别对待 —— 见 lib/gpu.ts。 */}
      {gpu?.available === false && (
        <Tooltip title={gpu.note ?? undefined}>
          <Tag color="red">GPU 指标读不到：{errorText(gpu.error)}</Tag>
        </Tooltip>
      )}
      {gpu?.attribution === 'passthrough' && (
        <Tooltip title={gpu.note ?? undefined}>
          <Tag color="orange">GPU 直通：卡级读数（不可按 VM 归因）</Tag>
        </Tooltip>
      )}
    </div>
  )
}
