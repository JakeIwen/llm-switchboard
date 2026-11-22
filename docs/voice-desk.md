# Voice desk (phase 1: the Pi)

The desk lets Jacob send a prompt to an enrolled Claude session from a tablet
page and read (and hear) its reply. The tablet reaches the Pi only over a USB
`adb reverse` forward to `https://localhost:8796`. Each enrolled Claude session
runs one MCP **channel server** that connects to the **hub** over a Unix
socket; the hub pushes desk events into the session and the session answers
through the channel's `reply` tool.

Phase 2 (the Mac relay) and phase 3 (a "prime" orchestrator) are not built.
Production sends are off (`desk sends off`, the default) until a live tablet
round trip passes.

## Modules

| Module | Role | Concern |
| --- | --- | --- |
| `desk/config.py` | model | `DeskConfig`: paths, port 8796, pinned channel argv and launch flags |
| `desk/protocol.py` | model | Channel socket framing and messages; the delivery suffix and channel instructions |
| `desk/text_rule.py` | rules | The prompt text rule (`check_text`, `normalize_text`); mirrored by `static/text_rule.js` over `text_rule_cases.json` |
| `desk/model.py` | model | `RequestState`, `Registration`, request/ticket/token/pairing records, limits |
| `desk/hub_state.py` | rules | The hub's in-memory state machine; clock and randomness injected, no I/O |
| `desk/locks.py` | adapter | The target lock file (fsync before handoff; refuses on unreadable, oversize, malformed or full) |
| `desk/enrollment.py` | adapter | `enrolled.json` (JsonFile) and `desk-settings.json` (`sends_enabled`) |
| `desk/labels.py` | adapter | Target labels from a bounded, cached read of each transcript's head and tail |
| `desk/peer.py` | rules + adapter | Channel identity: peer PID from the socket, `/proc` walk, session-file check and recheck; `process_presence` (whether a pid and start time still run), shared with locks and the startup acknowledgement |
| `desk/startup_identity.py` | rules | Launch identity for the startup acknowledgement: `StartupRefusal` (reasons and statuses), argv, foreground and session-file rules, the view digest |
| `desk/startup_ack.py` | adapter | The startup acknowledgement: reads the supervisor's tmux panes and `/proc`, lists blocked sessions, sends the one guarded Enter; one Enter per view; rate limits |
| `desk/startup_registry.py` | adapter | The bounded, non-blocking read of claude's `sessions/<pid>.json` for the acknowledgement (absent vs. unreadable) |
| `desk/channel.py` | runtime | The channel server: MCP over stdio plus the hub socket client |
| `desk/hub.py` | runtime | Wires the state, the channel socket, the operator socket and HTTPS |
| `desk/http_server.py` | adapter | HTTPS (stdlib `ssl` + `ThreadingHTTPServer`), Host/Origin/token guards, long-poll |
| `desk/operator.py` | adapter | The 0600 operator socket and its client (`pair`, `revoke`, `sends`, `status`, `clear`, `enroll`, `unenroll`, `startup_view`, `startup_ack`) |
| `desk/cli.py` | entry | `llm-switchboard desk serve|pair|revoke|status|enroll|unenroll|sends|ack|flags|install-config` |
| `launcher.py`, `supervisor.py` | runtime | Start a kept Pi session through `claudepd`, adding desk flags only if enrolled at launch |
| `screen.py`, `supervisor_state.py` | rules | Identify the complete development-channel dialog and its selected option, and report `startup_blocked` without restarting the session |
| `tmux.py` | adapter | `Tmux.panes()` (pane id, pid, copy mode, by `@car_session`; raises when tmux fails rather than reading as no panes), `capture(..., check=True)`, and `send_enter_if(pane_id, PaneGuard)`, the server-side guarded Enter the acknowledgement uses |
| `desk/static/` | page | The tablet page: plain HTML/CSS/JS, no build step, Chrome 119; `startup.js` is the startup-blocked view |
| `bin/desk-channel` | entry | Pinned channel server entry (`.venv/bin/python -I -B bin/desk-channel`) |
| `bin/desk-adb-link` | entry | The USB reverse-forward loop for one tablet serial |
| `systemd/llm-switchboard-desk.service`, `systemd/llm-switchboard-desk-adb.service` | unit | Hub and adb link user units (templated by `install.sh`) |
| `scripts/desk-ca.sh` | entry (Mac) | One-off: single-use CA, signs the leaf, destroys the CA key |

