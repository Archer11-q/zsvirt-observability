/** ECharts 视觉常量 —— 图表颜色的唯一来源。
 *
 * ECharts **不读 antd 的 token**（它是独立渲染的 canvas），所以如果调色板只写在
 * `TopologyPage.tsx` 里，它就会与 `theme.ts` 的强调色各走各的——这正是本项目
 * 之前的状态：按钮是 antd 默认蓝 `#1677ff`，拓扑图却用 ECharts 出厂板的
 * `#5470c6`，两者在同一屏上互不相干。
 *
 * 因此图表相关取值集中在这里，与 `theme.ts` 对齐，页面只消费不定义。
 */

import { CHART_PALETTE, FONT_SANS, SEMANTIC } from '../theme'

/** 资源类型 → 颜色。
 *
 * **按类型固定，不按出现顺序分配。** 早先是 `PALETTE[i % n]`，其中 `i` 来自
 * 响应里类型第一次出现的次序 —— 换一个根节点下钻，GPU 就可能从绿色变成黄色。
 * 对一个用来判断"这是哪一层的问题"的图，**颜色不稳定等于图例失效**。
 */
export const KIND_COLOR: Record<string, string> = {
  host: CHART_PALETTE[0],
  gpu: CHART_PALETTE[1],
  vgpu: CHART_PALETTE[2],
  vm: CHART_PALETTE[3],
  container: CHART_PALETTE[4],
  process: CHART_PALETTE[5],
  ai_service: CHART_PALETTE[6],
  agent: CHART_PALETTE[7],
  task: '#A9784A',
}

/** 未登记类型（如 `unresolved` 占位）的兜底色。 */
export const FALLBACK_KIND_COLOR = SEMANTIC.neutral

export function kindColor(kind: string): string {
  return KIND_COLOR[kind] ?? FALLBACK_KIND_COLOR
}

/** 拓扑图的公共样式。
 *
 * 抽出来是因为 `borderColor` / `borderType` / `lineStyle` 原先是散落在
 * `nodeToEcharts` 与 series 配置里的魔法值（`#ff4d4f`、`#d9d9d9`、`#bfbfbf`），
 * 与主题无关地硬编码了两遍。
 */
export const CHART_STYLE = {
  /** 节点描边 */
  nodeBorder: 'rgba(15,23,32,0.18)',
  nodeBorderError: SEMANTIC.error,
  /** 边 */
  edge: 'rgba(100,116,139,0.45)',
  /** 节点标签 */
  label: {
    show: true,
    position: 'right' as const,
    fontSize: 13,
    fontFamily: FONT_SANS,
    color: '#33414F',
  },
  /** 图例 */
  legend: {
    top: 4,
    itemWidth: 12,
    itemHeight: 12,
    textStyle: { fontSize: 13, fontFamily: FONT_SANS, color: '#4A5A6A' },
  },
  tooltip: {
    backgroundColor: 'rgba(15,23,32,0.92)',
    borderWidth: 0,
    textStyle: { color: '#F1F5F9', fontSize: 13, fontFamily: FONT_SANS },
  },
} as const

/** 迷你曲线（诊断证据里的 sparkline，契约 §4.11）。
 *
 * 尺寸很小，因此刻意去掉坐标轴、网格与图例：它只用来看**形状** ——
 * 阶跃（配置变更）还是缓升（资源耗尽）。数值由它旁边的证据文本给出，
 * 曲线本身不承担读数职责，所以也不需要刻度。
 */
export const SPARKLINE = {
  width: 132,
  height: 32,
  color: CHART_PALETTE[3],
  lineWidth: 1.5,
  grid: { left: 2, right: 2, top: 4, bottom: 4 },
} as const
