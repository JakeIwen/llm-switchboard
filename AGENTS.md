# llm-switchboard

Python 3.11 standard library only. Run tests with
`python3 -m unittest discover -s tests -f` (Python, required Node tests in
`tests/js`, architecture contracts, and the repo-owned backend-layout suite). After UI changes, use the existing
`browser_clean` MCP driver with isolated fixtures from `tests/browser/fixture.py`
and the behaviors in `tests/browser/ui-test.mjs`. Use separate fixture homes,
tmux sockets and loopback ports. For refactor comparisons, keep fixture timestamps
and path lengths equivalent, compute DOM/style hashes inside the page, and return
only hashes and bounded diagnostics to the driver. Use bounded action timeouts
and small comparison batches. The service runs from this checkout
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
| `llm_switchboard/settings.py`, `settings_cli.py` | Canonical install settings, validation and locked `settings.json` persistence; the lightweight `settings` / `settings set` CLI |
| `llm_switchboard/local_config.py` | This install's machine names from the untracked `local.env` (`local.env.example` lists them) |
| `llm_switchboard/session_id.py` | Session id validation and short ids |
| `llm_switchboard/store.py` | The enabled-session list (locked, atomic JSON) |
| `llm_switchboard/transcripts.py` | Finding and summarizing `~/.claude/projects/*/*.jsonl` (cached); the record markers and the custom-title line |
| `llm_switchboard/live_registry.py` | Which claude processes are running (`~/.claude/sessions`, `/proc`) |
| `llm_switchboard/tmux.py` | The dedicated tmux server and its windows |
| `llm_switchboard/launcher.py` | The pane command (goes through `claudepd`) |
| `llm_switchboard/screen.py` | Reading a captured Claude Code screen: `/rc` indicator, blocking prompts |
| `llm_switchboard/connectivity.py` | API reachability gate |
| `llm_switchboard/supervisor_state.py` | Pure `State`/`Status`/`Exit` records, backoff and screen-state decisions |
| `llm_switchboard/supervisor.py` | Reconcile loop and ordered tmux effects; keeps the state module's former public names available |
| `llm_switchboard/session_actions.py` | Rename / create / delete / move / fork / restore, aware of running processes (`Conflict` → 409); per-folder unique names |
| `llm_switchboard/conflict.py` | `Conflict`: an action the session's current state doesn't allow (→ 409) |
| `llm_switchboard/session_names.py` | Pure title validation (`clean_title`, `MAX_TITLE_LEN`) and comparison keys |
| `llm_switchboard/names.py` | Per-folder uniqueness across sources (`Names`); compatibility imports for title validation |
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
| `llm_switchboard/json_file.py` | `JsonFile`: locked, atomic JSON files shared by the service and short-lived commands (store, hosts, Mac ledger) |
| `llm_switchboard/host_protocol.py` | The Mac → Pi exchange: version, timing (`PUSH_INTERVAL`, `OFFLINE_AFTER`), `SessionState`, row validation |
| `llm_switchboard/host_actions_protocol.py` | Mac actions: `ActionKind` allow-list, `ActionState`, TTL and size caps, body/result/trash validation (both machines) |
| `llm_switchboard/hosts.py` | `Hosts`: per-host snapshots under `~/.local/share/llm-switchboard/hosts/`, the exchange, page rows for other machines |
| `llm_switchboard/host_queue.py` | The Mac action queue: enqueue, dispatch, results, expiry (at-most-once delivery) |
| `llm_switchboard/host_exchange.py`, `host_authorize.py` | The SSH forced command, and installing a host's restricted key in `~/.ssh/authorized_keys` |
| `llm_switchboard/host_import.py` | Fork onto the Pi: folder mapping, checks, writing the uploaded transcript as a new not-running session |
| `llm_switchboard/host_web.py` | The `/api/hosts/<host>/sessions/<id>/<action>` route |
| `llm_switchboard/host_cli.py` | `host-exchange`, `host-authorize`, `host-agent`, `host-install` (light imports; `__main__` dispatches them before cli.py) |
| `llm_switchboard/host_agent.py`, `host_transport.py` | Mac side: the 10 s push loop, and bounded `ssh` I/O |
| `llm_switchboard/host_launchd.py` | Mac side: the launchd job (`dev.llm-switchboard.host-agent`) |
| `llm_switchboard/mac_catalog.py`, `mac_registry.py` | Mac side: metadata-only rows (no prompts), macOS live-process check |
| `llm_switchboard/mac_actions.py`, `mac_action_runner.py` | Mac side: running allowed actions with its own checks; the durable ledger that never repeats one |
| `llm_switchboard/mac_uploads.py` | Mac side: the one transcript export path (notification, log, 10/hour quota) |
| `llm_switchboard/search_records.py` | Transcript search vocabulary (`SearchTool`, `SearchRole`, `IndexState`) and API record shapes |
| `llm_switchboard/search_query.py` | Transcript search syntax; user terms become quoted, bound FTS5 parameters |
| `llm_switchboard/search_extract.py` | What is indexed from Claude Code and Codex JSONL: your prompts and the visible replies |
| `llm_switchboard/search_index.py` | The SQLite FTS5 schema, status, results grouped per session, context around a match |
| `llm_switchboard/search_indexer.py` | Incremental indexing: offsets, rebuilds on shrink or new inode, removals, the bulk first build |
| `llm_switchboard/search_process.py` | `serve`'s 30 s low-priority indexer child |
| `llm_switchboard/search_api.py`, `search_cli.py` | Search routes (plus Mac title/folder matches) and the `search` / `index` commands (light imports; `__main__` dispatches them before cli.py) |
| `llm_switchboard/listen.py` | Where `serve` listens (localhost + Tailscale, bound once Tailscale is up) and which peers it answers |
| `llm_switchboard/web.py` | HTTP API and static page; `make_app` wires both session sources |
| `llm_switchboard/cli.py` | argparse entry point |
| `llm_switchboard/static/index.html`, `app.css` | The picker's markup and styles |
| `llm_switchboard/static/app.js` | Picker state, polling and event wiring (ES module, no build step) |
| `llm_switchboard/static/session-cards.js` | Session-card badges and row markup; state labels mirror `Supervisor.State` |
| `llm_switchboard/static/session-actions.js` | Sequential bulk execution/progress and blocked-session partitioning |
| `llm_switchboard/static/api.js` | The page's JSON calls to the server (`api`) |
| `llm_switchboard/static/format.js` | Text formatting for the page: `esc`, `age`, `size`, `plural`, `nowS` |
| `llm_switchboard/static/feedback.js` | Toasts, dialog bodies listing sessions, the clipboard |
| `llm_switchboard/static/search.js` | The Sessions filter syntax (`-neg`, `"phrase"`, `dir:`, `is:`, `age:`) and compatible-query translation into Transcripts |
| `llm_switchboard/static/toolbar.css` | Responsive search-scope / search / sort row and the status / tool / folder row |
| `llm_switchboard/static/dirs.js` | Stable folder choices from unfiltered eligibility (`listed` options: depth, minSessions, hidden); counts and total after other filters; per-machine keys and path tints |
| `llm_switchboard/static/folder-filter.js`, `folder-filter.css` | Persisted folder multiselect, searchable machine groups, removable chips, and mobile bottom sheet |
| `llm_switchboard/static/sort.js` | List orders (`SORTS`, `sortRows`): kept first, activity, name, folder, size |
| `llm_switchboard/static/selection.js` | Multi-select: click, cmd/shift ranges, keyboard cursor |
| `llm_switchboard/static/fulltext.js` | The Transcripts search mode: results, context, index status |
| `llm_switchboard/static/hosts.js`, `hosts.css` | Other machines' rows: badge, panel, locks, action routes and status |
| `llm_switchboard/static/settings.js` | Install settings dialog, field validation, and poll-interval fallback; mirrors the keys in `settings.py` |
| `llm_switchboard/static/dialog.js` | Modal confirm/prompt/form (text, select, checkbox fields; `validate`; Enter confirms, Esc cancels) |
| `tests/fake_codex.py` | In-memory Codex daemon for the unit and browser tests |
| `systemd/`, `install.sh` | User unit template and installer |
| `scripts/setup-mac.sh` | Mac setup: key, `host-authorize` over SSH, one push, launchd job |
| `scripts/check_layout.py`, `scripts/backend_layout/` | Repo-owned read-only layout checker; `.codeshape/config.json` declares its scope |
| `tests/test_settings.py`, `tests/js/settings.test.mjs` | Settings persistence, CLI, manual-launch isolation, and page field/polling contracts |
| `tests/test_architecture.py` | Enforced pure-owner imports, Mac/Pi separation, rendered systemd and launchd targets, checkout wrapper |

