import {
  ActionIcon,
  Badge,
  Button,
  Checkbox,
  Chip,
  createTheme,
  Menu,
  Popover,
  Switch,
  TextInput,
  Tooltip,
  type CSSVariablesResolver,
  type MantineColorsTuple,
} from '@mantine/core'

// Colors live in styles/palette.css; the theme only points Mantine at them.
const brand = Array.from({ length: 10 }, (_, i) => `var(--brand-${i})`) as unknown as MantineColorsTuple

// Density is set here once, not per control (docs/design.md): compact sizes
// for every control, a tight spacing scale, 15px body text.
export const theme = createTheme({
  primaryColor: 'brand',
  primaryShade: { light: 6, dark: 6 },
  colors: { brand },
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  fontFamilyMonospace: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
  fontSizes: { xs: '0.75rem', sm: '0.8125rem', md: '0.9375rem', lg: '1.0625rem', xl: '1.25rem' },
  spacing: { xs: '0.25rem', sm: '0.5rem', md: '0.75rem', lg: '1rem', xl: '1.5rem' },
  headings: { sizes: { h1: { fontSize: '1.25rem', lineHeight: '1.3' } } },
  defaultRadius: 'md',
  cursorType: 'pointer',
  components: {
    Button: Button.extend({ defaultProps: { size: 'compact-sm', variant: 'default' } }),
    ActionIcon: ActionIcon.extend({ defaultProps: { size: 'md', variant: 'subtle', color: 'gray' } }),
    TextInput: TextInput.extend({ defaultProps: { size: 'sm' }, classNames: { input: 'app-input' } }),
    Badge: Badge.extend({
      defaultProps: { size: 'sm', radius: 'sm', variant: 'light' },
      styles: { root: { textTransform: 'none', fontWeight: 600 } },
    }),
    Checkbox: Checkbox.extend({ defaultProps: { size: 'sm' } }),
    Switch: Switch.extend({ defaultProps: { size: 'sm' } }),
    Chip: Chip.extend({ defaultProps: { size: 'xs' } }),
    Menu: Menu.extend({ defaultProps: { shadow: 'md', position: 'bottom-start' } }),
    Popover: Popover.extend({ defaultProps: { shadow: 'md' } }),
    Tooltip: Tooltip.extend({
      defaultProps: { multiline: true, maw: 280, withArrow: true, events: { hover: true, focus: true, touch: true } },
    }),
  },
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
  // Quiet "light" buttons: accent text on a faint accent tint, in both modes.
  '--mantine-color-brand-light': 'var(--accent-soft)',
  '--mantine-color-brand-light-hover': 'var(--accent-soft-hover)',
  '--mantine-color-brand-light-color': 'var(--accent)',
  '--mantine-color-brand-text': 'var(--accent)',
}

export const cssVariablesResolver: CSSVariablesResolver = () => ({
  variables: {},
  light: shared,
  dark: shared,
})
