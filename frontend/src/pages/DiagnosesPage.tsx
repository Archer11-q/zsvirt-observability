import { useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  Modal,
  Progress,
  Radio,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { QueryError } from '../components/QueryError'
import { api } from '../api/endpoints'
import { useDict } from '../lib/dict'
import { fmtTime } from '../lib/format'
import type { Diagnosis, TriggerDiagnosisRequest } from '../types'

export default function DiagnosesPage() {
  const dict = useDict()
  const { message } = App.useApp()
  const qc = useQueryClient()
  const [detailId, setDetailId] = useState<string | undefined>()
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<'alert' | 'resource'>('alert')
  const [form] = Form.useForm()

  const q = useInfiniteQuery({
    queryKey: ['diagnoses'],
    queryFn: ({ pageParam }) => api.diagnoses({ cursor: pageParam, limit: 30 }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.meta.page?.nextCursor ?? undefined,
    refetchInterval: 30000,
  })
  const items = q.data?.pages.flatMap((p) => p.data.items) ?? []

  const trigger = useMutation({
    mutationFn: (body: TriggerDiagnosisRequest) => api.triggerDiagnosis(body),
    onSuccess: (res) => {
      message.success(`已触发诊断：${res.data.id}`)
      setOpen(false)
      form.resetFields()
      qc.invalidateQueries({ queryKey: ['diagnoses'] })
    },
    onError: (e) => message.error((e as Error).message),
  })

  const columns: ColumnsType<Diagnosis> = [
    { title: '时间', dataIndex: 'createdAt', width: 150, render: (v) => fmtTime(v) },
    { title: '根因', dataIndex: 'rootCause', render: (v) => <Tag>{dict.rootCause[v] ?? v}</Tag> },
    {
      title: '置信度',
      dataIndex: 'confidence',
      width: 140,
      render: (v) => <Progress percent={Math.round(v * 100)} size="small" style={{ width: 100 }} />,
    },
    { title: '受影响', dataIndex: 'affectedResources', width: 80, render: (v: string[]) => v.length },
    { title: '规则集', dataIndex: 'ruleSetVersion', width: 110 },
    { title: '耗时', dataIndex: 'durationMs', width: 90, render: (v) => (v == null ? '—' : `${v}ms`) },
    {
      title: '操作',
      key: 'op',
      width: 80,
      render: (_, r) => (
        <Button size="small" onClick={() => setDetailId(r.id)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <Card title="诊断" extra={<Button type="primary" onClick={() => setOpen(true)}>触发诊断</Button>}>
      {q.isError ? (
        <QueryError error={q.error} onRetry={() => q.refetch()} />
      ) : (
        <>
          <Table
            rowKey="id"
            columns={columns}
            dataSource={items}
            loading={q.isPending}
            pagination={false}
            size="middle"
          />
          <div style={{ textAlign: 'center', marginTop: 12 }}>
            <Button onClick={() => q.fetchNextPage()} disabled={!q.hasNextPage} loading={q.isFetchingNextPage}>
              加载更多
            </Button>
          </div>
        </>
      )}

      <Modal
        title="触发诊断"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={trigger.isPending}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(vals) => {
            if (mode === 'alert') {
              trigger.mutate({ alertId: vals.alertId })
            } else {
              trigger.mutate({
                anchorResourceId: vals.anchorResourceId,
                window: vals.from && vals.to ? { from: vals.from, to: vals.to } : undefined,
              })
            }
          }}
        >
          <Radio.Group
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            options={[
              { value: 'alert', label: '从告警' },
              { value: 'resource', label: '从资源 + 时间窗' },
            ]}
            style={{ marginBottom: 16 }}
          />
          {mode === 'alert' ? (
            <Form.Item name="alertId" label="告警 ID" rules={[{ required: true, message: '请输入告警 ID' }]}>
              <Input placeholder="alert_…" />
            </Form.Item>
          ) : (
            <>
              <Form.Item
                name="anchorResourceId"
                label="锚点资源 ID"
                rules={[{ required: true, message: '请输入资源 ID' }]}
              >
                <Input placeholder="vm-…" />
              </Form.Item>
              <Space.Compact style={{ width: '100%' }}>
                <Form.Item name="from" label="from（ISO）" style={{ flex: 1 }}>
                  <Input placeholder="2026-09-19T00:00:00Z" />
                </Form.Item>
                <Form.Item name="to" label="to（ISO）" style={{ flex: 1 }}>
                  <Input placeholder="2026-09-19T12:00:00Z" />
                </Form.Item>
              </Space.Compact>
            </>
          )}
        </Form>
      </Modal>

      <DiagnosisDetailDrawer id={detailId} onClose={() => setDetailId(undefined)} />
    </Card>
  )
}

function DiagnosisDetailDrawer({ id, onClose }: { id?: string; onClose: () => void }) {
  const dict = useDict()
  const q = useQuery({
    queryKey: ['diagnosis', id],
    queryFn: () => api.diagnosis(id!),
    enabled: !!id,
    refetchInterval: 30000,
  })
  const d = q.data?.data

  return (
    <Drawer title="诊断详情" width={720} open={!!id} onClose={onClose}>
      {d && (
        <>
          <Space wrap>
            <Tag>{dict.rootCause[d.rootCause] ?? d.rootCause}</Tag>
            <Progress type="circle" size={48} percent={Math.round(d.confidence * 100)} />
            <Typography.Text type="secondary">规则集 {d.ruleSetVersion}</Typography.Text>
            {d.durationMs != null && (
              <Typography.Text type="secondary">耗时 {d.durationMs}ms</Typography.Text>
            )}
          </Space>

          <Typography.Title level={5} style={{ marginTop: 16 }}>
            置信度构成
          </Typography.Title>
          <Table
            rowKey="ruleId"
            size="small"
            pagination={false}
            dataSource={d.confidenceBreakdown}
            columns={[
              { title: '规则', dataIndex: 'ruleId' },
              { title: '贡献', dataIndex: 'contribution', render: (v: number) => v.toFixed(3) },
              { title: '观测', dataIndex: 'observed', ellipsis: true },
            ]}
          />

          <Typography.Title level={5}>受影响资源（确定）</Typography.Title>
          <ResourceList ids={d.affectedResources} />

          <Typography.Title level={5}>可能受影响（视觉区分）</Typography.Title>
          <ResourceList ids={d.potentiallyAffected} />

          <Typography.Title level={5}>传播链</Typography.Title>
          <ResourceList ids={d.onChain} />

          <Typography.Title level={5}>证据</Typography.Title>
          {d.evidence.map((e, i) => (
            <div key={i} style={{ marginBottom: 8 }}>
              <Typography.Text code>{e.type}</Typography.Text> {e.name}
              {e.value != null && <Typography.Text type="secondary"> = {e.value}</Typography.Text>}
              {e.source && (
                <Tag style={{ marginLeft: 8 }} color={e.source === 'simulated' ? 'purple' : 'default'}>
                  {e.source}
                </Tag>
              )}
            </div>
          ))}

          <Typography.Title level={5}>建议</Typography.Title>
          {d.recommendation.map((r) => (
            <div key={r.code} style={{ marginBottom: 6 }}>
              <Tag>{dict.recommendation[r.code] ?? r.code}</Tag> {r.text}
            </div>
          ))}

          {d.notes.length > 0 && (
            <>
              <Typography.Title level={5}>备注</Typography.Title>
              {d.notes.map((n, i) => (
                <Typography.Paragraph key={i} type="secondary">
                  {n}
                </Typography.Paragraph>
              ))}
            </>
          )}
        </>
      )}
    </Drawer>
  )
}

function ResourceList({ ids }: { ids: string[] }) {
  if (!ids.length) return <Typography.Text type="secondary">无</Typography.Text>
  return (
    <Space wrap>
      {ids.map((x) => (
        <Tag key={x}>{x}</Tag>
      ))}
    </Space>
  )
}
