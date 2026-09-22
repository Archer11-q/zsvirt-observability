// 平台降级横幅：/api/health 非 ok 时在内容区顶部给出醒目提示（不止藏在状态条里）。
// 与 StatusBar 共享同一个 ['health'] 查询（TanStack 自动去重，不重复请求）。

import { useQuery } from '@tanstack/react-query'
import { Alert } from 'antd'
import { api } from '../api/endpoints'

export function HealthBanner() {
  const q = useQuery({ queryKey: ['health'], queryFn: () => api.health(), refetchInterval: 3000 })
  if (q.isPending || q.isError || !q.data) return null
  if (q.data.status === 'ok') return null

  const broken = Object.entries(q.data.components)
    .filter(([, c]) => c.status !== 'ok')
    .map(([n, c]) => `${n}: ${c.status}`)
    .join('，')

  return (
    <Alert
      type={q.data.status === 'degraded' ? 'warning' : 'error'}
      showIcon
      banner
      style={{ marginBottom: 12 }}
      message={q.data.status === 'degraded' ? '平台处于降级模式（部分组件异常）' : '平台不可用'}
      description={broken || '详情见顶部状态条'}
    />
  )
}
