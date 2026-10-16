# claude-always-remote

Keeps chosen Claude Code sessions running on the Pi, in tmux, with Remote
Control on, so they can always be reached from the Claude app or
claude.ai/code. A web page on the Pi picks which sessions those are, and
manages Claude Code and Codex sessions in bulk: search, filter by folder,
rename, fork, move, delete with undo, and start new ones.

Illustrated guide: http://vanpi.lan:8791/guides/always-remote.html (source in
`JakeIwen/visual-guides`; keep it current with any change in behaviour, see AGENTS.md).

## How it works

- A user systemd service (`claude-always-remote`, started at boot through
  linger) runs a supervisor and the picker web app on port 8777.
- Each enabled session gets a window in a dedicated tmux server
  (`tmux -L claude-always-remote`). The window runs
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
- Enabled sessions are stored in `~/.config/claude-always-remote/sessions.json`.

## Picker

Toggle a session to keep it running. Folder chips filter the list. Expand a
session (▾) for:

- **Open in Claude**: its claude.ai/code link (from its last Remote Control connection)
- **Recent messages**: the last few prompts and replies
- **Terminal screen** and **Restart** (kept-running sessions)
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
`~/.local/share/claude-always-remote/trash/`, with Undo and a "Recently
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
`claude-always-remote new --list`, then `claude-always-remote new ~/dev/obd-things "name"`.

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

## Install

```
./install.sh
loginctl enable-linger "$USER"   # once, so user services start at boot
```

Picker: `http://vanpi:8777` (from the LAN or Tailscale).

## CLI

```
claude-always-remote list                 # sessions, enabled first
claude-always-remote status               # enabled sessions and their state
claude-always-remote enable c96934f7 [--name "van telemetrics"]
claude-always-remote disable c96934f7
claude-always-remote restart c96934f7
claude-always-remote attach c96934f7      # open its tmux window (detach: Ctrl-b d)
claude-always-remote new --list           # folders a new session can use
claude-always-remote new ~/dev/obd-things "what it's for"
claude-always-remote new ~/dev/obd-things "logger" --codex "add a logger"   # a Codex session
claude-always-remote fork c96934f7 "try another approach" [--folder ~/dev] [--keep]
claude-always-remote stop-all             # kill the tmux server
journalctl --user -u claude-always-remote -f
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
  cross-site requests from browsers, but anyone on the LAN/tailnet can use it.

## Tests

```
python3 -m unittest discover -s tests   # Python + JS unit tests
tests/browser/run.sh [screenshot-dir]   # headless Chromium, fake HOME, own socket/port
```
