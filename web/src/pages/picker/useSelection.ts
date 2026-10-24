import { useCallback, useMemo, useRef, useState } from 'react'
import { Selection } from '../../sessions/selection'

export interface SelectEvent {
  shiftKey: boolean
  metaKey: boolean
  ctrlKey: boolean
}

/** React state over the ported Selection model: `order` is the displayed ids,
    and anything no longer shown drops out of the selection and cursor. */
export function useSelection(order: readonly string[]) {
  const model = useRef(new Selection())
  const [version, setVersion] = useState(0)
  const bump = useCallback(() => setVersion((v) => v + 1), [])
  const orderRef = useRef(order)
  orderRef.current = order

  // Prune while rendering (the model is mutable), so a filter change never
  // leaves hidden rows selected for a bulk action.
  // The key changes only when pruning removed something, so the returned api
  // (and every row's props) stays the same object across ordinary polls.
  const pruned = useMemo(() => {
    model.current.prune(order)
    return `${model.current.size}|${model.current.cursor}|${model.current.anchor}`
  }, [order])

  /** The checkbox: toggles one row; Shift picks a range, ⌘/Ctrl+Shift adds one. */
  const onSelect = useCallback(
    (id: string, e: SelectEvent) => {
      const additive = e.metaKey || e.ctrlKey
      if (e.shiftKey) model.current.extendTo(orderRef.current, id, additive)
      else model.current.toggle(id)
      bump()
    },
    [bump],
  )

  const api = useMemo(
    () => ({
      ids: new Set(model.current.ids) as ReadonlySet<string>,
      cursor: model.current.cursor,
      count: model.current.size,
      list: () => model.current.list(orderRef.current),
      onSelect,
      /** ⌘/Ctrl- or Shift-click on a row body selects instead of expanding. */
      modifierClick: (id: string, e: SelectEvent) => {
        if (e.shiftKey) model.current.extendTo(orderRef.current, id, e.metaKey || e.ctrlKey)
        else model.current.toggle(id)
        bump()
      },
      moveCursor: (id: string) => {
        model.current.moveCursor(id)
        bump()
      },
      step: (delta: number, extend: boolean) => {
        const next = model.current.step(orderRef.current, delta)
        if (next === null) return null
        if (extend) model.current.extendTo(orderRef.current, next)
        else model.current.moveCursor(next)
        bump()
        return next
      },
      toggleCursor: () => {
        if (model.current.cursor === null) return
        model.current.toggle(model.current.cursor)
        bump()
      },
      selectAll: () => {
        model.current.selectAll(orderRef.current)
        bump()
      },
      clear: () => {
        model.current.clear()
        bump()
      },
      drop: (ids: Iterable<string>) => {
        for (const id of ids) model.current.ids.delete(id)
        bump()
      },
    }),
    [version, pruned, onSelect, bump],
  )
  return api
}
