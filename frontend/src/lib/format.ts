import dayjs from 'dayjs'

/** ISO 时间 → 本地短格式；无效值显示占位。 */
export function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = dayjs(iso)
  return d.isValid() ? d.format('MM-DD HH:mm:ss') : '—'
}

/** 字节数 → 人类可读（二进制单位）。 */
export function fmtBytes(bytes?: number | null): string {
  if (bytes === null || bytes === undefined) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

/** 0–100 数值 → 百分比文本。 */
export function fmtPercent(pct?: number | null): string {
  if (pct === null || pct === undefined) return '—'
  return `${pct.toFixed(0)}%`
}
