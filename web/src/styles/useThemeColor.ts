import { useEffect } from 'react'
import { useComputedColorScheme } from '@mantine/core'

/** Keep <meta name="theme-color"> (the phone's status bar) on the page's --bg
    as the scheme changes. public/theme.js sets it for the first paint. */
export function useThemeColor(): void {
  const scheme = useComputedColorScheme('light')
  useEffect(() => {
    // Read after Mantine has switched the scheme attribute that --bg follows.
    const frame = requestAnimationFrame(() => {
      const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()
      if (bg) document.querySelector('meta[name="theme-color"]')?.setAttribute('content', bg)
    })
    return () => cancelAnimationFrame(frame)
  }, [scheme])
}
