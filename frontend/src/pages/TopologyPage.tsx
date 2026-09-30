import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Select, Space, Spin, Switch, Typography } from 'antd'
import * as echarts from 'echarts'
import { api } from '../api/endpoints'
import { EChart } from '../components/EChart'
import { QueryError } from '../components/QueryError'
import { CHART_STYLE, kindColor } from '../lib/chart'
import { useDict } from '../lib/dict'
import { fmtTime } from '../lib/format'
import type { TopologyNode } from '../types'

/** 一个节点同时承载三个独立信息，用三种不同的视觉通道表达，互不抢占：
 *
 * | 信息 | 通道 | 取值 |
 * |---|---|---|
 * | 资源类型 | 填充色 | `kindColor(kind)`，**按类型固定** |
 * | 数据新鲜度 | 描边线型 | `isStale` ⇒ 虚线（"数据不新了"） |
 * | 健康状态 | 描边粗细+颜色 | `status==='error'` ⇒ 加粗红边（"需要动作"） |
 *
 * 两条历史教训都在这段映射里：
 *
 * 1. 早先把 `status==='error'` 与"陈旧"都编码成**描边颜色**，于是两种状态并存时
 *    只能显示一个 —— 一个"已离线且报错"的节点看起来和普通离线节点一样。
 * 2. 后来把"陈旧"接到了 `observability==='stale'` 上，而陈旧标记器默认关闭
 *    （`RESOURCE_STALE_AFTER_SEC=0`，因为演示数据时间基准固定在 2026-09-17）
 *    ⇒ `observability` 恒为 `active` ⇒ **这条虚线永远不会出现**。
 *    现在改用 `isStale`（按**年龄**判定，与 `observability` 无关，契约 §4.2.1）。
 *    `observability` 降为 tooltip 里的排障信息。
 */
function nodeToEcharts(n: TopologyNode, kindIdx: Record<string, number>): any {
  return {
    id: n.id,
    name: n.name ?? n.id,
    category: kindIdx[n.kind] ?? 0,
    symbolSize: n.kind === 'host' || n.kind === 'gpu' ? 46 : 34,
    rawKind: n.kind,
    rawStatus: n.status,
    rawObservability: n.observability,
    rawIsStale: n.isStale,
    rawLastSeen: n.lastSeenAt,
    itemStyle: {
      color: kindColor(n.kind),
      borderColor: n.status === 'error' ? CHART_STYLE.nodeBorderError : CHART_STYLE.nodeBorder,
      borderWidth: n.status === 'error' ? 3 : 1,
      borderType: n.isStale ? 'dashed' : 'solid',
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
        ...CHART_STYLE.tooltip,
        formatter: (params: any) => {
          if (params?.dataType !== 'node' || !params?.data) return ''
          const d = params.data as Record<string, any>
          return [
            `<strong>${d.name}</strong>`,
            `类型: ${dict.resourceKind[d.rawKind] ?? d.rawKind}`,
            `状态: ${dict.resourceStatus?.[d.rawStatus] ?? d.rawStatus}`,
            // 「数据是否陈旧」用 isStale，不去解析 staleness 字符串（契约 §4.2.1）。
            // observability 是 B 的生命周期判决，只作排障参考，不与前者混用。
            `数据${d.rawIsStale ? '陈旧（超过阈值未收到新观测）' : '新鲜'}`,
            `观测: ${dict.observability?.[d.rawObservability] ?? d.rawObservability}`,
            `最后心跳: ${fmtTime(d.rawLastSeen)}`,
          ].join('<br/>')
        },
      },
      legend: kinds.length
        ? { ...CHART_STYLE.legend, data: kinds.map((k) => ({ name: dict.resourceKind[k] ?? k })) }
        : undefined,
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          data: nodes.map((n) => nodeToEcharts(n, kindIdx)),
          links: edges.map((e) => ({ source: e.parentId, target: e.childId })),
          // 图例分类只用来分组与着色，颜色来自 kindColor（按类型固定）。
          // 这里**不再按 kinds 的下标取色** —— 那样换根节点下钻就会变色。
          categories: kinds.map((k) => ({
            name: dict.resourceKind[k] ?? k,
            itemStyle: { color: kindColor(k) },
          })),
          label: CHART_STYLE.label,
          force: { repulsion: 320, edgeLength: 130 },
          lineStyle: { color: CHART_STYLE.edge, width: 1.2, curveness: 0.06 },
          // 节点层级：数据量大时让大节点（宿主/GPU）压在小节点之上，避免被盖住
          emphasis: { focus: 'adjacency', scale: 1.15 },
        },
      ],
    } as echarts.EChartsOption
  }, [q.data, dict])

  const data = q.data?.data
  const staleCount = data?.nodes.filter((n) => n.isStale).length ?? 0

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
          {/* 虚线本身是"沉默的"信号：不解释的话，看到的人只会觉得图有毛病。
              只在真有陈旧节点时才出现，健康的拓扑上不增加噪声。 */}
          {staleCount > 0 && (
            <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              虚线描边 = 数据陈旧（{staleCount} 个节点超过后端阈值未收到新观测）
            </Typography.Text>
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
