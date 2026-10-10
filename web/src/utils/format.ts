/**
 * 显示层的两个纯函数。放在一处是因为**同一个数在两个组件里格式化不一样**很难看,
 * 而且这类函数最容易各自漂。
 */

/** 输入:字节数。行为:换成人类可读。输出:字符串(如 `1.2 MB`)。 */
export function humanBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB']
  let size = value / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`
}

/** 输入:后端给的时间(`YYYY-MM-DDThh:mm:ss`,本机时间、不带时区)。行为:压成一行。输出:字符串。 */
export function humanTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
