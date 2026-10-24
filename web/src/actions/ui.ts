// The seam between action flows (actions/*.ts: what to ask, what to send, what
// to say) and the page that shows dialogs and toasts (pages/picker). Flows get
// an ActionUi and never touch the DOM, so they are tested with a fake one.

export type FieldValues = Record<string, string | boolean>

interface FieldBase {
  key: string
  label: string
  /** A short hint under the label. */
  description?: string
  /** Hide the field (and skip its required check) unless this holds. */
  showWhen?: (values: FieldValues) => boolean
}

export interface TextField extends FieldBase {
  kind: 'text' | 'textarea'
  value?: string
  placeholder?: string
  required?: boolean
  maxLength?: number
  /** Offered as autocomplete options (known folders, say); free text is allowed. */
  suggestions?: readonly string[]
  autofocus?: boolean
  /** A monospace path or command. */
  mono?: boolean
}

export interface SelectField extends FieldBase {
  kind: 'select'
  value?: string
  options: readonly { value: string; label: string; disabled?: boolean }[]
}

export interface CheckboxField extends FieldBase {
  kind: 'checkbox'
  value?: boolean
}

export type FormField = TextField | SelectField | CheckboxField

export interface ConfirmOptions {
  title: string
  /** Paragraphs; a list of session titles goes in `items`. */
  body?: readonly string[]
  items?: readonly string[]
  confirmLabel: string
  danger?: boolean
}

export interface FormOptions extends ConfirmOptions {
  fields: readonly FormField[]
  /** A problem with the current values, shown inline; '' when they are fine. Confirm stays disabled meanwhile. */
  validate?: (values: FieldValues) => string
  /** Shown above the fields, e.g. the error from a failed save being retried. */
  error?: string
}

export interface ToastAction {
  label: string
  run?: () => void
  href?: string
}

export interface ToastOptions {
  message: string
  action?: ToastAction
  /** Default 5 s, or 10 s with an action. */
  durationMs?: number
  tone?: 'info' | 'error'
}

export interface ActionUi {
  /** Resolves true on confirm, false on cancel or Escape. */
  confirm(options: ConfirmOptions): Promise<boolean>
  /** Resolves the values on confirm (hidden fields included), null on cancel. */
  form(options: FormOptions): Promise<FieldValues | null>
  /** An OK-only message, e.g. a bulk run's failures. */
  alert(options: { title: string; body: readonly string[] }): Promise<void>
  /** One toast at a time: a new one replaces the current one. */
  toast(options: ToastOptions): void
  /** Bulk progress for the selection bar ("Deleting 2 of 5…"), or null when done. */
  progress(text: string | null): void
}
