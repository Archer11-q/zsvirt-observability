// 枚举 → 颜色 / 标签的小组件。文案来自字典（无字典时回退到原始枚举值）。

import { Tag } from 'antd'
import { useDict } from './dict'

const SEVERITY_COLORS: Record<string, string> = {
  info: 'default',
  warning: 'orange',
  error: 'red',
  critical: 'volcano',
}

const STATUS_COLORS: Record<string, string> = {
  running: 'green',
  stopped: 'default',
  error: 'red',
  unknown: 'default',
}

const OBS_COLORS: Record<string, string> = {
  active: 'green',
  stale: 'orange',
  gone: 'default',
}

export function SeverityTag({ severity }: { severity: string }) {
  const dict = useDict()
  return <Tag color={SEVERITY_COLORS[severity] ?? 'default'}>{dict.severity[severity] ?? severity}</Tag>
}

export function StatusTag({ status }: { status: string }) {
  const dict = useDict()
  return <Tag color={STATUS_COLORS[status] ?? 'default'}>{dict.resourceStatus[status] ?? status}</Tag>
}

export function ObsTag({ observability }: { observability: string }) {
  const dict = useDict()
  return (
    <Tag color={OBS_COLORS[observability] ?? 'default'}>
      {dict.observability[observability] ?? observability}
    </Tag>
  )
}

export function KindTag({ kind }: { kind: string }) {
  const dict = useDict()
  return <Tag>{dict.resourceKind[kind] ?? kind}</Tag>
}
