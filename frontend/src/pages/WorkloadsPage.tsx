import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Card, Progress, Select, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import { api } from '../api/endpoints'
import { useDict } from '../lib/dict'
import { fmtBytes } from '../lib/format'
import { ObsTag, StatusTag } from '../lib/tags'
import type { Workload } from '../types'

export default function WorkloadsPage() {
  const dict = useDict()
  const [kind, setKind] = useState<string>()

  const q = useQuery({
    queryKey: ['workloads', kind],
    queryFn: () => api.workloads({ kind }),
    refetchInterval: 15000,
  })

  const columns: ColumnsType<Workload> = [
    { title: '名称', dataIndex: 'name', render: (v, r) => v ?? r.id, ellipsis: true },
    { title: '状态', dataIndex: 'status', width: 100, render: (v) => <StatusTag status={v} /> },
    { title: '观测', dataIndex: 'observability', width: 90, render: (v) => <ObsTag observability={v} /> },
    {
      title: 'GPU 显存',
      key: 'mem',
      width: 190,
      render: (_, r) => {
        const used = r.resourceUsage.gpuMemoryUsedBytes
        const total = r.resourceUsage.gpuMemoryTotalBytes
        if (used == null) return r.resourceUsage.note ?? '—'
        const pct = total ? Math.round((used / total) * 100) : 0
        return (
          <Space size={4}>
            <Progress percent={pct} size="small" style={{ width: 70 }} />
            <span>
              {fmtBytes(used)} / {fmtBytes(total)}
            </span>
          </Space>
        )
      },
    },
    {
      title: 'GPU 利用率',
      key: 'util',
      width: 130,
      render: (_, r) => {
        const u = r.resourceUsage.gpuUtilizationPct
        return u == null ? '—' : <Progress percent={Math.round(u)} size="small" style={{ width: 90 }} />
      },
    },
    { title: '事件', dataIndex: 'eventCount', width: 70 },
    {
      title: '告警',
      dataIndex: 'alertCount',
      width: 120,
      render: (v, r) =>
        r.firingAlertCount > 0 ? <Tag color="red">{v}（{r.firingAlertCount} 触发中）</Tag> : v,
    },
    {
      title: '根因',
      dataIndex: 'rootCause',
      render: (v) => (v ? <Tag>{dict.rootCause[v] ?? v}</Tag> : '—'),
    },
    {
      title: '诊断',
      dataIndex: 'diagnosisId',
      render: (v) => (v ? <Link to="/diagnoses">{v.slice(0, 8)}…</Link> : '—'),
    },
  ]

  return (
    <Card
      title="工作负载"
      extra={
        <Space>
          <span>聚合层级</span>
          <Select
            allowClear
            placeholder="默认 ai_service"
            style={{ width: 160 }}
            value={kind}
            onChange={setKind}
            options={Object.entries(dict.resourceKind).map(([k, v]) => ({ value: k, label: v }))}
          />
        </Space>
      }
    >
      {q.isError ? (
        <Alert type="error" showIcon message="加载失败" description={String((q.error as Error).message)} />
      ) : (
        <Table
          rowKey="id"
          columns={columns}
          dataSource={q.data?.data.items ?? []}
          loading={q.isPending}
          pagination={false}
          size="middle"
        />
      )}
    </Card>
  )
}
