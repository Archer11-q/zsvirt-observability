// 顶部健康状态条：GET /api/health，3s 轮询（C 的轮询节奏契约）。
// gpuProvider 为模拟数据时必须**内联可见**（诚实标注），不能藏在 tooltip 里。

import { useQuery } from '@tanstack/react-query'
import { Tag, Tooltip } from 'antd'
import { ApiError } from '../api/client'
import { api } from '../api/endpoints'
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
    </div>
  )
}
