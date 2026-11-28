const pad = (value: number) => String(value).padStart(2, '0')

export function shortRef(value: string | null | undefined, length = 8): string {
  return value ? `#${value.slice(0, length)}` : '未提供'
}

export function formatStamp(value: string | null | undefined, fallback = '未提供'): string {
  if (!value) return fallback
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}