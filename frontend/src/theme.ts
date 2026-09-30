/** Crosslayer 设计令牌 —— 全站唯一的视觉决策点。
 *
 * ## 为什么需要这个文件
 *
 * 在此之前，`ConfigProvider` 只设了 `locale`，**没有任何主题配置**：颜色、字号、
 * 圆角全部是 Ant Design 5 的出厂默认值，另有 38 处内联 `style={{}}` 散落在各页面，
 * 字号甚至硬编码到了 `fontSize: 11`。结果是三套互不协调的颜色系统同时存在于一个
 * 屏幕上：
 *
 * 1. `primary` = antd 默认蓝 `#1677ff`（按钮、进度条、链接、选中态）
 * 2. 状态色 = antd 具名色（`volcano` / `orange` / `red` / `green`）
 * 3. 拓扑图 = ECharts 出厂调色板 `#5470c6 #91cc75 #fac858 …`
 *
 * 三者没有任何共同的设计意图，所以页面"看着单调但又不统一"——**不是配色朴素，
 * 是从来没有做过选择**。这个文件就是那个选择，也是唯一需要改的地方。
 *
 * ## 设计取向（可讨论，但必须是有意的）
 *
 * 这是一个**运维控制台**，不是营销页。受众是 on-call 工程师，深夜长时间盯着
 * 密集数据。因此取向是"数据清晰度优先"，而不是"科技感装饰"：
 *
 * - **基准字号 15px**（antd 默认 14px）。在 1600×1000 的显示器上，14px 的中文
 *   表格正文偏小；控制台的文字是**要读的**，不是要看的。
 * - **数值等宽**。ID、时间戳、耗时、置信度这类数字对齐后可以竖着扫读，
 *   `tabular-nums` 是运维界面的硬需求，不是排版偏好。
 * - **单一强调色**：`#0E7C86`（青蓝）。不用 antd 默认蓝，也不做紫色渐变——
 *   那是 AI 味的默认值。青蓝与红/黄/绿的语义色不会互相干扰。
 * - **红色只留给"需要动作"**：`critical` → `error` → `warning` 是同一条暖色轴，
 *   避免"到处都是红的"导致真正的 critical 被淹没。
 *
 * ## 边界
 *
 * 本文件**只改全局令牌**，不改任何页面结构、路由、组件 API。图表调色板由
 * `lib/chart.ts` 单独定义（ECharts 不读 antd token），状态色映射仍在
 * `lib/tags.tsx`——三者的**取值来源**在本文件顶部对齐，避免再次漂移。
 */

import type { ThemeConfig } from 'antd'
import { theme } from 'antd'

/** 品牌与强调色。改这里 = 改全站，不需要动任何页面。 */
export const BRAND = {
  /** 侧栏底色。深板岩而非纯黑：纯黑 + 彩色标签会显得刺眼。 */
  sidebarBg: '#0F1720',
  /** 侧栏选中项底色 */
  sidebarSelectedBg: '#12414A',
  /** 主强调色。饱和度 78%，低于"AI 味"的高饱和阈值。 */
  accent: '#0E7C86',
  /** 强调色的浅底（选中行、hover） */
  accentSoft: '#E6F4F5',
} as const

/** 数据可视化调色板。
 *
 * **刻意不用 ECharts 出厂板**（`#5470c6` 那套）：它与本文件的强调色冲突，
 * 且是"一看就是默认图表"的视觉标记。这里按**资源层级**给色，从冷到暖、
 * 从上到下：宿主偏冷（基础设施）、Agent 偏暖（业务逻辑），让读者不看图例也能
 * 靠色温猜层级。
 *
 * 顺序与 `RESOURCE_KIND_ORDER` 对应，替换时请同步 `lib/chart.ts`。
 */
export const CHART_PALETTE = [
  '#334E68', // host       深青灰 —— 最底层基础设施
  '#486581', // gpu
  '#627D98', // vgpu
  '#0E7C86', // vm         强调色，聚焦点
  '#2C9A9A', // container
  '#57AE9B', // process
  '#D9A441', // ai_service 暖色，业务层
  '#C97B4A', // agent
] as const

