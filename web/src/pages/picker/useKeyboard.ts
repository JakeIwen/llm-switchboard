import { useEffect, useRef } from 'react'

export interface KeyHandlers {
  /** False while a dialog is open or the page isn't showing the session list. */
  active: boolean
  /** In Transcripts mode Escape goes back to the session list, wherever focus is. */
  leaveTranscripts: (() => void) | null
  move: (delta: number, extend: boolean) => void
  toggleSelect: () => void
  toggleExpand: () => void
  selectAll: () => void
  clear: () => void
  remove: () => void
  focusSearch: () => void
  newSession: () => void
}

const typing = (target: EventTarget | null) =>
  target instanceof HTMLElement &&
  (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))

// Focus inside an open menu, popover or sheet belongs to it: Backspace or `n`
// there must not reach a session.
const inOverlay = (target: EventTarget | null) =>
  target instanceof HTMLElement && Boolean(target.closest('[role="menu"], [role="dialog"], [role="listbox"], .mantine-Popover-dropdown'))

// A focused control handles its own Space and Enter.
const ownsKeys = (target: EventTarget | null) =>
  target instanceof HTMLElement && Boolean(target.closest('button, a, summary, [role="menuitem"]'))

/** The desktop shortcuts from the old page (docs/design.md, Other views). */
export function useKeyboard(handlers: KeyHandlers) {
  const ref = useRef(handlers)
  ref.current = handlers
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const h = ref.current
      if (e.key === 'Escape' && h.leaveTranscripts && !inOverlay(e.target)) return h.leaveTranscripts()
      if (!h.active || e.altKey || typing(e.target) || inOverlay(e.target)) return
      const mod = e.metaKey || e.ctrlKey
      const key = e.key
      if (key === '/' && !mod) return void (e.preventDefault(), h.focusSearch())
      if (key === 'n' && !mod) return void (e.preventDefault(), h.newSession())
      if ((key === 'ArrowDown' || key === 'j' || key === 'J') && !mod) return void (e.preventDefault(), h.move(1, e.shiftKey))
      if ((key === 'ArrowUp' || key === 'k' || key === 'K') && !mod) return void (e.preventDefault(), h.move(-1, e.shiftKey))
      if (key === 'a' && mod) return void (e.preventDefault(), h.selectAll())
      if (key === 'Escape') return void h.clear()
      if ((key === 'Delete' || key === 'Backspace') && !mod) return void (e.preventDefault(), h.remove())
      if (ownsKeys(e.target)) return
      if (key === ' ') return void (e.preventDefault(), h.toggleSelect())
      if (key === 'Enter') return void (e.preventDefault(), h.toggleExpand())
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
