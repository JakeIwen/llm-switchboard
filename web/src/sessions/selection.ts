// Selection, keyboard cursor and range anchor. Order is always the displayed ids.
export class Selection {
  ids = new Set<string>()
  anchor: string | null = null
  cursor: string | null = null

  get size(): number { return this.ids.size }
  has(id: string): boolean { return this.ids.has(id) }
  list(order: readonly string[]): string[] { return order.filter((id) => this.ids.has(id)) }

  only(id: string): void {
    const wasOnly = this.ids.size === 1 && this.ids.has(id)
    this.ids = wasOnly ? new Set() : new Set([id])
    this.anchor = this.cursor = id
  }

  toggle(id: string): void {
    this.ids.has(id) ? this.ids.delete(id) : this.ids.add(id)
    this.anchor = this.cursor = id
  }

  extendTo(order: readonly string[], id: string, keep = false): void {
    const to = order.indexOf(id)
    if (to < 0) return
    let from = this.anchor === null ? -1 : order.indexOf(this.anchor)
    if (from < 0) {
      from = to
      this.anchor = id
    }
    const range = order.slice(Math.min(from, to), Math.max(from, to) + 1)
    this.ids = new Set(keep ? [...this.ids, ...range] : range)
    this.cursor = id
  }

  /** Return the next id without moving the cursor; arrow handlers decide how to select. */
  step(order: readonly string[], delta: number): string | null {
    if (!order.length) return null
    const at = this.cursor === null ? -1 : order.indexOf(this.cursor)
    if (at < 0) return delta > 0 ? order[0] : order[order.length - 1]
    return order[Math.max(0, Math.min(order.length - 1, at + delta))]
  }

  moveCursor(id: string): void { this.anchor = this.cursor = id }
  selectAll(order: readonly string[]): void { this.ids = new Set(order) }
  clear(): void { this.ids = new Set() }

  /** Hidden rows must not remain bulk-action targets. */
  prune(order: readonly string[]): void {
    const shown = new Set(order)
    this.ids = new Set([...this.ids].filter((id) => shown.has(id)))
    if (this.cursor === null || !shown.has(this.cursor)) this.cursor = null
    if (this.anchor === null || !shown.has(this.anchor)) this.anchor = null
  }
}
