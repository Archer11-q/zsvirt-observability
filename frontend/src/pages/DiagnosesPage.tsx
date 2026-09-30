import { useEffect, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
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
import { EChart } from '../components/EChart'
import { api } from '../api/endpoints'
import { SPARKLINE } from '../lib/chart'
import { useDict } from '../lib/dict'
import { fmtTime } from '../lib/format'
import type { Diagnosis, DiagnosisEvidence, TriggerDiagnosisRequest } from '../types'

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
  const { message } = App.useApp()
  // 工单文本。`actionable === false` 时**不直接复制**（见下），而是展开让人先读到。
  const [ticketDoc, setTicketDoc] = useState<{ text: string; actionable: boolean }>()

  const q = useQuery({
    queryKey: ['diagnosis', id],
    queryFn: () => api.diagnosis(id!),
    enabled: !!id,
    refetchInterval: 30000,
  })
  const d = q.data?.data

  // 换一条诊断（或关闭）时收起上一个的工单文本 —— 抽屉组件本身常驻挂载，
  // 不清的话点开下一条会看到**上一条**的工单，而这种错配没有任何提示。
  useEffect(() => setTicketDoc(undefined), [id])

  /** 复制为工单。**只读端点**：不写库、不写回 ZSvirt、不执行任何处置动作 ——
   *  因此这里刻意只有一个"复制"，没有"一键修复"。 */
  const makeTicket = useMutation({
    mutationFn: () => api.diagnosisTicket(id!),
    onSuccess: async (res) => {
      const t = res.data
      if (!t.actionable) {
        // 结论不可据此行动（UNKNOWN 或置信度 < 0.5）。契约 §4.12 要求走不同样式：
        // 一键把"待确认"粘进工单系统，正是这个功能要防的事。
        setTicketDoc(t)
        return
      }
      try {
        await navigator.clipboard.writeText(t.text)
        message.success('工单文本已复制')
      } catch {
        // 非安全上下文（如 file:// 的单文件演示版）下剪贴板不可用 ——
        // 退回到"展开让人自己复制"，而不是静默失败。
        setTicketDoc(t)
        message.warning('浏览器拒绝了剪贴板访问，工单文本已展开，请手动复制')
      }
    },
    onError: (e) => message.error((e as Error).message),
  })

  return (
    <Drawer
      title="诊断详情"
      width={720}
      open={!!id}
      onClose={onClose}
      extra={
        <Button
          size="small"
          disabled={!d}
          loading={makeTicket.isPending}
          onClick={() => makeTicket.mutate()}
        >
          复制为工单
        </Button>
      }
    >
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
            <EvidenceRow key={i} evidence={e} />
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

      {/* 工单文本。`actionable === false` 时用 warning 样式**先讲清楚为什么**再让人复制 ——
          "待确认"的结论看起来和确定结论一样，是这个功能最容易犯的错。 */}
      <Modal
        title="诊断工单文本"
        width={720}
        open={!!ticketDoc}
        onCancel={() => setTicketDoc(undefined)}
        footer={
          <Space>
            <Button onClick={() => setTicketDoc(undefined)}>关闭</Button>
            <Button
              type={ticketDoc?.actionable ? 'primary' : 'default'}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(ticketDoc?.text ?? '')
                  message.success('工单文本已复制')
                  setTicketDoc(undefined)
                } catch {
                  message.warning('剪贴板不可用，请手动选中复制')
                }
              }}
            >
              仍然复制
            </Button>
          </Space>
        }
      >
        {!ticketDoc?.actionable && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="该结论不可据此行动"
            description="置信度不足或根因未能识别。工单正文已写明原因与需要补充的信息 —— 请先确认，再决定是否归档。"
          />
        )}
        <Input.TextArea
          value={ticketDoc?.text ?? ''}
          readOnly
          autoSize={{ minRows: 12, maxRows: 22 }}
        />
      </Modal>
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

/** 一条证据 +（若是数值型且有资源）它的迷你曲线。
 *
 * 告警文案天然是时间序列语义 ——「延迟**飙升至** 800ms」「显存**持续**上升」——
 * 而在此之前工具里只有单点快照，回答不了"这是阶跃（配置变更）还是缓升（资源耗尽）"。
 * 这条曲线是契约 §4.11 的用途所在。
 */
function EvidenceRow({ evidence }: { evidence: DiagnosisEvidence }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <Typography.Text code>{evidence.type}</Typography.Text> {evidence.name}
      {evidence.value != null && (
        <Typography.Text type="secondary"> = {evidence.value}</Typography.Text>
      )}
      {evidence.source && (
        <Tag
          style={{ marginLeft: 8 }}
          color={evidence.source === 'simulated' ? 'purple' : 'default'}
        >
          {evidence.source}
        </Tag>
      )}
      {evidence.resourceId && (
        <EvidenceSparkline resourceId={evidence.resourceId} name={evidence.name} />
      )}
    </div>
  )
}

/** 数值型证据的迷你曲线。
 *
 * 几处刻意的克制，都是为了不把"没有数据"画成"数据是平的"：
 *
 * - **取不到序列就什么都不画**（而不是画一条空曲线或直线）。大多数证据本来就不是
 *   指标（`inference.timeout` 是事件类型），没有曲线是正常结果，不是加载失败；
 * - `unit` 恒为 `null`（契约 §4.11），因此只有裸数值，**不补单位** ——
 *   从名称猜单位在 `io_wait_pct` 上碰巧对、在 `value` 上就是编造；
 * - 被裁剪时在曲线旁标出来。一条"看起来完整其实被截断"的曲线会让人误判趋势平稳。
 */
function EvidenceSparkline({ resourceId, name }: { resourceId: string; name: string }) {
  const q = useQuery({
    queryKey: ['metrics', resourceId, name],
    queryFn: () => api.metrics({ resourceId: [resourceId], name: [name] }),
    staleTime: 60000,
  })

  const series = q.data?.data.series.find((s) => s.name === name)
  if (!series || series.points.length === 0) return null

  const values = series.points.map((p) => p.value)
  const option = {
    grid: SPARKLINE.grid,
    // 曲线只用来看形状，数值与时间由旁证文本给出，因此不需要刻度和轴线。
    xAxis: { type: 'category' as const, show: false, data: series.points.map((p) => p.at) },
    yAxis: { type: 'value' as const, show: false, scale: true },
    tooltip: {
      trigger: 'axis' as const,
      formatter: (params: any) => {
        const p = Array.isArray(params) ? params[0] : params
        // 裸数值 + 时间：unit 契约上恒为 null，不猜单位
        return p ? `${fmtTime(p.name)}<br/>${p.value}` : ''
      },
    },
    series: [
      {
        type: 'line' as const,
        data: values,
        showSymbol: false,
        lineStyle: { width: SPARKLINE.lineWidth },
        itemStyle: { color: SPARKLINE.color },
      },
    ],
  }

  return (
    <Space size={4} style={{ marginLeft: 8, verticalAlign: 'middle' }}>
      <div style={{ width: SPARKLINE.width, height: SPARKLINE.height }}>
        <EChart option={option} height={SPARKLINE.height} />
      </div>
      <Typography.Text type="secondary" style={{ fontSize: 11 }}>
        {values.length} 点{series.truncated ? ' · 已截断' : ''}
      </Typography.Text>
    </Space>
  )
}
