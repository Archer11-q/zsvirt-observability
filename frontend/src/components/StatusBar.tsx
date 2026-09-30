// 顶部健康摘要。GET /api/health，3s 轮询（C 的轮询节奏契约）。
//
// ## 这次改动解决什么
//
// 原先状态条把后端**组件键名**直接摊在顶栏：`database ok`、`zsvirt degraded`、
// `ingest ok`、`gpuProvider ok` —— 五个英文技术标识并排，而它出现在**每一页**
// 的最显眼位置。问题有三：
//
// 1. **它们是给机器看的，不是给人看的。** 值班工程师要的是"哪里不正常"，
//    不是后端 `components` 字典的键名；`ingest` / `gpuProvider` 属于实现细节。
// 2. **视觉嘈杂。** 五个彩色 Tag 常驻顶栏，把注意力从内容区抢走，而绝大多数
//    时候五个都是 ok —— 用最显眼的位置显示"一切正常"是浪费。
// 3. **`ok` 与 `degraded` 同时出现时没有结论。** 用户得自己判断这五个里哪个
//    要紧，而"要紧的那个"恰恰是唯一需要被突出的信息。
//
// 现在：顶栏只显示**一个**结论 + 异常计数；完整明细放进 Popover（保留所有
// 技术细节，需要时一键可查）。**诚实性没有削弱**：异常项的名称、状态与原始
// detail 一条不少，只是不再抢占默认视野；GPU 模拟渠道仍然内联可见（见下）。

import { useQuery } from '@tanstack/react-query'
import { Badge, Popover, Space, Tag, Typography } from 'antd'
import { ApiError } from '../api/client'
import { api } from '../api/endpoints'
import type { ComponentStatus, HealthResponse } from '../types'

/** 组件键名 → 可读名称。**未知键原样显示**，不猜也不隐藏。 */
const COMPONENT_LABEL: Record<string, string> = {
  database: '数据库',
  zsvirt: 'ZSvirt 平台',
  ingest: '探针接入',
  gpuProvider: 'GPU 指标渠道',
}

/** 组件的 detail 里最值得直接说给用户听的一句（没有则不显示）。 */
function explain(c: ComponentStatus): string | null {
  const d = c.detail
  if (!d) return null
  if (d.error === 'ZSVIRT_ENDPOINT_NOT_CONFIGURED') {
    return '测试环境端点未配置：平台侧数据取不到，GPU 相关列会显示缺口说明而不是 0'
  }
  if (d.error === 'ZWATCH_UNREACHABLE_OR_UNAUTHORIZED') {
    return 'ZWatch 不可达或未授权：GPU 卡级指标取不到'
  }
  if (d.error === 'CLOCK_DRIFT_EXCEEDS_THRESHOLD') {
    return '探针时钟漂移超阈值：跨层关联的时间窗会失效'
  }
  const parts = Object.entries(d)
    .filter(([k]) => k !== 'error' && k !== 'message')
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
  return parts.length ? parts.join(' · ') : null
}

export function StatusBar() {
  const q = useQuery({
    queryKey: ['health'],
    queryFn: () => api.health(),
    refetchInterval: 3000,
    retry: 1,
  })

  if (q.isError) {
    const msg = q.error instanceof ApiError ? `${q.error.code} ${q.error.message}` : '网络错误'
    return <Tag color="red">后端不可用：{msg}</Tag>
  }
  if (!q.data) return <Tag>连接中…</Tag>

  const h: HealthResponse = q.data
  const entries = Object.entries(h.components)
  const abnormal = entries.filter(([, c]) => c.status !== 'ok')
  // GPU 模拟渠道必须**内联可见**（诚实性要求）：模拟数据不得伪装成真实采集，
  // 这条不能因为"收起明细"而被藏进 Popover。
  const gpu = h.components.gpuProvider
  const simulated = gpu?.detail?.mode === 'simulated' || gpu?.detail?.simulated === true

  const overallText =
    h.status === 'ok' ? '运行正常' : h.status === 'degraded' ? '降级运行' : '服务不可用'

  const detail = (
    <div style={{ maxWidth: 460 }}>
      {entries.map(([name, c]) => (
        <div key={name} style={{ marginBottom: 10 }}>
          <Space size={8} align="center">
            <Badge status={c.status === 'ok' ? 'success' : c.status === 'down' ? 'error' : 'warning'} />
            <Typography.Text strong>{COMPONENT_LABEL[name] ?? name}</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {c.status}
            </Typography.Text>
          </Space>
          {explain(c) && (
            <div style={{ marginTop: 2, marginLeft: 22 }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {explain(c)}
              </Typography.Text>
            </div>
          )}
        </div>
      ))}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        api {h.version.api} · build {h.version.build}
      </Typography.Text>
    </div>
  )

  return (
    <Space size={10} align="center">
      <Popover content={detail} title="组件状态明细" placement="bottomLeft" trigger="click">
        <Space size={6} align="center" style={{ cursor: 'pointer' }}>
          <Badge status={h.status === 'ok' ? 'success' : h.status === 'down' ? 'error' : 'warning'} />
          <Typography.Text strong style={{ fontSize: 14 }}>
            {overallText}
          </Typography.Text>
          {abnormal.length > 0 && (
            <Tag color="warning" style={{ marginInlineEnd: 0 }}>
              {abnormal.length} 项异常
            </Tag>
          )}
        </Space>
      </Popover>
      {simulated && <Tag color="purple">GPU 数据为模拟</Tag>}
    </Space>
  )
}
