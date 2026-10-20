# llm-switchboard

One web page on the Pi for every Claude Code and Codex session on it: search,
sort, filter by folder, rename, fork, move, delete with undo, and start new
ones, one at a time or in bulk. It also keeps chosen Claude Code sessions
running in tmux with Remote Control on, so they can always be reached from
the Claude app or claude.ai/code.

Illustrated guide: http://vanpi.lan:8791/guides/llm-switchboard.html (source in
`JakeIwen/visual-guides`; keep it current with any change in behaviour, see AGENTS.md).

## How it works

- A user systemd service (`llm-switchboard`, started at boot through
  linger) runs a supervisor and the picker web app on port 8777.
- Each enabled session gets a window in a dedicated tmux server
  (`tmux -L llm-switchboard`). The window runs
  `bash -lic 'cd <cwd> && claudepd --resume <id> --remote-control <name>'`,
  so it uses the same `claudepd` function as an interactive shell (bypass
  permissions, no Model Gateway, which is what allows Remote Control).
- If claude exits, the supervisor relaunches it, backing off 10s, 20s, 40s…
  up to 10 minutes after quick failures. A run of 5+ minutes resets that.
- It won't launch a session that is already open in another claude process
  (say, an SSH terminal); it waits until that process exits.
- It waits for `api.anthropic.com:443` to be reachable before launching.
- The tmux server lives in its own systemd scope, so restarting or stopping
  the service leaves running sessions alone; the restarted supervisor adopts
  them.
- Enabled sessions are stored in `~/.config/llm-switchboard/sessions.json`.

## Picker

Toggle a session to keep it running. Folder chips filter the list. Expand a
session (▾) for:

- **Open in Claude**: its claude.ai/code link (from its last Remote Control connection)
- **Recent messages**: the last few prompts and replies
- **Terminal screen** (× closes it) and **Restart** (kept-running sessions)
- **Title / Rename**: types `/rename` into a running kept session (when idle),
  or appends Claude Code's own `custom-title` record to a closed one
- **Working folder / Move**: moves the transcript under another folder's
  project directory and points its `cwd` fields there, so `claudepd --resume`
  works from the new folder; a kept session restarts there
- **Move ▾ → Fork…**: copies the conversation so far into a new session (new
  id, a name you choose, in that folder or another), optionally kept running;
  the original is only read, so it can be running. The copy drops the
  original's Remote Control link and any half-written last line.
- Copyable resume and attach commands

The **×** on each session deletes it: a kept session is stopped first, and
the transcript (with its companion folder) moves to
`~/.local/share/llm-switchboard/trash/`, with Undo and a "Recently
deleted" list (Restore, or Empty now); entries expire after 30 days.

Rename, move and delete are refused while the session is open in another
claude process (like an SSH terminal), the same rule as `clauderm`/`claudeclean`.

No two sessions in one folder may share a name (ignoring case and spacing):
rename, new, move and fork are refused with a message when they would make a
duplicate. Claude Code itself doesn't enforce this.

### New sessions

