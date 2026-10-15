# claude-always-remote

Python 3.11 standard library only. Run tests with
`python3 -m unittest discover -s tests` (this also runs the node unit tests in
`tests/js` when node is installed). After UI changes, run the browser test,
`tests/browser/run.sh [screenshot-dir]` (needs /usr/bin/chromium; installs
puppeteer-core into tests/browser on first run). The service runs from this checkout
(`bin/claude-always-remote` → `python3 -m claude_always_remote`), so after a
code change: `systemctl --user restart claude-always-remote` (sessions keep
running; the supervisor adopts their windows).

To run a second instance (say, to try UI changes) without touching the real
one, give it its own HOME, socket and port (and `CLAUDE_ALWAYS_REMOTE_CODEX_SOCKET=`
to leave Codex out, or point it at `tests/fake_codex.py`):
`HOME=/tmp/fakehome CLAUDE_ALWAYS_REMOTE_TMUX_SOCKET=car-dev bin/claude-always-remote --port 8790 serve --host 127.0.0.1`.
Sharing the socket would let its supervisor close the real windows.

## Where things live

| Module | Concern |
| --- | --- |
| `claude_always_remote/paths.py` | Shared locations, names, the managed-process env marker, default port |
| `claude_always_remote/session_id.py` | Session id validation and short ids |
| `claude_always_remote/store.py` | The enabled-session list (locked, atomic JSON) |
| `claude_always_remote/transcripts.py` | Finding and summarizing `~/.claude/projects/*/*.jsonl` (cached); the record markers and the custom-title line |
| `claude_always_remote/live_registry.py` | Which claude processes are running (`~/.claude/sessions`, `/proc`) |
| `claude_always_remote/tmux.py` | The dedicated tmux server and its windows |
| `claude_always_remote/launcher.py` | The pane command (goes through `claudepd`) |
| `claude_always_remote/screen.py` | Reading a captured Claude Code screen: `/rc` indicator, blocking prompts |
| `claude_always_remote/connectivity.py` | API reachability gate |
| `claude_always_remote/supervisor.py` | Reconcile loop, `State` enum, backoff |
| `claude_always_remote/session_actions.py` | Rename / create / delete / move / fork / restore, aware of running processes (`Conflict` → 409); per-folder unique names |
| `claude_always_remote/conflict.py` | `Conflict`: an action the session's current state doesn't allow (→ 409) |
| `claude_always_remote/names.py` | Cleaning names (`clean_title`) and keeping them unique per folder across every source (`Names`) |
| `claude_always_remote/trash.py` | Deleted transcripts, kept 30 days for restore |
| `claude_always_remote/folders.py` | Folders a new session may use (existing, trusted per `~/.claude.json`, never `~`); `existing_folder` for typed paths |
| `claude_always_remote/notify.py` | Push notifications via `~/scripts/ntfy_send.sh` (`CLAUDE_ALWAYS_REMOTE_NOTIFY` overrides; empty disables) |
| `claude_always_remote/relocate.py` | Moving or forking a transcript into a working folder (Claude Code's project-folder naming, line rewriting) |
| `claude_always_remote/conversation.py` | Recent prompts and replies from a transcript's tail |
| `claude_always_remote/catalog.py` | Row shape shared by the web API and CLI (Claude and Codex rows; `tool` field) |
| `claude_always_remote/codex_client.py` | JSON-RPC over WebSocket to Codex's app-server daemon (`CodexUnavailable`, `CodexError`) |
| `claude_always_remote/codex_threads.py` | Listing Codex threads (cached), their messages, `CodexState`, short ids |
| `claude_always_remote/codex_actions.py` | Rename / delete (archive) / restore / fork / create Codex sessions |
| `claude_always_remote/codex_trash.py` | Which Codex sessions were deleted here, for Recently deleted and the 30-day purge |
| `claude_always_remote/web.py` | HTTP API and static page; `make_app` wires both session sources |
| `claude_always_remote/cli.py` | argparse entry point |
| `claude_always_remote/static/index.html`, `app.css` | The picker's markup and styles |
| `claude_always_remote/static/app.js` | Picker: rendering, actions, events (ES module, no build step) |
| `claude_always_remote/static/search.js` | Search syntax (`-neg`, `"phrase"`, `dir:`, `is:`, `age:`) |
| `claude_always_remote/static/selection.js` | Multi-select: click, cmd/shift ranges, keyboard cursor |
| `claude_always_remote/static/dialog.js` | Modal confirm/prompt/form (text, select, checkbox fields; `validate`; Enter confirms, Esc cancels) |
| `tests/fake_codex.py` | In-memory Codex daemon for the unit and browser tests |
| `systemd/`, `install.sh` | User unit template and installer |

## Rules

- Only the supervisor thread starts or kills tmux windows; the web app and
  CLI edit the store and call `wake()` / `request_restart()`.
- Sessions must launch through the `claudepd` bash function, never a copy
  of its flags: Remote Control depends on its settings.
- A session created from the picker has no transcript until Claude Code
  writes one; its store entry carries `cwd`/`title`, and the supervisor starts
  it with `--session-id … --name …` until the transcript exists, then `--resume`.
- Codex sessions are changed only through the daemon's API (codex_client.py),
  never by editing `~/.codex` files: Codex keeps their state in SQLite too.
  Its API is experimental; any Codex failure must leave the Claude side working.
- Never edit a transcript another claude process has open; a running
  process owns its title (rename it by typing `/rename` into our window).
- Session names are unique per folder (case and spacing ignored); every path
  that sets or moves a name goes through `Names.refuse_duplicate` (names.py).
- State names are the `State` enum in `supervisor.py`; the page's `STATE`
  table in `app.js` mirrors them for labels.
- The page polls every 4s; only rewrite DOM through `setHTML` (skips
  unchanged markup) so clicks aren't lost to a redraw mid-click.