## Contracts

| Path | Caller | Owner |
| --- | --- | --- |
| `bin/llm-switchboard` | Pi systemd, installed CLI link, SSH forced command | `llm_switchboard/__main__.py` |
| `python3 -m llm_switchboard` | Mac launchd, Mac setup, search index child, shell callers | `llm_switchboard/__main__.py` (light host/search dispatch), `cli.py` |
| `systemd/llm-switchboard.service` | `install.sh` template input | `install.sh` |
| `install.sh` | Pi operator installation | `systemd/llm-switchboard.service` and `bin/llm-switchboard` |
| `scripts/setup-mac.sh` | Mac operator setup | `llm_switchboard/host_cli.py`, `host_launchd.py` |
| `scripts/check_layout.py` | Developer checks and unittest bridge | `scripts/backend_layout/cli.py` |

The package remains shared by Pi and Mac. Public cross-feature boundaries are
listed in the ownership map; import records from their pure owner rather than
pulling in the runtime. `supervisor.py` and `names.py` retain their former
public imports permanently for callers. The `host_*`, `mac_*`, `codex_*` and
`search_*` families stay flat: they distinguish protocol, transport, storage,
CLI and runtime roles, and their existing public module paths remain supported.
Feature grouping is a separate migration, not a prefix-count requirement.