Everything under `desk/` is standard library only and never imports the HTTP
stack (FastAPI and friends), `web.py` or `cli.py`. `desk` is a light command in
`__main__.py`.

## Files

All 0600 in 0700 directories.

| Path (`DeskConfig`) | Content | Bound |
| --- | --- | --- |
| `config_dir/enrolled.json` | `{"sessions": {<session id>: {"since": <unix s>}}}` and nothing else; an unreadable file, or any key or record of another shape (a `null` record, an extra field), makes the whole file malformed: it reads as nobody enrolled, and enroll/unenroll refuse (503) to edit it | 64 sessions |
| `config_dir/desk-settings.json` | `{"sends_enabled": false}` | fixed |
| `config_dir/mcp.json` | The pinned MCP config (`desk install-config` writes it) | fixed |
| `config_dir/tls/leaf.pem`, `leaf.key` | The hub's TLS leaf (from `scripts/desk-ca.sh`) | fixed |
| `state_dir/locks.json` | Target locks, no prompt or reply content | 64 entries, 64 KiB |
| `runtime_dir/channel.sock`, `operator.sock` | Unix sockets | n/a |

The hub and these files never hold prompt or reply text on disk, and the
hub's logs carry ids and reasons only: a request id that came from a channel
is logged only when it has the hub-issued shape (`<epoch hex>.<16 hex>`),
otherwise as `<invalid id>`. That promise covers the desk's own code and
files, not Claude: the enrolled session's transcript records each desk event
(the prompt plus the delivery suffix) and its `reply` tool call (text and
spoken) like any other prompt and tool call, under Claude Code's own
retention.

## Hub state

- **Epoch**: 16 random bytes (hex) made at every hub start. Generations,
  tickets and request IDs carry it; anything from another epoch is refused.
- **Registration**: one per verified channel connection, keyed by session
  id, with a hub-issued generation (an increasing integer within the epoch).
  Two live connections claiming one session make it **ambiguous**: both are
  refused routing until one disconnects. Disconnect drops the registration.
- **Recheck** before each dispatch and every 5 s: the channel peer is still
  the same process, its parent still has the same start time, the parent's
  `sessions/<pid>.json` still names the registered session id, and the
  session is still enrolled. A failed recheck drops the registration (so
  `/clear`, resume and unenroll stop routing).
- **Tickets**: one-use, bound to `(epoch, session_id, generation)`, expire
  after 10 minutes, at most 32 live or consumed; a consumed ticket's record is
  kept until its expiry. A full table refuses new tickets.
- **Requests**: `pending` (accepted) -> `handed_off` (the lock is persisted
  and the state recorded *before* the event is written) -> `answered`, or
  -> `unknown` (after handoff: the event write failed, the connection
  dropped, the recheck failed, the channel answered `undeliverable`, or the
  hub restarted). `pending` -> `expired` when a check fails before handoff
  (nothing was written; safe to send again). Handoff rechecks, under the
  state's lock, everything the send was accepted on: the sending token is
  still valid, sends are on, the lock store is usable, the session is still
  enrolled (the hub rereads the files first) and its registration is the
  same single one. Reading the enrollment and settings files and publishing
  them into the state happen under one hub lock, which `desk sends` and the
  page's enroll/unenroll also hold while they write, so a slow read can't
  publish a stale value over a newer change; the CLI's `desk enroll` and
  `desk unenroll` go through the same lock on the operator socket while the
  hub runs, so an unenroll that has returned is never overtaken by a
  handoff that read the file before it. Every edit of either file, the
  CLI's offline one included, holds that file's cross-process lock (the
  `<name>.lock` flock `JsonFile` takes) for its whole read-modify-write, and
  the hub holds both files' locks from rereading them through persisting
  the target lock, so an offline edit is either seen by the handoff or
  waits for it (a hub starting meanwhile rereads under the same locks).
  Lock order, everywhere: `enrolled.json`'s lock, then
  `desk-settings.json`'s, then the hub's policy lock, then the state's
  lock; within the hub the file locks re-enter. The startup
  acknowledgement takes its own non-waiting lock before all of these and
  then only `enrolled.json`'s, and the state's lock only briefly inside
  `token_in_use` (see "Startup acknowledgement"). Any failure before handoff,
  expected or not, expires the pending request. One outstanding request per target: a target is **busy**
  while it has a lock. Answered, expired and unknown requests, with their
  reply text, are dropped 24 h after they finished (and beyond the last 20);
  a request still `handed_off` is kept, and no lock is ever aged out.