/** 语义色。与 `lib/tags.tsx` 的映射共用同一组取值。 */
export const SEMANTIC = {
  ok: '#2F855A',
  warning: '#C97B1E',
  error: '#C0392B',
  critical: '#8E1B1B',
  neutral: '#64748B',
} as const

/** 字体栈。
 *
 * 中文优先系统的 UI 字体：直接用 `system-ui` 在 Windows 上得到 Segoe UI 配
 * 微软雅黑，在 mac 上得到 SF Pro 配苹方，**中文不会掉进衬线兜底**。
 * 刻意不引 webfont：赛题「稳定性与可复现性」是独立评分项，多一个外链字体
 * 就多一个演示现场加载失败的可能。
 */
export const FONT_SANS =
  'system-ui, -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", ' +
  '"Microsoft YaHei UI", "Microsoft YaHei", "Source Han Sans SC", sans-serif'

/** 等宽字体栈。用于 ID、时间戳、耗时、置信度等需要竖排对齐的数值。 */
export const FONT_MONO =
  'ui-monospace, SFMono-Regular, "JetBrains Mono", "Cascadia Mono", Consolas, ' +
  '"Liberation Mono", Menlo, monospace'

/** 给整站用的 antd 主题。 */
export const antdTheme: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  cssVar: true,
  token: {
    // —— 形状：一级圆角 8px，二级 6px，全站统一（不混用直角与全圆）
    borderRadius: 8,
    borderRadiusLG: 10,
    borderRadiusSM: 6,

    // —— 字体：15px 基准，控制台要"读得清"
    fontFamily: FONT_SANS,
    fontFamilyCode: FONT_MONO,
    fontSize: 15,
    fontSizeSM: 13,
    fontSizeLG: 17,
    fontSizeHeading3: 20,
    fontSizeHeading4: 17,
    lineHeight: 1.6,

    // —— 颜色：单一强调色 + 中性灰阶
    colorPrimary: BRAND.accent,
    colorInfo: BRAND.accent,
    colorSuccess: SEMANTIC.ok,
    colorWarning: SEMANTIC.warning,
    colorError: SEMANTIC.error,
    colorTextBase: '#1A2430',
    colorBgLayout: '#F5F7F8',

    // —— 控件尺寸：控制台里按钮/输入框偏紧凑，但比 size="small" 高一级，
    //    避免"每个控件都得凑近看"
    controlHeight: 34,
    wireframe: false,
  },
  components: {
    Layout: {
      headerBg: '#FFFFFF',
      headerHeight: 56,
      headerPadding: '0 20px',
      bodyBg: '#F5F7F8',
      siderBg: BRAND.sidebarBg,
    },
    Menu: {
      darkItemBg: BRAND.sidebarBg,
      darkItemSelectedBg: BRAND.sidebarSelectedBg,
      darkItemColor: 'rgba(255,255,255,0.72)',
      darkItemSelectedColor: '#FFFFFF',
      itemHeight: 42,
      itemMarginInline: 8,
      itemBorderRadius: 6,
    },
    Card: {
      // 卡片用极浅边框而不是阴影：控制台里满屏卡片阴影会显得廉价且嘈杂
      headerBg: '#FFFFFF',
      paddingLG: 20,
    },
    Table: {
      headerBg: '#F1F4F6',
      headerColor: '#33414F',
      headerSplitColor: 'transparent',
      rowHoverBg: BRAND.accentSoft,
      cellPaddingBlock: 12,
      cellPaddingInline: 14,
      fontSize: 15,
    },
    Descriptions: { fontSize: 15 },
    Drawer: { paddingLG: 24 },
    Tag: { defaultBg: '#EEF2F5' },
    Statistic: { contentFontSize: 26 },
  },
}

/** 数值型文本的通用样式：等宽 + 表格数字。
 *
 * 抽成常量是刻意的——"哪些地方该用等宽"是一个设计决策，
 * 散落成 20 处内联样式就又会变成漂移源。
 */
export const NUMERIC_STYLE = {
  fontFamily: FONT_MONO,
  fontVariantNumeric: 'tabular-nums',
} as const