## Organization checks

`python3 scripts/check_layout.py` scans without importing project code. There
is deliberately no `.codeshape/baseline.json`: all generic layout findings are
advisory, including contract and declared-target findings. Do not create or
update a baseline without Jacob's separate instruction. The focused architecture
tests enforce pure-owner dependencies, the Mac/Pi runtime boundary, and real
launch targets independently. No runtime module imports the checker.

The generic checker currently interprets the systemd template's `@REPO@`
placeholder as a literal missing path; the enforced test renders the template
and checks its actual executable. Prefix-family findings are review candidates,
not exceptions or permission to move supported public modules. The checker
fixture suite is included by `tests/test_backend_layout.py`; its timeout is
180 seconds because it takes about two minutes on the Pi. Node is required;
missing Node fails setup. With an isolated HOME, use
`SESSION_SEARCH_SCRIPTS=/home/pi/scripts` on this Pi to include the installed
cross-repo search oracles. See [verification notes](docs/backend-organization.md).

## User guide

The user-facing guide is a page in the visual-guides repo
(`~/dev/visual-guides/static/guides/llm-switchboard.html`, served at
`LLM_SWITCHBOARD_GUIDE_URL` from `local.env`; clone
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
- State names are the `State` enum in `supervisor_state.py` (also exported
  by `supervisor.py`); the page's `STATE`
  table in `session-cards.js` mirrors them for labels.
- Mac sessions: the Mac pushes; the Pi never connects to it. Rows from a
  host carry metadata only (no prompts, messages or screens), and only the
  actions in `ActionKind` can be queued; the Mac re-checks every one. Nothing
  queued may start a process or send a prompt there. A transcript leaves the
  Mac only through `fork-pi` (mac_uploads.py).
- Transcript search indexes only your prompts and the visible replies:
  Claude's through `prompt_text`/`reply_text` (transcripts.py), Codex's from
  its event stream (`user_message`/`agent_message`, or `item_completed`
  items in newer rollouts), never from response items, which repeat them
  among reasoning, tool calls and injected instructions. Subagent rollouts
  are tracked with an empty session id and never indexed. For words in
  prompts and replies, `tests/test_search_oracle.py` checks that the index
  finds the sessions `~/scripts/claudesearch.sh`, `codexsearch.sh` and
  `llmsearch.sh` do, when they are installed (it sets `CLAUDESEARCH_ROOT`
  and `CODEXSEARCH_ROOT`, which make `llmsearch` grep instead of asking the
  index). `llmsearch` reads `search --json` lines and falls back to grep on
  its exit 3 (no index built yet), so keep both stable. A Codex rollout is
  named by its first `session_meta` (forks copy their ancestors' after it),
  a Claude transcript by its file name. Indexing runs only in `serve`'s niced child
  (`search_process.py`), never in the supervisor or a request thread; after
  changing what is extracted, rebuild: stop the service and delete
  `transcript-search.sqlite3*`.
- Manual resume commands use `claude_command` / `codex_command` from install settings
  (`STATE_DIR/settings.json`, normally `~/.config/llm-switchboard/settings.json`).
  These settings never change the supervisor's `claudepd` launch function. Search
  responses rebuild resume commands from current settings without reindexing.
- The page polls at `poll_seconds` (4s by default); only rewrite DOM through `setHTML` (skips
  unchanged markup) so clicks aren't lost to a redraw mid-click.
- Machine names (the Pi's SSH alias, the Mac's name, Tailscale names and
  addresses, LAN URLs) live only in the gitignored `local.env`, never in
  tracked files, so the repo can be public. Tests use generic names (`pi`, `mac`,
  `/Users/me`).
