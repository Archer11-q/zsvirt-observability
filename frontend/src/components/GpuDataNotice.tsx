// GPU 数据说明（内容区常驻）。状态条回答「渠道处于什么状态」，
// 这里回答「下面这些 GPU 数字该怎么读」—— 与数字同屏，才不会被误当成按 VM 归因的读数。
//
// 两种情况必须说明（backend/app/api/health.py 的 probe_gpu_provider 分支）：
//   1. available=false：指标读不到，相关列显示缺口说明而不是 0；
//   2. attribution=passthrough：当前环境是 GPU 直通，显存 / 利用率是**卡级**读数，
//      平台侧没有按虚拟机维度的占用，拆不到单台 VM，因此区分不了
//      「自身超配」与「邻居干扰」（D-104）。
//
// **不可关闭**：这是对数字口径的说明，不是可以关掉的提示。

import { Alert } from 'antd'
import { errorText, useGpuProvider } from '../lib/gpu'

export function GpuDataNotice() {
  const gpu = useGpuProvider()
  if (!gpu) return null

  if (gpu.available === false) {
    return (
      <Alert
        banner
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message={`GPU 指标读不到：${errorText(gpu.error)}`}
        description={gpu.note ?? 'GPU 相关列会显示缺口说明，不会用 0 顶替。'}
      />
    )
  }

  if (gpu.attribution === 'passthrough') {
    return (
      <Alert
        banner
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="GPU 直通模式：页面中的 GPU 显存 / 利用率是卡级读数，不是按虚拟机拆分"
        description="平台侧没有按虚拟机维度的显存占用，无法拆分到单台 VM，因此也区分不了「自身超配」与「邻居干扰」（D-104）。请结合访客侧探针数据解读。"
      />
    )
  }

  return null
}
