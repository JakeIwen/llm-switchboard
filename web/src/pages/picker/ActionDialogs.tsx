import { useCallback, useMemo, useRef, useState, type FormEvent } from 'react'
import { Autocomplete, Button, Checkbox, Group, Modal, Select, Text, Textarea, TextInput } from '@mantine/core'
import type {
  ActionUi,
  ConfirmOptions,
  FieldValues,
  FormField,
  FormOptions,
  ToastOptions,
} from '../../actions/ui'
import './ActionDialogs.css'

type Request = { seq: number } & (
  | { kind: 'confirm'; options: ConfirmOptions; resolve: (ok: boolean) => void }
  | { kind: 'form'; options: FormOptions; resolve: (values: FieldValues | null) => void }
  | { kind: 'alert'; options: { title: string; body: readonly string[] }; resolve: () => void }
)

const initialValues = (fields: readonly FormField[]): FieldValues =>
  Object.fromEntries(fields.map((f) => [f.key, f.kind === 'checkbox' ? Boolean(f.value) : (f.value ?? '')]))

const visible = (field: FormField, values: FieldValues) => !field.showWhen || field.showWhen(values)

function problem(options: FormOptions, values: FieldValues): string {
  for (const field of options.fields) {
    if (field.kind !== 'checkbox' && field.kind !== 'select' && field.required && visible(field, values)) {
      if (!String(values[field.key] ?? '').trim()) return ''
    }
  }
  return options.validate?.(values) ?? ''
}

const incomplete = (options: FormOptions, values: FieldValues) =>
  options.fields.some(
    (f) => f.kind !== 'checkbox' && f.kind !== 'select' && f.required && visible(f, values) && !String(values[f.key] ?? '').trim(),
  )

function Field({ field, value, onChange }: { field: FormField; value: string | boolean; onChange: (v: string | boolean) => void }) {
  const common = { label: field.label, description: field.description }
  if (field.kind === 'checkbox') {
    return <Checkbox {...common} checked={Boolean(value)} onChange={(e) => onChange(e.currentTarget.checked)} />
  }
  if (field.kind === 'select') {
    return (
      <Select
        {...common}
        data={field.options.map((o) => ({ value: o.value, label: o.label, disabled: o.disabled }))}
        value={String(value)}
        allowDeselect={false}
        comboboxProps={{ withinPortal: true }}
        onChange={(v) => onChange(v ?? '')}
      />
    )
  }
  const text = {
    ...common,
    value: String(value),
    placeholder: field.placeholder,
    required: field.required,
    maxLength: field.maxLength,
    'data-autofocus': field.autofocus || undefined,
    classNames: field.mono ? { input: 'dialog-mono' } : undefined,
    spellCheck: field.mono ? false : undefined,
  }
  if (field.kind === 'textarea') {
    return <Textarea {...text} autosize minRows={2} maxRows={8} onChange={(e) => onChange(e.currentTarget.value)} />
  }
  if (field.suggestions?.length) {
    return <Autocomplete {...text} data={[...field.suggestions]} limit={12} onChange={(v) => onChange(v)} />
  }
  return <TextInput {...text} onChange={(e) => onChange(e.currentTarget.value)} />
}

function Paragraphs({ body, items }: { body?: readonly string[]; items?: readonly string[] }) {
  return (
    <>
      {body?.map((p) => (
        <Text key={p} size="sm" className="dialog-paragraph">
          {p}
        </Text>
      ))}
      {items && items.length > 0 && (
        <ul className="dialog-items">
          {items.map((item, i) => (
            <li key={i}>{item}</li>
          ))}
        </ul>
      )}
    </>
  )
}

