# Voice desk (phase 1: the Pi)

The desk lets Jacob send a prompt to an enrolled Claude session from a tablet
page and read (and hear) its reply. The tablet reaches the Pi only over a USB
`adb reverse` forward to `https://localhost:8796`. Each enrolled Claude session
runs one MCP **channel server** that connects to the **hub** over a Unix
socket; the hub pushes desk events into the session and the session answers
through the channel's `reply` tool.

Beside the reviewed tap, the page offers **conversation mode** (below): one
tap starts a hands-free conversation with one session, sent on a spoken
"send it" and answered aloud.

Phase 2 (the Mac relay) and phase 3 (a "prime" orchestrator) are not built.
Production sends are off (`desk sends off`, the default) until a live tablet
round trip passes, and conversations are off (`desk voice off`, the default)
until the voice readiness check (in "Conversation mode") passes. Stage 1 of
conversation mode has not been qualified for driving.

## Modules

| Module | Role | Concern |
| --- | --- | --- |
| `desk/config.py` | model | `DeskConfig`: paths, port 8796, pinned channel argv and launch flags |
| `desk/protocol.py` | model | Channel socket framing and messages; the delivery suffix and channel instructions |
| `desk/text_rule.py` | rules | The prompt text rule (`check_text`, `normalize_text`); mirrored by `static/text_rule.js` over `text_rule_cases.json` |
| `desk/model.py` | model | `RequestState`, `Registration`, request/ticket/token/pairing records, limits |
| `desk/refusals.py` | model | `Refusal`: every reason the hub's HTTPS API and operator socket refuse with (hub, state, grants, HTTP layer) and the text rule's reasons, declared once, each with its `Ownership` class (whether the conversation page may speak its closing cue after it) and `OWNERSHIP_KEPT`, and its `Delivery` class (whether a send refused with it was certainly not handed off) and `NOT_SENT`; `tests/test_desk_refusals.py` fails on a refusal raised in those modules without it. The startup acknowledgement keeps its own `StartupRefusal` |
| `desk/hub_state.py` | rules | The hub's in-memory state machine; clock and randomness injected, no I/O |
| `desk/locks.py` | adapter | The target lock file (fsync before handoff; refuses on unreadable, oversize, malformed or full) |
| `desk/enrollment.py` | adapter | `enrolled.json` and `desk-settings.json` (`sends_enabled`, `voice_confirm_enabled`): read through `bounded_read`, written and locked through `JsonFile` |
| `desk/grants.py` | rules | Conversation grants (a `HubState` mixin): the one-page rule, grant, beat and end, the voice-send checks at `begin_send` and `hand_off`, the voice gate's in-memory copy; refusal reasons |
| `desk/page_build.py` | adapter | `PageFiles`: the page files read once at hub start and served from memory, and their build id (`<meta name="desk-build">`), which a pairing claim must match |
| `desk/bounded_read.py` | adapter | `read_bounded`: one non-blocking open, no link followed, a regular file only, at most N bytes (absent vs. unreadable); shared by enrollment and `startup_registry` |
| `desk/labels.py` | adapter | Target labels from a bounded, cached read of each transcript's head and tail |
| `desk/peer.py` | rules + adapter | Channel identity: peer PID from the socket, `/proc` walk, session-file check and recheck; `process_presence` (whether a pid and start time still run), shared with locks and the startup acknowledgement |
| `desk/startup_identity.py` | rules | Launch identity for the startup acknowledgement: `StartupRefusal` (reasons and statuses), argv, foreground and session-file rules, the view digest |
| `desk/startup_ack.py` | adapter | The startup acknowledgement: reads the supervisor's tmux panes and `/proc`, lists blocked sessions, sends the one guarded Enter; one Enter per view; rate limits |
| `desk/startup_registry.py` | adapter | Claude's `sessions/<pid>.json` for the acknowledgement, through `read_bounded` (absent vs. unreadable) |
| `desk/channel.py` | runtime | The channel server: MCP over stdio plus the hub socket client |
| `desk/hub.py` | runtime | Wires the state, the channel socket, the operator socket and HTTPS |
| `desk/http_server.py` | adapter | HTTPS (stdlib `ssl` + `ThreadingHTTPServer`), Host/Origin/token guards, long-poll |
| `desk/operator.py` | adapter | The 0600 operator socket and its client (`pair`, `revoke`, `sends`, `voice`, `status`, `clear`, `enroll`, `unenroll`, `startup_view`, `startup_ack`) |
| `desk/cli.py` | entry | `llm-switchboard desk serve|pair|revoke|status|enroll|unenroll|sends|voice|ack|flags|install-config` |
| `launcher.py`, `supervisor.py` | runtime | Start a kept Pi session through `claudepd`, adding desk flags only if enrolled at launch |
| `screen.py`, `supervisor_state.py` | rules | Identify the complete development-channel dialog and its selected option, and report `startup_blocked` without restarting the session |
| `tmux.py` | adapter | `Tmux.panes()` (pane id, pid, copy mode, by `@car_session`; raises when tmux fails rather than reading as no panes), `capture(..., check=True)`, and `send_enter_if(pane_id, PaneGuard)`, the server-side guarded Enter the acknowledgement uses |
| `desk/static/` | page | The tablet page: plain HTML/CSS/JS, no build step, Chrome 119. Scripts, in load order: `text_rule.js` (the text rule's mirror), `desk_logic.js` (the tap flow's pure decisions), `conversation_logic.js` (conversation mode's pure reducer and its vocabulary: phases, commands, cues, end reasons, limits, `startBlocker`), `sound_tracker.js` (the page's own sound: every utterance it submitted until its end, error or the page's cancel, the guard's start, uncertain periods and the overdue watchdog), `conversation_answers.js` (what the hub's answers to a conversation's calls establish, for the controller), `conversation.js` (the conversation controller: every mic, sound and send effect, no DOM), `conversation_view.js` (the Start control and the overlay; renders only), `page_audio.js` (the tap flow's own sound: reply speech, Play and one-shot Dictate, deferring to the controller), `startup.js` (the startup-blocked view), `app.js` (wiring and the tap flow). Node tests in `tests/js/` |
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
| `config_dir/enrolled.json` | `{"sessions": {<session id>: {"since": <unix s>}}}` and nothing else; an unreadable file (anything but a regular file, such as a FIFO or a link, or over 64 KiB, counts as unreadable and is never waited on), or any key or record of another shape (a `null` record, an extra field), makes the whole file malformed: it reads as nobody enrolled, the supervisor launches without desk flags, the startup acknowledgement and enroll/unenroll refuse (503) | 64 sessions, 64 KiB |
| `config_dir/desk-settings.json` | `{"sends_enabled": false, "voice_confirm_enabled": false}`, both switches off by default; read the same bounded way, and anything unreadable reads as both off. `desk sends` and `desk voice` each change one key in one locked read-modify-write that keeps the other (a file holding only one key keeps it; a broken file is replaced, and the other switch then reads as off); any other key is dropped | fixed, 64 KiB |
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
  and an event that was never written (no connection left to write to, or
  a voice handoff refused after its lock was written; see "A voice send"),
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

The page acts on an answer's status line as soon as it arrives: a 401 on
any protected request drops its token and returns it to pairing at the
headers. An answer whose body then fails to arrive, or doesn't parse, is
still the hub's answer, with its status and no body, never "the hub could
not be reached": a ticket is refused, a send's delivery is unknown unless
the status settles it (a tapped send's 401 is not sent), and a startup
acknowledgement's 5xx is unknown, since the outcome that may have typed
the key shares that status.

Unauthenticated:
- `GET /desk`, `/desk/app.js`, `/desk/app.css`, `/desk/text_rule.js`,
  `/desk/desk_logic.js`, `/desk/conversation_logic.js`,
  `/desk/sound_tracker.js`, `/desk/conversation_answers.js`,
  `/desk/conversation.js`, `/desk/conversation_view.js`, `/desk/page_audio.js`,
  `/desk/startup.js`, `/desk/text_rule_cases.json`: the
  page (`STATIC_ROUTES`), read once at hub start and served from memory;
  `/desk` carries the build id.
- `POST /desk/pair/start {secret_hash}` -> `{pair_id, code, expires_in}`.
- `POST /desk/pair/claim {pair_id, secret, build}` -> `{token, expires_at}`; 409
  `{"error": "not confirmed"}` while waiting (the page polls every 2 s); 403
  `page out of date; reload it` when `build` isn't this hub's page build; 409
  `a conversation is in progress` while a conversation grant is live.

Protected (`Authorization: Bearer <token>`; checked before the body is read
and again inside the state's lock with each mutation, so a token revoked
while its request was in flight changes nothing):
- `GET /desk/api/state?after=<version>`: returns at once when the state
  version differs from `after` (or `after` is absent), else holds up to 25 s
  and returns the unchanged state. Body:
  `{version, epoch, sends_enabled, voice_confirm_enabled, lock_store_error, targets: [Target], requests: [Request]}`.
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
  state, text, created, confirm: "tap"|"voice", reply: {text, spoken} |
  null}` for the last 20; `confirm` is how the send was confirmed, and the
  page never voices a reply to a `"voice"` request on its own (a
  conversation speaks it once; Play still can).
- `POST /desk/api/ticket {session_id, generation, epoch}` -> `{ticket, expires_at}`.
- `POST /desk/api/send {ticket, text, confirm?, grant?}` -> `{request_id,
  state}`. `confirm` is `"tap"` (the default when omitted) or `"voice"`; a
  voice send needs `grant` (see "Conversation mode").
- `POST /desk/api/conversation {session_id, generation, epoch}` -> `{grant,
  expires_in}`, `POST /desk/api/conversation/beat {grant}` -> `{grant,
  expires_in}`, `POST /desk/api/conversation/end {grant}` -> `{ended: true}`:
  the conversation grant (see "Conversation mode").
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
  process checks. A failed read is never reported as absence: a tmux whose
  pane listing fails (a socket it may not open, an error other than "no
  server") or an unreadable `enrolled.json` answers 503 rather than an
  empty list, and a pane whose screen can't be read is listed with
  `capture: null` and `refusal` **tmux did not answer**. Only a pane tmux
  no longer lists (it closed during the call) is left out. At most 60
  views a minute across all callers (429).
- `POST /desk/api/startup/ack {session_id, digest}` -> `{session_id, sent:
  true}`: presses Enter once in that session's pane (see "Startup
  acknowledgement"). At most 6 a minute, one at a time (429), and at most
  one per view: a digest that already had an attempt is refused (409) even
  when the outcome was unknown, and a full attempt table refuses (503).
  When tmux doesn't confirm the key the answer is 503 `{"error": "...",
  "delivery": "unknown"}`. It sends no
  prompt, so it does not depend on `sends on`, and works while sends are
  off. The page tells the three outcomes apart: sent (only on a 200 the
  hub confirmed), not sent (nothing typed this time), and unknown (check
  the session; also a connection that failed or gave no answer within
  20 s, since the request may have reached the hub). It draws the outcome
  as soon as the POST settles, never waiting for the next listing, and
  keeps it shown for a minute from when it was drawn, even after the
  session leaves the list. After a sent or unknown outcome that view's
  button stays disabled on the page until a listing shows the session with
  a different digest, or leaves it out; a listing of the session with
  `digest: null` (its pane briefly couldn't be inspected) keeps it disabled.

## Operator socket and CLI

`llm-switchboard desk <command>` runs as `pi` and talks to the hub over
`operator.sock` (same framing as the channel socket, request `{op, ...}`,
response `{ok, ...}`): `pair <code>`, `revoke [--token <id>]` (all tokens when
no id; exits 4 with **revoked for new requests; an acknowledgement is still
in progress** when its bounded wait for one ran out, and running it again
waits again), `status` (epoch, registrations, locks, token ids and expiry, sends
flag, voice flag, and the live conversation's session, generation and
token id or `null`), `clear <session id> <lock id>`, `sends on|off` and `voice
on|off` (persisted in `desk-settings.json`), `enroll|unenroll <session id>`
(the hub edits `enrolled.json` and publishes it under its policy lock;
unenroll drops the session's registrations at once). `sends`, `voice`,
`enroll` and `unenroll` edit their
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
  `tmux new-window -e`. Another program named `claude` is refused. The
  child scan reads each process's parent from its `stat` alone (the
  numeric fields after the last `)`, so a name with spaces, `)` or bytes
  that aren't UTF-8 doesn't matter) and reads the rest only for the pane's
  children, so an unrelated process can't stop it. A `/proc` entry that
  exists but can't be read (the pane's process, a child, or any process
  whose parent can't be read, which might be a second claude) refuses with
  503 **the session's process can't be checked**; only a missing entry
  counts as no process.
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
- The session is enrolled (read fresh, under `enrolled.json`'s lock: the
  file is opened and validated on every listing and acknowledgement, never
  answered from the hub's routing cache, so a permission change counts at
  once); the screen passes the exact warning matcher with option 1 (**I am
  using this for local development**) selected, never **Exit**; and the digest equals
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

## Conversation mode

A second way to send, for use without touching the tablet each turn. One tap
on **Start conversation** on the selected session starts a conversation with
that one session; it then runs hands-free until it ends. Jacob dictates a
prompt and says "send it"; the session's `spoken` reply is read aloud; the
mic opens again for the next turn. Nothing listens before the tap. The tap
flow (Review, then Send) is unchanged and stays the default. The hub offers
conversations only while the voice gate is on (`desk voice on`, off by
default). Its sound output is an operating condition (below); other outputs
(Sonos over the network, a line-in cable) are a later stage with their own
plan review.

### What a conversation gives up

The tap flow's purpose was that Jacob read the exact destination and text
before it ran with full tool authority in a bypass-permissions session. The
hub has never been able to tell a tap from any other request by a paired
page (the page's script holds the token); the ticket, identity and lock rules
settle where a prompt goes and that it goes once, not who spoke, what was
meant or whether the speaker had finished. A conversation has no reader, so
the residual risk Jacob accepts by using one is:

- The recognizer can't tell voices apart. Anyone or anything audible at the
  tablet (a passenger, the radio, a video) can add to the draft and can say
  the confirm.
- Misheard words can change a prompt's meaning.
- The voice notice the hub appends to every voice send asks the session to
  check before acting on anything destructive, irreversible or
  outward-facing, but it is advice to a model, not a gate: nothing in the
  desk prevents such an action. A session's question followed by a misheard
  "yes" is the same risk one turn later.
- **A misheard command is draft text.** A command counts only when the whole
  recognition result is the phrase; anything else ("sent it", "scratch
  dat") lands in the draft as words. A repeated "send it" then sends the
  draft with those stray words in it. "Read it back" before "send it" is the
  check: it speaks the exact text that would be sent.

What narrows it: the send command counts only as an isolated, final
recognition result; read-back speaks the draft unaltered; a voice draft is
at most 1,024 bytes; the target is frozen at the tap and any change ends the
conversation; every way a conversation becomes uncertain ends it with the
mic closed, and nothing re-arms it.

The desk must not hear itself: a reply's `spoken` text comes from a model
that may have read hostile content, so reply audio reaching an open mic
would be a draft the agent wrote for its own session. The one control is
the completion rule: after any desk sound the mic opens only on the `end`
event of that exact utterance, plus a 500 ms guard. No estimate, timeout or
flag opens it, and spoken text is never rewritten to make up for a leak.

### Operating conditions the page can't check

- **Output: the tablet's own speaker only.** For the whole of every
  conversation the tablet's Bluetooth is switched off, because a paired
  headset that is merely disconnected can reconnect mid-conversation and
  take the sound; nothing is plugged into the headphone jack or a USB audio
  adapter; and no cast or mirroring session is active. This is an operating
  condition the page cannot check: `speechSynthesis` neither chooses nor
  reports the route, and the completion rule holds only where sound stops
  when the tablet's `end` says it has. Loudness is the tablet's volume.
- **Rhythm: pause for the start tone.** The recognizer's own start tone marks
  the mic opening. Pause for it before each thought: a recognition session
  that opens while Jacob is already speaking hears nothing of those words,
  and they are dropped silently. Say each command on its own, after a pause.
- **One page.** When moving to a new page (after an install, a reload or a
  second tab), close the previous desk page, hear that it is quiet, and
  revoke its token if `desk status` still lists it, before starting a
  conversation. The hub's one-page rule (below) keeps a second page from
  holding a token during a conversation, but a token count can't prove
  quiet: a revoked or disconnected page keeps its last snapshot and can
  still play a reply locally, an utterance already playing outlives a
  revoke, and a closed tab's token stays valid for its seven days.

### Commands

Recognition runs `continuous = false`, `interimResults = true`,
`maxAlternatives = 1`, `en-US`; only a final result is used, and only its
first alternative. A final result is a command only when the whole result,
lowercased, with punctuation removed and spaces collapsed, is one of these:

| Say | What happens |
| --- | --- |
| "send it" | The draft is sent exactly as it stands. An empty draft gets the cue "Nothing to send."; a draft the text rule refuses gets "Can't send that: <reason>." and stays |
| "scratch that" | The draft is cleared; cue "Cleared." |
| "read it back" | The whole draft is spoken, unaltered, with the mic closed ("The draft is empty." when it is), then the mic reopens with the draft intact |
| "stop listening" | The conversation ends |

Anything else is appended to the draft word for word, and nothing is ever
removed from it: "…and summarize them send it" in one breath stays in the
draft as spoken and sends nothing. Commands are heard only while the mic is
open; during a cue, a read-back, a send, the wait or the reply, the stop is
the full-screen **End conversation** control and the limits below.

### States

The overlay covers the page for the whole conversation and shows the
session's label, the state in words, the draft and the words being heard,
and, once the current request's reply has come, the reply's full text. It
is modal: focus moves to its End control, and the page behind it is inert
(not reachable by keyboard) until Close, which returns focus to Start, or
to the session's heading while Start is disabled. When the token goes (a
401, a revoke, expiry), the overlay keeps only the end reason: the reply,
the draft and the session's label are dropped with the snapshot they came
from, also from an overlay already closed, and the page behind keeps
nothing the hub sent.

| State | Overlay | What happens |
| --- | --- | --- |
| `starting` | Starting. The mic is closed. | Fetches a fresh snapshot, takes the screen wake lock, asks for the grant, then checks again that the frozen target is still selected and available with no request outstanding and no lock, that sends and voice confirm are on, that the page is visible and the tablet has a voice. Requests the snapshot already holds for the target are history; any other request on the target, in any state, or a lock not tied to this conversation's request is *foreign activity* from then on |
| `listening` | Listening. | The only state with an open mic. Recognition restarts on each `end`; `no-speech` restarts it |
| `reading` | Speaking. The mic is closed. | A read-back or a cue inside a turn |
| `sending` | Sending. The mic is closed. | A ticket for the frozen identity, then `POST /desk/api/send {ticket, text, confirm: "voice", grant}`. No retry: a refusal ends the conversation with its reason, and a send with no usable answer is unknown |
| `waiting` | Sent. Waiting for the reply. The mic is closed. | After the "Sent." cue; follows the exact request id the send returned |
| `speaking` | Reading the reply. The mic is closed. | The reply's `spoken` text exactly as received ("No spoken summary." when it is empty; the full text is in the overlay). On its `end` and the guard, `listening` with an empty draft and a new turn |
| `ended` | Conversation ended: <reason>. | Mic and speech stopped, draft discarded, wake lock released. The overlay stays, with **Close**, until tapped; only a new tap starts another conversation |

While a conversation runs or its closing cue plays, Dictate, Review, Send,
Test and Play are disabled and the page doesn't speak new replies itself: a
reply is spoken once, by the conversation, and is never spoken again after
it ends. Nothing in a conversation clears a lock, retries a request,
acknowledges a startup warning, changes the target or enrolls anything;
those stay touch-only, as does starting one.

One-shot **Dictate** opens a mic too, so it follows the same completion
rule: it starts only when no utterance of this page is queued or playing,
the 500 ms guard after the last one's end, error or cancel has passed, sound
isn't uncertain, and no conversation holds the voice. Otherwise it is
disabled with "Dictate is unavailable: <reason>." (the desk may still be
speaking; wait until it is quiet, or a conversation is in progress), and it
comes back on its own when that passes. A dictation already running keeps
its Stop. While it listens, the draft and Test (which rewrites the draft)
are disabled, so only its own words change the draft; both come back when
it ends, however it ends. A recognition error ends it at once, with the
note "Dictation stopped (<error>). You can type instead.", whether or not
the browser follows the error with an `end`.

**Start conversation** is enabled only when the snapshot has sends and voice
confirm on and no lock-store error, the selected session is available with
no lock and no outstanding request, the tablet has speech recognition and a
voice, no tapped send is in flight (on any target), no conversation is in
progress, no grant request a past conversation gave up on, or that failed
with no response (a transport failure doesn't prove the hub never read it),
is still unanswered (nor the release of a grant it brought; only its answer
or a snapshot from a new hub epoch lets it go), no utterance of this
page is queued or playing, and sound isn't uncertain (below). Otherwise it
is disabled and the reason is written under it: "Start conversation is
unavailable: <reason>." with one of: the hub has not answered yet; sends
are off; voice confirm is off; the hub's lock store failed; the session is
not available for sends; the session is busy; this tablet has no speech
recognition; this tablet has no voice; a tapped send is still in progress;
a conversation is in progress; the hub has not answered the last
conversation's grant request; reload the page if it never does; the desk
may still be speaking; wait until it is quiet.

### Limits

- The guard: 500 ms after any desk sound ends before a mic opens.
- An utterance gets twice its estimated duration (1 s plus 0.4 s a word) to
  report `end`; past that, or on an error, the page cancels speech and the
  conversation ends with the voice failed. That deadline belongs to the
  utterance playing: one the page replaced or cancelled (a Play tapped
  again) loses it at once, and its late `end` or `error` counts toward the
  guard but never cancels newer speech. The first `error` of the Play or
  reply speech playing cancels speech too, and makes sound uncertain. Every cancel the page issues goes
  through the one tracker (`sound_tracker.js`), which marks what it stopped.
- A recognizer that hasn't reported audio 5 s after `start()`, any
  recognition error but `no-speech`, a failed `start()`, or three restarts
  in a row that each end within a second end the conversation.
- Idle: three minutes without a final result. Turn: five minutes from the
  turn's first mic opening, which nothing resets or pauses (read-backs and
  cues count); it is disarmed for the send and the wait and starts again at
  the next turn.
- A final result that would take the draft past 1,024 UTF-8 bytes clears the
  whole draft with the cue "Too long. The draft was cleared.", so no shorter
  prefix can be sent unnoticed.
- After the send is acknowledged, the request must appear in a snapshot
  within 30 s, and its reply must come within ten minutes.
- Each start step (the snapshot, the wake lock, the grant) and the ticket
  get 10 s; the send POST gets 20 s from when it is issued, whatever
  becomes of the conversation: an end leaves it open until then (its
  answer only settles the reason shown), and at that deadline it is
  aborted with delivery unknown.
- The grant is beaten every 5 s, one beat at a time, each abandoned after
  4 s. A grant request succeeds only with a 200 naming a grant in the hub's
  shape (32 lowercase hex digits); any other answer fails Start like a
  refused grant, silently. A beat succeeds only with a 200 naming the very
  grant it beat; any other 200, like a refusal, means the grant is gone.
  The page treats it as good until 10 s after the *start* of the last
  beat that succeeded (the grant request counts as the first), on a
  monotonic clock, and checks that before every mic opening and before
  using any result; past it the conversation ends. That is how a pulled USB
  cable or a dead hub is noticed while the recognizer still works over
  Wi-Fi.

### Cues and end reasons

Spoken cues: "Sent.", "Nothing to send.", "Cleared.", "The draft is
empty.", "Too long. The draft was cleared.", "Can't send that: <reason>.",
"No spoken summary." and "Conversation ended: <reason>.". What can be told
without looking: the start tone marks the mic opening; "Sent.", the reply
and the end are spoken; waiting is the silence after "Sent.". When the
voice itself fails there is no sound: the mic is closed and the screen says
why.

The end is spoken, and the grant released after it, only when the page is
visible and not being closed or frozen, has a voice, still holds a fresh grant and cut no utterance off on
the way. Otherwise the page speaks nothing (a successor may be listening),
releases any grant it holds at once, and shows the reason only.

**The closing cue after a refusal.** When the hub refuses the turn's ticket
or send, the page keeps the sound slot only if the refusal is on a short
allow-list, the `Refusal` members whose `Ownership` is `INTACT` in
`desk/refusals.py`, which the page's `OWNERSHIP_KEPT` must equal. The hub
gives each of them only after checking, in the same call, the epoch, the
target's one registration at the frozen generation and, on a send, the
grant:

- busy: in `issue_ticket` after the epoch, routing, registration and
  generation checks; in `begin_send` after those and the grant check;
- ticket table full: `issue_ticket` only, after its busy check;
- too many outstanding requests: `begin_send` only, after its busy check.

Every other refusal, one with no words or words the page has never seen
included, ends the conversation as lost: nothing is spoken. An answer the
page cannot use (a 200 with no ticket, or a send's 200 that is neither
handed off nor answered) is treated like a refusal it doesn't know, so it
ends silently too. That covers the
refusals the hub can give before those checks, such as sends off, voice
confirm off, the text rule's reasons and too long for a voice send. Every
`Refusal` member states its class, so a new one can't be added without
being classified, and `tests/test_desk_conversation_js.py` drives a real
hub into each class on both paths and checks what the page does. A ticket
or send that got no answer at all proves nothing either way: the closing
cue then depends on freshness, like any other end. The lock store's own
messages are on no list, so the page treats them as unknown.

The page has also lost the sound slot, whatever end reason it shows, when a
refused beat or a 401 says the grant is gone, or a snapshot shows another
hub epoch or the target missing or at another generation, even alongside
sends or voice confirm turned off. From then on nothing is spoken, and a
closing cue already playing is cut (sound uncertain) and the grant let go.

**Uncertain sound.** An utterance stopped before its own `end`, by anything
(End, hiding, a timeout, an error, the end of the conversation, the page's
own Play cancelled), marks sound uncertain: no cue is spoken to probe for
silence, and Start stays disabled for twice that text's estimated duration.
The next conversation takes a tap made when it is audibly quiet; an empty
synthesis queue is not evidence of quiet.

**Late sound.** A real `end` or `error` that arrives after the page wrote
its utterance off (it timed out, was cancelled or had already ended) shows
it may have sounded until then, so whatever a mic heard since is suspect.
If a conversation has its mic open, or a draft holding words recognized
since that utterance was written off, it ends silently with "the desk may
have heard its own speech" and the draft discarded. While the turn's ticket
is still being fetched, the draft is never sent; once its send POST is out,
the end says delivery is unknown and that POST's own answer, if it comes,
settles it (see the end reasons below). A running one-shot dictation
stops and the draft goes back to what it was when the dictation started,
which removes only the words it added (nothing else could change the draft
meanwhile), with the note "Dictation stopped: the desk may have heard its
own speech. Its words were discarded.". Either way sound is uncertain again
for twice that utterance's estimate, and Start and Dictate come back on
their own once that has passed. Otherwise the late event only moves the guard.

End reasons, as the overlay and the cue word them: you said stop listening;
End was tapped; the page was hidden; the page was closed or frozen (on
`pagehide` or `freeze`, whatever the visibility, and always silent); another
session was selected; this tablet is no longer paired; the screen wake lock
was released; the screen can't be kept awake; this tablet has no voice;
nothing was heard for three minutes; the turn took over five minutes; the
hub stopped answering; the hub ended the conversation; the hub refused the
conversation: <reason>; the hub could not be reached; speech recognition
failed (with the recognizer's error); speech recognition did not start;
speech recognition keeps stopping; the desk may have heard its own speech
(always silent; see Late sound); the voice failed; the hub restarted;
the session changed or went away; sends are off; voice confirm is off; the
hub's lock store failed; the session is busy; another request reached the
session; not sent: <reason>; delivery is unknown; check the session; the
request ended without a reply; no reply came in ten minutes. A conversation
that ended never resumes when the link returns. Whatever ends a
conversation while its send POST is out with no answer, the reason is
followed by "; delivery is unknown; check the session" (for example
"Conversation ended: the hub stopped answering; delivery is unknown; check
the session."), since the prompt may have been delivered; nothing is
retried, and nothing is spoken when the grant was lost. An end before any
POST shows its cause alone. If that POST's own answer still comes (after
the end, after the freshness deadline, or as the 401 that ended it), it
settles the reason shown, after the same cause: "; the prompt was handed
off; check the session for its reply" when the hub accepted it, or "; not
sent: <reason>" when it refused it. That answer changes the reason only:
the conversation stays ended, nothing is spoken, no mic opens, and a
conversation started since never sees it. An answer that settles nothing
(a 5xx, an unrecognised body) leaves delivery unknown.

Whether a send's refusal says "not sent" is a separate question from
whether the page may speak after it: it is not sent only for a 401 or a
refusal whose `Delivery` is `NOT_SENT` in `desk/refusals.py` (the page's
`NOT_SENT` list in `conversation_answers.js` must equal that set). The
send route gives each of them only before it writes the event to the
channel: the HTTP layer's checks, `begin_send`, the dispatch recheck and
`hand_off` (for example not enrolled, channel identity unusable; not sent,
and request not pending). Any other words, ticket already used (an earlier
send with that ticket may have gone through) and internal error (it can
follow the write) among them, leave delivery unknown.

### Hub side

- **The voice gate.** `voice_confirm_enabled` in `desk-settings.json`,
  switched by `llm-switchboard desk voice on|off` (through the hub when it
  runs, else the locked file edit, like `desk sends`). The hub reads it fresh
  from the file with every refresh, including the one just before each
  handoff, so an unreadable file means off. It is a product gate for honest
  pages: a page that lied about `confirm` could send as a tap. A
  conversation that outlives an off-then-on toggle may go on; no voice send
  is handed off while the gate is off.
- **One current page.** A pairing claim carries the page's build id
  (`desk/page_build.py`): the first 16 hex digits of a SHA-256 over every
  served route and its file, as read at hub start. A claim from another
  build is refused (403 `page out of date; reload it`), so a tab loaded
  before an install can't pair. A grant is given only to a token that is the
  only valid token (409 `another page is paired; revoke its token first`);
  expired tokens are deleted before that count, so a wall-clock step back
  can't make one valid again during the grant. Closing a tab doesn't revoke
  its token. While a grant is live, pairing
  claims are refused (409 `a conversation is in progress`).
- **The grant** (`desk/grants.py`). `POST /desk/api/conversation {session_id,
  generation, epoch}` returns `{grant, expires_in: 15}`: a random id bound to
  the token and that target, which expires 15 s after the hub last renewed
  it, measured on the hub's monotonic clock (a wall-clock correction neither
  extends nor shortens it). It is refused like a ticket (400 bad input, 409 `stale epoch`, `stale
  generation`, `not enrolled`, `not registered`, `ambiguous`, `busy`, 403
  `sends off`, 503 a lock-store error) and also with 403 `voice confirm off` and 409 `another page is
  paired; revoke its token first`. At most one grant exists; a new one
  replaces the old. `POST /desk/api/conversation/beat {grant}` renews it (409
  `target changed` ends it when the target is no longer the same single
  registration); `POST /desk/api/conversation/end {grant}` ends it. Beat,
  end, `begin_send` and `hand_off` each require the exact current, unexpired
  grant for that token, checked under the state's lock (409 `conversation
  grant not current`): an expired grant is never revived by a late beat, and
  a delayed end naming an old grant doesn't touch a newer one. Revoke, token
  expiry and token eviction end the grant. Grants live in memory only and
  die with the epoch.
- **A voice send.** `POST /desk/api/send` with `confirm: "voice"` needs the
  voice gate on (403 `voice confirm off`), the current grant (409
  `conversation grant not current`) for the ticket's target (409
  `conversation grant not for this target`) and text within 1,024 bytes (400
  `too long for a voice send`); any other supplied `confirm`, JSON `null`
  included, is 400 `bad confirm`.
  The mode and the grant id are stored on the request and both are checked
  again at `hand_off`, so a grant that expired or was replaced, or the gate
  switched off, between `begin_send` and the handoff refuses it (the request
  expires; nothing was written). The grant is checked once more after the
  target lock is on disk and before handed_off is recorded, since a slow
  write can outlast it: then the handoff is refused the same way (409
  `conversation grant not current`, the request expired, nothing sent), and
  the lock just written stays. Removing it there would be a second store
  write while the state's lock and the hub's policy and file locks are
  held, so a write slow enough to cross expiry could stall state reads,
  beats and revokes twice over. The target therefore shows **Busy** with
  that lock, its request expired, until Jacob clears it (the page's Clear or
  `desk clear`): in that hub run the clear needs no channel check, since
  nothing reached the channel; a failed clear fails closed like any other.
  After a hub restart it is an earlier run's lock and gets the process
  check. An omitted `confirm` is a tap, exactly as
  before. Tickets, one outstanding request per target, the target lock,
  reply acceptance and channel identity are the same as for a tap. Logs
  record the mode, never content.
- **The channel's text.** The channel instructions say an event comes from
  Jacob's desk, where he types or dictates it, and that a voice event's
  words may include recognition errors or other voices or audio near the
  mic. A voice send's event ends, after the delivery suffix, with the voice
  notice (`protocol.VOICE_NOTICE`): `[voice desk: this was dictated
  hands-free in Jacob's van and sent on a spoken confirm; nobody read it,
  and it may contain recognition errors or words from other people or audio
  near the mic. If it asks for something destructive, irreversible or
  outward-facing, or it doesn't make sense, don't act on it: reply asking
  Jacob to confirm in a few words. He is listening, not reading: make spoken
  a complete, conversational answer in at most five short sentences with no
  paths, code, ids or lists, and when you need a decision from him, end
  spoken with the question.]` The `reply` tool's description of `spoken`
  (at most 1,024 bytes) says the same.

### Voice readiness check

The notice is best-effort guidance, and the hub can't see which model a
session runs, so voice readiness is an operator gate. A voice round trip
needs both gates on (a grant is refused while voice is off), so the check
switches them on for the test only. Run it before normal use is first
authorized, and again before using a session on a model not yet tried in a
conversation, in this order:

1. Park the van.
2. Enroll only a disposable test session: `desk unenroll` every other
   target, and confirm with `desk status` that the test session is the
   only one enrolled.
3. Switch both gates on for the test: `desk sends on`, `desk voice on`.
4. Run one full voice round trip (Start, a short prompt, "read it back",
   "send it", the reply spoken, then "stop listening"), and send one prompt
   that should draw a confirming question (a request to delete something,
   say); the reply must ask Jacob to confirm instead of acting.
5. Switch `desk voice off` again, pass or fail, and `desk sends off` too
   unless the tap flow's own tablet checks have already authorized sends.
   Only then re-enroll other sessions.

Normal use (voice on with real sessions enrolled) is authorized by Jacob
separately, after a passing check, never by leaving the test's switches on.
Even then, stage 1 has not been qualified for driving: a passing check
covers parked use only.
