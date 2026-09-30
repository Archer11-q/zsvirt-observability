import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Progress, Select, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'
import { QueryError } from '../components/QueryError'
import { api } from '../api/endpoints'
import { useDict } from '../lib/dict'
import { fmtBytes, fmtTime } from '../lib/format'
import { useGpuProvider } from '../lib/gpu'
import { ObsTag, StatusTag } from '../lib/tags'
import type { RootCauseScope, Workload } from '../types'

/** 根因列。
 *
 * `own` / `shared` 必须用**不同视觉**：一张 GPU 卡满时，卡上每个工作负载都受影响，
 * 但**没有一个是"自己配错了"**。不区分的话，界面会把共享基础设施的故障显示成每个
 * 服务各自的故障 —— 运维会去改一个没问题的服务。（实测踩过：健康场景
 * `demo-svc-healthy` 曾显示 `GPU_MEMORY_EXHAUSTED`，与真正的容器 OOM 长得一模一样。）
 *
 * 契约 §4.3：`rootCauseScope === null` 表示**无归属**（结构不相关，或该诊断没有证据），
 * 此时**不显示根因** —— `rootCause` 即便有值也不代表是这个工作负载的问题。
 */
function RootCauseCell({
  rootCause,
  scope,
}: {
  rootCause: string | null
  scope: RootCauseScope | null
}) {
  const dict = useDict()
  if (!rootCause || !scope) return <Typography.Text type="secondary">—</Typography.Text>
  const label = dict.rootCause[rootCause] ?? rootCause

  if (scope === 'shared') {
    // 弱化处理：默认灰标签（与 own 的红标签形成对比），并**写明**是共享基础设施 ——
    // 只靠颜色深浅区分是不够的，用户得知道"该找邻居还是该改自己"。
    return (
      <Space size={4} wrap>
        <Tag>{label}</Tag>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          共享基础设施
        </Typography.Text>
      </Space>
    )
  }
  // own：点名了它自身或它拥有的资源 —— "这是你的故障"，正常强调。
  return <Tag color="red">{label}</Tag>
}

/** 计数单元格。计数为 0 而该工作负载**有根因**时，补一个「窗口内」标记。
 *
 * 计数按 `windowFrom` ~ `windowTo` 统计（默认近 24h），窗口不可见时会出现看起来
 * 自相矛盾的现象：`alertCount: 0` 同时 `rootCause` 非空 —— 因为事件全在窗口之外。
 * 契约 §4.3 要求把口径讲清楚，否则用户只会认为界面坏了。
 */
function CountCell({ value, hasRootCause }: { value: number; hasRootCause?: boolean }) {
  if (value === 0 && hasRootCause) {
    return (
      <Space size={4}>
        <span>0</span>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          窗口内
        </Typography.Text>
      </Space>
    )
  }
  return <>{value}</>
}

export default function WorkloadsPage() {
  const dict = useDict()
  const [kind, setKind] = useState<string>()
  // 直通模式下后端给的是**卡级**读数：列名必须跟着变，否则表头「GPU 显存」
  // 会让人以为这是按这台 VM 拆出来的占用（D-104）。
  const gpu = useGpuProvider()
  const cardScope = gpu?.attribution === 'passthrough' ? '（卡级）' : ''

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
      title: `GPU 显存${cardScope}`,
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
      title: `GPU 利用率${cardScope}`,
      key: 'util',
      width: 130,
      render: (_, r) => {
        const u = r.resourceUsage.gpuUtilizationPct
        return u == null ? '—' : <Progress percent={Math.round(u)} size="small" style={{ width: 90 }} />
      },
    },
    { title: '事件', dataIndex: 'eventCount', width: 70, render: (v) => <CountCell value={v} /> },
    {
      title: '告警',
      dataIndex: 'alertCount',
      width: 130,
      render: (v, r) =>
        r.firingAlertCount > 0 ? (
          <Tag color="red">
            {v}（{r.firingAlertCount} 触发中）
          </Tag>
        ) : (
          <CountCell value={v} hasRootCause={!!r.rootCause} />
        ),
    },
    {
      title: '根因',
      dataIndex: 'rootCause',
      render: (v, r) => <RootCauseCell rootCause={v} scope={r.rootCauseScope} />,
    },
    {
      title: '诊断',
      dataIndex: 'diagnosisId',
      render: (v) => (v ? <Link to="/diagnoses">{v.slice(0, 8)}…</Link> : '—'),
    },
  ]

  const data = q.data?.data

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
        <QueryError error={q.error} onRetry={() => q.refetch()} />
      ) : (
        <>
          {/* 统计窗口**必须可见**（契约 §4.3）：事件 / 告警计数是按这个窗口算的，
              窗口不可见时「0 条告警 + 一个确定根因」看起来就是界面坏了。
              渠道也一并标注 —— GPU 数字旁边要能看出是不是模拟数据。 */}
          {data && (
            <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              统计窗口：{fmtTime(data.windowFrom)} ~ {fmtTime(data.windowTo)}（事件 / 告警计数口径）
              {data.gpuProviderMode === 'simulated' && ' · GPU 渠道：模拟（simulated）'}
            </Typography.Text>
          )}
          <Table
            rowKey="id"
            columns={columns}
            dataSource={data?.items ?? []}
            loading={q.isPending}
            pagination={false}
            size="middle"
          />
        </>
      )}
    </Card>
  )
}