function FormDialog({ request, close }: { request: Extract<Request, { kind: 'form' }>; close: () => void }) {
  const { options } = request
  const [values, setValues] = useState<FieldValues>(() => initialValues(options.fields))
  const message = problem(options, values)
  const disabled = incomplete(options, values) || Boolean(message)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (disabled) return
    request.resolve(values)
    close()
  }
  return (
    <form onSubmit={submit} className="dialog-form">
      <Paragraphs body={options.body} items={options.items} />
      {options.error && <p className="dialog-error">{options.error}</p>}
      {options.fields
        .filter((f) => visible(f, values))
        .map((f) => (
          <Field key={f.key} field={f} value={values[f.key]} onChange={(v) => setValues((old) => ({ ...old, [f.key]: v }))} />
        ))}
      {message && (
        <p className="dialog-error" role="alert">
          {message}
        </p>
      )}
      <Group justify="flex-end" gap="sm" className="dialog-buttons">
        <Button onClick={() => (request.resolve(null), close())}>Cancel</Button>
        <Button type="submit" variant="light" color={options.danger ? 'red' : undefined} disabled={disabled}>
          {options.confirmLabel}
        </Button>
      </Group>
    </form>
  )
}

function cancel(request: Request) {
  if (request.kind === 'confirm') request.resolve(false)
  else if (request.kind === 'form') request.resolve(null)
  else request.resolve()
}

/** The page's ActionUi: dialogs one at a time in a modal, one toast slot, and
    the bulk progress text for the selection bar. */
export function useActionUi() {
  const [queue, setQueue] = useState<Request[]>([])
  const [toast, setToast] = useState<(ToastOptions & { id: number }) | null>(null)
  const [progress, setProgress] = useState<string | null>(null)
  const timer = useRef<number | undefined>(undefined)
  const counter = useRef(0)

  const seq = useRef(0)
  const push = useCallback((request: Request) => setQueue((q) => [...q, request]), [])
  const ui = useMemo<ActionUi>(
    () => ({
      confirm: (options) => new Promise((resolve) => push({ seq: ++seq.current, kind: 'confirm', options, resolve })),
      form: (options) => new Promise((resolve) => push({ seq: ++seq.current, kind: 'form', options, resolve })),
      alert: (options) => new Promise((resolve) => push({ seq: ++seq.current, kind: 'alert', options, resolve })),
      toast: (options) => {
        window.clearTimeout(timer.current)
        const id = ++counter.current
        setToast({ ...options, id })
        const ms = options.durationMs ?? (options.action ? 10000 : 5000)
        timer.current = window.setTimeout(() => setToast((t) => (t?.id === id ? null : t)), ms)
      },
      progress: setProgress,
    }),
    [push],
  )

  const current = queue[0] ?? null
  const close = () => setQueue((q) => q.slice(1))
  const dialogs = (
    <Modal
      opened={current !== null}
      onClose={() => {
        if (current) cancel(current)
        close()
      }}
      title={current?.options.title}
      size="md"
      centered
      classNames={{ title: 'dialog-title' }}
    >
      {current?.kind === 'form' && <FormDialog key={current.seq} request={current} close={close} />}
      {current?.kind === 'confirm' && (
        <form
          className="dialog-form"
          onSubmit={(e) => {
            e.preventDefault()
            current.resolve(true)
            close()
          }}
        >
          <Paragraphs body={current.options.body} items={current.options.items} />
          <Group justify="flex-end" gap="sm" className="dialog-buttons">
            <Button onClick={() => (current.resolve(false), close())}>Cancel</Button>
            <Button type="submit" variant="light" color={current.options.danger ? 'red' : undefined} data-autofocus>
              {current.options.confirmLabel}
            </Button>
          </Group>
        </form>
      )}
      {current?.kind === 'alert' && (
        <div className="dialog-form">
          <Paragraphs body={current.options.body} />
          <Group justify="flex-end" className="dialog-buttons">
            <Button variant="light" data-autofocus onClick={() => (current.resolve(), close())}>
              OK
            </Button>
          </Group>
        </div>
      )}
    </Modal>
  )
  return { ui, dialogs, toast, dismissToast: () => setToast(null), progress, dialogOpen: current !== null }
}
