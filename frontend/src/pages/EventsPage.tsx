import { useState } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Drawer, Input, Select, Space, Table, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { api } from '../api/endpoints'
import { useDict } from '../lib/dict'
import { fmtTime } from '../lib/format'
import { SeverityTag } from '../lib/tags'
import type { EventItem, Severity } from '../types'

const SEVERITY_OPTIONS = ['info', 'warning', 'error', 'critical']

export default function EventsPage() {
  const dict = useDict()
  const [minSeverity, setMinSeverity] = useState<Severity | undefined>()
  const [type, setType] = useState<string | undefined>()
  const [resourceId, setResourceId] = useState<string | undefined>()
  const [detail, setDetail] = useState<EventItem | undefined>()

  const q = useInfiniteQuery({
    queryKey: ['events', { minSeverity, type, resourceId }],
    queryFn: ({ pageParam }) =>
      api.events({
        minSeverity,
        type: type ? [type] : undefined,
        resourceId: resourceId ? [resourceId] : undefined,
        cursor: pageParam,
        limit: 50,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.meta.page?.nextCursor ?? undefined,
    refetchInterval: 15000,
  })

  const items = q.data?.pages.flatMap((p) => p.data.items) ?? []
  const newest = q.data?.pages[0]?.data.newestOccurredAt
  const oldest = q.data?.pages[q.data.pages.length - 1]?.data.oldestOccurredAt

  const columns: ColumnsType<EventItem> = [
    { title: '时间', dataIndex: 'occurredAt', width: 150, render: (v) => fmtTime(v) },
    { title: '资源', dataIndex: 'resourceId', ellipsis: true },
    { title: '类型', dataIndex: 'type', width: 140, render: (v) => dict.eventType[v] ?? v },
    { title: '严重级', dataIndex: 'severity', width: 100, render: (v) => <SeverityTag severity={v} /> },
    { title: '消息', dataIndex: 'message', ellipsis: true },
    { title: 'traceId', dataIndex: 'traceId', width: 120, render: (v) => v ?? '—', ellipsis: true },
  ]

  return (
    <Card title="事件">
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          allowClear
          placeholder="≥ 严重级"
          style={{ width: 150 }}
          value={minSeverity}
          onChange={setMinSeverity}
          options={SEVERITY_OPTIONS.map((s) => ({ value: s, label: `≥ ${dict.severity[s] ?? s}` }))}
        />
        <Select
          allowClear
          placeholder="类型"
          style={{ width: 160 }}
          value={type}
          onChange={setType}
          options={Object.entries(dict.eventType).map(([k, v]) => ({ value: k, label: v }))}
        />
        <Input.Search
          allowClear
          placeholder="资源 ID"
          style={{ width: 240 }}
          onSearch={(v) => setResourceId(v || undefined)}
        />
        {newest && (
          <Typography.Text type="secondary">
            覆盖 {fmtTime(oldest)} ~ {fmtTime(newest)}
          </Typography.Text>
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
            onRow={(r) => ({ onClick: () => setDetail(r), style: { cursor: 'pointer' } })}
          />
          <div style={{ textAlign: 'center', marginTop: 12 }}>
            <Button onClick={() => q.fetchNextPage()} disabled={!q.hasNextPage} loading={q.isFetchingNextPage}>
              加载更多
            </Button>
          </div>
        </>
      )}

      <Drawer title="事件详情" width={560} open={!!detail} onClose={() => setDetail(undefined)}>
        {detail && <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{JSON.stringify(detail, null, 2)}</pre>}
      </Drawer>
    </Card>
  )
}
