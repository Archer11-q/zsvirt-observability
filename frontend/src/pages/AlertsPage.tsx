import { useState, type Key } from 'react'
import { useInfiniteQuery, useMutation, useQuery } from '@tanstack/react-query'
import { Alert, App, Button, Card, Drawer, Popconfirm, Select, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import { api } from '../api/endpoints'
import { useDict } from '../lib/dict'
import { fmtTime } from '../lib/format'
import { SeverityTag } from '../lib/tags'
import type { AlertAction, AlertItem, AlertState, Severity } from '../types'

const STATE_COLORS: Record<string, string> = {
  firing: 'red',
  acked: 'blue',
  resolved: 'green',
  silenced: 'orange',
}

const SEVERITY_OPTIONS = ['info', 'warning', 'error', 'critical']

export default function AlertsPage() {
  const dict = useDict()
  const { message } = App.useApp()
  const [state, setState] = useState<string[]>([])
  const [minSeverity, setMinSeverity] = useState<Severity | undefined>()
  const [selected, setSelected] = useState<Key[]>([])
  const [detailId, setDetailId] = useState<string | undefined>()

  const q = useInfiniteQuery({
    queryKey: ['alerts', { state, minSeverity }],
    queryFn: ({ pageParam }) =>
      api.alerts({ state: state as AlertState[], minSeverity, cursor: pageParam, limit: 50 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.meta.page?.nextCursor ?? undefined,
    refetchInterval: 15000,
  })

  const items = q.data?.pages.flatMap((p) => p.data.items) ?? []
  const summary = q.data?.pages[0]?.data

  const act = useMutation({
    mutationFn: (args: { ids: string[]; action: AlertAction }) =>
      api.alertsBulkAction(args.ids, { action: args.action }),
    onSuccess: (res) => {
      message.success(`完成：变更 ${res.data.changed}，未变 ${res.data.unchanged}，失败 ${res.data.failed}`)
      setSelected([])
      q.refetch()
    },
    onError: (e) => message.error((e as Error).message),
  })

  const columns: ColumnsType<AlertItem> = [
    { title: '告警', dataIndex: 'title', render: (v, r) => v ?? r.ruleId, ellipsis: true },
    { title: '严重级', dataIndex: 'severity', width: 100, render: (v) => <SeverityTag severity={v} /> },
    {
      title: '状态',
      dataIndex: 'state',
      width: 90,
      render: (v) => <Tag color={STATE_COLORS[v] ?? 'default'}>{dict.alertState[v] ?? v}</Tag>,
    },
    { title: '资源', dataIndex: 'resourceId', ellipsis: true },
    { title: '次数', dataIndex: 'count', width: 70 },
    { title: '首次', dataIndex: 'firstFiredAt', width: 150, render: (v) => fmtTime(v) },
    { title: '最近', dataIndex: 'lastFiredAt', width: 150, render: (v) => fmtTime(v) },
    {
      title: '诊断',
      dataIndex: 'diagnosisId',
      width: 90,
      render: (v) => (v ? <Link to="/diagnoses">{v.slice(0, 8)}…</Link> : '—'),
    },
    {
      title: '操作',
      key: 'op',
      width: 210,
      render: (_, r) => (
        <Space size={4}>
          {r.state !== 'acked' && (
            <Button size="small" onClick={() => act.mutate({ ids: [r.id], action: 'ack' })}>
              确认
            </Button>
          )}
          {r.state !== 'resolved' && (
            <Button size="small" onClick={() => act.mutate({ ids: [r.id], action: 'resolve' })}>
              解决
            </Button>
          )}
          <Button size="small" onClick={() => setDetailId(r.id)}>
            详情
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <Card title="告警">
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          mode="multiple"
          allowClear
          placeholder="状态"
          style={{ minWidth: 200 }}
          value={state}
          onChange={setState}
          options={Object.entries(dict.alertState).map(([k, v]) => ({ value: k, label: v }))}
        />
        <Select
          allowClear
          placeholder="≥ 严重级"
          style={{ width: 150 }}
          value={minSeverity}
          onChange={setMinSeverity}
          options={SEVERITY_OPTIONS.map((s) => ({ value: s, label: `≥ ${dict.severity[s] ?? s}` }))}
        />
        {summary &&
          Object.entries(summary.stateCounts).map(([s, n]) => (
            <Tag key={s} color={STATE_COLORS[s] ?? 'default'}>
              {dict.alertState[s] ?? s}: {n}
            </Tag>
          ))}
        {selected.length > 0 && (
          <>
            <Popconfirm
              title={`批量确认 ${selected.length} 条？`}
              onConfirm={() => act.mutate({ ids: selected as string[], action: 'ack' })}
            >
              <Button type="primary">批量确认</Button>
            </Popconfirm>
            <Popconfirm
              title={`批量解决 ${selected.length} 条？`}
              onConfirm={() => act.mutate({ ids: selected as string[], action: 'resolve' })}
            >
              <Button danger>批量解决</Button>
            </Popconfirm>
          </>
        )}
      </Space>

      {q.isError ? (
        <Alert type="error" showIcon message="加载失败" description={String((q.error as Error).message)} />
      ) : (
        <>
          <Table
            rowKey="id"
            columns={columns}
            dataSource={items}
            loading={q.isPending}
            pagination={false}
            size="small"
            rowSelection={{ selectedRowKeys: selected, onChange: setSelected }}
          />
          <div style={{ textAlign: 'center', marginTop: 12 }}>
            <Button onClick={() => q.fetchNextPage()} disabled={!q.hasNextPage} loading={q.isFetchingNextPage}>
              加载更多
            </Button>
          </div>
        </>
      )}

      <AlertDetailDrawer id={detailId} onClose={() => setDetailId(undefined)} />
    </Card>
  )
}

function AlertDetailDrawer({ id, onClose }: { id?: string; onClose: () => void }) {
  const dict = useDict()
  const alertQ = useQuery({ queryKey: ['alert', id], queryFn: () => api.alert(id!), enabled: !!id })
  const evidenceQ = useQuery({
    queryKey: ['alert-evidence', id],
    queryFn: () => api.alertEvidence(id!),
    enabled: !!id,
  })

  const a = alertQ.data?.data
  return (
    <Drawer title="告警详情" width={640} open={!!id} onClose={onClose}>
      {a && (
        <>
          <Typography.Title level={5}>{a.title ?? a.ruleId}</Typography.Title>
          <Space wrap>
            <SeverityTag severity={a.severity} />
            <Tag color={STATE_COLORS[a.state] ?? 'default'}>{dict.alertState[a.state] ?? a.state}</Tag>
          </Space>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, marginTop: 12 }}>
            {JSON.stringify(a, null, 2)}
          </pre>

          <Typography.Title level={5} style={{ marginTop: 16 }}>
            证据事件（{evidenceQ.data?.data.evidenceCount ?? a.evidenceEventIds.length}）
          </Typography.Title>
          {evidenceQ.data && (
            <>
              {evidenceQ.data.data.missingEventIds.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message={`缺失 ${evidenceQ.data.data.missingEventIds.length} 个已被清理的事件`}
                />
              )}
              {evidenceQ.data.data.events.map((e) => (
                <div key={e.id} style={{ marginBottom: 8 }}>
                  <Space>
                    <SeverityTag severity={e.severity} />
                    <Typography.Text code>{e.type}</Typography.Text>
                    <Typography.Text type="secondary">{fmtTime(e.occurredAt)}</Typography.Text>
                  </Space>
                  <div>{e.message ?? '—'}</div>
                </div>
              ))}
            </>
          )}
        </>
      )}
    </Drawer>
  )
}