- **Delivery boundary**: the channel makes its own last check (its parent
  still runs the registered session) just before writing the event to
  Claude's stdin and answers `undeliverable {request_id, reason}` instead
  when it fails; the hub then makes the request unknown (lock kept) and
  rechecks that connection at once. What stays non-atomic: Claude can change
  sessions (`/clear`, resume) between that check and the pipe write, or
  after the write and before reading it, and the 5 s recheck can't retract
  an event already written. The lock and Jacob's explicit clear cover that
  window: a request in doubt stays unknown and the target busy.
- **Target lock** (`locks.json`): `{lock_id, host, session_id, request_id,
  ticket_hash, since, channel_pid, channel_start}`: the last two name the
  channel process the event is handed to (its pid and `/proc/<pid>/stat`
  start time; no content). Written completely and fsynced before handoff;
  a registration without a usable pid and start time is never handed an
  event. Cleared by the verified answer, or by Jacob's explicit clear
  naming that `lock_id` (a clear naming an older lock id is refused).
- **Clear rule**: a lock is cleared only on positive evidence that no
  writer can still emit its event. While the connection it was handed to is
  registered and hasn't reported, the delivery is in progress (409
  `delivery in progress`): a socket write only means the channel has the
  event, and it stays in progress until that connection reports `delivered`
  (written to Claude's stdin) or `undeliverable`, or answers. Those reports,
  and an event that was never written (no connection left to write to),
  are the evidence. Without them (the connection went away first: a
  disconnect, a failed recheck, or the 45 s idle timeout of a channel
  blocked writing to Claude, which can't answer heartbeats; the hub's event
  write failed part way; or the lock is from an earlier hub run, whose
  reports this run never saw) the request is unknown, never a success, and
  the hub reads the start time of the lock's `channel_pid` from `/proc`:
  no such process in a mounted process tree (`/proc/self` present; a
  missing or unmounted `/proc` proves nothing), or one with another start
  time (a reused pid), allows
  the clear; the same start time refuses it with 409 `the session's channel
  is still running; stop the session, then clear`; an entry whose start
  time can't be read or parsed refuses it with 503 `the session's channel
  can't be checked; stop the session, then clear`. A session that kept
  running across a hub restart therefore has to be stopped before its
  earlier run's lock clears.
- **Locks survive hub restarts**: a locked target stays
  busy in the new epoch, shown as `unknown (earlier hub run)`, until cleared.
  An unreadable, oversize (over 64 KiB), corrupt, full or unwritable lock
  file, or any entry with a malformed field (lock id not 16 hex, unknown
  host, session id not a session id, request id not the hub-issued shape,
  ticket hash not SHA-256 hex, `since` not a finite positive number,
  `channel_pid` not a positive integer, `channel_start` not decimal digits,
  extra or missing keys), refuses every send and shows a lock-store error.
- **Replies** count only from the connection the request was handed to,
  while it is `handed_off`, with its request id, under the size caps, as
  strict UTF-8 (a lone surrogate is refused before anything changes), and
  only after that connection passes the identity recheck again: after
  `/clear` the reply is refused (`session changed`), the connection dropped
  and the request left unknown with its lock. The
  first valid one wins; late, duplicate, oversize or mismatched replies are
  dropped and logged without content. A ring keeps the last 20 answered
  replies (and at most 256 KiB of text) in memory only.
- **Replied**: a registration shows `replied` once a request handed to it
  has been answered through `reply` on this connection. It is history, not
  readiness: the hub can't see Claude's current model or whether it will
  answer the next one. A new registration starts `registered`.
- **Pairing**: `pair_start(secret_hash)` returns `pair_id` and a 6-character
  code (Crockford base32, shown as `XXX-XXX`), expiring after 5 minutes, at
  most 3 pending, at most 10 starts a minute. The operator's `desk pair
  <code>` confirms it. `pair_claim(pair_id, secret)` hands out the token once,
  only when `sha256(secret) == secret_hash` and it is confirmed; 5 wrong
  secrets kill the pairing.
- **Tokens**: 32 random bytes, base64url; the hub keeps only their SHA-256.
  Expire after 7 days, at most 4 (pairing a fifth drops the oldest). Each has a
  short id for `desk status` and `desk revoke --token <id>`. A revoked or
  dropped token's long-polls return 401 at once. A revoke deletes the
  tokens, then waits (at most `STARTUP_REVOKE_WAIT_SECONDS`, 8 s) for every
  startup acknowledgement still running on a matching token that is no
  longer valid, including one whose token an earlier revoke deleted (so a
  second revoke during the first one's wait waits too). In-flight
  acknowledgements are counted apart from the valid tokens for this. Only a
  revoke whose wait finished reports success, and then no Enter can follow
  on those tokens; when the wait runs out it answers `{"ok": false, "error":
  "revoked for new requests; an acknowledgement is still in progress",
  "revoked": <n>, "in_progress": true}` (`desk revoke` exits 4). There is no
  HTTP revoke route.

## Text rule

`check_text(text)` returns `None` when the hub accepts `text`, else a reason.
Accepted text is non-empty, at most 4096 UTF-8 bytes, has no leading or
trailing whitespace, contains no control character other than `\n` and `\t`
(no C0, DEL or C1), no bidi control (U+202A..U+202E, U+2066..U+2069), no
U+2028/U+2029, and does not start with `/`, `+` or `!`. `normalize_text(text)`
is what the page applies before review: `\r\n` and `\r` to `\n`, then strip.
The hub never normalizes; it refuses text that `check_text` rejects. The
shared cases in `desk/text_rule_cases.json` run through both implementations.

## HTTPS API

Served only over TLS on `127.0.0.1:<port>`. Every request needs `Host:
localhost:<port>` exactly. Every response carries `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` (not
`no-referrer`: under that policy a browser sends `Origin: null` on the page's
own POSTs, which the exact Origin guard refuses; `same-origin` keeps the
Origin and sends no referrer anywhere else) and
`Content-Security-Policy: default-src 'self'; frame-ancestors 'none';
base-uri 'none'; form-action 'none'`. No CORS headers, ever. POST bodies are
JSON (`Content-Type: application/json`), at most 16 KiB, and need `Origin:
https://localhost:<port>` exactly (`null` is refused). Errors are
`{"error": "<reason>"}` with 400 (bad input), 401 (no or bad token), 403
(refused: sends off, wrong origin), 404, 409 (state conflict: busy, stale
ticket or generation, not enrolled, delivery in progress, the session's
channel still running, a startup acknowledgement whose checks fail), 413,
429 (rate limit, or another startup acknowledgement in progress) or 503
(lock store error, enrollment file unreadable, the session's channel or
process can't be checked, tmux didn't answer). A 503 whose body also has
`"delivery": "unknown"` (only from the startup acknowledgement) means tmux
may have typed the key; every other error means nothing was typed.

Unauthenticated:
- `GET /desk`, `/desk/app.js`, `/desk/app.css`, `/desk/text_rule.js`,
  `/desk/desk_logic.js`, `/desk/text_rule_cases.json`: the page.
- `POST /desk/pair/start {secret_hash}` -> `{pair_id, code, expires_in}`.
- `POST /desk/pair/claim {pair_id, secret}` -> `{token, expires_at}`; 409
  `{"error": "not confirmed"}` while waiting (the page polls every 2 s).

Protected (`Authorization: Bearer <token>`; checked before the body is read
and again inside the state's lock with each mutation, so a token revoked
while its request was in flight changes nothing):
- `GET /desk/api/state?after=<version>`: returns at once when the state
  version differs from `after` (or `after` is absent), else holds up to 25 s
  and returns the unchanged state. Body:
  `{version, epoch, sends_enabled, lock_store_error, targets: [Target], requests: [Request]}`.
  `Target = {host: "pi", session_id, label, cwd, enrolled, registration:
  "replied"|"registered"|"none"|"ambiguous", generation, lock: {lock_id,
  request_id, since, earlier_run} | null}`. `label` is the session's title
  from its transcript, else its folder name, else the id's first 8
  characters; the hub works it out in its 5 s loop and on registration from
  the transcript's first 64 KiB and last 256 KiB, rereading only when the
  file's inode, size or mtime changed, never while answering this request
  (a custom title only in the middle of a larger file is missed; a record
  of an unexpected shape is skipped, and a transcript that can't be read at
  all labels its target by the id, without affecting startup or the
  channel). Targets
  are the enrolled
  sessions plus any live registration. `Request = {request_id, session_id,
  state, text, created, reply: {text, spoken} | null}` for the last 20.
- `POST /desk/api/ticket {session_id, generation, epoch}` -> `{ticket, expires_at}`.
- `POST /desk/api/send {ticket, text}` -> `{request_id, state}`.
- `POST /desk/api/clear {session_id, lock_id}` -> `{cleared: true}`.
- `POST /desk/api/enroll {session_id}`, `POST /desk/api/unenroll {session_id}`.
- `GET /desk/api/startup` -> `{sessions: [Blocked]}`: the enrolled sessions
  whose supervisor pane shows the development-channel warning. `Blocked =
  {session_id, capture, digest, refusal}`: `capture` is the pane's text
  (only text the exact warning matcher accepted, so at most 16 KiB and no
  control characters; the page shows it through `textContent`), `digest`
  is the value to acknowledge it with, or `null` with `refusal` naming
  the failed check (including **an Enter was already sent for this view**,
  or **already attempted** when tmux never confirmed it).
  Each pane's screen is read first; only one showing the warning gets the
  process checks. A tmux that fails (a socket it may not open, an error
  other than "no server") answers 503 rather than an empty list. At most 60
  views a minute across all callers (429).
- `POST /desk/api/startup/ack {session_id, digest}` -> `{session_id, sent:
  true}`: presses Enter once in that session's pane (see "Startup
  acknowledgement"). At most 6 a minute, one at a time (429), and at most
  one per view: a digest that already had an attempt is refused (409) even
  when the outcome was unknown, and a full attempt table refuses (503).
  When tmux doesn't confirm the key the answer is 503 `{"error": "...",
  "delivery": "unknown"}`. It sends no
  prompt, so it does not depend on `sends on`, and works while sends are
  off. The page tells the three outcomes apart: sent, not sent (nothing
  typed), and unknown (check the session), and keeps the outcome shown for
  a minute after the session leaves the list.

## Operator socket and CLI

`llm-switchboard desk <command>` runs as `pi` and talks to the hub over
`operator.sock` (same framing as the channel socket, request `{op, ...}`,
response `{ok, ...}`): `pair <code>`, `revoke [--token <id>]` (all tokens when
no id; exits 4 with **revoked for new requests; an acknowledgement is still
in progress** when its bounded wait for one ran out, and running it again
waits again), `status` (epoch, registrations, locks, token ids and expiry, sends
flag), `clear <session id> <lock id>`, `sends on|off` (persisted in
`desk-settings.json`), `enroll|unenroll <session id>` (the hub edits
`enrolled.json` and publishes it under its policy lock; unenroll drops the
session's registrations at once). `sends`, `enroll` and `unenroll` edit their
file directly only when the connect proves no hub listens (no socket file,
or nothing accepting on it), and the hub reads it at its next recheck or
start. Once connected, a missing, reset, timed-out or garbled answer (or
one without a boolean `ok`) means
the hub may have applied the change: the command writes nothing, prints
``result unknown; run `llm-switchboard desk status` ``, and exits 4 (3: no
hub for the other commands, 1: refused). `ack <session id>` prints the warning screen and its digest
from `startup_view`, asks `Press Enter once in this session to accept the
warning? [y/N]` when stdin is a terminal, and otherwise prints the command
to send it; `ack <session id> --digest <hex>` sends at once with a digest
it printed, from this hub run only (a digest from before a hub restart is
refused). It exits 4 when tmux didn't confirm the Enter. Both go only through the hub (exit 3 without one), with the
same checks as the page. `flags` (prints the launch flags) and
`install-config` (writes `mcp.json`) never need the hub.
`serve` runs the hub. A request line is at most 4 KiB; at most 4 connections
are served at once, and another gets `{"ok": false, "error": "busy"}` at once.

## Channel identity

See `desk/peer.py`. Linux only in phase 1 (`SO_PEERCRED`); macOS
(`LOCAL_PEERPID`) comes with the Mac relay. The peer's `/proc/<pid>/cmdline`
must equal `DeskConfig.channel_argv`, and `/proc/<pid>/exe` must resolve to
the same file as that argv's interpreter. Its parent (ppid, never
`CLAUDE_PID`) must have a `sessions/<ppid>.json` whose `procStart` equals the
parent's start time, whose `sessionId` equals the hello's session id, and the
parent's cmdline must carry `--mcp-config <DeskConfig.mcp_config>` as two
adjacent arguments. The session must be enrolled.

## Tablet link

`bin/desk-adb-link <serial> <port>` polls `adb devices` every
`DESK_ADB_POLL_SECONDS` (default 3), because `wait-for-device` only returns for
state `device` and `get-state` fails the same way for an absent and an
unauthorized serial. A serial that is absent, `unauthorized` or `offline` makes
it wait and log once per change (the tablet may be plugged in or the USB
debugging prompt accepted later); it never exits for these. On `device` it
reads `adb -s <serial> reverse --list`: an identical `tcp:<port> tcp:<port>`
mapping is adopted, a different mapping on that remote port is refused (exit
4); otherwise `adb -s <serial> reverse --no-rebind tcp:<port> tcp:<port>`,
and a failure there is a disconnect when the serial has left the list, else
retried. Then `adb -s <serial> wait-for-disconnect`, and the loop starts over.
It never runs `kill-server`, `am`, `input`, `shell` or `reverse --remove`. The
unit doesn't restart after exit 2 (bad arguments) or 4.

`install.sh` hands the two desk units to `scripts/desk-units.sh`, which
reconciles them on every run. Both units are rendered first (a failed render
or replacement aborts before any reload or restart). The hub is restarted when
active (a rotated leaf or changed unit takes effect), enabled if it was
active but disabled, and stopped and disabled when its leaf or key is missing.
The adb unit is restarted when the serial, `bin/desk-adb-link`'s checksum
(both recorded in `~/.config/llm-switchboard/desk/adb-link.applied`) or its
unit file changed, enabled if active but disabled, and disabled and stopped
when `DESK_ADB_SERIAL` is empty. Each action is printed.

## Enrollment and launch

For a kept Pi session, `llm-switchboard desk enroll <session id>` adds the
pinned flags to its **next launch** through `claudepd`, whether new or resumed.
It does not restart a running session. `desk unenroll <session id>` stops hub
routing immediately; the next launch omits the flags. If enrollment cannot
be read, the supervisor launches normally without desk flags. The prompt-send
switch (`desk sends on|off`) does not control enrollment or launching.

Claude waits at **Loading development channels** before Remote Control starts.
The picker shows **Startup blocked**; expand the row for its attach command
(`llm-switchboard attach <session id>`). The supervisor neither sends Enter
nor restarts the pane while this dialog is on screen. A resize may reflow the
text; a changed or unrecognized dialog is not identified by a substring.
Acknowledge it on the desk page, with `llm-switchboard desk ack <session
id>`, or in the attached terminal.

## Startup acknowledgement

Decided by Jacob (2026-11-22): every launch of an enrolled kept session waits
at the warning until he acknowledges it. The desk shows the captured warning
with an **Acknowledge startup warning** button; tapping it (or confirming
`desk ack`) has the hub press Enter once, only if the screen still matches at
that moment. It is operator-initiated: nothing presses Enter on its own.

**Launch identity, not channel identity.** On Claude Code 2.1.330 the warning
appears before `~/.claude/sessions/<pid>.json` is written (for `--resume` and
`--session-id` alike), so the channel check ("the session file names the
session") can't apply: nothing yet proves which conversation the process
holds. Jacob approved (2026-11-22) this weaker identity for the one Enter,
and accepted its risk: a resume of a bogus or missing id reaches the same
warning, and the Enter then opens that empty or failed session. Every one
must hold (`desk/startup_identity.py`, `desk/startup_ack.py`,
`desk/startup_registry.py`):

- The window is the supervisor's for this session on the dedicated tmux
  server: the one window whose `@car_session` option (set by `Tmux.start`,
  which the supervisor matches its windows by) names it, holding exactly one
  live pane that is not in copy mode. No window title is consulted; two
  tagged windows are refused.
- The pane's foreground process (the terminal's foreground group leader,
  from the pane process's `/proc` stat) is the pane program's only `claude`
  child (`argv[0]` named `claude`), its resolved `/proc/<pid>/exe` is one
  of the native installer's executables (a file directly in
  `~/.local/share/claude/versions/`, `DeskConfig.claude_versions_dir`,
  named for its version; on the Pi `~/.local/bin/claude` links there, and a
  running claude may still be an older version), and its environment
  carries the supervisor's `LLM_SWITCHBOARD_SESSION=<session id>` from
  `tmux new-window -e`. Another program named `claude` is refused.
- Its options (argv up to a `--`; what follows is a prompt) are compared by
  name, never by spelling: `--name=value` sets `--name`, and every letter
  of a single-dash argument counts as a short option (`-cr` is `-c` and
  `-r`, `-r<id>` is `-r`), since Claude Code's parser reads them so.
  They carry `--mcp-config <DeskConfig.mcp_config>` and
  `--dangerously-load-development-channels server:desk` each exactly once
  (no second copy in any spelling), in the bare form with the value as the
  next argument, and exactly one session-choosing option (`--resume`/`-r`,
  `--session-id`, `--continue`/`-c`, `--fork-session`, `--from-pr`,
  `--teleport`, `--cloud`, from 2.1.330's `--help`), which is `--resume` or
  `--session-id` in the bare form followed by exactly this id (never a
  substring, never `=`).
- `sessions/<pid>.json`, if it exists, is readable and names this session; a
  missing file is expected at this stage. Only a definitive "no such file"
  counts as missing: the file is opened once, without following a link and
  without blocking, must be a regular file of at most 64 KiB, and anything
  else (permission denied, a FIFO, a link, oversize) refuses with 503.
  Nothing writes or fakes one.
- The session is enrolled (read fresh, under `enrolled.json`'s lock); the
  screen passes the exact warning matcher with option 1 (**I am using this
  for local development**) selected, never **Exit**; and the digest equals
  the one the page or CLI was shown. The digest is SHA-256 over the capture
  together with the window id, pane id, pane pid and start time, claude's
  pid and start time, and the hub's epoch, so a pane respawned with an
  identical screen needs a fresh view, and a digest from before a hub
  restart never matches. The supervisor's `startup_blocked` is this same
  matcher over its own capture; the hub doesn't read the supervisor's
  memory.

**The order of an acknowledgement.**

1. All the checks above, with the screen read between two reads of the
   pane and the process (a change during it is a refusal), and the digest
   compared with that view. A digest that already had an attempt in this
   hub run is refused: **an Enter was already sent for this view** when
   tmux confirmed that attempt's key, else **an Enter was already attempted
   for this view and its delivery is unknown; don't retry**. Then room in
   the attempt table (below), whose `/proc` reads stay out of the token
   hold.
2. The tablet token, checked again and held (`token_in_use`): a revoke
   from here on waits until step 5 is over.
3. The attempt is recorded (digest, pane, process and epoch), before any key.
4. The final capture (`tmux capture-pane -p -J -t <pane id>`), compared
   with the digest. Nothing else runs between it and step 5.
5. One tmux command presses the key only if tmux's own guard holds:
   `tmux if-shell -t <pane id> -F '<guard>' 'send-keys -t <pane id> Enter ;
   display-message -p desk-enter-sent' 'display-message -p
   desk-enter-refused'`. The guard is a format condition (no shell) over the
   pane as tmux sees it at that moment: its pane id, pane pid, window id and
   `@car_session` tag equal the checked ones, its window has one pane, it is
   not dead, not in a mode and takes input, and its visible rows contain
   the warning's title, `Channels: server:desk`, `❯ 1. I am using this for
   local development` and the footer, and not `❯ 2. Exit` (tmux's `#{C:}`
   pane search). tmux evaluates the guard and runs its branch in the same
   pass of its command queue, so no pane output is processed between the
   test and the key. One key, never text.

