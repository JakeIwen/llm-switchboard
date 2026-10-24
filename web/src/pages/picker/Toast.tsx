import { Button, CloseButton } from '@mantine/core'
import type { ToastOptions } from '../../actions/ui'
import './Toast.css'

interface Props {
  toast: ToastOptions | null
  onDismiss: () => void
}

/** The one toast slot, bottom centre, above the selection bar when it shows. */
export function Toast({ toast, onDismiss }: Props) {
  if (!toast) return null
  const action = toast.action
  return (
    <div className={toast.tone === 'error' ? 'toast is-error' : 'toast'} role="status" aria-live="polite">
      <span className="toast-message">{toast.message}</span>
      {action &&
        (action.href ? (
          <Button component="a" href={action.href} target="_blank" rel="noopener" variant="subtle" onClick={onDismiss}>
            {action.label}
          </Button>
        ) : (
          <Button
            variant="subtle"
            onClick={() => {
              onDismiss()
              action.run?.()
            }}
          >
            {action.label}
          </Button>
        ))}
      <CloseButton size="sm" aria-label="Dismiss" onClick={onDismiss} />
    </div>
  )
}
