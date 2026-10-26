# llm-switchboard

Python 3.11+ backend on FastAPI and Pydantic v2, served by Uvicorn, with
dependencies locked by uv. The Mac host agent, the SSH commands
(`host-exchange`, `host-authorize`), `search`/`index` and the `settings` CLI
stay standard-library only and must not import the HTTP stack
(`tests/test_http_imports.py`). Use the system Python only:
`UV_PYTHON_PREFERENCE=only-system uv sync --frozen`. The picker in `web/`
(Vite, React, TypeScript, Mantine) is served at `/`; former `/next/` URLs
permanently redirect to the same root path, preserving the query string.
Run `npm ci` in `web/` once, then the whole
suite with `.venv/bin/python -m unittest discover -s tests -f` (Python, the
`web/` typecheck, unit tests, Node-based react-app checks and build,
architecture contracts, and the repo-owned backend-layout suite). After
UI changes, use the existing `browser_clean` MCP driver with isolated fixtures
from `tests/browser/fixture.py` and the behaviors in `tests/browser/next-ui.mjs`
(at `/`; also checks the `/next/` redirect; browser_clean reads files
only under this checkout). The page's design rules are `docs/design.md`. Use separate fixture homes,
tmux sockets and loopback ports. For refactor comparisons, keep fixture timestamps
and path lengths equivalent, compute DOM/style hashes inside the page, and return
only hashes and bounded diagnostics to the driver. Use bounded action timeouts
and small comparison batches. The service runs from this checkout:
`bin/llm-switchboard` runs the checkout's `.venv/bin/python` when it exists,
otherwise `python3`; the fallback serves only the standard-library commands,
so never restart the service without `.venv`. After a code change:
`systemctl --user restart llm-switchboard` (sessions keep running; the
supervisor adopts their windows). Production logs a response that doesn't
match its schema and still sends it; `LLM_SWITCHBOARD_STRICT_RESPONSES=1`
(set by the tests and the parity runner) makes that fatal. After changing a
route or record, run `npm run gen:api` in `web/`; a test fails while the
generated schema and types are stale.

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
| `llm_switchboard/mac_catalog.py`, `mac_registry.py` | Mac side: metadata-only rows (titles, no prompt fields), macOS live-process check |
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
| `llm_switchboard/web.py` | FastAPI routes, the root picker and former preview redirects, and the deterministic OpenAPI export; keeps the `App` and `make_app` imports |
| `llm_switchboard/web_app.py` | HTTP-independent picker operations and the composition of local, Codex and Mac session sources |
| `llm_switchboard/http/records.py` | Pydantic projections of the canonical stdlib records; never imported by light paths |
| `llm_switchboard/http/requests.py`, `http/responses.py` | HTTP request and response schemas; the existing domain validators keep the rules |
| `llm_switchboard/http/transport.py` | JSON encoding as before, request guards (JSON-only POST, 16 KiB), error mapping, the POST audit log, escaped-path compatibility |
| `llm_switchboard/http/server.py` | Uvicorn sockets, late Tailscale binding, peer rejection, clean SIGTERM |
| `llm_switchboard/http/lifespan.py` | Starts and stops the supervisor and the search-index child; leaves tmux running |
| `scripts/api_parity/` | Old/new HTTP comparison on isolated fixtures (`python -m scripts.api_parity`) |
| `pyproject.toml`, `uv.lock` | Locked HTTP and development dependencies under uv; system Python only |
| `llm_switchboard/page.py` | The root page (`web/`, built into `web/dist`): guarded file paths, cache headers, SPA fallback outside `/api/`, and `/next/` redirects |
| `web/` | The picker: Vite, React, TypeScript, Mantine; design rules in `docs/design.md`; `npm run build` writes `web/dist` (not tracked), which the service serves at `/` |
| `web/src/api/` | The page's JSON calls (`client.ts`) and the types generated from the server's OpenAPI schema (`schema.d.ts` from `web/openapi.json`, `npm run gen:api`) |
| `web/src/actions/` | Action flows (every POST with its confirms, forms, toasts and undo) behind the `ActionUi` seam in `ui.ts`; no DOM |
| `web/src/search/`, `sessions/`, `folders/`, `hosts/`, `settings/`, `format/` | Pure helpers: query syntax, sort, selection, badges, bulk partitioning, folder choices and saved selection, Mac locks, settings fields, text formatting |
| `web/src/pages/PickerPage.tsx`, `pages/picker/` | The one page (wiring only) and its sections and hooks: polling, filters, selection, keyboard, dialogs and toasts |
| `web/vite.config.ts` | Dev server on 127.0.0.1:5173 proxying `/api` to `SWITCHBOARD_API` (default the Pi's 8777 through an SSH tunnel), read-only unless `SWITCHBOARD_WRITES=1` |
| `llm_switchboard/cli.py` | argparse entry point |
| `tests/fake_codex.py` | In-memory Codex daemon for the unit and browser tests |
| `systemd/`, `install.sh` | User unit template and installer |
| `scripts/setup-mac.sh` | Mac setup: key, `host-authorize` over SSH, one push, launchd job |
| `scripts/check_layout.py`, `scripts/backend_layout/` | Repo-owned read-only layout checker; `.codeshape/config.json` declares its scope |
| `tests/test_settings.py`, `web/src/settings/settingsFields.test.ts` | Settings persistence, CLI, manual-launch isolation, and page field/polling contracts |
| `tests/test_architecture.py` | Enforced pure-owner imports, Mac/Pi separation, rendered systemd and launchd targets, checkout wrapper |

## Contracts

| Path | Caller | Owner |
| --- | --- | --- |
| `bin/llm-switchboard` | Pi systemd, installed CLI link, SSH forced command (runs `.venv/bin/python` when present) | `llm_switchboard/__main__.py` |
| `python3 -m llm_switchboard` | Mac launchd, Mac setup, search index child, shell callers | `llm_switchboard/__main__.py` (light host/search dispatch), `cli.py` |
| `systemd/llm-switchboard.service` | `install.sh` template input | `install.sh` |
| `install.sh` | Pi operator installation | `systemd/llm-switchboard.service` and `bin/llm-switchboard` |
| `scripts/setup-mac.sh` | Mac operator setup | `llm_switchboard/host_cli.py`, `host_launchd.py` |
| `scripts/check_layout.py` | Developer checks and unittest bridge | `scripts/backend_layout/cli.py` |
| `python -m llm_switchboard openapi` | `web/` type generation (`npm run gen:api`) and the stale-types test | `llm_switchboard/cli.py`, `web.py` |
| `/`, `/index.html`, `/assets/*`, `/theme.js` | Picker browsers; extensionless client routes also use the index | `llm_switchboard/page.py`, `web/dist` |
| `/next`, `/next/*` | Former preview bookmarks; permanent redirects to the matching root URL | `llm_switchboard/page.py` |
| `python -m scripts.api_parity` | Developer HTTP parity checks | `scripts/api_parity/__main__.py` |

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
  by `supervisor.py`); the page's `STATE` table in
  `web/src/sessions/badges.ts` mirrors them for labels.
- Mac sessions: the Mac pushes; the Pi never connects to it. Rows from a
  host carry metadata only (no prompts, messages or screens; an unnamed
  session's title is its first prompt cut to one line, Jacob's call on
  2026-10-26), and only the
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
- The page polls at `poll_seconds` (4s by default), paused while hidden.
  `web/src/pages/picker/useSessions.ts` reuses unchanged row objects;
  `SessionList.tsx` keys rows by session id so React preserves controls and
  focus across polls rather than replacing the list's HTML.
- Machine names (the Pi's SSH alias, the Mac's name, Tailscale names and
  addresses, LAN URLs) live only in the gitignored `local.env`, never in
  tracked files, so the repo can be public. Tests use generic names (`pi`, `mac`,
  `/Users/me`).
