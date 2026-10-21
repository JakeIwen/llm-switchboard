import { confirmDialog } from "./dialog.js";
import { plural } from "./format.js";

export class BulkActions {
  constructor({ lookup, showProgress, refresh }) {
    this.lookup = lookup;
    this.showProgress = showProgress;
    this.refresh = refresh;
  }

  async run(verb, ids, action) {
    const done = [], failed = [];
    for (const [index, id] of ids.entries()) {
      this.showProgress(ids.length > 1 ? `${verb} ${index + 1} of ${ids.length}…` : `${verb}…`);
      try { await action(id); done.push(id); }
      catch (error) { failed.push(`${this.lookup(id)?.title || id}: ${error.message}`); }
    }
    this.showProgress("");
    await this.refresh();
    if (failed.length) {
      await confirmDialog({ title: `${plural(failed.length, "session")} failed`, body: failed.join("\n"), confirm: "OK" });
    }
    return done;
  }
}

export function splitBlocked(ids, lookup, blockedReason) {
  const sessions = ids.map(lookup).filter(Boolean);
  return {
    targets: sessions.filter((session) => !blockedReason(session)),
    blocked: sessions.filter((session) => blockedReason(session)),
  };
}
