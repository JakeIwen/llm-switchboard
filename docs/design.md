# Page design

The design brief for the page in `web/`. It records what the page is for and
the rules every screen follows, with the reason for each, so a later change
can tell a rule from an accident. Jacob's own words are the source; where he
didn't say, the rule says it is a judgment call.

## What it is for

One place, mostly on a phone, to reach every Claude Code and Codex session on
the Pi and the Mac (about 500 today, a handful kept running), and to keep the
important Pi ones alive with Remote Control on.

The tasks, most frequent first. Layout follows this order: the first one gets
the first screen, the last one may take two taps.

1. **Glance**: are my kept sessions up? Which one needs attention? Kept
   sessions sort first, and a problem shows as a colored state on the row
   without opening anything.
2. **Find and open**: type a few letters of a title or folder, tap the row,
   tap Open in Claude (or copy the resume command). When the title doesn't
   ring a bell, search inside the transcripts from the same box.
3. **Start a session** in a folder, from the header, in one short form.
4. **Tidy**: rename, move, fork, keep or stop keeping, delete with undo,
   restore; one at a time or a selection.

## Layout

- **The list is the page.** No hero, tagline or explanation; the title is a
  plain "LLM Switchboard" at normal heading size. The first screen on a phone
  shows the search box and the first rows.
