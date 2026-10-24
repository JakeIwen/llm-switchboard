// The search syntax help, as data: [term, what it matches].

export type HelpLine = readonly [term: string, meaning: string]

export const SESSION_SEARCH_HELP: readonly HelpLine[] = [
  ['word', 'in the title, first prompt, folder, id or Remote Control name; every word must match'],
  ['"two words"', 'a phrase'],
  ['-word', 'excludes; works before any term below, e.g. -dir:tmp'],
  ['dir:obd', 'folder (also folder: and in:)'],
  ['title:telem · id:c969', 'only the title, or only the id'],
  ['is:kept · is:off', 'keep-running switch on or off'],
  ['is:running · is:attention · is:elsewhere', 'kept sessions in that state; state:backoff for any state name'],
  ['is:open · is:busy', 'open in any claude process; working on a turn now'],
  ['is:claude · is:codex', 'one tool'],
  ['age:<2d · age:>1w', 'active in the last 2 days; older than a week (m h d w)'],
]

export const TRANSCRIPT_SEARCH_HELP: readonly HelpLine[] = [
  ['word word', 'all words in one prompt or reply; words match whole'],
  ['"two words" · -word', 'a phrase; exclude sessions containing a word'],
  ['by:me · by:reply', 'only your prompts, or only the replies'],
  ['tool:claude · dir:obd', 'one tool; part of the folder'],
  ['age:<2d · after:2026-10-01 · before:2026-10-18', 'when the record was written'],
]

export const KEYBOARD_HELP =
  '↑↓ or j k move · Space select · ⇧↑↓ extend · ⌘A all shown · Enter details · ⌫ delete · Esc clear · / search · n new session'