A failure at steps 1 to 4, or a false guard, typed nothing: it is a 409
with its reason (503 when a process tree, the session file or tmux can't be
read), and the attempt is forgotten. Once tmux has the step-5 command, an
answer that isn't one of the two markers (a timeout, an error) is 503
**tmux did not confirm the Enter; it may have been delivered, so check the
session** with `"delivery": "unknown"`; the attempt stays recorded, so that
view gets no second Enter even then. A fresh, different view (a redraw, a
respawn, or the same screen after a hub restart) may be acknowledged again.
An attempt record is removed only on proof: nothing was typed for certain
(above), or its claude process is proven gone (no `/proc/<pid>` entry in a
mounted tree, or another start time, `peer.process_presence`). An
unreadable process keeps its record. Records are never evicted: when 1024
are held for processes not proven gone, a new acknowledgement is refused
with 503 (**the hub is tracking too many earlier acknowledgements**);
restarting the hub clears them, since its new epoch invalidates every old
digest.

**Who sends, and the lock order.** The hub sends, over the dedicated tmux
socket (the supervisor's, `LLM_SWITCHBOARD_TMUX_SOCKET` or
`llm-switchboard`). The supervisor runs in the picker's process with no
authenticated path from the hub, and keys typed into a supervisor window
from outside its loop already have a precedent (the picker types
`/rename`); only the supervisor starts or kills windows. That is also why
the hub unit has no `PrivateTmp`: it reaches the socket under
`/tmp/tmux-<uid>/`. An acknowledgement takes its own lock without waiting
(429 while another runs), then `enrolled.json`'s lock, which it holds across
its tmux calls and `/proc` reads so an unenroll that has returned is never
overtaken by an Enter that read the enrollment before it. It never takes the
policy lock, and takes the state's mutex only briefly to enter and leave
`token_in_use`, never across a tmux call or a lock wait; a revoke waits on
the state's condition (releasing the mutex) instead. Each tmux call times
out after 3 s, which bounds how long the 5 s recheck and a send's handoff
wait on that file lock, and the token stays in use across two tmux calls at
most. Having the supervisor send while the hub held its state mutex across
the supervisor's enrollment-lock wait would deadlock with housekeeping
(which takes the file lock first); this order has no cycle.

**What stays non-atomic (best-effort, Jacob's decision of 2026-11-22).**
tmux can only test its own copy of the screen. If Jacob, attached to the
pane, presses Enter himself, or Remote Control types into it, just before
the desk's key, and Claude hasn't processed that key yet, the warning is
still on tmux's screen, the guard passes, and the desk's Enter lands on the
next screen: normally Claude's empty prompt, where an empty Enter does
nothing. Between the final capture (step 4) and the guarded send (step 5)
is one tmux round trip; in it the guard covers the pane and the warning's
rows, while the claude process inside the pane was checked at step 1 (a
claude replaced inside the same pane process without the pane changing
would not be seen; the supervisor's pane program runs claude once). Jacob
accepted this remaining race in exchange for at most one Enter per view and
honest reporting of unknown outcomes. The hub logs session ids and refusal
reasons only, never screen content.

Hand-started sessions still use `claudepd $(llm-switchboard desk flags)
--resume <session id>` (default Pi paths); they are not adopted into a
supervisor tmux window. Acknowledge in the terminal that launched them.