- **One header line**: title, a short status summary (host, kept count, the
  Mac's online state) and two buttons: **New** and a settings icon. On a
  phone the summary moves under the title in small type rather than wrapping
  the buttons. *Why: the header is chrome; it should cost one or two lines,
  not a screen.*
- **The search box gets its own full-width line**, sticky at the top with
  the filter row, so it is always one tap away while scrolling 500 rows.
  *Why: on a phone the thumb reaches the top of a sticky bar faster than it
  scrolls back up.*
- **One search box, two scopes, both named.** Typing filters the session list
  live (titles, first prompts, folders, ids). A reserved line under the box
  offers "Search inside transcripts for '…'" (Enter does the same); while the
  box is empty that line says so as a hint, so rows never jump while typing.
  Choosing it is a mode switch: the filter row gives way to a scope line
  (Transcripts, how many sessions matched, the index state, Back), and hits
  render as ordinary rows with their match count and excerpt, so a hit can be
  opened or kept directly. *Why: Jacob asked that each search say what it searches and that
  the box get room; a scope toggle beside the box took both away. This is the
  "search suggestions" pattern: the second scope is offered where the eye
  already is.*
- **Filters are one row of fixed-width buttons**: Show (status and tool in
  one menu), Folders, Sort, and Reset at the end. Each shows its current value
  and is accent-tinted when not the default; Reset is always there, dimmed
  when everything is at its default, so clearing never needs a menu open and
  nothing moves. *Why: Jacob asked for a clear-all that works without opening
  the dropdown, and disliked Clear jumping around; controls that move make the
  thumb miss.* Short choices open a menu under the button on every screen;
  only Folders opens a bottom sheet on a phone.
- **Folder picker**: chips that wrap, grouped by machine (this Pi, then the
  Mac), labelled without the `~/` prefix, each with its count, a find box on
  top and Clear / Invert / Done in a footer that stay fixed while the chips
  scroll. On a phone it is a bottom sheet sized to its content. Which folders
  are listed is a setting (`folders.depth`, `min_sessions`, `hidden`). *Why: a one-per-row checklist spent a row of
  height on a few characters of text; chips use the width.*
- **Selection actions in a bottom bar** that appears only while something is
  selected, within thumb reach on a phone.

## Session rows

- A row shows, in this order of weight: the title (up to two lines; the
  expanded row shows it whole), then one meta line (machine and tool tags as
  a pair, the folder tinted by machine, last activity, size) that ends with at
  most two state pills: the state, then memory on kept rows; otherwise
  Working or Open in a terminal. Sentence case, never shouting capitals.
- Rows are flat and divided by hairlines inside one surface, not separate
  cards: denser, and the list reads as one thing.
- **Tapping the row body expands it**; only the checkbox selects. *Why: on a
  phone a tap is the only gesture, and expanding is the common need;
  selection is the rarer, deliberate one.*
- The keep-running switch sits at the right of rows that can be kept (Pi
  Claude sessions). Kept rows get an accent edge so the group reads as one.
- No delete button on the collapsed row: Delete is in the expanded row and in
  the selection bar, always with Undo. *Judgment call: a × on every row is
  clutter and the easiest wrong tap on a phone; deleting is rarer than
  opening.*
- The expanded row puts the actions first, then facts (folder, ids, Remote
  Control name, state detail), with Delete last and set apart. Rename, Move,
  Fork and Remote Control name open small forms (prefilled; folders from an
  autocomplete over known folders) instead of four always-open text fields.
  Move keeps the split button Jacob asked for: a drop-down arrow beside it
  offers Fork.

| Kind | Primary | Other actions |
| --- | --- | --- |
| Pi Claude | Open in Claude (when it has a link), else Copy resume | keep switch, messages, terminal screen and Restart (kept), rename, Remote Control name, move / fork, delete |
| Pi Codex | Copy resume | messages, rename, fork, delete |
| Mac Claude | Copy resume | rename, move / fork on the Mac, fork onto the Pi, delete (queued for the Mac) |
| Mac Codex | Copy resume | rename, fork on the Mac, delete (queued; list-only without its daemon) |

- A queued Mac action shows as a pill on its row (queued, delivered, done,
  failed with the Mac's reason) plus a toast with the result, instead of a box
  above the list.
- Mac rows say why they can't do something (offline, open elsewhere) on the
  disabled control's tooltip, not in a paragraph.

- Blue marks a selection (checkbox, row tint, keyboard cursor); orange marks
  kept-running sessions. The two never share a meaning.
- The old page's selected-folder chips under the toolbar are gone: the
  Folders button names the one picked folder (home-relative, in its
  machine's color) or the count, and Reset clears it in one tap.
- The old footer line explaining how kept sessions run is gone; the keep
  switch's tooltip says it.

## Density and spacing

- Density is set once, in `web/src/theme.ts`: compact control sizes as the
  default for buttons, inputs and badges; no control sets its own padding.
  *Why: Jacob called card-sized button padding "obscene"; one setting keeps
  every control consistent.*
- Touch targets: 44px (Apple's minimum) for the row body, the switch, the
  checkbox and menu items; the hit area grows past the drawn edge without
  making the control look bigger. Inside groups of compact buttons, 8px gaps
  keep a fingertip from landing on a neighbour. *Why: a phone needs room for
  a fingertip, the eye doesn't.*
- Inputs are 16px on touch screens (set once in the theme): iOS zooms into
  any smaller focused field and leaves the page zoomed and scrolling sideways.
- On a phone the Sort button shows a short value (Kept, Recent, A–Z…) and
  tints when it isn't the default; desktop shows the full label. The search
  box keeps its own line at every width.
- Spacing comes from the theme's scale (`xs` 4 to `xl` 24-ish); rows sit
  6-8px apart; the page has a 16px side gutter on a phone and a 860px max
  width on desktop.
- Nothing overflows the viewport at 375px; the page never scrolls sideways.
  Long titles wrap or clamp; paths and commands break or scroll inside their
  own box.

## Color and type

- The old page's palette stays (Jacob: keep the color): warm paper
  background, orange accent, semantic green/blue/amber/red badges, teal for
  this Pi's paths and violet for the Mac's. Light and dark follow the system.
- Buttons are quiet: neutral by default; the one primary action in a place
  (New, Open in Claude) gets a tinted background, never a solid fill. *Why:
  Jacob has asked, in several projects, for buttons "a lot less loud".*
- The kept rows' accent edge is the one strong mark on the list; accent
  otherwise marks state and active filters. Text labels, not an icon on every
  button.
- System font, 15px body on a phone, 13px meta; monospace only for ids,
  commands and terminal output.

## Other views

- **New session**: a form with tool, folder (autocomplete over allowed
  folders), name, and a first message for Codex; a toast when it is ready.
- **Settings**: a sheet with the resume commands, folder listing rules and
  poll interval, saved to the server.
- **Recently deleted**: Restore and Empty Pi trash, at the end of the list.
- **Empty**: "No session matches" names the active filters and offers Reset.
  First load is one quiet line, no skeleton shimmer.
- **Keyboard on desktop**: ↑↓ or j k move, Space selects, Shift extends,
  ⌘A selects all shown, Enter expands, ⌫ deletes, Esc clears, `/` searches,
  `n` starts a session; ⌘- and Shift-click select on pointer devices.

## Live updates

- The list refreshes every `poll_seconds` (4s default), paused while the tab
  is hidden and refreshed on return. It must never steal a tap, a focus or a
  scroll position: rows keep their identity across refreshes (unchanged rows
  are the same objects, so they don't re-render), and an open row, a field
  being typed in, a menu or a dialog stays as it is.
- All rows render (no virtual list), so find-in-page works; an expanded row
  scrolls clear of the sticky bar.
- When the Pi stops answering, the page says so in one line and keeps the
  last list.

## Explanations

- Syntax help, state meanings and why a control is disabled live in info
  icons and tooltips (tap on a phone), not in text across the page.
- Error and refusal messages come from the server and are shown as is, in a
  toast for actions and in place for search.

## Anti-patterns

- Hand-rolled controls where Mantine has one (segmented buttons, menus,
  multiselect, dialogs).
- Rebuilding the list as an HTML string; any layout that needs "don't redraw
  while the pointer is down" guards.
- Per-control padding, margins or font sizes that bypass the theme.
- Controls that appear, vanish or change width when a filter changes.
- Explanatory paragraphs on the page; marketing copy of any kind.
- Splitting one screen's design across several people or agents.
