// Text formatting shared across the page.

export const nowS = (): number => Date.now() / 1000

export const plural = (n: number, word: string, many = `${word}s`): string => `${n} ${n === 1 ? word : many}`

/** "just now", "5m ago", "3h ago", "2d ago" for a Unix time in seconds; "" when unknown. */
export function age(t: number | null | undefined, now: number = nowS()): string {
  if (!t) return ''
  const s = now - t
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

/** Bytes as "812 KB" or "19.1 MB"; "" when unknown. */
export const size = (b: number | null | undefined): string =>
  b == null ? '' : b < 1048576 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1048576).toFixed(1)} MB`
