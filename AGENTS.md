# llm-switchboard

Python 3.11 standard library only. Run tests with
`python3 -m unittest discover -s tests` (this also runs the node unit tests in
`tests/js` when node is installed). After UI changes, run the browser test,
`tests/browser/run.sh [screenshot-dir]` (needs /usr/bin/chromium; installs
puppeteer-core into tests/browser on first run). The service runs from this checkout
(`bin/llm-switchboard` → `python3 -m llm_switchboard`), so after a
code change: `systemctl --user restart llm-switchboard` (sessions keep
running; the supervisor adopts their windows).

To run a second instance (say, to try UI changes) without touching the real
one, give it its own HOME, socket and port (and `LLM_SWITCHBOARD_CODEX_SOCKET=`
to leave Codex out, or point it at `tests/fake_codex.py`):
`HOME=/tmp/fakehome LLM_SWITCHBOARD_TMUX_SOCKET=switchboard-dev bin/llm-switchboard --port 8790 serve --host 127.0.0.1`.
Sharing the socket would let its supervisor close the real windows.

## Where things live

| Module | Concern |
| --- | --- |
| `llm_switchboard/paths.py` | Shared locations, names, the managed-process env marker, default port |
| `llm_switchboard/session_id.py` | Session id validation and short ids |
| `llm_switchboard/store.py` | The enabled-session list (locked, atomic JSON) |
| `llm_switchboard/transcripts.py` | Finding and summarizing `~/.claude/projects/*/*.jsonl` (cached); the record markers and the custom-title line |
| `llm_switchboard/live_registry.py` | Which claude processes are running (`~/.claude/sessions`, `/proc`) |
| `llm_switchboard/tmux.py` | The dedicated tmux server and its windows |
| `llm_switchboard/launcher.py` | The pane command (goes through `claudepd`) |
| `llm_switchboard/screen.py` | Reading a captured Claude Code screen: `/rc` indicator, blocking prompts |
| `llm_switchboard/connectivity.py` | API reachability gate |
| `llm_switchboard/supervisor.py` | Reconcile loop, `State` enum, backoff |
| `llm_switchboard/session_actions.py` | Rename / create / delete / move / fork / restore, aware of running processes (`Conflict` → 409); per-folder unique names |
| `llm_switchboard/conflict.py` | `Conflict`: an action the session's current state doesn't allow (→ 409) |
| `llm_switchboard/names.py` | Cleaning names (`clean_title`) and keeping them unique per folder across every source (`Names`) |
| `llm_switchboard/trash.py` | Deleted transcripts, kept 30 days for restore |
| `llm_switchboard/folders.py` | Folders a new session may use (existing, trusted per `~/.claude.json`, never `~`); `existing_folder` for typed paths |
| `llm_switchboard/notify.py` | Push notifications via `~/scripts/ntfy_send.sh` (`LLM_SWITCHBOARD_NOTIFY` overrides; empty disables) |
| `llm_switchboard/relocate.py` | Moving or forking a transcript into a working folder (Claude Code's project-folder naming, line rewriting) |
| `llm_switchboard/conversation.py` | Recent prompts and replies from a transcript's tail |
| `llm_switchboard/catalog.py` | Row shape shared by the web API and CLI (Claude and Codex rows; `tool` field) |
| `llm_switchboard/codex_client.py` | JSON-RPC over WebSocket to Codex's app-server daemon (`CodexUnavailable`, `CodexError`) |
| `llm_switchboard/codex_threads.py` | Listing Codex threads (cached), their messages, `CodexState`, short ids |
| `llm_switchboard/codex_actions.py` | Rename / delete (archive) / restore / fork / create Codex sessions |
| `llm_switchboard/codex_trash.py` | Which Codex sessions were deleted here, for Recently deleted and the 30-day purge |
| `llm_switchboard/listen.py` | Where `serve` listens (localhost + Tailscale, bound once Tailscale is up) and which peers it answers |
| `llm_switchboard/web.py` | HTTP API and static page; `make_app` wires both session sources |
| `llm_switchboard/cli.py` | argparse entry point |
| `llm_switchboard/static/index.html`, `app.css` | The picker's markup and styles |
| `llm_switchboard/static/app.js` | Picker: rendering, actions, events (ES module, no build step) |
| `llm_switchboard/static/search.js` | Search syntax (`-neg`, `"phrase"`, `dir:`, `is:`, `age:`) |
| `llm_switchboard/static/sort.js` | List orders (`SORTS`, `sortRows`): kept first, activity, name, folder, size |
| `llm_switchboard/static/selection.js` | Multi-select: click, cmd/shift ranges, keyboard cursor |
| `llm_switchboard/static/dialog.js` | Modal confirm/prompt/form (text, select, checkbox fields; `validate`; Enter confirms, Esc cancels) |
| `tests/fake_codex.py` | In-memory Codex daemon for the unit and browser tests |
| `systemd/`, `install.sh` | User unit template and installer |

## User guide

The user-facing guide is a page in the visual-guides repo
(`~/dev/visual-guides/static/guides/llm-switchboard.html`, served at
http://vanpi.lan:8791/guides/llm-switchboard.html; clone
`github.com/JakeIwen/visual-guides` if the checkout is missing). It describes
the page, the keep-running states, the search syntax, the details panel, bulk
actions, new sessions, delete/restore, the Codex differences, the CLI and
troubleshooting. Any change to what the user sees or can do (a control, a
badge, a search term, a CLI flag, a state, a refusal message, a path) must
update that guide in the same piece of work: edit the page, bump its Updated
date (the `facts` list and the entry in `static/index.html`), then follow that
repo's AGENTS.md to test, commit, push and deploy. Mention the guide commit in
the report. If the guide can't be updated (no checkout, no network), say so in
the report instead of leaving it silently stale.

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