**+ New session** (or `n`) asks for a folder and a name (required). Only
folders that already have sessions, still exist, and that Claude Code already
trusts are offered; `~` never is, since Claude Code asks about it every time.
The session starts through `claudepd --session-id <id> --name <name>
--remote-control <name>` in tmux and is kept running like any other. The page
says when it's ready (with an Open in Claude button), and a push notification
goes out through `~/scripts/ntfy_send.sh` with the claude.ai link (or a
"needs attention" one if it doesn't come up). From a shell:
`llm-switchboard new --list`, then `llm-switchboard new ~/dev/obd-things "name"`.

### Codex sessions

Codex sessions are listed beside Claude Code ones, tagged **Codex** (the
**All tools / Claude / Codex** switch, or `is:codex`, narrows the list). The
picker reads and changes them through Codex's shared app-server daemon, the
same way the Codex TUI does, never by editing Codex's files. For a Codex
session:

- **Rename**, **Recent messages**, and a copyable `codexp resume` command.
- **Fork…** copies it into a new Codex session, in the same or another folder.
  There's no Move: Codex has no way to change a session's folder.
- **Delete** archives it in Codex and lists it under Recently deleted, with
  Undo/Restore (unarchive); after 30 days, or Empty now, it's deleted for
  good. Codex won't delete one that a fork still points at; that one just
  stays archived in Codex. A session working on a turn can't be deleted.
- No keep-running switch: the Codex daemon already holds every session, and
  its remote control is one setting for the whole Pi
  (`codex app-server daemon enable-remote-control`), not per session.
- **+ New session** with Tool: Codex asks for a first message too, because
  Codex saves a session only once it has one. The session starts in the
  daemon and works on that message right away, with approval policy
  `never` (what `codexp` passes).

If the daemon isn't running, the page says so above the list and the Claude
side works as usual (`codex app-server daemon start` starts it). The daemon's
API is marked experimental, so a Codex update can break this half.

### Mac sessions

The Mac's (`m4mac`) Claude Code and Codex sessions show beside the Pi's,
tagged **Mac**, while the Mac is awake. The Mac runs this same package as a
launchd job (`host-agent`) that opens no port: every 10 s it computes its own
rows and pushes them to the Pi over SSH with a dedicated key, which the Pi's
`~/.ssh/authorized_keys` restricts to one command
(`restrict,command="…/bin/llm-switchboard host-exchange m4mac"`). The Pi has
no way into the Mac. The header shows `m4mac: online`, or `offline, last seen
HH:MM` after 35 s without a push, and the Mac's rows grey out.

- Rows carry only ids, titles, folder, times, size and open/busy status.
  Prompts, replies and terminal screens never leave the Mac, so Mac rows have
  no Recent messages, Terminal screen, keep-running switch or Restart, and
  match the search box by title and folder only. Untitled sessions show as
  `(untitled)` rather than their first prompt.
- **Rename**, **Move**, **Fork on the Mac…**, **Delete** (to the Mac's own
  trash, restorable from Recently deleted) are queued for the Mac, which runs
  them on its next push after checking them itself. Nothing that starts a
  process or sends a prompt can be queued. A queued action expires after 60 s;
  the box above the list shows each one as queued, delivered, done, failed
  (with the Mac's reason) or expired. An action the Mac received but never
  answered expires as "outcome unknown"; it is never sent twice.
- **Fork onto the Pi…** (Claude only) is the one way a transcript leaves the
  Mac: the Mac posts a notification, logs it, and uploads that one transcript
  (at most 16 MiB, 10 per hour). It lands as a new, not-running Pi session in
  `/home/pi/<rest>` for a Mac folder `/Users/jacobr/<rest>`, which must
  already exist on the Pi.
- Mac Codex sessions need the Mac's Codex daemon for actions; without it they
  are listed from disk, read-only.
- The resume command to copy uses plain `claude --resume` / `codex resume`.
- **Empty Pi trash now** leaves the Mac's trash alone.

Setting up the Mac: clone this repo there and run `scripts/setup-mac.sh`
(Homebrew Python 3.11+). It makes the key `~/.ssh/llm-switchboard-m4mac`,
installs it with `ssh vanpi …/llm-switchboard host-authorize m4mac < key.pub`
over the normal SSH login, does one push, and loads the launchd job
`dev.llm-switchboard.host-agent` (logs in `~/Library/Logs/llm-switchboard/`).
The Pi keeps what the Mac pushed, and the action queue, in
`~/.local/share/llm-switchboard/hosts/m4mac.json`.

### Selecting several

Click a session to select it, cmd/ctrl-click to add, shift-click for a
range, or tap the circle. Keys: ↑/↓ (or j/k) move, Space selects, Shift+↑/↓
extends, Cmd/Ctrl+A selects everything shown, Enter opens details, ⌫/Delete
deletes, Esc clears, `/` jumps to search. The bar at the bottom applies
**Keep running**, **Stop keeping**, **Move to folder…** and **Delete** to the
selection. Dialogs confirm with Enter and cancel with Esc.

### Search

Every term must match, case-insensitive (`n` opens New session). `word` searches titles, first
prompts, folders, ids and Remote Control names; `"two words"` is a phrase;
`-word` excludes (also `-dir:tmp`, `-"a phrase"`). Prefixes: `dir:` (or
`folder:`, `in:`), `title:`, `id:`, `is:kept|off|running|open|busy|attention|elsewhere|claude|codex`,
`state:<supervisor state>`, `age:<2d` / `age:>1w` (units m h d w). The **?**
next to the search box shows this. The drop-down after the filters sorts what's
shown: kept first (default), recent or oldest activity, name, folder, or size;
the choice is remembered per browser.

### Transcript search

**Transcripts**, next to the search box, searches what you typed and what
Claude Code and Codex replied on the Pi, newest match first. Press Enter to search;
Esc or clearing the box goes back to the session list. Each result shows the
match count and the latest matching excerpt; **Open matches** shows each
match with the records around it and the resume command. Mac sessions match
on title and folder only, since their text never leaves the Mac.

All words must appear in one record; `"two words"` is a phrase; `-word`
excludes sessions containing it. Words match whole (`pcan` finds
`pcan_view` and `PCAN-USB`, not `PCANView`). Filters: `tool:claude|codex`,
`dir:`, `by:me` (your prompts), `by:reply` (the replies), `age:<2d` /
`age:>1w`, `after:2026-10-01`, `before:2026-10-18`. A query that would take
over 5 s is stopped; add a more specific word.

The index is `~/.local/share/llm-switchboard/transcript-search.sqlite3`.
`serve` updates it every 30 s in a low-priority child process that reads only
what was appended; the first build reads every transcript and takes a while
(the line under the search box says so). Thinking, tool calls, tool output,
injected instructions and Codex subagents are left out: they are nearly all
of the bytes. `llmsearch` (in Jacob's scripts repo) searches this index
through `search --json`, and `llmsearch --grep` greps everything, those
included; for prompts and replies, tests check that both find the same
sessions.

## Install

```
./install.sh
loginctl enable-linger "$USER"   # once, so user services start at boot
```

Picker: `http://vanpi.tail059b13.ts.net:8777` (or `http://100.82.91.76:8777`) over
Tailscale, or `http://127.0.0.1:8777` on the Pi. Not from the van LAN: `serve`
listens only on localhost and the Tailscale address, and answers only localhost
and Tailscale peers. It binds the Tailscale address once Tailscale is up, so it
can start first at boot. `serve --host ADDR` (repeatable; `tailscale` stands for
the Tailscale address) changes where it listens.

## CLI

```
llm-switchboard list                 # sessions, enabled first
llm-switchboard status               # enabled sessions and their state
llm-switchboard enable c96934f7 [--name "van telemetrics"]
llm-switchboard disable c96934f7
llm-switchboard restart c96934f7
llm-switchboard attach c96934f7      # open its tmux window (detach: Ctrl-b d)
llm-switchboard new --list           # folders a new session can use
llm-switchboard new ~/dev/obd-things "what it's for"
llm-switchboard new ~/dev/obd-things "logger" --codex "add a logger"   # a Codex session
llm-switchboard fork c96934f7 "try another approach" [--folder ~/dev] [--keep]
llm-switchboard search '"some phrase" by:me tool:codex' [--limit 20] [--session codex:019f] [--json]
llm-switchboard index                # update the transcript index now
llm-switchboard stop-all             # kill the tmux server
llm-switchboard host-authorize m4mac < key.pub   # install a Mac's restricted key (run over SSH)
llm-switchboard host-exchange m4mac  # the restricted key's forced command (JSON on stdin)
# On the Mac (python3 -m llm_switchboard …):
#   host-agent [--once] [--pi vanpi] [--host-name m4mac] [--identity ~/.ssh/llm-switchboard-m4mac]
#   host-install [--unload]           # write and load (or unload) the launchd job
journalctl --user -u llm-switchboard -f
```

Ids can be any unique prefix; Codex sessions also go by the short id `list` shows.

## States

| State | Meaning |
| --- | --- |
| running | Window up and the status line shows `/rc active` (a bare `/rc` while a turn runs) |
| starting | Launched in the last 45s, or the status line shows `/rc connecting` / `/rc reconnecting` |
| attention | Running, but a prompt is waiting (e.g. folder trust), `/rc failed`, or no indicator after 45s; check the screen or attach |
| elsewhere | Open in another claude process; starts when that exits |
| offline | API unreachable; waiting |
| backoff | Exited; relaunch scheduled |
| missing | No transcript with that id |

## Notes

- Each session is a full claude process (~280 MB RSS on the Pi 4).
- If you `/clear` inside a kept session, Claude Code starts a new session id;
  the supervisor still relaunches the originally enabled id after an exit.
- The picker has no login. POSTs must be `application/json`, which blocks
  cross-site requests from browsers, but anyone on the tailnet can use it.

## Tests

```
python3 -m unittest discover -s tests   # Python + JS unit tests
tests/browser/run.sh [screenshot-dir]   # headless Chromium, fake HOME, own socket/port
```
