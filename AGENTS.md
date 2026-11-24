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
| `llm_switchboard/local_config.py` | This install's machine names and Mac search page-origin allow-list from the untracked `local.env` (`local.env.example` lists them) |
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
| `llm_switchboard/ssh_authorized_keys.py` | The one edit of `~/.ssh/authorized_keys`: its lock, replacing or removing the line whose final comment is a marker, every other line kept (v1's `host-authorize` and the desk link's commands) |
| `llm_switchboard/ssh_public_key.py` | The one check of an OpenSSH public key blob against its key type (pure, both machines): v1's `host-authorize` and the desk link's signer and proof checks |
| `llm_switchboard/host_import.py` | Fork onto the Pi: folder mapping, checks, writing the uploaded transcript as a new not-running session |
| `llm_switchboard/host_web.py` | The `/api/hosts/<host>/sessions/<id>/<action>` route |
| `llm_switchboard/host_cli.py` | `host-exchange`, `host-authorize`, `host-agent`, `host-install`, `host-search` (light imports; `__main__` dispatches them before cli.py) |
| `llm_switchboard/host_agent.py`, `host_transport.py` | Mac side: the 10 s push loop, and bounded `ssh` I/O |
| `llm_switchboard/host_launchd.py` | Mac side: the launchd jobs, the publisher (`dev.llm-switchboard.host-agent`), the 30 s background transcript indexer (`dev.llm-switchboard.search-index`), and loopback search (`dev.llm-switchboard.host-search`) |
| `llm_switchboard/mac_search_server.py` | Mac-only stdlib read-only HTTP search on `127.0.0.1:SEARCH_PORT`, Host guard and CORS allow-list; reuses `search_api` over the Mac's own index, never sends text to the Pi |
| `llm_switchboard/mac_catalog.py`, `mac_registry.py` | Mac side: metadata-only rows (titles, no prompt fields), macOS live-process check |
| `llm_switchboard/mac_actions.py`, `mac_action_runner.py` | Mac side: running allowed actions with its own checks; the durable ledger that never repeats one |
| `llm_switchboard/mac_uploads.py` | Mac side: the one transcript export path (notification, log, 10/hour quota) |
| `llm_switchboard/search_records.py` | Transcript search vocabulary (`SearchTool`, `SearchRole`, `IndexState`) and API record shapes |
| `llm_switchboard/search_query.py` | Transcript search syntax; user terms become quoted, bound FTS5 parameters |
| `llm_switchboard/search_extract.py` | What is indexed from Claude Code and Codex JSONL: your prompts and the visible replies; a Codex rollout's first prompt (the Mac's title for an unnamed session when Codex is offline) |
| `llm_switchboard/search_index.py` | The SQLite FTS5 schema, status, results grouped per session, context around a match |
| `llm_switchboard/search_indexer.py` | Incremental indexing: offsets, rebuilds on shrink or new inode, removals, the bulk first build |
| `llm_switchboard/search_process.py` | `serve`'s 30 s low-priority indexer child |
| `llm_switchboard/search_api.py`, `search_cli.py` | Search routes (plus Mac title/folder matches) and the `search` / `index` commands (light imports; `__main__` dispatches them before cli.py) |
| `llm_switchboard/desk/` | The voice desk (tablet page -> enrolled Claude sessions through per-session MCP channels); standard library only, never imports the HTTP stack; module map, files, API and state rules in `docs/voice-desk.md` |
| `llm_switchboard/desk/hub.py`, `desk/hub_state.py`, `desk/model.py` | `desk serve`: wiring of the channel socket, operator socket, HTTPS and the 5 s recheck; the pure state machine (tickets, requests, locks, pairing, tokens) and its records and limits |
| `llm_switchboard/desk/startup_identity.py`, `desk/startup_ack.py`, `desk/startup_registry.py` | The startup acknowledgement: launch-identity rules and refusal vocabulary; the tmux and `/proc` adapter that shows the development-channel warning and presses Enter once per view behind tmux's own guard; the bounded read of claude's session file |
| `llm_switchboard/desk/grants.py`, `desk/page_build.py` | Conversation mode on the hub: grants, the one-page rule and the voice-send checks (a `HubState` mixin); the page files served from memory and their build id, which a pairing claim must match |
| `llm_switchboard/desk/shallow_json.py` | JSON another process wrote, parsed only after its nesting depth is counted against a bound (strings skipped), because CPython 3.13 crashes instead of raising RecursionError on deep nesting in a small-stack thread; every desk reader of JSON from a file or a socket parses through it (a structural test enforces it) |
| `llm_switchboard/desk/child_process.py` | Bounded child runs for the Mac link (`ssh-keygen`, `ssh -G`, `ssh`): no shell, `close_fds`, an explicit environment, pipes read to their caps, a total deadline, and on every path SIGTERM, 2 s, SIGKILL and `waitpid`, the cleanup a separate bound after the deadline |
| `llm_switchboard/desk/run_lock.py` | Run locks (`hub.lock`, `relay.lock`): a process-lifetime exclusive `flock` (`O_NOFOLLOW`, `O_CLOEXEC`, a regular file, never released before exit, an optional bounded start wait) and the non-blocking probe held for a block by the offline decisions |
| `llm_switchboard/desk/link_writer.py` | The Mac link's one-thread writer (hub link, relay ssh pipe, relay channels): non-blocking writes, per-frame deadlines from submit, the begun mark, one outcome cell per frame (`model.WriteOutcome`), shutdown before the decision; only the writer thread closes the descriptor |
| `llm_switchboard/desk/link_protocol.py` | The Mac link's framing (both machines): `LinkType`, `Side`, `LINK_VERSIONS`, `MAX_LINK_LINE_BYTES`, the one code table `LINK_CODES` and its keyed maps over `ReplyRefusal`, `UndeliverableReason` and `ChannelProcess`, `encode_frame`/`decode_frame` over one set of exact shapes (parsed through `shallow_json`), `LineSplitter`, the field validators, link timing and caps |
| `llm_switchboard/desk/link_proof.py` | The Mac link's proof of possession (both machines): the exact signed message, `ssh-keygen -Y sign` and `-Y verify` through `child_process` (no agent, 5 s, 4 KiB, the signer line and signature as `/dev/fd` pipes), `verify_proof` binding the host it checks, the self-test vector, and the one strict reader and writer of `link-signers` |
| `llm_switchboard/desk/link_pump.py`, `bin/desk-link` | The Mac link's forced command on the Pi: connects to the hub's `link.sock` and copies whole lines both ways, each checked against `MAX_LINK_LINE_BYTES` before it is forwarded, never a partial line; the no-hub `link_refused` frame; one `desk-link: <code>` exit line (`PumpExit`); parses nothing |
| `llm_switchboard/desk/link_hub.py` | The Mac link on the hub (Pi): `LinkHub` owns `link.sock`, accept caps, publication under the links lock (the signer line reread, per-host rate, host cap, replacement), one reader per link dispatching its frames to the hub's handlers, the heartbeat and idle rule, the signer check (first step of housekeeping), presence for a Mac lock, the link-wait caps (`LinkWaitSurface`), link records |
| `llm_switchboard/desk/link_admission.py` | Admission steps 2 to 6 on `link.sock` (uid, hello and versions, the hello host's signer line, challenge, proof through `link_proof.verify_proof`) within one deadline; `AdmissionRefusals`, the unattributed refusals, one entry per `link_refused` code |
| `llm_switchboard/desk/link_session.py` | `LinkSession`, one published link (host, verified key and fingerprint, machine, writer, chan and query high-water marks, presence queries, `retired`), and `RemoteChannel`, a Mac registration's proxy in `Hub._connections` with the `ChannelConnection` surface plus `send_event` |
| `llm_switchboard/desk/link_views.py` | The Mac link's part of `HubState` (a mixin): `LinkState`, `set_links`, `set_admission_refusals`, `set_label` for Mac targets, `lock_info` for a clear's presence prefetch, and their snapshot and `status` fields |
| `llm_switchboard/desk/remote_enrollment.py` | Mac enrollment on the Pi: `enrolled-hosts.json` (`HostEnrollment`, locked by `enrolled.json`'s lock), the owners map and the published view (an id with two owners routes nowhere), and the one edit rule online and offline (`enroll_checked`: both files read fresh, owners, registrations and locks on another host; an unenroll needs only its own file) |
| `llm_switchboard/desk/enrollment_views.py` | Enrollment in `HubState` (a mixin): `set_enrollment(enrollment, conflicts)` set whole by `Hub.refresh_files`, the routing checks (`enrolled on two hosts`, a registration's host must be the view's), an enroll's check against registrations and locks, `set_candidates` |
| `llm_switchboard/desk/hub_enrollment.py` | Enrollment edits on the hub (a `Hub` mixin): `enroll`/`unenroll` and the host-qualified `enroll_host`/`unenroll_host` from the page and the operator socket (a phase 1 message carrying `host` refused), unenroll dropping registrations before it returns, and the operator-only `clear_stopped` |
| `llm_switchboard/desk/hub_link_ops.py` | The Mac link's operator ops on the hub (a `Hub` mixin): `link_drop` (the signer check now, answering `{readable, signer, link}` from one hold of the links lock) and `link_fence` (`LinkHub.fence_check`, then `HubState.fence_lock_view`) |
| `llm_switchboard/desk/link_fence.py` | The rollback fence's rules, shared by the hub and the offline half: no signer line, and no lock whose host isn't `pi` in `locks.json` (read through `inspect_lock_file`) or the hub's list, no store error (`FenceLockView`) |
| `llm_switchboard/desk/link_authorize.py` | The Mac link's commands on the Pi's shell: `desk link-authorize [--self-test]` (the Ed25519 check, both files checked and written inside `link-signers.lock` then the `authorized_keys` lock, the proposed `link-signers` passed through the hub's reader first) and `link-revoke`, each step idempotent and reported, confirmed by the hub's `link_drop` answer; `desk link-fence`; with no hub answering, every confirmation and the fence decide from the files under the run lock (`run_lock.probe`) or refuse while a hub process holds it |
| `llm_switchboard/desk/link_candidates.py` | Mac sessions asking to join (`Candidates`): a `register` refused only as not enrolled, capped, cut and expiring on an injected clock, with the recently-unenrolled table |
| `llm_switchboard/desk/relay_ssh.py` | Mac SSH route and host-key selection from two bounded `ssh -G` reads; prepares an isolated desk-key-only invocation and classifies stderr without logging it |
| `llm_switchboard/desk/peer_mac.py`, `desk/mac_process.py` | Verify and recheck the Mac channel and its parent, and query process presence; `LibprocReader` is the Darwin kernel adapter |
| `llm_switchboard/desk/relay_channels.py` | Mac relay: the channel socket (`DeskConfig.channel_socket`) where the phase 1 channel connects: uid before hello, the `peer_mac` check, `register` over the link (`Uplink`), the hub's text for its codes, local ping/pong and the idle rule, the 5 s and per-event/per-reply rechecks, one `LinkWriter` per channel and the evidence rules for events, local reply refusals, the 25 s not-enrolled cache |
| `llm_switchboard/desk/relay_link.py` | Mac relay: `RelayLink`, one ssh attempt and link (the child through `child_process`, the 30 s handshake on `CLOCK_MONOTONIC`, negotiation and the proof, the ssh writer, 15 s pings, the 45 s idle rule, the ping records and the 40 s freshness rule), the `Uplink` relay_channels registers on; `RelayConnector`, links one after another with the backoff each end calls for |
| `llm_switchboard/desk/relay.py`, `bin/desk-relay` | Mac relay process: `relay.lock` for its life (a second exits 3), the rotating `desk-relay.log` (1 MiB x 2) with every thread's uncaught exceptions, the launch log (`bin/desk-relay` points stderr at it before any package import, truncated over 256 KiB; closed to /dev/null once logging runs), `serve`: the channel socket and `RelayConnector` until SIGTERM, then the link retired and the socket closed |
| `llm_switchboard/desk/relay_launchd.py` | Mac relay: the launchd job `dev.llm-switchboard.desk-relay` (the plist from `DeskConfig.relay_argv`, output to /dev/null), `desk relay-install` (checks first: macOS, the link key, the checkout's interpreter; v1's `host_launchd.bootstrap`) and `--unload` (bootout of its label only, the plist deleted, done only once `launchctl print` no longer finds the label and `relay.lock` is free, else exit 1 after 5 s); every launchctl call and the LaunchAgents directory behind `Launchd`, which tests replace |
| `llm_switchboard/desk/checkout_build.py` | Mac relay: the hello's `relay_build`, the checkout's HEAD as 12 hex digits read from git's files (worktrees and packed refs included), never by running git; '' when unknown |
| `llm_switchboard/desk/mac_machine.py` | Mac relay: the link hello's `machine`, a SHA-256 derivation of the hardware UUID (`gethostuuid` through `ctypes`, written as `ioreg` shows it) so the raw UUID never leaves the Mac |
| `llm_switchboard/desk/rate_log.py` | At most one log line a minute per key (the relay's repeated codes and refusals) |
| `llm_switchboard/desk/session_identity.py` | Shared session evidence: the bounded registry file reader (optionally requiring the opened file to postdate process start) and the native installer executable path rule |
| `llm_switchboard/desk/peer_credentials.py` | A Unix socket peer's uid on Linux (`SO_PEERCRED`) and macOS (`LOCAL_PEERCRED`), for the Mac link's sockets |
| `llm_switchboard/desk/static/conversation_vocabulary.js`, `conversation_logic.js`, `conversation_answers.js`, `conversation_timers.js`, `conversation_fetches.js`, `conversation_recognition.js`, `conversation.js`, `conversation_view.js` | Conversation mode on the tablet page: its vocabulary (phases, cues, end reasons, limits, Start's blockers); the pure reducer, which re-exports it; what the hub's answers to its calls establish; its timers, each limit kept as the time it is due, and the one order an outside event is applied in (one that silences before any overdue timer); the controller's fetches, their kinds and own limits, those in flight and the answers an end still listens for; the conversation's one speech recognizer; the controller that owns every mic, sound and send effect, and the gate every status line the hub sends reaches; the Start control and overlay (render only). Node tests in `tests/js/` |
| `llm_switchboard/desk/static/sound_tracker.js` | The tablet page's own sound as far as it can be known: every utterance it submitted until its end, error or the page's own cancel, when the last one ended, uncertain periods and the overdue watchdog; owned by the conversation controller |
| `llm_switchboard/desk/static/hub_api.js` | The tablet page's one way to the hub: the `api` every call of the page goes through, with the status line reported as it arrives |
| `llm_switchboard/desk/static/desk_hosts.js`, `link_view.js` | The tablet page's Mac link: the words for host badges, the link and unattributed refusal lines, the Mac hint, the host choice and which enrollment route an edit takes (pure, Node-tested in `tests/js/desk_*.test.js`); the link and refusal lines and the host badge (render only) |
| `llm_switchboard/desk/static/enroll_view.js` | The tablet page's enrollment: the settings list (host badge, enroll or unenroll per host), the typed form's host choice, the Mac sessions asking to join and the Enroll now confirmation every Mac enroll waits for; each edit through `api`, keyed by (host, session_id) |
| `llm_switchboard/desk/static/page_audio.js` | The tablet page's tap-flow sound: automatic reply speech, Play and one-shot Dictate, tracked by and deferring to the conversation controller |
| `llm_switchboard/desk/cli.py` | `llm-switchboard desk <command>`: serve, pair, revoke, status, clear, clear-stopped (no offline form), link-authorize, link-revoke and link-fence (their no-hub outcomes under the run lock), sends, voice, enroll and unenroll (`--host` for a Mac session), ack, flags, install-config, relay (Mac, in `desk/relay_cli.py`, which imports the relay only when it runs) (a light command in `__main__`) |
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
| `tests/relay_fixture.py` | The Mac relay's surroundings for tests: an isolated Mac home with a fake libproc reader (`MacHome`, `FakeKernel`) and the hub's end of a link as the relay's channel socket sees it (`FakeUplink`) |
| `tests/link_peer.py` | The relay's half of the Mac link for tests (`LinkPeer`, throwaway `KeyPair`s, a fake verifier, `LinkHubCase`), written from `link_protocol` alone; reused by the Mac stage |
| `systemd/`, `install.sh` | User unit template and installer |
| `scripts/setup-mac.sh` | Mac setup: key, `host-authorize` over SSH, one push, `.venv`, the `llm-switchboard` link, launchd jobs |
| `scripts/check_layout.py`, `scripts/backend_layout/` | Repo-owned read-only layout checker; `.codeshape/config.json` declares its scope |
| `tests/test_settings.py`, `web/src/settings/settingsFields.test.ts` | Settings persistence, CLI, manual-launch isolation, and page field/polling contracts |
| `tests/test_architecture.py` | Enforced pure-owner imports, Mac/Pi separation, rendered systemd and launchd targets, checkout wrapper |

## Contracts

| Path | Caller | Owner |
| --- | --- | --- |
| `bin/llm-switchboard` | Pi systemd, installed CLI link, SSH forced command (runs `.venv/bin/python` when present) | `llm_switchboard/__main__.py` |
| `python3 -m llm_switchboard` | Mac launchd (host agent, search index, loopback search), Mac setup, search index child, shell callers | `llm_switchboard/__main__.py` (light host/search dispatch), `cli.py` |
| `systemd/llm-switchboard.service` | `install.sh` template input | `install.sh` |
| `install.sh` | Pi operator installation | `systemd/llm-switchboard.service` and `bin/llm-switchboard` |
| `scripts/setup-mac.sh` | Mac operator setup | `llm_switchboard/host_cli.py`, `host_launchd.py` |
| `scripts/check_layout.py` | Developer checks and unittest bridge | `scripts/backend_layout/cli.py` |
| `python -m llm_switchboard openapi` | `web/` type generation (`npm run gen:api`) and the stale-types test | `llm_switchboard/cli.py`, `web.py` |
| `/`, `/index.html`, `/assets/*`, `/theme.js` | Picker browsers; extensionless client routes also use the index | `llm_switchboard/page.py`, `web/dist` |
| `/next`, `/next/*` | Former preview bookmarks; permanent redirects to the matching root URL | `llm_switchboard/page.py` |
| `python -m scripts.api_parity` | Developer HTTP parity checks | `scripts/api_parity/__main__.py` |
| `bin/desk-channel` | Claude Code, through the pinned `mcp.json` that `desk install-config` writes; its argv is pinned by `DeskConfig.channel_argv` and checked by `desk/peer.py`, so moving it breaks enrolled sessions | `llm_switchboard/desk/channel.py` |
| `bin/desk-link` | sshd, through the `authorized_keys` forced command `desk link-authorize` writes; its argv is pinned by `DeskConfig.link_argv`, so moving it breaks the link | `llm_switchboard/desk/link_pump.py` |
| `~/.ssh/llm-switchboard-desk-<host>` (Mac) | The relay and Mac setup; the only identity the desk SSH invocation permits (`relay_ssh` names it in the ssh argv) | `llm_switchboard/desk/relay_link.py` (signs the link proof with it) |
| `config_dir/run/link.sock` (Pi) | `bin/desk-link` (`DeskConfig.link_socket`); 0600 in the 0700 run directory, created by `desk serve` at start | `llm_switchboard/desk/link_hub.py` |
| `config_dir/run/channel.sock` (Mac) | Mac channel servers (`DeskConfig.channel_socket`, the same path the phase 1 channel uses on the Pi); 0600 in the 0700 run directory, bound by the relay | `llm_switchboard/desk/relay_channels.py` |
| `bin/desk-relay` | launchd `dev.llm-switchboard.desk-relay` (`DeskConfig.relay_argv`); moving it breaks the link | `llm_switchboard/desk/relay.py` |
| launchd `dev.llm-switchboard.desk-relay` (Mac) | launchd, from `~/Library/LaunchAgents/dev.llm-switchboard.desk-relay.plist` that `desk relay-install` writes and `--unload` deletes | `llm_switchboard/desk/relay_launchd.py` |
| `config_dir/run/relay.lock` (Mac) | The relay (held for its life); `desk relay-install --unload`'s confirmation probes it | `llm_switchboard/desk/relay.py`, through `desk/run_lock.py` |
| `config_dir/run/hub.lock` (Pi) | `desk serve` (`cli._serve` takes it before constructing the hub and holds it until the process exits; `Hub.serve` requires it); the Mac link's offline decisions | `llm_switchboard/desk/hub.py`, through `desk/run_lock.py` |
| `config_dir/enrolled-hosts.json` (Pi) | The hub's `refresh_files`, the enroll and unenroll edits (hub and offline CLI), the picker's Mac rows (`web_app.py`); read and written only under `enrolled.json`'s lock | `llm_switchboard/desk/remote_enrollment.py` |
| Operator ops `enroll_host`, `unenroll_host`, `clear_stopped` | `llm-switchboard desk enroll --host`, `unenroll --host`, `clear-stopped` (a phase 1 hub answers `unknown op`) | `llm_switchboard/desk/hub_enrollment.py` |
| Operator ops `link_drop` (answer `{readable, signer, link}`), `link_fence` | `llm-switchboard desk link-authorize`, `link-revoke`, `link-fence` (a phase 1 hub answers `unknown op`); never the page | `llm_switchboard/desk/hub_link_ops.py` |
| `config_dir/link-signers` (Pi) | The hub's verifier and signer check (`link_proof.read_signers`); written whole only by `desk link-authorize` and `link-revoke` | `llm_switchboard/desk/link_authorize.py` |
| `authorized_keys` marker `llm-switchboard-desk-link:<host>` | `desk link-authorize`, `link-revoke` (v1's `host-authorize` never touches it) | `llm_switchboard/desk/link_authorize.py` |
| `llm-switchboard desk link-authorize --self-test`, `link-authorize <host>` (key on stdin), `link-revoke <host>` | The Mac's desk setup over ssh; the operator | `llm_switchboard/desk/link_authorize.py` |
| `POST /desk/api/enroll-host`, `/desk/api/unenroll-host` | The desk page (a phase 1 hub answers 404); `/desk/api/enroll` and `/unenroll` refuse a `host` key | `llm_switchboard/desk/hub_enrollment.py`, `desk/http_server.py` |
| `bin/desk-adb-link` | `systemd/llm-switchboard-desk-adb.service` | `llm_switchboard/desk/` (see `docs/voice-desk.md`) |
| `systemd/llm-switchboard-desk.service`, `systemd/llm-switchboard-desk-adb.service` | `install.sh` template inputs | `install.sh` |
| `scripts/desk-ca.sh` | Mac operator, one-off (the desk's local CA) | `docs/voice-desk.md` |
| `scripts/desk-units.sh <repo> <local.env>` | `install.sh` (reconciles the desk hub and adb link units) | `scripts/desk-units.sh` itself; `docs/voice-desk.md` |
| `llm-switchboard desk ...` | Operator CLI and the hub unit (`desk serve`) | `llm_switchboard/desk/cli.py` |

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
  queued may start a process or send a prompt there. A transcript is uploaded to
  the Pi only through `fork-pi` (mac_uploads.py). Search text may reach the Mac's
  own browser through `host-search` on loopback, never the Pi: the HTTPS picker
  fetches it directly with CORS restricted by `LLM_SWITCHBOARD_PAGE_ORIGINS` in
  the Mac's `local.env` (comma-separated full origins; empty allows none).
  The HTTPS picker is Tailscale Serve on the Pi (`sudo tailscale serve --bg
  8777`, persisted by tailscaled; `tailscale serve status`), proxying to
  127.0.0.1:8777. Chrome blocks loopback fetches from plain-http pages, so the
  `http://…:8777` address keeps working but never shows Mac text. The Pi doesn't
  use Tailscale DNS, so test it with `curl --resolve <name>:443:<tailscale ip>`.
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
  (`search_process.py`) on the Pi and in the `dev.llm-switchboard.search-index`
  launchd job (`index --quiet`, background priority) on the Mac, never in the
  supervisor or a request thread; after changing what is extracted, rebuild:
  stop the indexer (the service; on the Mac, `launchctl bootout` the job, and
  `host-install` loads it again) and delete `transcript-search.sqlite3*`. Each machine indexes and searches only
  its own transcripts: the Mac's index, its text and its search results never
  go to the Pi. The Pi matches Mac sessions by title and folder only
  (`remote_title_matches` in search_api.py); the HTTPS page on the Mac can also
  read its local `host-search` endpoint directly for full-text results.
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
