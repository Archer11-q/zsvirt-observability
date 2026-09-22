// 统一的查询错误展示。落实 C 的契约约束（frontend/README.md）：
// - 区分 502 UPSTREAM_UNAVAILABLE（上游挂了）与 503 SERVICE_DEGRADED（B 自身受损）
// - 错误码是契约，据此分支提示；带"重试"出口

import { Alert, Button } from 'antd'
import { ApiError } from '../api/client'

interface Desc {
  type: 'error' | 'warning'
  message: string
  description?: string
}

function describe(e: unknown): Desc {
  if (e instanceof ApiError) {
    if (e.status === 502) {
      return {
        type: 'warning',
        message: '下游依赖不可用（UPSTREAM_UNAVAILABLE）',
        description: `后端依赖（数据库 / ZSvirt / GPU 渠道）暂时不可用，已进入降级模式。${e.message}`,
      }
    }
    if (e.status === 503) {
      return { type: 'warning', message: '服务降级中（SERVICE_DEGRADED）', description: e.message }
    }
    if (e.status === 401 || e.status === 403) {
      return { type: 'error', message: '鉴权失败', description: e.message }
    }
    if (e.status === 404) {
      return { type: 'error', message: '资源不存在', description: e.message }
    }
    if (e.status === 0) {
      return { type: 'error', message: '网络错误（无法连接后端）', description: e.message }
    }
    return { type: 'error', message: `请求失败（${e.code}）`, description: e.message }
  }
  return { type: 'error', message: '加载失败', description: String(e) }
}

export function QueryError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const d = describe(error)
  return (
    <Alert
      type={d.type}
      showIcon
      message={d.message}
      description={d.description}
      action={
        onRetry ? (
          <Button size="small" onClick={onRetry}>
            重试
          </Button>
        ) : undefined
      }
    />
  )
}
