// Light or dark for this device. Mantine keeps the choice in localStorage,
// where public/theme.js reads it before the first paint.
export type Appearance = 'auto' | 'light' | 'dark'

export const APPEARANCE_LABELS: Record<Appearance, string> = { auto: 'System', light: 'Light', dark: 'Dark' }

export const isAppearance = (value: unknown): value is Appearance =>
  typeof value === 'string' && value in APPEARANCE_LABELS
