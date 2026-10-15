// Search box syntax. Every term must match (AND), case-insensitive:
//   word            title, first prompt, folder, id or Remote Control name contains it
//   "two words"     exact phrase
//   -word           must NOT match (works with any form below too: -dir:tmp, -"a b")
//   dir:obd         folder contains it (also folder:, in:)
//   title:telem     title or Remote Control name contains it
//   id:c969         session id contains it
//   is:kept         kept running (also on); is:off, is:running, is:open, is:busy,
//                   is:attention, is:elsewhere (open in another claude process)
//   state:backoff   supervisor state is exactly this
//   age:<2d         last active within 2 days; age:>1w older than a week (units m h d w)
// An unknown prefix (say "https:") is searched as plain text.

const TOKEN = /(-)?(?:([a-z]+):)?(?:"([^"]*)"?|(\S+))/gi;
const UNITS = { m: 60, h: 3600, d: 86400, w: 604800 };
const AGE = /^(<=?|>=?)?(\d+(?:\.\d+)?)([mhdw])$/;

const lower = (v) => String(v ?? "").toLowerCase();
const has = (fields, value) => fields.some((f) => lower(f).includes(value));

const IS = {
  kept: (s) => s.enabled,
  on: (s) => s.enabled,
  off: (s) => !s.enabled,
  running: (s) => s.enabled && s.status?.state === "running",
  open: (s) => s.enabled || s.open_elsewhere.length > 0,
  elsewhere: (s) => s.open_elsewhere.length > 0,
  busy: (s) => Boolean(s.status?.busy) || s.open_elsewhere.some((p) => p.status === "busy"),
  attention: (s) => s.status?.state === "attention",
};
export const IS_VALUES = Object.keys(IS);

function agePredicate(value) {
  const m = AGE.exec(value);
  if (!m) return null;
  const [, op = "<", n, unit] = m;
  const limit = Number(n) * UNITS[unit];
  return (s, now) => {
    if (!s.mtime) return false;
    const age = now - s.mtime;
    return op.startsWith("<") ? (op === "<=" ? age <= limit : age < limit) : (op === ">=" ? age >= limit : age > limit);
  };
}

function fieldPredicate(field, value) {
  switch (field) {
    case "dir": case "folder": case "in":
      return (s) => has([s.project, s.cwd], value);
    case "title":
      return (s) => has([s.title, s.rc_name], value);
    case "id":
      return (s) => lower(s.id).includes(value);
    case "is":
      return IS[value] ? (s) => IS[value](s) : { error: `is: takes ${IS_VALUES.join(", ")}` };
    case "state":
      return (s) => s.status?.state === value;
    case "age":
      return agePredicate(value) || { error: "age: looks like <2d, >1w, <30m" };
    default:
      return null;
  }
}

const textPredicate = (value) => (s) => has([s.title, s.first_prompt, s.project, s.cwd, s.id, s.rc_name], value);

// Returns { test(session, nowSeconds) -> bool, errors: [string], empty: bool }.
export function compileQuery(text) {
  const preds = [];
  const errors = [];
  for (const m of String(text).matchAll(TOKEN)) {
    const [, neg, rawField, quoted, bare] = m;
    const value = lower(quoted ?? bare ?? "");
    const field = rawField && lower(rawField);
    // Half-typed terms ("-", "dir:", "-is:") don't filter anything yet.
    if (!value || (!field && bare === "-" && !neg)) continue;
    let pred = field ? fieldPredicate(field, value) : textPredicate(value);
    if (pred === null) pred = textPredicate(`${field}:${value}`);
    if (pred.error) {
      errors.push(pred.error);
      continue;
    }
    preds.push(neg ? (s, now) => !pred(s, now) : pred);
  }
  return {
    test: (s, now) => preds.every((p) => p(s, now)),
    errors,
    empty: preds.length === 0,
  };
}
