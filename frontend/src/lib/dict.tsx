// 字典上下文：GET /api/v1/dict 一次拉取、全局缓存（C 的 Q11：10–15s 轮询 → 这里按 5min stale）。
// 字典是枚举与中文文案的唯一真源，前端不硬编码（Q14 / 契约 §4.6.1）。

import { createContext, useContext, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/endpoints'
import type { Dict } from '../types'

const EMPTY: Dict = {
  severity: {},
  alertState: {},
  resourceKind: {},
  resourceStatus: {},
  observability: {},
  eventType: {},
  rootCause: {},
  recommendation: {},
}

const DictContext = createContext<Dict>(EMPTY)

export function DictProvider({ children }: { children: ReactNode }) {
  const { data } = useQuery({
    queryKey: ['dict'],
    queryFn: async () => (await api.dict()).data,
    staleTime: 5 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
    retry: 2,
  })
  return <DictContext.Provider value={data ?? EMPTY}>{children}</DictContext.Provider>
}

export function useDict(): Dict {
  return useContext(DictContext)
}
