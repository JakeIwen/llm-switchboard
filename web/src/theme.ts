import { createTheme, type CSSVariablesResolver, type MantineColorsTuple } from '@mantine/core'

// Colors live in styles/palette.css; the theme only points Mantine at them.
const brand = Array.from({ length: 10 }, (_, i) => `var(--brand-${i})`) as unknown as MantineColorsTuple

// Density is set here once, not per control (docs/design.md).
export const theme = createTheme({
  primaryColor: 'brand',
  primaryShade: { light: 6, dark: 6 },
  colors: { brand },
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  fontFamilyMonospace: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
  defaultRadius: 'md',
  cursorType: 'pointer',
})

const shared = {
  '--mantine-color-body': 'var(--bg)',
  '--mantine-color-text': 'var(--text)',
  '--mantine-color-dimmed': 'var(--muted)',
  '--mantine-color-placeholder': 'var(--muted)',
  '--mantine-color-default': 'var(--surface)',
  '--mantine-color-default-hover': 'var(--surface-2)',
  '--mantine-color-default-color': 'var(--text)',
  '--mantine-color-default-border': 'var(--border)',
  '--mantine-color-anchor': 'var(--accent)',
  '--mantine-color-error': 'var(--bad)',
}

export const cssVariablesResolver: CSSVariablesResolver = () => ({
  variables: {},
  light: shared,
  dark: shared,
})
