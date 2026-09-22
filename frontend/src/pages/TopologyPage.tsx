import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Select, Space, Spin, Switch } from 'antd'
import * as echarts from 'echarts'
import { api } from '../api/endpoints'
import { EChart } from '../components/EChart'
import { QueryError } from '../components/QueryError'
import { useDict } from '../lib/dict'
import { fmtTime } from '../lib/format'
import type { TopologyNode } from '../types'

const KIND_PALETTE = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#9a60b4', '#fc8452', '#3ba272']

function nodeToEcharts(n: TopologyNode, kindIdx: Record<string, number>): any {
  return {
    id: n.id,
    name: n.name ?? n.id,
    category: kindIdx[n.kind] ?? 0,
    symbolSize: n.kind === 'host' || n.kind === 'gpu' ? 46 : 32,
    rawKind: n.kind,
    rawStatus: n.status,
    rawObservability: n.observability,
    rawLastSeen: n.lastSeenAt,
    itemStyle: {
      borderColor: n.status === 'error' ? '#ff4d4f' : '#d9d9d9',
      borderWidth: n.status === 'error' ? 3 : n.observability === 'stale' ? 2 : 1,
      borderType: n.observability === 'stale' ? 'dashed' : 'solid',
    },
  }
}

export default function TopologyPage() {
  const dict = useDict()
  const [rootId, setRootId] = useState<string | undefined>()
  const [depth, setDepth] = useState(3)
  const [includeStale, setIncludeStale] = useState(true)

  const q = useQuery({
    queryKey: ['topology', rootId, depth, includeStale],
    queryFn: () => api.topology({ rootId, depth, includeStale }),
    refetchInterval: 15000,
  })

  const option = useMemo<echarts.EChartsOption>(() => {
    const nodes = q.data?.data.nodes ?? []
    const edges = q.data?.data.edges ?? []
    const kinds = Array.from(new Set(nodes.map((n) => n.kind)))
    const kindIdx: Record<string, number> = {}
    kinds.forEach((k, i) => {
      kindIdx[k] = i
    })

    return {
      tooltip: {
        formatter: (params: any) => {
          if (params?.dataType !== 'node' || !params?.data) return ''
          const d = params.data as Record<string, any>
          return [
            `${d.name}`,
            `类型: ${dict.resourceKind[d.rawKind] ?? d.rawKind}`,
            `状态: ${d.rawStatus}`,
            `观测: ${d.rawObservability}`,
            `最后心跳: ${fmtTime(d.rawLastSeen)}`,
          ].join('<br/>')
        },
      },
      legend: kinds.length
        ? { data: kinds.map((k) => ({ name: dict.resourceKind[k] ?? k })), top: 0 }
        : undefined,
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          data: nodes.map((n) => nodeToEcharts(n, kindIdx)),
          links: edges.map((e) => ({ source: e.parentId, target: e.childId })),
          categories: kinds.map((k, i) => ({
            name: dict.resourceKind[k] ?? k,
            itemStyle: { color: KIND_PALETTE[i % KIND_PALETTE.length] },
          })),
          label: { show: true, position: 'right', fontSize: 11 },
          force: { repulsion: 240, edgeLength: 120 },
          lineStyle: { color: '#bfbfbf', width: 1, curveness: 0.08 },
        },
      ],
    } as echarts.EChartsOption
  }, [q.data, dict])

  const data = q.data?.data

  return (
    <Card
      title="资源拓扑"
      extra={
        <Space>
          <span>深度</span>
          <Select
            value={depth}
            onChange={setDepth}
            style={{ width: 80 }}
            options={[1, 2, 3, 4, 5, 6, 7].map((d) => ({ value: d, label: `${d}` }))}
          />
          <Switch
            checkedChildren="含离线"
            unCheckedChildren="仅在线"
            checked={includeStale}
            onChange={setIncludeStale}
          />
          {rootId && (
            <Button size="small" onClick={() => setRootId(undefined)}>
              返回顶层
            </Button>
          )}
        </Space>
      }
    >
      {q.isError ? (
        <QueryError error={q.error} onRetry={() => q.refetch()} />
      ) : q.isPending ? (
        <Spin />
      ) : data && data.nodes.length === 0 ? (
        <Empty description="无节点" />
      ) : (
        <>
          {data?.truncated && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="拓扑已截断"
              description={`${data.truncationReason ?? '节点过多'}（丢弃 ${data.droppedNodes} 个节点）`}
            />
          )}
          <EChart
            option={option}
            height={560}
            onClick={(p: echarts.ECElementEvent) => {
              if (p.dataType === 'node' && (p.data as { id?: string })?.id) {
                setRootId((p.data as { id: string }).id)
              }
            }}
          />
        </>
      )}
    </Card>
  )
}
