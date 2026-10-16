// Which sessions are selected, plus the keyboard cursor and the anchor that
// shift-click and shift-arrow ranges extend from. `order` is always the list
// of session ids as currently displayed.

export class Selection {
  constructor() {
    this.ids = new Set();
    this.anchor = null;
    this.cursor = null;
  }

  get size() { return this.ids.size; }
  has(id) { return this.ids.has(id); }
  list(order) { return order.filter((id) => this.ids.has(id)); }

  // Plain click: select just this one, or clear it if it already was the only one.
  only(id) {
    const wasOnly = this.ids.size === 1 && this.ids.has(id);
    this.ids = wasOnly ? new Set() : new Set([id]);
    this.anchor = this.cursor = id;
  }

  // Cmd/Ctrl-click, the checkbox, and Space.
  toggle(id) {
    this.ids.has(id) ? this.ids.delete(id) : this.ids.add(id);
    this.anchor = this.cursor = id;
  }

  // Shift-click / shift-arrow: select anchor..id. keep=true adds to the current selection.
  extendTo(order, id, keep = false) {
    const to = order.indexOf(id);
    if (to < 0) return;
    let from = order.indexOf(this.anchor);
    if (from < 0) {
      from = to;
      this.anchor = id;
    }
    const range = order.slice(Math.min(from, to), Math.max(from, to) + 1);
    this.ids = new Set(keep ? [...this.ids, ...range] : range);
    this.cursor = id;
  }

  // Arrow keys: the id `delta` rows from the cursor (first/last row if there's no cursor).
  step(order, delta) {
    if (!order.length) return null;
    const at = order.indexOf(this.cursor);
    if (at < 0) return delta > 0 ? order[0] : order[order.length - 1];
    return order[Math.max(0, Math.min(order.length - 1, at + delta))];
  }

  moveCursor(id) { this.anchor = this.cursor = id; }
  selectAll(order) { this.ids = new Set(order); }
  clear() { this.ids = new Set(); }

  // Drop anything no longer displayed, so bulk actions never touch hidden rows.
  prune(order) {
    const shown = new Set(order);
    this.ids = new Set([...this.ids].filter((id) => shown.has(id)));
    if (!shown.has(this.cursor)) this.cursor = null;
    if (!shown.has(this.anchor)) this.anchor = null;
  }
}
