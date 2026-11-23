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

Phase 2 (the Mac link) is designed in "Mac link" at the end of this file and
not built; phase 3 (a "prime" orchestrator) is not designed.
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
| `desk/refusals.py` | model | `Refusal`: every reason the hub's HTTPS API and operator socket refuse with (hub, state, grants, HTTP layer) and the text rule's reasons, declared once, each with its `Delivery` class (whether a send refused with it was certainly not handed off) and `NOT_SENT`; `tests/test_desk_refusals.py` fails on a refusal raised in those modules without it. The startup acknowledgement keeps its own `StartupRefusal` |
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
| `desk/static/` | page | The tablet page: plain HTML/CSS/JS, no build step, Chrome 119. Scripts, in load order: `text_rule.js` (the text rule's mirror), `desk_logic.js` (the tap flow's pure decisions), `conversation_vocabulary.js` (conversation mode's vocabulary, declared once: phases, commands, cues, end reasons, limits, Start's blockers), `conversation_logic.js` (conversation mode's pure reducer, which re-exports that vocabulary, and `startBlocker`), `sound_tracker.js` (the page's own sound: every utterance it submitted until its end, error or the page's cancel, the guard's start, uncertain periods and the overdue watchdog), `conversation_answers.js` (what the hub's answers to a conversation's calls establish, for the controller), `conversation_timers.js` (the conversation's timers and its fetches' deadlines, each kept as the time it is due, and `heard()`, the one order every event from outside them is applied in: one that silences the page before any overdue timer, any other only after every limit already due has applied), `conversation_fetches.js` (the kinds of fetch a conversation can have and each one's own limit, the fetches in flight, one of each kind, which answer is still current, and the two answers an end still listens for), `conversation_recognition.js` (the conversation's speech recognizer, one at a time, its callbacks honored only while it is current), `conversation.js` (the conversation controller: every mic, sound and send effect, no DOM; `app.js` gives it every status line the hub sends, on any call, as it arrives (its `hubStatus()`, the gate), and every snapshot the page receives (its `snapshot(state, from)`, whichever call brought it)), `conversation_view.js` (the Start control and the overlay; renders only), `page_audio.js` (the tap flow's own sound: reply speech, Play and one-shot Dictate, deferring to the controller), `startup.js` (the startup-blocked view), `hub_api.js` (the page's one way to the hub: `app.js`'s `api`, through which every call goes), `app.js` (wiring and the tap flow). Node tests in `tests/js/` |
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
headers. The status line is the hub's answer even when its body then fails
to arrive or doesn't parse: it is the status with no body, never "the hub
could not be reached". For a non-200 the gate has already acted on the
status line; for a 200 the lost body silences the page (see "The gate"
under Cues and end reasons). So a ticket is refused, a send's delivery is unknown unless
the status settles it (a tapped send's 401 is not sent), and a startup
acknowledgement's 5xx is unknown, since the outcome that may have typed
the key shares that status.

Unauthenticated:
- `GET /desk`, `/desk/app.js`, `/desk/app.css`, `/desk/text_rule.js`,
  `/desk/desk_logic.js`, `/desk/conversation_vocabulary.js`,
  `/desk/conversation_logic.js`, `/desk/sound_tracker.js`,
  `/desk/conversation_answers.js`, `/desk/conversation_timers.js`,
  `/desk/conversation_fetches.js`, `/desk/conversation_recognition.js`,
  `/desk/conversation.js`, `/desk/conversation_view.js`,
  `/desk/page_audio.js`, `/desk/startup.js`, `/desk/hub_api.js`,
  `/desk/text_rule_cases.json`:
  the page (`STATIC_ROUTES`), read once at hub start and served from
  memory; `/desk` carries the build id.
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
| `sending` | Sending. The mic is closed. | A ticket for the frozen identity, then `POST /desk/api/send {ticket, text, confirm: "voice", grant}`. No retry: a refusal ends the conversation silently, its reason on screen only ("not sent: <reason>" when the refusal proves nothing was handed off), and a send with no usable answer is unknown |
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
- Deadlines are absolute times. Each conversation timer
  (`conversation_timers.js`) keeps the time it is due, and every event from
  outside the timers (a status line, a body, a lost body, no answer at all,
  a snapshot, a wake-lock result) enters the controller through one
  function, `heard(silences, apply)`, in one of two orders. An event that
  silences the page (a status other than 200, a lost body, a snapshot that
  is missing or shows a target problem, an answer that fails its call's own
  success check or came after its call's own limit, no answer, a failed
  wake lock) is applied first, with no overdue timer run before it: some
  overdue timers start things (the guard opens the mic, the idle, turn and
  reply-wait limits and the start watchdog speak a closing cue), and the
  end clears them all, so once the page knows it must go silent none of
  them can open the mic, start a recognizer or speak on the way. Applied
  first is about order, not outcome: a beat that gets no answer at all
  runs no overdue timer but ends nothing by itself, and is left to the
  freshness deadline like every unanswered beat. And a snapshot silences in
  this sense only when it is missing or shows a target problem: one whose
  requests end the conversation for another reason (foreign activity, a
  busy target, a request gone) is applied after the timers already due, so
  a guard already due opens the mic for the instant before that snapshot
  ends the conversation, and an idle or turn limit already due ends it
  first with its own spoken cue, the order the timers would have given had
  they run on time. Any other
  event (a success, a usable snapshot, a granted wake lock) is applied only
  after every timer already due has run, in due order (`catchUp`, reached
  only through `heard`), so a limit that has passed wins over a late
  success. Whether an answer came after its call's own limit is decided
  from the clock: that call's own limits, its fetch deadline and the
  controller timer bounding the same step (the step limit, or the beat's
  abort), apply then, at its status line or its body, and no other overdue
  timer does (`lapse`). So an answer handled after its call's limit is
  treated as that limit having passed, whatever order the browser ran the
  callbacks in: a body that arrives after a deadline that applied late is
  cut off and counts as lost. A 200's status line establishes nothing and
  runs no timer; its body is what is used. This covers
  the step limit (Start's snapshot, the wake lock, the grant, the ticket),
  the beat abort, the send deadline, the 30 s wait for the request to appear
  and the ten-minute reply wait; freshness, the turn limit and the idle limit
  are absolute too. The recognizer's 5 s `audiostart` watchdog and the
  speech-end limit are not: each bounds the wait for proof that a browser
  object is alive, and a late event is that proof.
- Each start step (the snapshot, the wake lock, the grant) and the ticket
  get 10 s, a beat 4 s (below), and the send POST 20 s, each from when it
  is issued. A hub call keeps its own deadline whatever becomes of it:
  current, abandoned by the conversation, or left open by an end. The call
  whose status line ended the conversation may still settle the reason
  with its body until its deadline, at which it is aborted and the reason
  shown stands; a send POST aborted so leaves delivery unknown. The one
  exception is a grant request the conversation gave up on: it is aborted
  only once a non-200 shows it granted nothing, since an abort cannot
  prove the hub will not still grant it.
- The grant is beaten every 5 s, one beat at a time, each abandoned after
  4 s. A grant request succeeds only with a 200 naming a grant in the hub's
  shape (32 lowercase hex digits); any other answer fails Start like a
  refused grant, silently. A beat succeeds only with a 200 naming the very
  grant it beat; any other answer (a refusal, a 5xx, any other 200, a body
  that never arrived) means the grant is gone and ends the conversation
  silently, as "the hub ended the conversation". A beat with no answer at
  all is left to the freshness deadline.
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
without looking: the start tone marks the mic opening; "Sent." and the
reply are spoken; waiting is the silence after "Sent."; an end the page
decided itself is spoken (below). After "send it" on a sendable draft he
hears "Sent." or nothing, unless he taps End: nothing means the send was
refused, its answer was unusable or delivery is unknown, and the screen
says which, so look at it once parked. When the voice itself fails there
is no sound: the mic is closed and the screen says why.

**The closing cue.** "Conversation ended: <reason>." is spoken only for an
end the page decides itself, `SPOKEN_ENDS` in `conversation_logic.js`: you
said stop listening; End was tapped; nothing was heard for three minutes;
the turn took over five minutes; no reply came in ten minutes; speech
recognition failed (the recognizer's error, a `start()` that threw, or a
mic that would have opened inside the guard); speech recognition did not
start (the 5 s watchdog); speech recognition keeps stopping. Even then it
is spoken only while the page is visible and not being closed or frozen,
has a voice, still holds a fresh grant and cut no utterance off on the
way, and the grant is released when it ends. A reason added to the
vocabulary is silent until it is added to that list.

Every other end is silent: the page shows the reason, speaks nothing and
releases any grant it holds at once. That includes every end the hub
brings, since once the hub has answered against this page another page
may own the sound slot and nothing the hub says can prove otherwise: any
refusal of the ticket, the send, the grant request or a beat, whatever its
words; an answer of the wrong status or shape, or one whose body never
arrived (a poll answered 200 with no snapshot, or a ticket or send answer
no one is waiting for any more that is not its call's success, ends with
"a hub answer could not be used"); a snapshot that makes the target unusable (another epoch, sends
or voice confirm off, a lock-store error, the target gone, at another
generation, unenrolled or unregistered); another request on the session;
a request that ended without a reply; and the ends the hub's silence
brings (a lapsed grant, a ticket or send with no answer, the token gone).
The page's own ends outside the list are silent too: hidden, closed or
frozen, another session selected, the wake lock released or refused, no
voice, the voice failed, and the desk may have heard its own speech.

**The gate.** Every call the page makes goes through one function,
`hub_api.js`'s `api`, and every status line the hub sends reaches the
controller's gate (`hubStatus` in `conversation.js`) the moment it
arrives, before and whatever becomes of the body, on every call: the
conversation's own (Start's snapshot, the grant request, the beat, the
grant release, the ticket and the send), the poll, the tap flow's
ticket, send and refresh, the startup listing and acknowledgement, a lock
clear, enroll and unenroll, and pairing. It hears it whether or not the
call's caller still wants the answer: abandoned, superseded or made for
an earlier conversation. Only a 200 can be a call's success. Any other
status cuts a closing cue that is playing (sound uncertain) and releases
its grant, with nothing spoken in its place, and ends a running
conversation, in any phase, silently and at once: the recognizer stopped,
no cue, the grant released. So a hub hiccup on any call ends the
conversation, and Start is tapped again. The status is applied before any
timer already overdue (`heard`, under Limits), so an overdue guard, idle
or turn limit never opens the mic or speaks first, and an overdue
freshness check never replaces the status shown, or what the call's body
then says, with "the hub stopped answering"; only that call's own limit,
when it had passed before the status line, applies first. A response that never came (a
network error, an abort before the headers) is no answer: freshness and
the deadlines cover it. A non-200 to a grant request an earlier
conversation gave up on proves it granted nothing, so Start stops waiting
for it.

A 200 whose body is lost in transport (the body read rejects) is reported
at the same shared boundary: `hub_api.js` calls `onBodyLost`, `app.js`
passes it to the controller's `bodyLost()`, which silences the page on every
call the page makes, current or abandoned, before any caller-specific code.
A body that arrived but is empty or not JSON is not a lost body; it is
content, judged by the call. A 200 is otherwise judged by the call that
made it, and one that fails that call's success check silences the page the
same way, before any overdue timer (`heard`), whether the call is current
or abandoned, and whether a conversation is running or only its closing
cue plays (the page still beats the grant and reads every poll while the
cue plays). The one function, `silence` in `conversation.js`, cuts a
playing cue and ends a running conversation silently, in any phase: a
ticket answer with no ticket, a send answer that is neither handed off nor
answered, a beat that does not renew the grant it named, any 200 to a
grant request an earlier conversation gave up on (a grant in it replaced
the one this page holds), a 200 whose body was lost in transport, and a
snapshot that is missing or shows a target problem. Every snapshot the
page receives goes through the controller's one entry point,
`snapshot(state, from)`, whichever call brought it: the poll, the tap
flow's refresh after a send, an abandoned Start snapshot, or an earlier
pairing's answer. A running conversation takes a snapshot in version
order; an older one, or one from an earlier pairing, counts only for the
target problem it shows and is never stored as state. A poll answered 200
with no snapshot silences the page too. A send POST that reaches its 20 s
deadline with no answer leaves the cue to finish; a freshness lapse cuts
it.

**What the end says.** Ending at the status line, the page shows the
status: "a hub call failed: HTTP <status>", or "this tablet is no longer
paired" for a 401, which needs no body (the token is checked before
anything else, and the page returns to pairing then). When the status is
one of the conversation's own calls, the page keeps the conversation as it
was at that status line, and when that call's body arrives its answer is
replayed there, so the end then says what the call would have shown on
its own: "not sent: <reason>" for a ticket refusal or a send refusal on
the `Delivery` list below, "the hub refused the conversation: <reason>"
for a grant request, "the hub ended the conversation" for a beat, "the
hub could not be reached" for Start's snapshot, "delivery is unknown;
check the session" for a send answer that settles nothing. A body that
never arrives or can't be read settles nothing, so the status stays
shown, and a send POST's stays "delivery is unknown" after it: the page
never says "not sent" for what isn't proven. A status on any other call
(the poll, the tap flow, the startup view, a release) stays shown as it
is. A 401 to the send proves at its status line that the prompt was not
sent ("this tablet is no longer paired; not sent: HTTP 401", its body's
words in place of "HTTP 401" if they arrive), and neither a lost body nor
the POST's deadline changes that. A send whose 200 body is lost ends with
"a hub answer could not be used; delivery is unknown; check the session",
never "not sent" and never "handed off".

The reason shown is two facts composed: the end's cause, and the outcome
of a send the end left open. Each is settled once, by its own evidence,
and a settled fact never changes. The cause is settled by the answer of
the call whose status line ended the conversation, or stays as the end
gave it. The send's outcome is settled by that POST's own answer
(handed off, not sent), by a 401 at its status line, or, when no answer
proves anything, at its deadline as unknown. So a late body of the ending
call cannot turn "handed off" or "not sent" back into "delivery is
unknown", and the send's deadline cannot either. A status that arrives
after freshness has lapsed is still recorded as the cause, and refined by
its body, without renewing anything; the end stays silent.

**The page trusts a 200 from its own hub.** The link is TLS to a leaf of
the desk's own CA, and the page build is checked at pairing, so a 200
whose body has a shape the hub can't produce (a whitespace-only ticket,
an empty request id, a snapshot without a version, requests that are
empty records, a reply of `{}`, a poll body of `{}`) is a hub bug or a
hostile hub, which could do worse than mislead the page. The page checks a
200 only for what its calls name as success (above, and the grant id's
shape, the beat naming its grant, the snapshot checks behind a target
problem) and validates no further shape. What it trusts is a 200's
contents: a 200 whose body was lost in transport has no contents, so it
established nothing about its call and silences the page; the shape of a
body that did arrive is not second-guessed beyond those checks.

`tests/test_desk_conversation_js.py` drives a
real hub into each refusal of the ticket, the send, the grant request and
the beat, and feeds it to the page while the conversation runs and while
the cue plays; every `Refusal` member is driven there or named as out of
reach of a conversation's calls, so a new one fails until it is
classified.

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

End reasons, as the overlay and the cue word them: you said stop
listening; End was tapped; the page was hidden; the page was closed or
frozen (on `pagehide` or `freeze`, whatever the visibility); another
session was selected; this tablet is no longer paired; the screen wake
lock was released; the screen can't be kept awake; this tablet has no
voice; nothing was heard for three minutes; the turn took over five
minutes; the hub stopped answering; the hub ended the conversation; the
hub refused the conversation: <reason>; the hub could not be reached; a
hub call failed: HTTP <status> (see What the end says); a hub answer
could not be used (silent); speech recognition failed (with the recognizer's error); speech
recognition did not start; speech recognition keeps stopping; the desk may
have heard its own speech (see Late sound); the voice failed; the hub
restarted; the session changed or went away; sends are off; voice confirm
is off; the hub's lock store failed; the session is busy; another request
reached the session; not sent: <reason>; delivery is unknown; check the
session; the request ended without a reply; no reply came in ten minutes.
Only the eight in the closing cue's list are ever spoken; the screen shows
every one. A conversation that ended never resumes when the link returns.
Whatever ends a conversation while its send POST is out with no answer,
the reason is followed by "; delivery is unknown; check the session" (for
example "Conversation ended: the hub stopped answering; delivery is
unknown; check the session."), since the prompt may have been delivered;
nothing is retried. An end before any POST shows its cause alone. If that
POST's own answer still comes (after the end or after the freshness
deadline), it settles the reason shown, after the same cause: "; the prompt was handed off; check the session for its
reply" when the hub accepted it, or "; not sent: <reason>" when it refused
it. That answer changes the reason only: the conversation stays ended,
nothing is spoken, no mic opens, a closing cue still playing is cut unless
the answer is the send's success (handed off or answered), and a
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

## Mac link (phase 2, designed, not built)

This section is the contract for reaching Claude sessions on the Mac from the
same desk. Nothing in it exists yet; everything above stays true for Pi
sessions except where a "Pi side" note below says otherwise. In it, `<mac>`
is the Mac's host name from `local.env` (`LLM_SWITCHBOARD_MAC_NAME`, the same
name the v1 host exchange uses), `pi` is the desk's fixed name for the Pi's
own sessions (`model.HOST`), and `/Users/me` is the Mac's home.

### Who the design defends against

Jacob decided this on 2026-11-23. A Mac session launched with `claudep
--desk` runs with bypass permissions, so a prompt that reaches it can do
whatever Jacob's account can do on the Mac. He accepted that the Pi's side
holds that power through this link and declined a Mac-side restriction on
desk-origin turns ("allow full access via Claudep"). The same day the Pi's
unrestricted SSH key was removed from the Mac, so this link is the one path
from the Pi to the Mac. Every later statement in this section is written to
the following model.

- **Accepted: the Pi's `pi` account has full authority over flagged Mac
  sessions.** Any process running as `pi` on the Pi, or as root there, can
  send prompts to every Mac session that was launched with the desk flags
  and is registered with a running relay, whether or not the desk shows it
  enrolled. It can do so through the hub's sockets (the operator socket
  takes its uid as the operator, so it can enroll, turn sends on and pair a
  tablet), or by standing in for the hub on `link.sock` while the hub is
  down: the genuine pump then connects it to the genuine relay, whose proof
  tells a listener which Mac is connecting while nothing tells the relay
  which process is the hub. The Pi's enrollment list, pairing and sends
  switch are controls for honest operation on the Pi, not limits the Mac
  can verify. Nothing in this design restricts what such a prompt can make
  a Mac session do.
- **Out of scope: a process running as Jacob on the Mac.** It already holds
  every authority a prompt could obtain (it can read his keys, type into his
  terminals and start `claude` itself), so the design doesn't try to stop it
  from using the desk key, posing as the relay or as a channel, binding the
  relay's socket, or starting the relay again after Jacob unloads it. The
  Mac-side checks (the relay's channel check and recheck, the parent and
  flag rule, the session-file rules) exist to prevent accidents and
  confusion between honest processes: a stale session file, a reused pid, a
  channel attaching to the wrong session after `/clear`, an MCP
  configuration that starts the desk channel in a session that wasn't
  launched for the desk. They are not security boundaries, and nothing below
  relies on them as one.
- **In scope:**
  - *Other users on either machine*: 0600 sockets in 0700 directories, the
    uid checks on every socket (`SO_PEERCRED`, `LOCAL_PEERCRED`), 0600 key
    and policy files.
  - *Anyone on the network without the desk key*: the SSH transport (strict
    host-key checking; a key that can run only the forced command) and the
    proof of possession, which is also how the hub knows which Mac key a
    link belongs to.
  - *Honest-but-buggy behavior* of the Pi, the Mac, the link and upgrades:
    a wrong target, double delivery, a retry or a lock release while the
    prompt may still run, mixed versions, sleep and wake, rollback,
    unbounded growth, a hub hang.
- **What the off switches do** (commands in "Switching it off", below):
  - *Stop the relay job (Mac)*: unloading the relay ends the Mac's link and
    opens no new one until the relay is loaded again on the Mac. Since the
    Mac opens every link, this is the switch the Mac enforces. The Pi's
    signer line stays, so loading the relay again restores the link.
  - *Withdraw the Mac's credential (Pi)*: `desk link-revoke <mac>` removes
    the key's signer line and its `authorized_keys` line from the Pi's
    installation. An honest hub then retires the link that key holds and
    admits no new one (Revocation, under SSH, below, says when). It is not
    a defense the Mac enforces: the `pi` account, which this model accepts
    as authoritative, can write both lines back, and a loaded relay keeps
    retrying. Deleting the Mac's copy of the key is tidying.
  - *Take a session out of reach (Mac)*: close it, or launch it without
    `--desk`. Unenrolling it on the desk stops the hub handing it new
    events; it is not a limit the Mac verifies.
  - *What none of them does*: none recalls an event already past the link:
    one the relay has handed to a channel, one a channel holds while it
    waits to write Claude's stdin pipe, or one Claude has read and queued
    (behind a running turn or a dialog on its screen). An honest session
    can still receive and act on such an event after the command returns,
    with the session's full authority, even if the relay never runs again.
    So no switch except closing the session means the session is quiet,
    and none makes a lock safe to clear: a lock clears only by the request
    table's rules (Targets, below).

### Shape

```
Mac                                                     Pi
claude --desk session                                   desk hub (desk serve)
  └─ bin/desk-channel ──0600 Unix socket──► desk relay    ▲ link.sock (0600)
     (the phase 1       run/channel.sock    (launchd,     │
      channel server)                       bin/desk-relay)
                                              │ ssh -F none -T
                                              └──────────►sshd ─► bin/desk-link
                                                 one stream,      (forced command:
                                                 new key          a bounded line pump)
```

- The Mac opens the one stream; the Pi never connects to the Mac.
- On the Mac, the channel server is the phase 1 `bin/desk-channel`, whose
  one change is the bounded session-file read that lands before this
  feature on both machines (Module plan, commit 1). It connects to `DeskConfig.channel_socket`, which on
  the Mac is the relay's socket, and speaks the channel protocol
  (`protocol.py`) to the relay as it does to the hub on the Pi.
- The relay checks each channel's identity on the Mac (Identity on the Mac,
  below) and carries frames over the link. It is not a second hub: the hub
  issues every epoch, generation, request id, ticket and lock, holds
  enrollment and decides routing.
- The hub admits a link only after the relay proves, over the link, that it
  holds the Mac's desk private key (Admission, below). That is how the hub
  knows which Mac key a link belongs to; the SSH login is transport.
- The v1 host agent, its key, its forced command (`host-exchange`), its
  actions and its frames are untouched, and the desk adds nothing to them.

### Processes, sockets and files

**Mac.**

| Item | What |
| --- | --- |
| launchd job `dev.llm-switchboard.desk-relay` | `ProgramArguments`: `DeskConfig.relay_argv` = `[<repo>/.venv/bin/python, -I, -B, <repo>/bin/desk-relay]`, `RunAtLoad`, `KeepAlive`, `ThrottleInterval` 10, `ProcessType` `Standard`, `EnvironmentVariables` `HOME` and `PATH` as the v1 jobs (`host_launchd.PATH`) and nothing else, `StandardOutPath` and `StandardErrorPath` `/dev/null` |
| `bin/desk-relay` | The relay's entry point. Standard library only. Before importing anything else it opens `desk-relay-launchd.log` with `O_APPEND`, truncates it when it is over 256 KiB, and `dup2`s it onto stderr, so a failed start leaves its traceback there. Once `relay.py` has its rotating log and has set `sys.excepthook` and `threading.excepthook` to write to it, it `dup2`s `/dev/null` onto stderr: after startup nothing reaches the launch log, so it never grows past 256 KiB plus the output of one failed start, however long the relay runs or however often it restarts. A failure before that point (no interpreter, an unreadable script) goes to `/dev/null`; launchd records the exit in the unified log |
| `/Users/me/.config/llm-switchboard/desk/run/channel.sock` | The relay's listener (0600 in the 0700 `run/` directory, `operator.bind_private_socket`, which refuses to bind while something listens there), where Mac channel servers connect. The relay refuses to start when the path is over 103 bytes (macOS `sun_path`) |
| `/Users/me/.config/llm-switchboard/desk/run/relay.lock` | Empty file; the relay holds an exclusive `flock` on it for its lifetime, so only one relay runs (a second exits 3) |
| `/Users/me/.config/llm-switchboard/desk/mcp.json` | The Mac's pinned MCP config, written by `llm-switchboard desk install-config` on the Mac; it names the Mac checkout's `.venv/bin/python -I -B <repo>/bin/desk-channel` |
| `/Users/me/.ssh/llm-switchboard-desk-<mac>`, `.pub` | The link key (SSH, below) |
| `/Users/me/Library/LaunchAgents/dev.llm-switchboard.desk-relay.plist` | Written by `desk relay-install`, deleted by `desk relay-install --unload` |
| `/Users/me/Library/Logs/llm-switchboard/desk-relay.log` | The relay's own log through a `RotatingFileHandler`, 1 MiB, one backup (2 MiB in all); uncaught exceptions in any thread go here |
| `/Users/me/Library/Logs/llm-switchboard/desk-relay-launchd.log` | Tracebacks from a failed start only, capped by `bin/desk-relay` as above |

The relay keeps no other state on disk. Its logs carry link states, wire
codes (Framing, below), ssh exit codes with a classified stderr code, session
id prefixes and request ids of the hub's shape; never prompt text, reply
text, labels, folders or any free-form text from ssh, a channel or the hub.

Every child (`ssh -G`, `ssh`, `ssh-keygen`) runs through
`desk/child_process.py`: `close_fds=True` (no child holds a channel
connection), its pipes read to their caps, a total deadline, and on every
exit path, normal or not, the cleanup: `SIGTERM`, up to 2 s for it to exit,
then `SIGKILL` and `waitpid`, so no child is left running or a zombie. The
deadline and the cleanup are separate bounds: the deadline ends the wait
for the child's work (the caller gets its timeout code then), and the
cleanup follows, holding no lock, so a child can outlive its deadline by
the 2 s grace plus the time `SIGKILL` takes to land, and no more.

The relay does not keep the Mac awake (no power assertion). While the Mac
sleeps the link is down and the desk says so.

**Pi.**

| Item | What |
| --- | --- |
| `bin/desk-link` | The forced command, run as `<repo>/.venv/bin/python -I -B <repo>/bin/desk-link` (`DeskConfig.link_argv`); a line pump between its stdin/stdout (the SSH channel) and the hub's link socket. No arguments, no state, no log of its own; on exit it writes one line `desk-link: <code>` to stderr, which ssh carries back to the relay |
| `runtime_dir/link.sock` | A third 0600 socket of the hub, beside `channel.sock` and `operator.sock`, created by `desk serve` at start |
| `runtime_dir/hub.lock` | The run lock (Hub side, below): an empty 0600 file, never written or deleted; `desk serve` holds an exclusive `flock` on it for the life of its process, and the offline decisions take it non-blocking |
| `config_dir/link-signers` | The allowed-signers list the hub verifies link proofs against: one line per Mac, `<mac> namespaces="llm-switchboard-desk-link" ssh-ed25519 AAAA…`, at most 4 lines and 4 KiB, 0600, written only by `desk link-authorize` and `desk link-revoke`, each holding `link-signers.lock` and then the `authorized_keys` lock across both of its edits, always as a whole file that the hub's own reader accepts. The hub reads it at each admission, at publication, at each housekeeping pass, on `link_drop` and for `link_fence` |
| `config_dir/enrolled-hosts.json` | Mac enrollment (Enrollment, below) |
| `~/.ssh/authorized_keys` | One added line per Mac (SSH, below) |

The Pi holds only public material for the link. The hub keeps everything
else about it in memory: one link object per host, the remote registrations,
the candidates list, the unattributed admission refusals.

### SSH

- **A new key, used for nothing else**: `~/.ssh/llm-switchboard-desk-<mac>`
  (ed25519, no passphrase, because launchd uses it unattended; comment
  `llm-switchboard-desk-<mac>`), generated on the Mac; the private key never
  leaves it. The v1 key `~/.ssh/llm-switchboard-<mac>` stays as it is.
- **The `authorized_keys` line** on the Pi:

  ```
  restrict,command="/home/pi/dev/llm-switchboard/.venv/bin/python -I -B /home/pi/dev/llm-switchboard/bin/desk-link" ssh-ed25519 AAAA… llm-switchboard-desk-link:<mac>
  ```

  `restrict` turns off port, agent and X11 forwarding, the pty and
  `~/.ssh/rc`. The command is `shlex.join(DeskConfig.link_argv)`. The final
  comment is the marker that installs and removals match, as v1's
  `llm-switchboard-host:<mac>` is for its line. The line limits what the
  key can run; which Mac a link is comes from the proof (Admission, below).
- **Installer**: `llm-switchboard desk link-authorize <mac>` on the Pi reads
  one public-key line on stdin. Its steps:
  1. It runs the proof verifier's self-test (Admission, below) through the
     exact code path the hub uses. A failure refuses with "the Pi's
     ssh-keygen can't verify SSH signatures (OpenSSH 8.1 or later is
     needed); nothing changed", exit 2. The self-test is the version check:
     it needs no version parsing. `desk link-authorize --self-test` (no host,
     no stdin) runs only this step and changes nothing; the Mac's setup runs
     it first (Installing, below).
  2. **The key and the name**, before any file is read or locked.
     `host_authorize.public_key` parses the line, and its type must be
     `ssh-ed25519`, the only type `link-signers` holds: an RSA or ECDSA key
     is refused with "the desk link needs an Ed25519 key (ssh-keygen -t
     ed25519); nothing changed", exit 1. `pi` and bad names are refused
     the same way.
  3. **Check and write, inside the locks.** It takes `link-signers.lock`,
     then the `authorized_keys` lock (the one `ssh_authorized_keys` uses;
     always in that order), and holds both for the whole step. It reads
     `link-signers` through the hub's own reader (`link_proof`'s parser, the
     rule of Admission step 4) and reads `authorized_keys`. It refuses, with
     neither file changed, exit 1, when `link-signers` is unreadable or
     malformed ("link-signers is unreadable or malformed; nothing changed;
     move it aside and run link-authorize again for each Mac"), when the
     key blob appears on any other `authorized_keys` line or on another
     host's `link-signers` line (so the v1 key or another host's key can't
     be reused), and when the host would be a fifth. It then builds the
     whole proposed `link-signers` (this host's line replaced or added,
     every other line as read), checks it with the same reader, and only
     then writes it (temporary file, fsync, rename) and replaces any earlier
     `authorized_keys` line carrying its marker. So the duplicate and
     host-count checks see the files as they are written, a refused
     authorization leaves every existing signer as it was, and the hub never
     reads a file its reader rejects (which would retire every Mac's link).
  4. Holding no file lock, it asks a running hub for the signer check
     (operator op `link_drop {host}`, whose answer Revocation, below,
     describes) and prints `done` when the answer's `signer` is the
     fingerprint of the key it installed and its `link` is null or has that
     fingerprint. When `signer` is null or another key, a `link-revoke` or
     `link-authorize` for that host ran after this command released its
     locks: it prints "a later link-revoke or link-authorize changed <mac>'s
     signer line; the key this command installed is not listed" and exits 1.
     With no hub answering (`HubAbsent`), it applies the run lock rule (Hub
     side, below): holding the lock, it rereads `link-signers` through the
     hub's reader and reports `no hub running: no link to drop` as done when
     the host's line has its key, or the message above when it doesn't; with
     the lock held elsewhere the step fails with the run lock's refusal:
     confirmation changed nothing, the credential edits already made stand,
     and running the command again confirms them. Each outcome holds as of
     the hub's answer (or the offline reread): an edit that starts after it
     is not this command's to report, and the next command run sees it.
     When a lock of that host was handed over a link with another key, it
     prints that those locks now need `desk clear-stopped` (Hub side,
     below).

  Each step is idempotent. The command prints one line per step (`done`,
  `already so`, or the failure) and, on a failure, exits 1 naming the steps
  still to do; running it again completes them. Interrupted between the two
  file writes, the new key is listed but can't log in yet, the old key can
  still log in but its proof fails (the line names the new key), and the
  old key's established link is retired at the hub's next housekeeping pass
  (Established links follow the signer file, below).

  The `authorized_keys` edit (lock, temp file, fsync, rename, every
  unrelated line kept) moves out of `host_authorize.authorize` into a
  shared `ssh_authorized_keys.py` (`locked`, `replace_marked_line`,
  `remove_marked_line`) in a separate tidying commit that leaves v1's
  behavior and tests as they are; both commands use it.
- **Revocation**: `llm-switchboard desk link-revoke <mac>` on the Pi, in
  three idempotent steps reported as above. Steps 1 and 2 run in one hold
  of both file locks, taken in the installer's order (`link-signers.lock`,
  then the `authorized_keys` lock) and released before step 3. Since
  `link-authorize` holds the same two locks in the same order for its whole
  check and write, the two commands' edits never interleave: whenever
  neither holds the locks, both files show the commands that ran in one
  serial order (a command killed between its two edits leaves the state its
  interrupted step describes, and running it again completes it).
  1. Read `link-signers` through the same reader and write it back without
     the host's line (temporary file, fsync, rename). An unreadable or
     malformed file is refused with the message above, the command stops
     with neither file changed, and while the file is so the hub admits no
     link and retires every one.
  2. Remove exactly the `authorized_keys` line with that marker.
  3. **Confirm the cutoff**, holding no file lock: ask a running hub for
     the signer check (`link_drop {host}`). The hub rereads `link-signers`,
     retires under its links lock every link the file no longer lists
     (Established links follow the signer file, below), drops their
     registrations, and answers, from that one hold of the links lock, with
     the host's signer line as it read it and the host's live link, if any:
     `{ok: true, signer: null | {fingerprint}, link: null | {fingerprint}}`.
     The command prints `done` when both are null: as of the hub's answer,
     the signer state this command left still held and no link remained (an
     edit that starts after the answer is not this command's to report; the
     next command run sees it). When
     `signer` is a key, a `link-authorize` for that host ran after this
     command released its locks; the command prints "a later link-authorize
     listed key <fingerprint> for <mac>; this revocation was overtaken and the
     credential is not withdrawn; run link-revoke again to withdraw it" and
     exits 1. A `link` with a null `signer` can't come from an honest hub
     (the check retires every link the file doesn't list) and exits 1 as an
     unconfirmed cutoff.

  What step 1 alone gives. From the moment its rename lands, every read of
  the file sees the removal, but a publication that reread the file before
  that moment can still finish: its reread and its publication are one
  hold of the hub's links lock (Admission step 7), which the command doesn't
  take, so a link for the host can appear after step 1 returns and carry
  traffic until it is retired. The cutoff is definite once a signer check
  that began after step 1 has run: every publication that read the old line
  finished before that check took the links lock, and every later one reads
  the new file and is refused. Step 3's answer is such a check, confirmed.
  Without it, the next housekeeping pass is one, unconfirmed. With no hub
  answering (`HubAbsent`), step 3 applies the run lock rule (Hub side,
  below): holding the lock, no hub process exists to hold a link, and one
  that starts later reads the file at every admission, so the command
  rereads `link-signers` through the hub's reader and reports `no hub
  running: no link to drop` as done when the host has no line, or the
  overtaken message above when a later authorization listed one; with the
  lock held elsewhere (a hub starting or stopping, which may still hold a
  link) step 3 fails with the run lock's refusal, exit 1: confirmation
  changed nothing, and the credential edits steps 1 and 2 already made
  stand. A hub that doesn't answer leaves step 3 failed ("cutoff not confirmed; the hub
  retires the link at its next housekeeping pass; run link-revoke again to
  confirm"), exit 1.

  Interrupted after step 1, the key can still log in and get a pump but no
  link; running the command again removes the line and confirms. The v1
  line is never touched by either command, and `host-authorize` never
  touches the desk line (the markers differ).
- **The relay's ssh call.** The relay never lets ssh read a configuration
  file, so no `IdentityFile`, `CertificateFile` or agent from Jacob's
  configuration can be offered. Each connection attempt:
  1. **Routing from `ssh -G`.** `/usr/bin/ssh -G <pi alias>` and
     `/usr/bin/ssh -G -F none <pi alias>`. The alias is
     `LLM_SWITCHBOARD_PI_SSH` from the Mac's `local.env` (an environment
     variable of that name overrides it, as for v1; the launchd job sets
     none). Each runs with stdin `/dev/null`, 5 s, output read to at most
     64 KiB, no shell. Neither opens an SSH connection, but the first
     evaluates Jacob's configuration including its `Match exec`, whose
     command runs: on this Mac a short `nc` reachability probe that picks
     the LAN or Tailscale. The second gives OpenSSH's own defaults for the
     same alias. The relay reads each output as `<keyword> <values…>` lines
     (values split on whitespace) and takes:

     | Keyword | Passed to ssh | Validation |
     | --- | --- | --- |
     | `hostname`, `port`, `user` | always | one token each, no control characters or quotes; port 1..65535 |
     | `hostkeyalias` | when present (`-G` omits it when unset) | one token, the characters of a host name |
     | `userknownhostsfile`, `globalknownhostsfile` | only when the value differs from the `-F none` output | 1 to 4 tokens, each an absolute path (`-G` prints them with `~` expanded) without whitespace, control characters or quotes; passed as one `-o` value with the tokens space-separated, which is ssh's own list syntax |
     | `hostkeyalgorithms` | only when it differs from the `-F none` output | one token of `[A-Za-z0-9@._+,-]` |

     A value left at its default is not passed, so ssh under `-F none`
     applies the same default with its own default behavior (for host key
     algorithms, preferring the types already known for that host). The
     relay refuses (`ssh_config_unsupported`, a 10-minute backoff) when
     `proxycommand`, `proxyjump` or `knownhostscommand` is present and not
     `none`, since `-F none` would drop them and connect another way. Any
     other directive the configuration sets is dropped on purpose; the live
     checks connect on both routes.

     Checked 2026-11-23 on this Mac (OpenSSH_10.3p1) for the Pi's alias,
     values not recorded: `userknownhostsfile` and `globalknownhostsfile`
     each print two paths (`known_hosts` and `known_hosts2`), equal to the
     `-F none` defaults, so neither is passed; `hostkeyalias` is set, to the
     alias itself, so the Pi's host key is recorded under the alias on both
     routes and must be passed (without it ssh would look the key up under
     the route's address); `hostkeyalgorithms` is set; no proxy or
     known-hosts command; the output is the same under an environment of
     only `HOME` and `PATH`; each `-G` call took under 0.15 s.
  2. `/usr/bin/ssh -F none -T -i <key> -o IdentitiesOnly=yes -o
     IdentityAgent=none -o PubkeyAuthentication=yes -o
     PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o
     HostbasedAuthentication=no -o GSSAPIAuthentication=no -o BatchMode=yes
     -o StrictHostKeyChecking=yes -o UpdateHostKeys=no -o HostName=<hostname>
     -o Port=<port> -o User=<user> [-o HostKeyAlias=…]
     [-o UserKnownHostsFile=…] [-o GlobalKnownHostsFile=…]
     [-o HostKeyAlgorithms=…] -o ConnectTimeout=15 -o ServerAliveInterval=15
     -o ServerAliveCountMax=3 -o ControlMaster=no -o ControlPath=none -o
     ClearAllForwardings=yes -o ForwardAgent=no -o RequestTTY=no <pi alias>
     desk-link`, the bracketed options as the table says, with the
     environment `HOME` and `PATH` only (no `SSH_AUTH_SOCK`).
     `UpdateHostKeys=no` keeps the relay from ever writing `known_hosts`.
     The remote command word is informational; the forced command ignores
     it.

  Why `-F none`: with Jacob's configuration loaded, `-i` adds to the
  configuration's `IdentityFile` entries and `IdentitiesOnly=yes` keeps
  them, so a refused desk key would fall back to another key with an
  ordinary shell. Checked 2026-11-23 on the Mac (OpenSSH_10.3p1) with
  throwaway keys and a throwaway configuration: `ssh -F <cfg> -i desk -o
  IdentitiesOnly=yes -G x` lists both identity files; `ssh -F none -i desk
  -o IdentitiesOnly=yes -o IdentityAgent=none -G x` lists only the desk key
  and `identityagent none`. No multiplexing: a shared master would run the
  session under another key's authentication.

### Admission and proof of possession (hub, `desk/link_hub.py` and `desk/link_proof.py`)

The hub admits a connection on `link.sock` as the link for `<mac>` only when
the connecting side signs a fresh challenge with the private key whose
public half `link-signers` lists for `<mac>`. Nothing about the connecting
process, its parent or its SSH session counts. What this gives: the hub knows
which Mac key a link belongs to, so routing, locks and key rotation follow
keys, and a peer without a listed key (a wrong key, another host's key, a
misconfigured login) gets no link. It is not a limit on processes running as
`pi`, which hold the hub's authority already (Who the design defends
against).

1. **Accept.** At most 2 admissions run at once and at most 20 connections
   are accepted a minute across hosts; beyond either, the connection is
   closed at accept, unread (logged once a minute). Steps 2 to 7 share one
   10 s deadline from accept.
2. **Peer uid.** `SO_PEERCRED`'s uid must be the hub's (it repeats what the
   0600 socket in its 0700 directory already ensures).
3. **Hello.** `link_hello {versions, relay_build, host, machine}` (Framing,
   below). `host` passes `host_protocol.host_name` and is not `pi`;
   `machine` is 32 hex characters. No common version: `link_refused
   {version, versions}`.
4. **Signer entry.** `link-signers` is read through `read_bounded` (4 KiB)
   and validated strictly (at most 4 lines, each `<host>
   namespaces="llm-switchboard-desk-link" ssh-ed25519 <blob>`, hosts and
   blobs unique). `host` must have a line, else `link_refused
   {not_authorized}`; an unreadable or malformed file refuses every link
   (`signers_unreadable`).
5. **Challenge.** The hub sends `link_challenge {version, epoch, nonce}`:
   the negotiated version, its epoch, and 32 fresh random bytes as hex,
   used once. The relay answers `link_proof {v, signature}`: an armored
   SSH signature (`-----BEGIN SSH SIGNATURE-----` to `-----END SSH
   SIGNATURE-----`, at most 4 KiB) over the exact bytes

   ```
   llm-switchboard-desk-link-proof
   version=<version>
   host=<host>
   machine=<machine>
   epoch=<epoch>
   nonce=<nonce>
   ```

   in namespace `llm-switchboard-desk-link`, made by `/usr/bin/ssh-keygen
   -Y sign -f <key> -n llm-switchboard-desk-link` with the message on stdin
   and the signature on stdout (environment `PATH=/usr/bin:/bin` only, so no
   agent is consulted; 5 s; output read to at most 4 KiB).
6. **Verify.** `/usr/bin/ssh-keygen -Y verify -f /dev/fd/<a> -I <host> -n
   llm-switchboard-desk-link -s /dev/fd/<b>`, the message on stdin, `<a>` a
   pipe carrying only that host's validated `link-signers` line, `<b>` a
   pipe carrying the signature (so ssh-keygen reads exactly what the hub
   validated, and the hub writes nothing to disk). No shell; `pass_fds` only
   those two; environment `PATH=/usr/bin:/bin`; a 5 s deadline, then
   `child_process`'s cleanup; stdout and stderr each read to at most 4 KiB
   and never logged.
   Success is exit 0 and stdout beginning `Good
   "llm-switchboard-desk-link" signature for <host> with `. The log line is
   a code: `proof_ok`, `proof_bad`, `verifier_timeout` or
   `verifier_unavailable`.
7. **Publish**, under the hub's links lock: reread `link-signers`; the
   host's line must still carry the blob just verified (a revocation or a
   rotation since step 4 refuses, `not_authorized`). Then the per-host rate
   (below), the host cap (4), retiring the host's current link if there is
   one (Link objects, below), installing the new link object with the
   verified key's blob, its SHA-256 fingerprint and `machine`, and
   `link_welcome`. The reread and the publication are one hold of the links
   lock, so every signer check (below) runs wholly before or wholly after a
   publication, which is what lets a check confirm a cutoff (Revocation,
   under SSH, above).

The link's host is the principal whose `link-signers` line verified the
signature. The hello's `host` only says which line to try; no frame field
and no argv word decides it alone. The signed message binds the nonce, the
hub's epoch, the negotiated version, the host and the machine, so a proof
can't be replayed into another admission, run or version.

**Unattributed admission refusals.** Nothing before publication is
attributed to a host, since the hello's `host` is unproven until then. Every
`link_refused` the hub sends adds to one in-memory record per code: `{code,
count, last}`, plus, for `version` only, the refused relay's `versions` list
as validated (at most 4 integers). There is at most one entry per
`link_refused` code, so the record's size is fixed by the code table
whatever names hellos carry. It never changes any host's link state, so a
refused hello can't mark a healthy Mac's link as anything. The page and
`desk status` show it as one line per code (What the desk page shows,
below).

**Established links follow the signer file.** The signer check, under the
links lock, rereads `link-signers` (bounded, validated as in step 4) and
retires every link whose host has no line, or a line with a key blob other
than the one that link verified; an unreadable or malformed file retires
every link. Its registrations are dropped after the lock is released (Link
objects, below). It runs at two points:

- On `link_drop {host}` (operator op), which answers after the retirements,
  their registrations dropped, with that host's signer line as this check
  read it and its live link, both from the check's one hold of the links
  lock: `{ok: true, signer: null | {fingerprint}, link: null |
  {fingerprint}}`. This answer is the confirmed cutoff the commands report,
  as of the answer, and its `signer` is what each command compares with the
  state it intended (SSH, above). An unreadable or malformed file answers
  `{ok: false, error: "link-signers is unreadable or malformed"}` after
  retiring every link.
- As the first step of every housekeeping pass. A pass starts 5 s after the
  previous pass ended, and after the signer check it refreshes the policy
  files (waiting for their cross-process locks), expires requests, rechecks
  every registration and refreshes labels, so two signer checks are 5 s
  plus one pass apart. A pass normally takes well under a second, but it
  has no fixed upper bound, since it can wait on those file locks and on
  label reads; this is the fallback, not the confirmation.

So a revocation or a rotation reaches an established link through the file
alone, at the next housekeeping pass, whether or not the command that
edited it reached the hub. A stopped hub holds no links.

**Self-test.** `desk/link_proof.py` carries a fixed test vector (a public
key, a message, its signature; the private key was discarded when it was
made). At hub start, and in `link-authorize`, the verifier must accept the
vector and refuse it with one byte of the message changed, through the same
pipe path as step 6. A failure disables links (every listed host shows
`verifier_unavailable`, every `link_hello` is refused with it); Pi targets
are unaffected.

Checked 2026-11-23 on the Mac (OpenSSH_10.3p1) with throwaway keys:
`-Y sign` reads the message from stdin and writes a 322-byte armored
signature; `-Y verify` accepts it with the signature given as `/dev/fd/3`,
from a file and from a pipe, and exits 255 for another principal, another
namespace and a changed message. `-Y` exists from OpenSSH 8.1. The Pi side
(its OpenSSH version, `/dev/fd` pipes on Linux) is checked by the self-test
itself and in the live checks.

**The pump** (`bin/desk-link`, `desk/link_pump.py`) connects to `link.sock`
and copies whole lines both ways. It checks each line's length against
`MAX_LINK_LINE_BYTES` before forwarding it and exits on an oversize line
(`desk-link: oversize`, exit 4), on EOF from either side (exit 0; a partial
line is never forwarded), or when it can't connect: then it writes
`{"type": "link_refused", "reason": "hub_not_running", "versions": [1]}`
(the version-independent shape, `versions` from `link_protocol`) to stdout
and exits 3. It parses nothing else: the hub is the one parser. It connects
to whatever listens on `link.sock` and doesn't authenticate it (Who the
design defends against). Each direction blocks on a full peer: when ssh
stops reading, the pump stops reading the hub, and the hub's link writer
reaches its deadline (Deadlines, below).

### Framing (`desk/link_protocol.py`, both machines)

One JSON object per line, UTF-8, `\n`-terminated, at most
`MAX_LINK_LINE_BYTES` = 80 KiB. Every reader (hub, relay, pump) checks the
length before parsing. The hub and the relay close the link on an oversize
line, invalid JSON, a non-object, a wrong `v`, an unknown `type`, a field of
the wrong shape, a code outside its table, or a message the receiver can't
accept in its state (the exceptions for late messages are listed below).
The cap is the channel protocol's 64 KiB line plus room for the link fields,
so any channel frame the relay accepts fits once rewrapped (a test encodes
the largest valid reply both ways).

**Version negotiation.** The first frame each way has a shape that never
changes between versions and carries no `v`:

- relay -> hub: `link_hello {versions: [1], relay_build: "<git short hash or ''>",
  host, machine}`. `machine` is the first 32 hex characters of the SHA-256 of
  `llm-switchboard-desk-machine:` plus the Mac's hardware UUID
  (`gethostuuid`), so the raw UUID never leaves the Mac.
- hub -> relay: `link_challenge {version, epoch, nonce}`, the highest version
  both list; or `link_refused {reason, versions}` and close.

Then `link_proof {v, signature}` and `link_welcome {v, link_id,
heartbeat_seconds, idle_timeout_seconds, max_line_bytes}` (or
`link_refused`). Every later frame carries `"v": <version>`. A mixed pair
with no common version is refused, never downgraded silently:

- The hub counts it as an unattributed admission refusal (Admission,
  above), with the relay's version list; no host's state changes.
- The relay logs it once per change, refuses every channel hello with
  `desk link version mismatch`, and retries only after 10 minutes.

**Wire codes.** Every `reason` on the link is a code, `[a-z_]{1,40}`. There
is one table, `link_protocol.LINK_CODES`, keyed by message type: senders
build every reason through it and receivers check against it, so a code one
side emits is one the other side accepts (a test enumerates each sender's
code paths against it). Free text from a channel, from ssh or from the hub
never crosses the link and never reaches a log. The relay turns a code back
into the exact text a Pi channel would get from the hub for the same case,
so Claude sees the same words on both machines. The hub's reply refusal
reasons and the channel's undeliverable reasons become named constant sets
(`model.ReplyRefusal`, `channel.UndeliverableReason`, strings unchanged, a
tidying commit), and the table's entries are keyed maps over them, so a
reason without a code fails a test.

| Message | Codes |
| --- | --- |
| `link_refused` | `version`, `hub_not_running`, `too_many_reconnects`, `too_many_hosts`, `not_authorized`, `proof_failed`, `signers_unreadable`, `verifier_unavailable`, `admission_timeout` |
| `register_refused` | `not_enrolled`, `enrolled_on_another_host`, `too_many_registrations`, `too_many_registers` |
| `unregistered` (relay) | `channel_closed`, `channel_idle`, `channel_write_failed`, `channel_busy`, `relay_recheck_failed` |
| `drop` (hub) | `unenrolled`, `hub_recheck_failed` |
| `undeliverable` (relay) | the channel's own report, classified by the relay from `channel.UndeliverableReason`: `guard_session_file`, `guard_session_changed`, `stdout_failed`, `channel_other`. The relay's own, each meaning the relay handed no byte of the event to the channel: `channel_closed`, `channel_busy`, `channel_not_written`, `relay_recheck_failed` |
| `reply_result` | `ok: true` with `reason: null`; `ok: false` with one of `bad_reply`, `not_utf8`, `too_large`, `unknown_request`, `wrong_connection`, `not_outstanding`, `lock_store_error`, `session_changed` |
| `presence` `state` | `gone`, `running`, `unverifiable` |

**Messages (version 1).** `chan` is the relay's number for one channel
connection, from a counter that runs for the relay process's whole life
(1..2^31-1; the relay exits at the end of the range), so a `chan` never
recurs, across links either. `query_id` is an integer 1..2^31-1 from a
counter on the hub's link object, starting at 1 on each link. `request_id`
has the hub's shape (`model.REQUEST_ID_PATTERN`).

| Direction | Type | Fields | Meaning |
| --- | --- | --- | --- |
| relay -> hub | `register` | `chan, session_id, channel_pid, channel_start, claude_pid, claude_start, label, cwd` | A channel passed the Mac identity check. `label` is the session file's `name` (one line, at most `MAX_TITLE_LEN`, no control characters, may be empty); `cwd` its `cwd` (absolute, at most 4096, no control characters, may be empty) |
| hub -> relay | `registered` | `chan, generation` | The hub registered it; the relay answers the channel `welcome {epoch, generation}` |
| hub -> relay | `register_refused` | `chan, reason` | The relay answers the channel `refused {reason text}` and closes it |
| relay -> hub | `unregistered` | `chan, reason` | The channel connection ended or failed a Mac recheck; the hub drops that registration |
| hub -> relay | `drop` | `chan, reason` | The hub dropped the registration; the relay closes that channel connection |
| hub -> relay | `event` | `chan, request_id, content` | A handed-off desk event for exactly that channel connection |
| relay -> hub | `delivered` | `chan, request_id` | The channel reported writing it to Claude's stdin pipe |
| relay -> hub | `undeliverable` | `chan, request_id, reason` | The channel reported the event not written, or the relay handed it no byte (codes above) |
| relay -> hub | `reply` | `chan, request_id, text, spoken` | The channel's `reply` tool call, after the relay's checks (below) |
| hub -> relay | `reply_result` | `chan, request_id, ok, reason` | Passed back to the channel as `reply_result` with the code's text |
| hub -> relay | `presence_query` | `query_id, channel_pid, channel_start` | Is that Mac channel process still running? |
| relay -> hub | `presence` | `query_id, state` | The answer, on the link the query came on |
| either | `ping`, `pong` | `n` (a counter per link and direction, from 1) | Heartbeat; a `pong` echoes its `ping`'s `n`; never reaches a channel or Claude. The relay's records of its own pings are in Link freshness, below |

**Replies the relay refuses itself.** Before forwarding a channel's `reply`,
the relay checks that `request_id` has the hub's shape and is one of the
last 8 request ids it forwarded to that `chan` as an `event`. Otherwise it
answers the channel `reply_result {ok: false, reason: "no desk request with
that id on this session; the reply was not delivered"}` and forwards
nothing, so a model's typo never puts its text on the link. The hub still
decides acceptance of everything forwarded.

**Late and unsolicited messages.** Teardown races are normal, so these are
dropped quietly (counted, not logged per message), never treated as a bad
frame:

| Receiver | Message | When | Handling |
| --- | --- | --- | --- |
| relay | `registered` | the channel already left or was refused for time | send `unregistered {chan, channel_closed}` |
| relay | `register_refused`, `drop`, `reply_result`, `event` | the `chan` is already closed | ignore; for `event`, send `undeliverable {chan, request_id, channel_closed}` (nothing was written to any channel) so the hub's evidence is exact |
| relay | `reply_result` | no waiting reply for that id | ignore |
| hub | `unregistered`, `delivered`, `undeliverable`, `reply` | a `chan` at or below the highest `chan` named by a `register` on this link, not registered now | ignore (a `reply` gets no `reply_result`; the channel's own 10 s rule tells Claude it is unconfirmed) |
| hub | `presence` | a `query_id` at or below the highest this link issued, no longer outstanding (answered or timed out) | ignore |

A `chan` the receiver never saw on this link (the hub: above the highest
`chan` named by a `register` on it; the relay: not yet issued) and a
`query_id` above the highest issued on this link are protocol violations:
the link closes. Each rule compares with one integer kept per link, so the
history it needs is bounded however long the link lives.

### Deadlines, heartbeats and backpressure

Every wait has a total deadline that includes queue and lock waits. No
state, policy, enrollment, admission or links lock is held across link I/O
or a wait for it; the links lock covers in-memory work, socket shutdowns
and the bounded 4 KiB read of `link-signers`, and is never held while
taking a policy lock or the state's lock. On the relay, link-level timing (the handshake, the idle rule,
the freshness rule) uses `CLOCK_MONOTONIC`, which on macOS counts sleep, as
time does for the Pi on the other end; deadlines on the relay's local writes
(to ssh's stdin pipe and to channels) use `time.monotonic()`, which on macOS
is awake time and is the clock the channel's own timers use, so a sleep
alone never expires one.

| Operation | Total deadline | On expiry |
| --- | --- | --- |
| Admission (hub, accept to `link_welcome`) | 10 s | close; `admission_timeout` |
| Handshake (relay, from spawning ssh to `link_welcome`, partial lines and signing included) | 30 s (`ConnectTimeout` 15 s plus the hub's 10 s, with room) | the attempt fails then: logged `handshake_timeout`, backoff from 1 s; the ssh child then gets `child_process`'s cleanup (`SIGTERM`, up to 2 s, `SIGKILL`, `waitpid`), so it is gone by about 32 s |
| A hub frame to the link | 5 s from enqueue, enforced by the link's writer | the writer sets the frame's outcome (Writers, below) and the link is retired |
| `Hub.send`'s event | the frame's 5 s; the caller waits for the frame's outcome, guarded at 6 s | the outcome reaches `Hub.send` (Writers, below) |
| Presence at clear (enqueue to answer) | 5 s | `unverifiable` (503) |
| A relay frame to ssh | 5 s of awake time, enforced by the relay's ssh writer | the link is retired |
| A relay frame to one channel | 5 s of awake time, enforced by that channel's writer | the writer sets the frame's outcome; the channel is closed with `unregistered {channel_write_failed}`, preceded, for an `event` the writer never began, by `undeliverable {channel_not_written}` |
| A channel's `register` (hello to `welcome`) | the channel's 5 s (`WELCOME_WAIT_SECONDS`) | the relay waits 3.5 s for `registered`, then answers the channel `refused {desk link slow}`; a later `registered` gets `unregistered` |
| A channel's `reply` (the tool call to `reply_result`) | the channel's 10 s (`REPLY_WAIT_SECONDS`), counted from the tool call, not from the event | the channel tells Claude the delivery is unconfirmed and resets its connection; the hub's decision stands and the desk shows it |

**Heartbeats.**

| Leg | Rule |
| --- | --- |
| Hub <-> relay | The hub sends `ping {n}` after 15 s without sending; the relay sends `ping {n}` every 15 s whatever else it sends (the freshness rule needs acknowledged pings). Each side answers a `ping` with its `pong` and retires the link after 45 s without a frame from the other. The hub measures on `time.monotonic()` (the Pi doesn't sleep); the relay on `CLOCK_MONOTONIC`, plus the freshness rule (Link freshness and sleep, below) |
| Relay <-> each channel | The relay answers a channel's `ping` with `pong` itself, sends the channel `ping` after 15 s without sending to it, and closes a channel with no frame from it for 45 s (`unregistered {channel_idle}`). The unchanged channel pings every 15 s from its one serve thread, which is also the thread that writes events to Claude's stdin, so a channel blocked on that write goes silent and is unregistered within 45 s; an idle healthy channel stays up on the relay's pings |
| ssh | `ServerAliveInterval` 15 x `ServerAliveCountMax` 3: a dead TCP path ends the ssh process within about 45 s even when no frame is due. It doesn't end a live transport whose far end sends no frame; the handshake deadline and the idle rule do |
| Pump | No timer: it exits when either side closes, so the hub's and ssh's timeouts end it |

**Writers: one outcome per frame.** Every writer (the hub's, one per link
object; the relay's to ssh; the relay's, one per channel) is one thread
that owns its socket or pipe, writes it non-blocking (`send`, `os.write`
and `poll`) and enforces each frame's deadline itself; the logic is shared
in `desk/link_writer.py`. Each queued frame has one outcome cell, set once,
under the writer's lock:

| Outcome | Who can set it | Means |
| --- | --- | --- |
| `not_begun` | only on positive evidence that the frame was never marked begun: the writer, for such a frame whose deadline passed or whose link or channel retired first; retirement or a channel close, for every queued frame not marked begun. The writer marks a frame begun, under the same lock, before it hands the kernel any byte, and never begins a frame whose outcome is already set | no byte of the frame reached the kernel, so nothing can emit it |
| `written` | the writer only | the kernel accepted every byte, the final `\n` included |
| `unfinished` | the writer, for every frame it marked begun and didn't complete, whatever number of bytes the kernel accepted, zero included (the peer closed, the link or channel retired, or the socket stayed full until the deadline, right after the begun mark or mid-frame); or a waiter whose 6 s guard ran out (a writer bug), which can set nothing else | part, all or none of the frame may have gone; it counts as attempted |

When a deadline passes or a write fails after the begun mark, the writer
shuts the socket or pipe down before it sets the outcome, so no later byte
can complete the frame, and it decides from the byte counts `send`
returned, which are exact: `written` when they cover the whole frame,
otherwise `unfinished`, zero bytes included. A frame marked begun never
becomes `not_begun`. Retiring a link or closing a channel from another thread
shuts the socket down, which fails the writer's next `send`; only the
writer thread closes the descriptor. A deadline that passes just after the
kernel accepted the final byte, before the writer recorded it, is still
decided by the writer from its counts, as `written`. A frame proven to lack
its final `\n` is still `unfinished`, never evidence of non-delivery: the
receivers' whole-line readers would drop it, but the request table doesn't
depend on that.

How outcomes reach a request:

- **`Hub.send`** calls `connection.send_event(request_id, content) ->
  WriteOutcome`, new on both connection kinds (`send()` keeps its boolean
  for every other frame). A Pi `ChannelConnection` returns `written` when
  `sendall` returns and `unfinished` on any error, exactly phase 1's
  `attempted=True`; a `RemoteChannel` returns its link frame's outcome.
  `Hub.send` maps `written` to `finish_write(written=True)`, `not_begun` to
  `finish_write(False, attempted=False)` and `unfinished` to
  `finish_write(False, attempted=True)`, and drops the connection unless
  `written`. Only `not_begun` lets the lock clear without a presence check.
- **The relay's channel writer** has no `Hub.send` waiter: the relay reports
  each `event` frame's outcome to the hub itself, once the writer (or the
  close that ended the channel) has set it. `not_begun` sends `undeliverable
  {channel_not_written}` (or `{channel_busy}` when the event never fit the
  queue). `unfinished`, zero bytes accepted included, sends no
  `undeliverable`, only `unregistered {channel_write_failed}`, so the hub
  drops the registration and the request needs a presence check. `written`
  waits for the channel's own report. Whatever closes a channel (the idle
  rule, a failed recheck, a full queue, the writer itself), the relay sends
  one of its own `undeliverable` codes for an event only when the event was
  never queued to that channel's writer or its outcome is `not_begun`; for
  any other outcome, the registration's `unregistered`,
  whatever its code, is what the hub acts on, and the request needs a
  presence check. When the link itself is gone, nothing is reported (Link
  objects, below).

**Queues and caps.**

| Where | Bound | When full |
| --- | --- | --- |
| Hub -> link | One writer per link object, 64 frames and 1 MiB | The link is retired. An `event` that couldn't be queued is `not_begun` |
| Pump | One line in flight per direction | It stops reading its source |
| Relay -> ssh | One writer, 64 frames and 1 MiB | `reply`: refused back to the channel as `reply_result {ok: false, reason: "desk link busy; the reply was not delivered"}` (never queued; the request stays handed off; Claude may call `reply` again). `register`: the channel is refused `busy`. `delivered`, `undeliverable`, `unregistered`, `presence`: the link is retired, since losing them would lose evidence. `ping`: skipped (the idle rule ends a link that stays stuck) |
| Relay -> each channel | One writer per channel, 8 frames | An `event` that doesn't fit is `not_begun`: `undeliverable {channel_busy}`, then `unregistered {channel_busy}`, and the channel is closed. Any other frame: the channel is closed and `unregistered {channel_busy}` |
| Relay channels | 16 verified and 8 unverified connections | Refused at accept |
| Relay refusal cache | 32 entries (LRU) | A channel refused `not_enrolled` is refused locally, for that session id and claude pid, for 25 s, without a `register` (Enrollment, below) |
| Hub per link | 16 registrations, 4 outstanding presence queries, 120 `register` frames a minute | `register_refused {too_many_registrations}` or `{too_many_registers}`, the link kept; a presence query beyond 4 answers `unverifiable` at once |
| Hub admissions | 2 at once, 20 accepts a minute, 6 admissions a minute per host | closed at accept; per host, `link_refused {too_many_reconnects}` |
| Hub link waits | At most 4 HTTP calls and 1 operator call waiting on any link at once (a send to a Mac target, a clear that needs a Mac presence answer) | HTTP: 503 `desk link busy; try again`, before any handoff (a send's pending request expires and is never handed off; a clear changes nothing); operator: `{ok: false, error: "busy: a Mac link call is in progress"}` |

So a slow or saturated link holds at most 4 of the hub's 16 HTTP slots and 1
of its 4 operator slots, each for at most 6 s, and its reader threads only
enqueue. Pi sends, token revokes, `desk status`, `link_drop`, `link_fence`
and the housekeeping pass never wait on a link; they need
only a free slot on their own surface, which a link can't fill. (The
tablet's 25 s long polls hold HTTP slots as in phase 1.)

**Reconnect and backoff (relay).** 1 s, doubling to 60 s, with ±20 %
jitter, reset once a link has stayed up 60 s; `handshake_timeout` follows
this schedule. `hub_not_running`: doubling to 5 minutes. `version`,
`not_authorized`, `proof_failed`, `verifier_unavailable`,
`ssh_config_unsupported`, ssh exit 255 with a `permission_denied` or
`host_key` stderr code, or an ssh exit whose first stdout line isn't a link
frame (`forced_command_failed`: a missing `bin/desk-link` under Python
exits 2, not 127): a fixed 10 minutes, logged once per change. ssh's stderr
is read to at most 64 KiB and only classified (`permission_denied`,
`host_key`, `connection_refused`, `timeout`, `name_resolution`, `other`);
the code is logged at most once a minute.

Hub logs for the link: link up, down and refused (host once published,
version, code), registrations (session id prefix, generation), refusals,
request ids of the hub's shape, presence results. Repeated identical
refusals are logged at most once a minute per code. Never content, labels
or folders.

### Link objects, replacement and the fence on old work

Each admitted link is one object (`LinkSession`: `link_id` from the hub's
counter, host, key blob and fingerprint, machine, socket, writer, reader,
the highest `chan` registered, the presence query counter, a `retired`
flag). On the relay the same holds for its side (`RelayLink`). Nothing ever
looks a host's link up by name to act on it:

- A remote registration's proxy, a pending presence query, a queued frame
  and a reader each hold the link object they began on. Writing to a
  retired object fails at once (`not_begun`), without touching its
  successor.
- **Retiring** (EOF, idle, a bad frame, a writer deadline, replacement, the
  signer check, `link_drop`, hub stop), under the links lock: set
  `retired`; remove the host's entry only if it is this object; shut the
  socket down; set every queued frame not marked begun to `not_begun` (a
  frame the writer had begun gets its outcome from the writer). Then,
  outside the lock, every registration whose proxy holds this object is
  dropped through `_drop_connection`, so its requests follow
  `drop_registration` (`pending` -> `expired`, `handed_off` -> `unknown`,
  the lock kept), and its pending presence queries answer `unverifiable`.
  Between the two parts its registrations still exist but can't take a new
  event: the handoff gate (Hub side, below) reads the object's `retired`
  flag under the links lock.
- **Replacement**: a new link for a host that has one retires the old one
  first (as above; no `drop` is sent, the old socket is simply shut down),
  then publishes the new one. Replacement is what makes a wake from sleep
  reconnect at once while the Pi still holds the half-open old link. The old
  reader's finalizer drops only its own object's registrations, so it can't
  drop the successor's.
- The relay, on retiring its side: closes every channel connection, which
  sets their queued frames `not_begun` (an `event` among them is reported
  nowhere: its link is gone, and the hub has already made the request
  `unknown`); waiting `reply` calls answer `connection to the desk hub
  dropped; delivery unconfirmed` (the channel's existing wording). The
  channels reconnect through their own backoff (1 s doubling to 30 s), are
  refused `desk link down` until a new link is up, then register afresh
  with new generations.

### Link freshness and sleep (relay)

The relay doesn't detect sleep. It acts on a hub frame only while it has
evidence that the hub's idle rule can't have retired the link, and a sleep
is simply time on `CLOCK_MONOTONIC`, which keeps counting through it on
macOS (checked 2026-11-23: it differs from the awake-only
`CLOCK_UPTIME_RAW` by the Mac's accumulated sleep).

- **Acknowledged time.** Before the relay enqueues `ping {n}` for its ssh
  writer, it reads `CLOCK_MONOTONIC` and records `{n: t}`. No byte of that
  ping can reach the kernel before the record exists, so `t` is no later
  than the hub's receipt of the ping, however long the writer or the whole
  relay is then descheduled or asleep, before or after the kernel takes the
  bytes. A time is usable only once the matching acknowledgement arrives:
  when `pong {n}` finds a record for `n`, `acked_at` becomes the later of
  itself and that `t`, and the record goes. `link_proof` works the same
  way: its `t` is read before it is enqueued, and `acked_at` becomes that
  `t` when `link_welcome` arrives.
- **Unmatched records are bounded.** At most 4 ping records are kept (a
  fifth drops the oldest), and a record 40 s old is dropped, since its
  `pong` could no longer pass the rule. A `ping` skipped because the queue
  is full makes no record. A `pong` whose `n` is above the highest `n` sent
  on this link is a protocol violation (the link closes); one at or below
  it with no record (already matched, dropped or expired) is ignored and
  changes nothing.
- **The freshness rule.** Before dispatching any frame received from the
  hub other than a `pong` (to a channel, or to its own handling), the relay
  requires
  `CLOCK_MONOTONIC − acked_at < 40 s`; otherwise it retires its side at
  once, dispatching neither that frame nor any later one. It holds because
  the hub received the frame recorded at `acked_at` before it sent the
  matching `pong` or `link_welcome`, and that frame reached the kernel no
  earlier than `acked_at`, so the hub's last receipt is at or after
  `acked_at`; and the hub retires a link only after 45 s without a frame.
  While the rule passes, the hub's idle rule can't have fired.
- **Awake**, the relay pings every 15 s, so `acked_at` is at most about
  15 s plus a round trip and the ping's time in the queue old; the 25 s
  margin absorbs a slow link and scheduling delay, and a link whose pongs
  stop is retired by the 45 s idle rule anyway. There is no threshold on clock noise: the rule compares one
  clock with a fixed bound far above it.
- **Across a sleep**, the time asleep counts. After a sleep that leaves
  `acked_at` 40 s or more behind, the relay retires the link before acting
  on anything that arrived; after a shorter one, the hub can't have retired
  it for idleness, so frames that arrive were written while it was current
  and are dispatched in order.
- **What the rule doesn't do.** It governs dispatch decisions only. It
  cancels nothing a writer has begun: a frame whose writer began before or
  during a sleep (to ssh or to a channel) ends by its writer's rule, with
  that writer's deadline on awake time. A dispatch decided before a sleep
  stands. Relay frames that ssh accepted before a sleep reach the hub after
  wake only if that link object is still current there; on a retired link
  the hub never reads them.
- **Other retirements.** The hub also retires links for a writer deadline,
  a bad frame, replacement, the signer check, `link_drop` or its own stop.
  The relay learns of these at EOF, and frames the hub wrote before
  retiring may be dispatched before the EOF arrives, awake or not. For an
  `event` that leaves the request `unknown` with its lock needing a presence
  check, which is the state the hub has already recorded; nothing is
  retried and the lock isn't released.

### Identity on the Mac (`desk/peer_mac.py`)

These checks keep honest processes from being confused with one another
(Who the design defends against); none of them is a security boundary.

Process facts come from the kernel through `ctypes` (standard library):
`proc_pidinfo(PROC_PIDTBSDINFO)` for the parent pid, uid and start time
(`pbi_start_tvsec`, `pbi_start_tvusec`), `proc_pidinfo(PROC_PIDUNIQIDENTIFIERINFO)`
for `p_uniqueid` and `p_puniqueid`, `proc_pidpath` for the executable, and
`sysctl(KERN_PROCARGS2)` for the exec path and argv. Same-user processes are
readable without privileges. A process that is gone (`ESRCH`) is distinct
from one that can't be read (anything else).

**The pinned-interpreter rule**: a process runs pinned argv `A` when its
argv from the second element on equals `A[1:]`, and its first element, its
`KERN_PROCARGS2` exec path and its `proc_pidpath` all equal the interpreter
`A[0]` really runs: `realpath(A[0])`, or, when that is a framework build's
`…/Python.framework/Versions/<v>/bin/python<v>`, that framework's
`…/Versions/<v>/Resources/Python.app/Contents/MacOS/Python`. Homebrew's
framework Python re-executes into `Python.app` with the same pid and
replaces argv[0] (checked 2026-11-23 with the checkout's `.venv/bin/python`,
3.13.2), so the phase 1 rule (argv equal, exe equal to `realpath(argv[0])`)
can never hold on the Mac. The expected path is worked out at each check, so
it follows a Homebrew upgrade; processes started before an upgrade are
refused until they restart.

**The channel check** (the relay, on each connecting channel, before it
says anything to the hub; again before every event and every reply it
forwards, and every 5 s):

1. **Peer**: `getsockopt(SOL_LOCAL, LOCAL_PEERPID)` gives the pid, and
   `LOCAL_PEERCRED` the uid, which must be the relay's (other users).
2. **Pinned entrypoint**: the peer runs `DeskConfig.channel_argv`
   (`-I -B <repo>/bin/desk-channel`) by the pinned-interpreter rule, so the
   relay talks only to desk channel servers.
3. **Parent is an installed Claude with the desk flags first**: the peer's
   parent pid (never `CLAUDE_PID`) is a live process whose `p_uniqueid`
   equals the peer's `p_puniqueid` (pid reuse can't substitute a parent);
   whose `proc_pidpath` is a regular file directly in
   `realpath(DeskConfig.claude_versions_dir)` named for a version (the Pi's
   `startup_identity.is_installed_claude` rule, reused); and whose argv
   elements 1 to 4 are exactly `DeskConfig.launch_flags` (`--mcp-config
   <mcp.json> --dangerously-load-development-channels server:desk`). In
   first position nothing can make them the value of another option or put
   them after `--`, so they are effective options of that launch. This keeps
   the opt-in per launch: a session whose MCP configuration starts the desk
   channel some other way is never registered. Checked 2026-11-23 on this
   Mac's own running Claude: `proc_pidpath` is
   `~/.local/share/claude/versions/<version>`, the exec path
   `~/.local/bin/claude`, argv[0] `claude`.
4. **Session file**: `~/.claude/sessions/<parent pid>.json`, read through
   `bounded_read.read_bounded` (one non-blocking open, no link followed, a
   regular file, at most 64 KiB), has `pid` equal to the parent's pid,
   `sessionId` equal to the hello's session id, and `procStart` equal to the
   parent's start second; and the file's `st_mtime` is at or after the
   parent's start (`pbi_start_tvsec`, `pbi_start_tvusec`), so a file left by
   an earlier process with the same pid and start second is refused. On the
   Mac `procStart` is a UTC ctime-style string (`"Mon Nov 23 02:00:40
   2026"`): the relay collapses runs of spaces, parses it with `%a %b %d
   %H:%M:%S %Y` as UTC, and compares it with `pbi_start_tvsec`. Claude
   rewrites the file while it runs (checked 2026-11-23: a live file's mtime
   was 10 minutes after its process started), so the mtime rule never
   refuses a live session.
5. The identity the relay keeps and sends: `channel_pid`, `channel_start`
   (`pbi_start_tvsec * 1_000_000 + pbi_start_tvusec` as decimal digits, so
   it fits `model.CHANNEL_START_PATTERN`), `p_uniqueid` of both (kept on the
   Mac only), `claude_pid`, `claude_start` (same digit form) and the session
   id.

**Recheck** (`peer_mac.recheck`): the channel still has the same pid,
`p_uniqueid` and parent; the parent still has its `p_uniqueid`; the session
file still names the session. Enrollment is not checked on the Mac (the hub
holds it). A failed recheck before an event: `undeliverable
{relay_recheck_failed}`, then `unregistered {relay_recheck_failed}`, and the
channel is closed, with nothing written to it. Before a reply: the channel
gets `reply_result {ok: false, reason: "session changed"}`, then
`unregistered`, and it is closed. Every 5 s: `unregistered`.

**The last check is the channel's own** (`HubLink._session_still_ours`,
under its stdout lock, right before the write), and its boundary is not
atomic: between that check and Claude reading the line from its stdin pipe,
`/clear` can still move the session, and the event then reaches the new
session. The checks narrow that window; nothing closes it. `delivered`
therefore means the line was written to the pipe, not that Claude handled it
in the session the hub chose.

**Conflicting registrations.** The relay doesn't arbitrate: it forwards
every verified channel. Two live registrations for one session id from the
same host are **ambiguous** at the hub, as on the Pi, and neither is routed
until one goes.

**Presence** (`peer_mac.process_presence`): `gone` when `proc_pidinfo` says
`ESRCH` or the start time differs, `running` when it matches, `unverifiable`
otherwise.

**No listener check, on either machine.** A channel doesn't check which
process listens on its socket, on the Mac as on the Pi (phase 1 behavior,
unchanged). The 0600 socket in its 0700 directory keeps other users out, and
the relay checks each channel's uid. No honest process stands in for the
relay by accident: `bind_private_socket` refuses to bind while something
listens, the relay's `flock` keeps one relay, and a stale socket file
refuses connections. A process running as Jacob that binds the socket while
the relay is down is out of scope.

**What the hub takes from the relay, and what it decides itself.**

- Taken as honest reports, once the link has proved the Mac's key: that a
  `register` names a channel that passed the channel check on the Mac; that
  `delivered`, `undeliverable` and `reply` come from that channel connection
  after a recheck; that `presence` answers truly. The hub can't verify
  these, and under the threat model it doesn't need to.
- Decided by the hub alone: the host (the verified signer); the epoch; the
  generation; whether `(host, session id)` is enrolled; ambiguity; tickets,
  request ids, the target lock and its clear rule; whether a reply is
  accepted (the request handed to that registration, still handed off, the
  size caps, strict UTF-8, first valid wins); sends on or off; and every
  readiness label.
- A relay cannot make a Pi target. Every registration from a link gets that
  link's host, and a session id whose enrollment names another host is
  refused (`enrolled_on_another_host`). The relay never sees a Pi session's
  events, since the hub writes an event only to the registration it handed
  it to.
- The relay can't originate a request: the link has no message that creates
  one.

### Hub side (`desk/link_hub.py`)

- `LinkHub` owns `link.sock`: accept, admission, negotiation, replacement,
  one reader and one writer thread per link object, the link heartbeat,
  presence queries, the 5 s signer check, candidates, the per-host link
  records, the unattributed refusals, and the link-wait caps.
- A remote registration is a `Registration` whose `host` is the link's host
  and whose `identity` is a `model.RemoteIdentity(host, link_id, chan,
  channel_pid, channel_start, claude_pid, claude_start, session_id)`. Its
  conn_id comes from the hub's own counter (`Hub._conn_ids`), so local and
  remote connections share one id space.
- Each remote registration also has a `RemoteChannel` proxy in
  `Hub._connections` under its conn_id, holding its link object, with the
  `ChannelConnection` surface the hub already calls plus `send_event`:
  `send_event` enqueues the link's `event` and waits for its outcome
  (Writers, above); `send(REPLY_RESULT)` enqueues and returns; `send(PING)`
  is a no-op (the link has its own heartbeat; the relay pings the channel);
  `close()` enqueues `drop` when its link is live and never waits. So
  `Hub.send`, `_drop_connection`, `_drop_unenrolled` and `_send_heartbeats`
  work for Mac targets without branches, and only `send_event` and a
  presence clear wait on a link, within the link-wait caps.
- Link frames call the hub's existing handlers with the proxy as the
  connection: `state.mark_delivered`, `Hub._handle_undeliverable`,
  `Hub._handle_reply`. The link layer maps `_handle_reply`'s
  `model.ReplyRefusal` to its code and a `None` reason to `reason: null`.
- `Hub._recheck(identity)` for a `RemoteIdentity`: `None` while its link
  object is not retired and is still the host's current link (which, by the
  signer check, still had its key in `link-signers` at its last signer
  check), and `(host, session id)` is still enrolled for that host; else the
  reason. It is cheap and local, so it runs in the housekeeping pass, before
  handoff and before a reply as for Pi identities. Before handoff it is the
  early check; the handoff gate is the one the fence relies on.
- **The handoff gate.** `Hub._prepare_handoff` for a `RemoteIdentity`
  enters `LinkHub.handoff_gate(identity)` inside its policy-file locks,
  around the reread of the files and `HubState.hand_off`. Entering takes
  the links lock briefly: the identity's link object must not be retired
  and must be its host's current link, else 409 `target changed; not sent`
  and the request expires with no lock written; on success it adds one to
  the hub's count of remote handoffs, kept under the links lock, and
  releases the lock. Leaving, on every path (`finally`), takes the links
  lock again and subtracts one. `hand_off` returns only after the lock is
  fsynced or its write has failed, so a handoff leaves the gate only once
  its write to `locks.json`, whole or failed, is over. A failed write sends
  no event and expires the request, but it may already have changed the
  file: `LockStore._write` can complete its `os.replace` and then fail the
  directory's fsync, leaving the Mac lock in `locks.json` though the hub's
  list lacks it, and the hub then records the store's error. The gate
  doesn't cover that case; the fence's fresh read of the file and its check
  of the store's error do (Switching it off, below). So a count of zero and
  no live link object, both read in one hold of the links lock, mean no Mac
  handoff can still write to the lock file: none is past the gate, and one
  still between its dispatch recheck and the gate (registrations of a
  retiring link included) fails at the gate. No lock is held across
  `hand_off`'s fsync by the gate, and the links lock is never taken while
  the state's lock is held.
- **The run lock: what "no hub" means.** An absent operator socket
  (`HubAbsent`) proves only that nothing listens there. A hub that is
  starting (before `OperatorServer.start`) or stopping (after `Hub.close`,
  which closes its listeners but can't join the daemon HTTP, operator,
  channel and link threads still running a request) has none, and such a
  thread can still be inside the handoff gate or hold a link. So the `desk
  serve` command (`cli._serve`), after making the run directory and before
  it constructs anything of the hub, takes an exclusive `flock` on
  `runtime_dir/hub.lock` (created 0600 if missing, opened `O_NOFOLLOW`
  and `O_CLOEXEC`, so no child the hub spawns inherits the descriptor) and
  never releases it: the descriptor closes when the process exits, which
  ends every thread. It tries non-blocking every 0.1 s for up to 5 s
  (longer than any command's hold, below) and otherwise exits 3, "another
  desk hub or a desk command holds hub.lock", and systemd starts it again
  5 s later. Only then does it call `Hub(config).serve(run_lock)`;
  `Hub.serve` takes the held lock as an argument, so a hub can't be served
  without one.
  - **Nothing is read before the lock.** No hub state is read from disk
    for a live hub until the run lock is held: not the lock table, the
    enrollment files, the settings, `link-signers` or the TLS files. Taking
    the lock before `Hub(config)` puts every read the constructors and
    `Hub.start` make after it. `Hub.__init__`'s chain reads only the lock
    table (`HubState.__init__` calls `LockStore.load`); `Enrollment`,
    `DeskSettings`, `SessionLabels`, `StartupAck` and the two servers read
    nothing until called or started, and `LinkHub`'s constructor reads
    nothing (`link-signers` is read by the self-test, at admission and at
    the signer check, all from `Hub.start` on). An incoming hub that waited
    for an outgoing one therefore loads the lock table the outgoing hub
    left, never one read while that hub could still write.
  - **What it gives.** The lock is held from before a hub process's first
    read of its state until after its last thread, and two hub processes
    never overlap, so a hub that answers on the operator socket is the only
    one that can hold a link or a handoff.
  - **The rule.** Every offline decision that rests on there being no hub
    (no live link, no handoff in progress, no in-memory state that
    disagrees with the files) takes `hub.lock` non-blocking on `HubAbsent`
    (creating it as the hub does) and holds it while it reads and decides;
    while it holds it no hub process exists and none can start. It never
    waits for the lock: held elsewhere, the step refuses and changes
    nothing (edits an earlier step of the same command made stand), "a
    desk hub process is starting or stopping; run <command>
    again once the hub answers, or once `systemctl --user is-active
    llm-switchboard-desk` prints inactive", exit 1. Its hold covers only
    bounded, non-blocking reads of `link-signers` and `locks.json`. The
    lock is only ever taken non-blocking, so it can't join a deadlock, and
    a command holding it takes no other lock. The decisions under the
    rule: the offline `link-fence`, `link-revoke` step 3's and
    `link-authorize` step 4's no-hub outcomes (SSH, above). A phase 1 hub
    process doesn't take the lock; it holds no link and writes no Mac lock,
    so none of these decisions depends on it.
  - **Offline paths that don't rest on absence.** The offline `desk
    enroll` and `desk unenroll` (with or without `--host`) and `desk sends`
    rest on the policy files' cross-process locks, which every handoff
    holds from its reread through its lock write (Hub state, above), so a
    stopping hub's handoff either sees the edit or the edit waits for it.
    The offline enroll's lock check reads `locks.json` through
    `inspect_lock_file` (Locks bound to the Mac, below) under
    `enrolled.json`'s lock, and a lock is added only by a handoff holding
    that lock (a clear only removes one, which can only make the check
    refuse); a stopping hub's registrations don't need checking, since a
    handoff requires its registration's host to equal the owner it
    rereads. They take no run lock, and phase 1's offline edits behave as
    before.
  - **No offline form.** `clear-stopped` acts only through a running hub:
    with none answering it exits 1, "the desk hub is not running; start it
    and run clear-stopped again", with nothing changed, because a hub
    process rewrites `locks.json` whole from its own list.
- **Clearing a Mac lock**: `Hub.clear` looks the lock up first
  (`HubState.lock_info(session_id, lock_id)`, a read under the state's
  lock). For a host other than `pi`, presence counts only from the host's
  current link when that link's key fingerprint and machine equal the
  lock's `link_key` and `machine` (Locks, below). The hub asks that link
  outside every lock, within 5 s and within the link-wait cap, the query
  bound to its `query_id` and to that link object (an answer on any other
  link, or after the timeout, is not used), and then calls the unchanged
  `HubState.clear_lock` with a `channel_process` that returns that answer.
  `gone` is final (pid and start time are never reused together on one
  machine), so it can't go stale in between. No such link (down, or a link
  with another key or machine): `unverifiable`, 503, with the reason `the
  Mac that holds this lock is not connected, so its channel can't be
  checked; reconnect that Mac, or, once you have seen the session exit
  there, use desk clear-stopped on the Pi`. A delivery the channel reported
  (`delivered`, `undeliverable`) in this hub run needs no presence check, as
  on the Pi.
- **Operator-only override**: `llm-switchboard desk clear-stopped <session
  id> <lock id>` on the Pi's shell, operator op `clear_stopped` (a phase 1
  hub answers `unknown op`). It is for a lock whose Mac can't answer: the
  link removed for good, the key rotated, the Mac replaced.
  - The command first shows the lock (host, session id, since, key
    fingerprint, machine prefix) and the statement it records: "I have
    seen this Mac session exit and its desk channel is not running". On a
    terminal Jacob types the session id's first 8 characters to confirm;
    off a terminal, `--confirm <lock id>` must repeat the exact lock id.
    The op carries `confirm` equal to the lock id. This guards against a
    mistaken clear, not against anything running as `pi`.
  - The hub rechecks everything at the mutation, under the state's lock:
    the lock exists with that id and session; its host is not `pi`; no
    current link for that host has the lock's key and machine (when one
    does, the presence clear is the way); the request is not in delivery.
  - It is never offered on the page and no HTTP route reaches it; it is
    logged (lock id, session prefix, host).
- **The rollback fence**: operator op `link_fence` (`desk link-fence`,
  Switching it off, below), built on the signer file, the live link objects,
  the handoff gate's count, and `locks.json` read from disk through
  `inspect_lock_file` together with the hub's list in one hold of the
  state's lock; offline, on the run lock.

### Locks bound to the Mac that wrote them

A lock handed over a link records, besides phase 1's fields, `link_key` (the
link's verified key fingerprint, `SHA256:<base64>`) and `machine` (the
hello's machine id, part of the signed proof). `locks.py` requires exactly
phase 1's fields for a `pi` lock and phase 1's fields plus these two for any
other host. Presence about a lock is taken only from a link with the same
key and machine, so a replacement Mac that takes the name (a new key), or a
second Mac given a copy of the key (another machine id), never answers
`gone` for the original's channel. Replacing a host's key is therefore
always safe for locks: the old key's locks become clearable only by
`clear-stopped`, which `link-authorize` says when it replaces a key.

**Reading `locks.json` without the store.** `locks.inspect_lock_file(path)`
returns the file's locks without touching any `LockStore` instance: it reads
through `bounded_read.read_bounded(path, MAX_LOCK_FILE_BYTES)` (one
non-blocking open, no link followed, a regular file only, at most 64 KiB)
and decodes with the store's own decoder (`_parse`, at `MAX_LOCKS`). A
missing file is empty; a FIFO (with or without a writer), a symlink, any
other non-regular file, an unreadable, oversize or undecodable file raises
`LockStoreError` at once. It changes no list and no broken flag, so it can
run beside the hub's live store. Every read of `locks.json` outside the
store goes through it: the online and the offline fence, and the offline
enroll's lock check. The hub's own `LockStore.load` at start is phase 1's
and is unchanged.

### Enrollment for Mac sessions

- **The Mac's opt-in is the launch.** A Mac session takes desk events only
  when Jacob starts it with `claudep --desk`, which puts the pinned flags
  first in claude's argv; the relay requires them (identity step 3).
  Nothing on the Mac records enrollment.
- **The hub's list governs routing.** `config_dir/enrolled-hosts.json` on
  the Pi: `{"hosts": {"<host>": {"<session id>": {"since": <unix s>}}}}`,
  0600, at most 4 hosts, 64 sessions and 64 KiB, read through
  `read_bounded` and validated as strictly as `enrolled.json`. It is a
  separate file because the phase 1 code (supervisor, picker, hub) reads any
  unknown key in `enrolled.json` as a malformed file, which would unenroll
  every Pi session. It is read and written only under `enrolled.json`'s
  lock (`Enrollment.locked()`), so the lock order is unchanged. It is a
  control for honest operation (Who the design defends against).
- **One owner per session id, enforced, not assumed.** Session ids are
  random UUIDs, but `--session-id`, a copied transcript resumed elsewhere,
  or unenrolling on one host and enrolling on another can all put one id on
  two hosts. The hub keeps targets keyed by session id with `host` as an
  attribute, and checks the host everywhere it matters:
  - **One published view.** `refresh_files` reads both files under
    `enrolled.json`'s lock and, before flattening anything, builds
    `{session id: owners}` from `enrolled.json` (owner `pi`) and every host
    map in `enrolled-hosts.json`. An id with more than one owner, whether
    `pi` and a Mac or two Macs inside `enrolled-hosts.json`, goes into the
    published `conflicts` set and is enrolled nowhere: its target shows
    `enrolled on two hosts` and nothing routes to it until an unenroll
    leaves one owner. The rest is published as one map `{session id: host}`
    in one `HubState.set_enrollment(enrollment, conflicts)` call, so no
    reader sees one file's change without the other's. An unreadable or
    malformed `enrolled-hosts.json` publishes no Mac enrollment (Pi
    enrollment stays). The desk's edits never create a second owner
    (below); one appears only from a hand edit, a bug, or a phase 1 CLI's
    offline `desk enroll` of an id a Mac owns, and in each case the id is
    unroutable, never routed to one of its owners.
  - **Registration, both ways.** A Mac `register` is refused unless the
    published view names that link's host for the id; a Pi channel's
    registration is refused unless it names `pi` (the phase 1 `is_enrolled`
    becomes "enrolled for `pi` in the published view", which differs from
    phase 1 only for an id that some Mac also owns).
  - **Tickets and handoff.** A ticket is bound to `(epoch, host, session_id,
    generation)`; `issue_ticket` and `hand_off` both require the target's
    registration host to equal the published enrollment host.
  - **Edits, online and offline, one rule.** An enroll for any host, `pi`
    included, from the page, an operator op or the offline CLI (`desk
    enroll` and `desk enroll --host`, when the connect proved no hub
    listens), reads both files (bounded, validated) under `enrolled.json`'s
    lock before writing either. It is refused when either file is
    unreadable or malformed, since exclusivity can't then be checked
    (online 503 `enrollment files unreadable; nothing changed`, offline
    exit 1; nothing written). It is refused (409 `enrolled on another
    host`, offline exit 1) when the id has an owner on another host in
    either file, a live registration from another host, or a lock for
    another host (online: the hub's state; offline: `locks.json` through
    `inspect_lock_file`, and a file it refuses refuses the edit too). The
    offline edit needs no run lock (Hub side, above). An unenroll needs only its own file readable, since removing an
    owner can't create a conflict. This adds refusals to the Pi's `desk
    enroll`, online and offline: an unreadable `enrolled-hosts.json`, and an
    id a Mac owns or holds a lock for.
  - **The page** keys its selection, its request history, its speech and
    its enrollment mutations by `(host, session_id)`: `renderReplies` and
    `speakNewReplies` match both fields, so after a reassignment it never
    shows or speaks one host's answer under the other.
- **Host-qualified operations the phase 1 code refuses.** A new CLI or page
  can meet a phase 1 hub during an upgrade or a rollback, so Mac enrollment
  never rides the phase 1 messages:
  - Operator ops `enroll_host {session_id, host}` and `unenroll_host
    {session_id, host}`; a phase 1 hub answers `unknown op`, and the CLI
    reports "the running desk hub is older and can't enroll Mac sessions;
    nothing changed" (exit 1). The CLI falls back to the offline edit only on
    `HubAbsent`, never after an answer.
  - HTTP `POST /desk/api/enroll-host` and `/desk/api/unenroll-host
    {session_id, host}`; a phase 1 hub answers 404, and the page says the
    same. The page is served `no-store`, but a tab left open across a
    rollback keeps the newer script, so this matters.
  - The new hub refuses `enroll`/`unenroll` (op or route) carrying a `host`
    key (400 `use enroll_host`), so nothing host-qualified is ever silently
    read as a Pi edit. `enroll` without `host` stays the Pi edit.
- **Enrolling a running Mac session takes effect at once. This is the Mac
  exception to "enrolling takes effect at the next launch".** On the Pi the
  launch adds the desk flags, so enrollment waits for it. A Mac session was
  already launched with the desk flag, so enrolling it (from a candidate,
  the typed form or `desk enroll --host`) makes that running session
  reachable from the desk within about a minute (the relay's 25 s refusal
  cache plus the channel's backoff), with no action on the Mac. The page's
  button reads **Enroll now** and its confirmation says "Enroll now: this
  running Mac session becomes reachable from the desk immediately"; the CLI
  prints the same; the guide says it.
- **Candidates**: a `register` the hub refuses only because the session
  isn't enrolled adds or refreshes an in-memory candidate `{host,
  session_id, label, folder, seen}`. `label` is cut to 80 characters and
  `folder` is the last two components of `cwd`, cut to 120 characters, so
  each entry is small whatever the frame carried. At most 16 candidates,
  each forgotten 10 minutes after its last attempt. A session unenrolled in
  the last 10 minutes is not listed (a second table of at most 16 entries,
  oldest evicted). The desk page lists candidates as **Mac sessions asking
  to join**, each with **Enroll now**. `desk status` lists them without the
  folder.
- **Unenroll stops routing at once**: `_unenroll_host` rewrites the file
  under the locks, republishes enrollment, and `_drop_unenrolled` drops
  every registration of that session, which for a Mac session enqueues
  `drop`. Routing stops in the hub before the call returns, whatever the
  relay does. An unenroll that has returned is never overtaken by a handoff,
  by the same locks as on the Pi. It recalls nothing already handed off: an
  event the relay or the channel holds, or Claude has queued, can still be
  acted on (Who the design defends against, the off switches), and its
  request and lock follow the request table. The Mac session keeps running and its
  channel keeps retrying, refused (and throttled by the relay's cache).
- **The picker** shows the enrollment of Mac rows read-only, the same badge
  as Pi rows (`desk_enrolled`), from `enrolled-hosts.json` for that host.

**The launcher change** (a separate change in the Mac shell-config repo,
`mac/shell/agent-commands.zsh`, which the parent routes there): `claudep`
takes `--desk` as its first argument only, removes it, and puts the flags
`llm-switchboard desk flags --lines` prints (one argument per line) first,
before every other argument. `claudepr` and the other launchers are
unchanged; a desk resume is `claudep --desk --resume <session id>`.

```zsh
claudep() {
  local -a desk_flags
  if [[ ${1-} == --desk ]]; then
    shift
    desk_flags=("${(@f)$(llm-switchboard desk flags --lines)}") || return
    if (( ${#desk_flags} != 4 )); then
      print -u2 "claudep --desk: llm-switchboard desk flags failed"; return 1
    fi
  fi
  command claude "${desk_flags[@]}" --permission-mode bypassPermissions "$@"
}
```

`desk flags --lines` is new (the existing space-joined `desk flags` stays for
the Pi's bash callers); zsh doesn't word-split `$(...)`, and lines keep a
path with spaces whole. `llm-switchboard` is on the Mac's PATH through the
`~/.local/bin` link `scripts/setup-mac.sh` makes, and it runs the checkout's
`.venv` Python, so the flags name the Mac checkout's `mcp.json`. On code
without `--lines` (a rollback), the command fails and `claudep --desk`
launches nothing.

**Installing the Mac side**: `scripts/setup-mac-desk.sh` (new; v1's
`setup-mac.sh` is unchanged), run on the Mac after `setup-mac.sh`. The first
two steps check both machines and change nothing on either:

1. Sign and verify a test message through `ssh-keygen -Y` with a throwaway
   key in a temporary directory, deleted afterwards; stop when the Mac's
   ssh-keygen can't.
2. `ssh <pi alias> /home/pi/dev/llm-switchboard/bin/llm-switchboard desk
   link-authorize --self-test` (Jacob's ordinary SSH access to the Pi, as
   v1's setup uses); stop when the Pi's OpenSSH is too old (exit 2).
3. Generate `~/.ssh/llm-switchboard-desk-<mac>` if absent.
4. `ssh <pi alias> /home/pi/dev/llm-switchboard/bin/llm-switchboard desk
   link-authorize <mac> < ~/.ssh/llm-switchboard-desk-<mac>.pub`.
5. `llm-switchboard desk install-config` (the Mac's `mcp.json`).
6. `<repo>/.venv/bin/python -m llm_switchboard desk relay-install`, which
   refuses off macOS or without the key, writes the plist and bootstraps it
   (the bootout-then-retry pattern of `host_launchd._bootstrap`).
7. `launchctl print gui/$(id -u)/dev.llm-switchboard.desk-relay`.

`scripts/setup-mac-desk.sh --remove` undoes it: `desk relay-install
--unload` (the switch the Mac enforces: bootout, delete the plist, confirm
no relay is left), `ssh <pi alias> … desk link-revoke <mac>` (withdraws the
credential on the Pi and confirms the cutoff), then deletes the two key
files (tidying). It leaves `mcp.json`, which nothing loads without `--desk`.
Like every off switch, it recalls no event a session already holds
(Switching it off, below).

### Targets, tickets, requests, locks and replies

- `Registration`, `Request` and the lock already have or gain `host`
  (`model.HOST` by default, so Pi code paths and the conversation-mode
  changes need no edits). `hand_off` writes the lock's `host` from the
  registration, and for a remote registration its `link_key` and `machine`
  from the link object.
- `model.KNOWN_HOSTS` gives way to `model.is_desk_host(value)`: `pi` or a
  valid host name. The lock file's validator uses it.
- `Target.host` is the session's host. `POST /desk/api/ticket` takes an
  optional `host`; when given it must equal the target's host (409 `wrong
  host`). The generation is unique within the epoch across hosts.
- `Request` views carry `host`. The state body gains `links: [{host, state:
  "up"|"down"|"verifier_unavailable", since, signer: true|false}]` (hosts in
  `link-signers`, with an enrollment, with a lock, or with a link published
  in this hub run; never a name from an unproven hello),
  `admission_refusals: [{code, count, last, versions}]` (Admission, above)
  and `candidates`.

**Request states across the link.** "Handed off" is recorded, and the lock
fsynced, before the hub enqueues the `event` frame, exactly as for a Pi
channel. A handed-off request is never retried by anyone: the hub writes each
event once, the relay forwards it at most once to the one channel `chan`
names and discards it if that channel is gone, and nothing re-sends a frame
after a reconnect. A lost acknowledgement anywhere reads as unknown, and
only evidence that no byte of the event was handed on lets a lock clear
without a presence check.

What the channel's reports prove: `delivered` means the line was written to
Claude's stdin pipe, not that Claude handled it (see the last check, above);
a guard refusal (`guard_session_file`, `guard_session_changed`) means the
channel wrote nothing; `stdout_failed` means the write failed and Claude's
end of the pipe is gone, so whether a prefix of the line reached it is
unknown. Each ends the writer's part (nothing can emit the event later), so
each permits Jacob's explicit clear after he has checked the session, and
none unlocks the target or retries anything by itself.

| What happens | Request | Lock | Clearing it |
| --- | --- | --- | --- |
| Link down, or registration gone, before `hand_off` (the dispatch recheck or the handoff gate refuses) | `pending` -> `expired` | none | nothing to clear; safe to send again |
| `hand_off` done; the hub's link frame `not_begun` (not queued, retired first, or its deadline passed before the writer began it) | `unknown` | kept | clears at once: nothing could be written |
| The hub's link frame `unfinished` (marked begun and not proven complete, zero bytes accepted included, or the waiter's guard ran out) | `unknown` | kept | presence check (part or all of it may have reached the pump) |
| `written` on the link; link lost before a report | `unknown` | kept | presence check through a link with the lock's key and machine: `running` refuses (409, stop the session), `gone` clears |
| The relay's channel frame `unfinished`, zero bytes included (`unregistered {channel_write_failed}`, no `undeliverable`) | `unknown` | kept | presence check |
| `delivered` received; link lost before the reply | `unknown` | kept | Jacob's clear needs no presence check in this hub run (the writer reported), and he checks the session first, since the line reached Claude's pipe; after a hub restart, presence check |
| `undeliverable` with a guard code, or the relay's `channel_closed`, `channel_busy`, `channel_not_written`, `relay_recheck_failed` | `unknown` | kept | clears at once: no byte reached Claude |
| `undeliverable {stdout_failed}` | `unknown` | kept | clears at once; the channel can't write it later, but a prefix may have reached a Claude that was exiting |
| Mac channel disconnects (session exit, `/clear`) | `unknown` | kept | presence check: normally `gone` |
| Mac sleeps | the hub retires the link 45 s after the last frame it got (the relay pings every 15 s, so any sleep from about 30 s up); once its last acknowledged ping is 40 s old the relay acts on nothing more from it (freshness rule) | kept | as link loss; on wake the relay reconnects and the channels re-register with new generations |
| Relay restarts or is unloaded, or the Mac's credential is revoked | link loss, channels disconnected | kept | as link loss; the switch recalls nothing a channel or Claude already holds, so it never clears a lock by itself |
| Hub restarts (new epoch) | requests forgotten | kept, `earlier_run` | presence check through the next link with the lock's key and machine |
| Reply arrives on a new link | refused (`wrong_connection`) | kept | as above |
| Link lost while a reply is in flight | the hub may have accepted it | removed only if accepted | the channel tells Claude `delivery unconfirmed`; the desk shows what the hub decided |
| The Mac's key is rotated, or another Mac takes the name | unchanged | kept | only `desk clear-stopped`, after Jacob saw the session exit on the original Mac |

A tab's ticket or review bound to an older generation or epoch is stale, as
on the Pi.

### Readiness, Test, and the startup warning on the Mac

- Readiness is the hub's, unchanged: a Mac registration shows `registered`
  ("no desk reply verified yet") until an exchange Jacob tapped, or the
  **Test** button, is answered through `reply` on that registration, then
  `replied`. No hidden probe crosses the link; the heartbeats never reach
  Claude.
- The Mac combination (`claudep --desk` through the model gateway, tool search
  on, Jacob's hooks) counts as ready only after a disposable Mac session
  answers Test through `reply` on that exact setup; the hub can't see the
  model, so this is an operator gate written here and in the guide, repeated
  for a model not yet tried.
- **Startup warning**: Claude shows "Loading development channels" at every
  `claudep --desk` launch, and Jacob presses Enter in his own Mac terminal.
  There is no tmux and no supervisor on the Mac, so the hub's startup
  acknowledgement (`/desk/api/startup`, `desk ack`) lists and acts on Pi
  supervisor panes only; a Mac session never appears there. Until the Enter
  the session file may not exist, so the relay refuses the channel and it
  retries; the desk shows the Mac target as **not connected**, with the hint
  "launch it with `claudep --desk` on the Mac and press Enter at the warning
  there", plus the link's state.

### What the desk page shows

- Each target carries a host badge (`Pi`, or the Mac's name).
- One line per listed host: **Mac link up**, **Mac link down since
  <time>**, or **Mac links unavailable: the Pi can't verify Mac keys
  (OpenSSH 8.1 or later is needed)**. While a host's link is down its
  targets are listed (enrolled, with any lock) but have no registration, so
  Dictate, Review, Send and Test are disabled for them; enroll, unenroll and
  clear still work (clear answers as above).
- Unattributed refusals, one line per code seen in this hub run, never
  under a host: **A Mac link was refused: version mismatch (the relay speaks
  2, the hub speaks 1; update both machines), last at <time>**, or **A Mac
  link was refused: <code>, <count> times, last at <time>**.
- **Mac sessions asking to join**, each with **Enroll now** and its
  confirmation (Enrollment, above).
- The typed enroll form gains a host choice (`pi` and the hosts in `links`);
  choosing a Mac host shows the same **Enroll now** confirmation.
- No **Acknowledge startup warning** for Mac targets.

### Privacy

The boundary, exactly: **the desk itself writes no prompt or reply content to
any log, to the v1 host exchange or to the Pi's disk, and adds nothing to v1
frames.**

- Desk prompts and replies cross between machines only on the link, only
  between the hub and the relay, and only for a request the hub handed off
  (prompt) or a reply to one (text and spoken).
- On the Pi they live in the hub's memory (the 20-reply, 256 KiB ring, as
  for a Pi session), which, like any process memory, can be paged to the
  Pi's swap. The desk's files on either machine never hold them.
- No desk log holds prompt or reply text, labels or folders: the hub's, the
  relay's, the pump's one exit line, the relay's launch log. Logs carry
  codes, ids and prefixes only.
- The desk adds nothing to the v1 exchange's frames, host rows, snapshots
  under `~/.local/share/llm-switchboard/hosts/`, the action queue and its
  history, or the picker's POST audit log, and never reads them.
- **A desk exchange is part of the Mac session's own transcript**, which
  follows the rules that already apply to every Mac session (AGENTS.md
  "Mac sessions"). An unnamed session's title is its first prompt cut to one
  line and goes to the Pi as metadata (Jacob's call on 2026-10-26). Whether
  a desk event can be that prompt is v1's extraction rule applied to the
  record Claude Code writes for a channel event: `transcripts.prompt_text`
  skips text that begins with `<`, so a wrapped event record is not a
  prompt and an unwrapped one is; the desk changes that rule in neither
  direction. The Mac's own search index covers the transcript under its
  usual extraction rule, on the Mac only, and an explicit `fork-pi` uploads
  the whole transcript, desk exchanges included. The desk doesn't filter
  these and doesn't promise more than v1 does for any Mac session.

**AGENTS.md's "Mac sessions" rule** says "the Pi never connects to it" and
"Nothing queued may start a process or send a prompt there." The link
doesn't queue v1 actions or start a process, and the Mac opens it, but it
does carry prompts to the Mac, with full authority. In that rule, "Nothing
queued may start a process or send a prompt there." becomes "Nothing queued
through the exchange may start a process or send a prompt there.", followed
by:

> The one exception is the voice desk's link (`docs/voice-desk.md`, "Mac
> link"), accepted by Jacob on 2026-11-23 with full authority. It is the one
> way the Pi's side can send a prompt to a Mac session, and it reaches only
> sessions launched with `claudep --desk` and registered with the Mac's
> running desk relay, which opens the link (the Pi still never connects to
> the Mac). Those sessions run with bypass permissions, so whatever controls
> the Pi's `pi` account can, through the link, make them do anything
> Jacob's account can do on the Mac; the desk's enrollment list, pairing and
> sends switch govern honest use, not that authority. Unloading the Mac's
> desk relay ends the link and is the switch the Mac enforces; `desk
> link-revoke <mac>` withdraws the Mac's credential from the Pi; launching
> without `--desk` keeps a session out of the link's reach. None of them
> recalls an event a session already holds, so none means a session is
> quiet or a lock is safe to clear. The desk writes no prompt or reply text
> to any log, to the host exchange or to the Pi's disk, and adds nothing to
> the exchange's frames. A desk exchange is part of the session's
> transcript on the Mac, which these rules cover like any Mac session's.

### Bounds

| Where | What | Bound |
| --- | --- | --- |
| Pi, SD | `config_dir/enrolled-hosts.json` | 4 hosts, 64 sessions, 64 KiB; rewritten only on enroll and unenroll |
| Pi, SD | `config_dir/link-signers` | 4 lines, 4 KiB; rewritten only by `link-authorize` and `link-revoke` |
| Pi, SD | `~/.ssh/authorized_keys` | one desk line per Mac, at most 4 (replaced, never appended twice) |
| Pi, SD | `state_dir/locks.json` | unchanged: 64 entries, 64 KiB, Pi and Mac locks together (a Mac lock is about 100 bytes longer) |
| Pi, SD | `runtime_dir/hub.lock` | one empty file, never written or deleted; holds only a `flock` |
| Pi, memory | admissions | 2 at once (2 threads), 20 accepts a minute, 10 s each, plus up to 2 s of a timed-out verifier's cleanup |
| Pi, memory | links | 4 hosts, 2 threads each; writer queue 64 frames / 1 MiB each; one remote-handoff count for the hub |
| Pi, memory | registrations, presence queries | 16 per link within the hub's 64; 4 queries per link; one `chan` and one `query_id` high-water mark per link |
| Pi, memory | unattributed admission refusals | one entry per `link_refused` code (9), each with at most 4 version numbers |
| Pi, memory | candidates | 16 entries (label 80, folder 120 characters) for 10 minutes; 16 recently unenrolled |
| Pi, memory | verifier | one `ssh-keygen` per admission, a 5 s deadline then up to 2 s of cleanup, 4 KiB of output |
| Pi, threads | link waits | 4 of the 16 HTTP slots, 1 of the 4 operator slots, 6 s each |
| Pi, operator socket | `desk status` | fits the 64 KiB response line at the worst case (64 registrations, 64 locks, links for every host those and the files name, 9 refusal codes, 16 candidates without folders, labels escaped as `\uXXXX`) |
| Pi, journal | hub link logs | state changes and once-a-minute refusals, under journald's limits |
| Mac | relay log | 1 MiB x 2, uncaught thread exceptions included |
| Mac | relay launch log | 256 KiB plus the output of one failed start; nothing written after logging starts |
| Mac, memory | relay queues | 64 frames / 1 MiB to ssh; 8 frames per channel; 16 + 8 channels; refusal cache 32; 4 ping records, none older than 40 s |
| Mac, processes | relay children | one ssh, at most one `ssh -G` and one `ssh-keygen` at a time, each with a deadline, then at most 2 s of `SIGTERM` grace before `SIGKILL`, reaped on every path |

`ROOTFS_PATH_BUDGETS_MIB` (the scripts repo's
`pi/scripts/backup/backup_conf.sh`) has an entry for
`/home/pi/.local/state/llm-switchboard/desk` (1 MiB) and none for
`/home/pi/.config/llm-switchboard/desk`, which phase 1 already writes
(`enrolled.json`, `desk-settings.json`, `mcp.json`, the TLS leaf) and which
gains `enrolled-hosts.json`, `link-signers` and the empty `run/hub.lock`:
the same change adds
`/home/pi/.config/llm-switchboard/desk:1` (a separate change in that repo).

### Switching it off, revocation and rollback

- **Stop the relay job (Mac)**: `<repo>/.venv/bin/python -m
  llm_switchboard desk relay-install --unload` boots out the job and deletes
  its plist, so it doesn't return at login, and reports done only when
  `launchctl print` no longer finds the label and a non-blocking `flock` on
  `relay.lock` succeeds, so no relay process is left (it waits up to 5 s for
  one that is exiting, then exits 1 naming the lock's holder as still
  running). From then the Mac has no link and opens none until the relay is
  loaded again on the Mac; this is the switch the Mac enforces. The Pi's
  signer line stays, so loading the relay again restores the link. Running
  `--desk` sessions keep retrying the missing socket, each failure one line
  in that channel's stderr (Claude's MCP log), backing off to every 30 s;
  launching with `--desk` stays harmless while it's off.
- **Withdraw the Mac's credential (Pi)**: `llm-switchboard desk link-revoke
  <mac>` (Revocation, under SSH, above): the signer line, then the key line,
  in one hold of both file locks, then `link_drop`, whose answer confirms,
  as of the answer, that no link for that Mac is live, that none can be published from the
  removed line, and that the hub read no signer line for that Mac (one
  listed by a later `link-authorize` is reported, exit 1). A hub that doesn't
  answer leaves the cutoff unconfirmed (exit 1); it still retires the link
  at its next housekeeping pass. The relay's next attempts are refused and
  it retries every 10 minutes, logging it once. This withdraws the
  credential from the Pi's installation; it is not a defense the Mac
  enforces against the `pi` account, which can write both lines back while
  a relay is loaded (Who the design defends against). `scripts/setup-mac-desk.sh
  --remove` on the Mac does both switches: it unloads the relay, runs this
  over ssh, and deletes the Mac's key files (tidying).
- **Take a session out of reach (Mac)**: close it, or launch it without
  `--desk`. Unenrolling it on the desk stops the hub handing it new events
  at once (Enrollment, above); it is not a limit the Mac verifies.
- **What no switch does.** Unloading the relay, withdrawing the credential
  and unenrolling act only on events not yet handed to a channel. None
  recalls an event a channel already holds (written to Claude's stdin pipe,
  or waiting at that write) or one Claude has read and queued behind a
  running turn or a dialog; an honest session can still act on it after the
  command returns, with its full authority, even if the relay never runs
  again. So none of them means the session is quiet, and none makes a lock
  safe to clear: a lock clears by presence `gone`, by a channel report in
  this hub run, or by `clear-stopped` after Jacob has seen the session exit
  (Targets, above). Closing the session on the Mac is what makes it quiet.
- **While off**, the desk shows **Mac link down since <time>**, Mac targets
  unavailable with their locks, and nothing for Pi targets changes.
- **Rolling the Pi back to phase 1 code**, in this order:
  1. `desk link-revoke <mac>` for every Mac host, each confirmed.
  2. For each remaining Mac lock: stop that session on the Mac, see it
     exit, then `desk clear-stopped <session id> <lock id>`.
  3. Immediately before the downgrade: `llm-switchboard desk link-fence`
     must exit 0. With a hub answering it asks it (operator op
     `link_fence`); by the run lock (Hub side, above) that hub is the only
     process that can hold a link or a handoff. The hub, in one hold of its
     links lock, rereads `link-signers`, which must be readable and have no
     line, and confirms that no link object is live and that the handoff
     gate's count is zero. No Mac handoff can then write to the lock file:
     none is past the gate, none can pass it without a live link, and no
     link can be published without a signer line. After releasing the links
     lock, it takes the state's lock, which every lock write holds
     (`LockStore.add` and `remove` run only under it), and in that one hold
     reads its in-memory list, its store's error and `locks.json` from disk
     through `inspect_lock_file` (Locks bound to the Mac, above), so the
     disk view and the list are of one moment and no concurrent lock write,
     for a Pi target or any other, falls between them. It refuses when the
     file is one `inspect_lock_file` refuses (unreadable, not a regular
     file, oversize or malformed), when the store reports an error (which
     covers a failed write that still replaced the file, Hub side, above),
     or when the file or the list holds a lock whose host isn't `pi`. With
     no hub answering (`HubAbsent`), the command applies the run lock rule:
     with the lock held elsewhere it refuses ("a desk hub process is
     starting or stopping; run link-fence again once the hub answers, or
     once `systemctl --user is-active llm-switchboard-desk` prints
     inactive"); holding it, it reads `link-signers` through the hub's
     reader and `locks.json` through `inspect_lock_file`, with the same
     refusals (a missing file is empty to both), decides, and releases it.
     Any refusal exits 1 naming what is left; a handoff in progress reads "a
     Mac send is being handed off; run link-fence again". The fence never
     waits for a handoff or for the run lock (it takes the state's lock as
     any operator op does) and holds nothing after it answers. Its exit 0
     holds as of its answer; after a confirmed fence no Mac link
     can be admitted, so no Mac handoff and no new Mac lock can happen until
     `link-authorize` runs again; don't run it between this step and the
     downgrade.
  4. Downgrade. A phase 1 hub reads a `locks.json` entry with a host other
     than `pi` as a corrupt lock file and refuses every send until it's
     gone; it ignores `enrolled-hosts.json` and `link-signers`; its
     operator socket and routes answer the host-qualified operations with
     `unknown op` and 404. A forced command whose `bin/desk-link` is missing
     exits 2 (Python can't open the script) and the relay backs off 10
     minutes (`forced_command_failed`).
- **Rolling the Mac back**: unload the relay first (`relay-install
  --unload`), so launchd doesn't keep restarting a `bin/desk-relay` the old
  code lacks. Channel processes keep the code they started with until their
  sessions exit. The channel protocol is the same in both versions, so a
  channel of either version works with either relay. One channel behavior
  differs between versions: the bounded session-file read (Module plan,
  commit 1). A running channel keeps the read it started with until its
  session restarts, after an upgrade (a channel started earlier keeps phase
  1's unbounded, link-following read in its final guard) and after a
  rollback past that commit alike. It matters only for an accident (a FIFO
  or a huge file in place of the session file blocks that channel's
  delivery; the relay's idle rule unregisters a blocked channel within
  45 s, and the request needs a presence check), so nothing gates on it.
  Nothing else in this design depends on which channel code a running
  process has (there is no listener check to lose; other users are kept
  out by the socket's modes in both versions). On rolled-back code `claudep
  --desk` launches nothing (no `desk flags --lines`). Close the flagged
  sessions to put every channel on one version.

### Module plan

Commits that come before the feature, each with the whole suite green:

1. **Behavior: bounded session-file reads.** `peer.read_session_file`
   reads through `bounded_read.read_bounded` (64 KiB, one non-blocking open,
   no link followed, a regular file). Today it is an unbounded, following
   `read_text()`, and the channel's final guard (`_session_still_ours`) calls
   it under the stdout lock, so a FIFO or a huge file in its place blocks
   event delivery; the Pi hub's `verify_peer` and `recheck` use it too. After
   this commit such a file reads as `None` (the guard refuses with
   `guard_session_file`; the hub refuses or drops the registration), on both
   machines. Tests: `test_session_file_fifo_does_not_block_channel_guard`,
   `test_oversize_session_file_reads_none`,
   `test_session_file_symlink_reads_none`.
2. **Tidying**: `ssh_authorized_keys.py` out of `host_authorize.py`, v1
   behavior and tests unchanged.
3. **Tidying**: `model.ReplyRefusal` and `channel.UndeliverableReason` as
   named constant sets, strings unchanged.
4. **Tidying**: `model.WriteOutcome` and `ChannelConnection.send_event`;
   `Hub.send` maps the outcome to `finish_write` (Writers, above). For a Pi
   channel the mapping gives exactly phase 1's `written`/`attempted` values,
   which the existing send tests confirm.

New modules, one concern each, standard library only (`ctypes` included),
none importing the HTTP stack:

| Module | Machine | Concern |
| --- | --- | --- |
| `desk/link_protocol.py` | both | Link framing: versions, the hello and challenge shapes, `LinkMessage`, `MAX_LINK_LINE_BYTES`, field validators, the one code table `LINK_CODES`, link timing and caps |
| `desk/link_writer.py` | both | The one-thread writer: non-blocking writes, per-frame deadlines, begun marking, the outcome cells, shutdown-before-decision; used by the hub's link writer and the relay's ssh and channel writers |
| `desk/child_process.py` | both | Bounded child runs: spawn with `close_fds`, capped pipe reads, a total deadline, terminate and reap on every path; used by `link_proof`, `relay_ssh` and `relay_link` |
| `desk/link_proof.py` | both | The signed message, the namespace, the bounded `ssh-keygen -Y sign` and `-Y verify` calls, the self-test vector, `link-signers` parsing |
| `desk/link_pump.py`, `bin/desk-link` | Pi | The forced command: connect to `link.sock`, pump bounded whole lines both ways |
| `desk/link_hub.py` | Pi | `LinkHub`: admission, `LinkSession` objects and their writers, `RemoteChannel` proxies, presence queries, the signer check (housekeeping and `link_drop`, with its answer: the signer fingerprint it read and the live link, from one hold of the links lock), the handoff gate and its count, the unattributed refusals, the link-wait caps, candidates, link records, the fence check |
| `desk/remote_enrollment.py` | Pi | `enrolled-hosts.json` under `enrolled.json`'s lock; the owners map, conflicts and the published view; the edit checks over both files and the locks, online and offline |
| `desk/link_authorize.py` | Pi | `desk link-authorize` (and `--self-test`) / `link-revoke`: the Ed25519 check, the checks and the whole proposed `link-signers` validated by the hub's reader inside both locks, the `link-signers` line and the `authorized_keys` line in their order, both commands holding both locks across both edits, the `link_drop` confirmation against the answer's signer fingerprint, the no-hub outcomes under the run lock, idempotent steps and their report; the offline half of `link-fence` (under the run lock, through `inspect_lock_file`) |
| `desk/run_lock.py` | both | A process-lifetime exclusive `flock` (`O_NOFOLLOW`, `O_CLOEXEC`, never released before exit; the hub's bounded start wait) and the non-blocking probe held for a block; used by `desk serve` before it constructs the hub and by the offline decisions (`hub.lock`), and by the relay and `relay-install --unload` (`relay.lock`) |
| `ssh_authorized_keys.py` | Pi | (tidying) the marked-line edit and its lock, shared by `host_authorize` and `link_authorize` |
| `desk/peer_mac.py` | Mac | macOS process facts through `ctypes` (loaded on first use, so the module imports on Linux for tests): the pinned-interpreter rule, the channel check, `procStart` and mtime rules, recheck, presence |
| `desk/relay_channels.py` | Mac | The relay's channel socket: accept, uid, hello, verify, local ping/pong and idle, per-channel writers, the 5 s recheck, the refusal cache, local reply refusals |
| `desk/relay_ssh.py` | Mac | The two `ssh -G` reads, the diff against defaults and validation, the `-F none` argv, ssh stderr classification |
| `desk/relay_link.py` | Mac | `RelayLink`: the ssh child, the 30 s handshake deadline, negotiation and proof, the ssh writer, heartbeats, the ping records and the freshness rule on `CLOCK_MONOTONIC`, backoff |
| `desk/relay.py`, `bin/desk-relay` | Mac | Wires the two, the `flock`, the rotating log and the exception hooks, the launch log and its closing; `desk relay` |
| `desk/relay_launchd.py` | Mac | `dev.llm-switchboard.desk-relay`: plist, install, `--unload` and its confirmation (label gone, `relay.lock` free) |
| `scripts/setup-mac-desk.sh` | Mac | Install (checks first) and `--remove` |

Wiring edits to existing files, kept small because the conversation-mode
branch also edits several of them:

- `desk/channel.py`: its undeliverable reasons from `UndeliverableReason`
  (tidying commit 3); no behavior change on either OS.
- `desk/model.py`: `is_desk_host`; `Registration.host`, `Request.host`
  (default `HOST`); `RemoteIdentity`; `ReplyRefusal`; `WriteOutcome`.
- `desk/locks.py`: the host check uses `is_desk_host`; `link_key` and
  `machine` required exactly for non-`pi` locks; `inspect_lock_file`
  (`read_bounded` and the existing decoder, no store touched).
- `desk/hub_state.py`: `register(..., host=HOST)`; `hand_off` takes the
  lock's host, key and machine from the registration; `set_enrollment({session
  id: host}, conflicts)` replacing `set_enrolled`'s callers, and
  `_check_routing` reading it; the host in tickets; `lock_info`;
  `clear_stopped`; `set_links`, `set_admission_refusals` and
  `set_candidates` (like `set_labels`) and their snapshot fields; `host` in
  target and request views; `fence_lock_view(inspect)`: the in-memory
  list, the store's error and the disk read, in one hold of the mutex.
- `desk/hub.py`: `Hub.serve(run_lock)` takes the held run lock; build,
  start and close `LinkHub`; `Hub.send` through
  `send_event` (commit 4) and the link-wait cap for Mac targets; `_recheck`
  dispatch for `RemoteIdentity`; `refresh_files` publishes the combined
  view; `refresh_labels` takes Mac labels from the link; `clear` prefetches
  presence for remote locks within the cap; `_prepare_handoff` enters the
  handoff gate for Mac targets; the signer check as the first step of the
  housekeeping pass; operator ops `enroll_host`, `unenroll_host`,
  `clear_stopped`, `link_drop` (answering with the host's link),
  `link_fence`; `status` with links, refusals and candidates;
  the refusal of `host` on the phase 1 ops; the enroll checks of both files.
- `desk/operator.py`: the new `OperatorOp` members.
- `desk/http_server.py`: the `enroll-host` and `unenroll-host` routes.
- `desk/config.py`: `link_socket`, `link_argv`, `link_signers_file`,
  `relay_argv`, `relay_lock`, `remote_enrolled_file`, `hub_run_lock`.
- `desk/cli.py`: `enroll|unenroll --host` (through `enroll_host`, never the
  phase 1 op), `clear-stopped`, `link-authorize [--self-test]`,
  `link-revoke`, `link-fence` (Pi), with the run lock rule on `HubAbsent`;
  `_serve` takes the run lock before `Hub(config)`
  and no offline form for `clear-stopped`; `relay`, `relay-install
  [--unload]` (Mac); `flags --lines` (both).
- `desk/static/app.js`, `index.html`, `app.css`: host badges, link lines,
  the unattributed refusal lines, candidates with **Enroll now**, the host
  choice, selection, history and speech keyed by `(host, session_id)`, the
  Mac hint.
- `web_app.py`: `desk_enrolled` for Mac rows from `enrolled-hosts.json`.
- `host_authorize.py`: uses `ssh_authorized_keys` (the tidying commit only).
- `AGENTS.md`: map rows, the contracts below, the "Mac sessions" rule
  (Privacy, above).
- `tests/test_architecture.py`: Mac-only modules (`peer_mac`, `relay*`)
  never imported by Pi runtime modules or by `channel.py`, and the reverse;
  the relay's launchd target exists.
- The user guide: the Mac section, `claudep --desk` and what it grants (the
  first section, in plain words), the link line and the refusal lines, the
  candidates and **Enroll now** (the Mac exception), the three off switches
  and what each does and doesn't do (none recalls an event a channel
  already holds or Claude has queued, which an honest session can still act
  on after the command returns; none means a session is quiet or a lock is
  safe to clear; unloading the relay is the switch the Mac enforces, while
  `link-revoke` withdraws the credential from the Pi and is no defense
  against the Pi's `pi` account; `link-revoke`'s confirmed cutoff and what
  an unconfirmed one means, and the overtaken report), `clear-stopped`,
  `link-fence` (and the starting-or-stopping refusal), the CLI, what
  `delivered` proves.

New contracts (paths other things call):

| Path | Caller | Owner |
| --- | --- | --- |
| `bin/desk-link` | sshd, through the `authorized_keys` forced command (`DeskConfig.link_argv`); moving it breaks the link | `desk/link_pump.py` |
| `bin/desk-relay` | launchd (`DeskConfig.relay_argv`); moving it breaks the link | `desk/relay.py` |
| `config_dir/run/link.sock` (Pi) | `bin/desk-link` | `desk/link_hub.py` |
| `config_dir/link-signers` (Pi) | the hub's verifier and signer check | `desk/link_authorize.py` |
| `config_dir/run/hub.lock` (Pi) | `desk serve`; the offline `link-fence`, `link-revoke` and `link-authorize` | `desk/hub.py`, through `desk/run_lock.py` |
| `config_dir/run/channel.sock` (Mac) | Mac channel servers (`DeskConfig.channel_socket`) | `desk/relay_channels.py` |
| `authorized_keys` marker `llm-switchboard-desk-link:<host>` | `desk link-authorize`, `link-revoke` | `desk/link_authorize.py` |
| `~/.ssh/llm-switchboard-desk-<host>` | the relay, `setup-mac-desk.sh` | `desk/relay_link.py` |
| launchd `dev.llm-switchboard.desk-relay` | launchd | `desk/relay_launchd.py` |
| Operator ops `enroll_host`, `unenroll_host`, `clear_stopped`, `link_drop` (answer `{ok, signer, link}`), `link_fence`; routes `/desk/api/enroll-host`, `/desk/api/unenroll-host` | the CLI, the page | `desk/hub.py`, `desk/http_server.py` |
| `llm-switchboard desk link-authorize --self-test`, `link-authorize <host>`, `link-revoke <host>` | `scripts/setup-mac-desk.sh` over ssh | `desk/link_authorize.py` |
| `llm-switchboard desk flags --lines` | `claudep --desk` in the Mac shell config | `desk/cli.py` |
| `scripts/setup-mac-desk.sh [--remove]` | Mac operator | itself |

### Tests

Unit and integration tests run on both machines in the whole suite. The
macOS kernel calls are faked behind `peer_mac`'s libproc reader; `ssh` is
replaced by socket pairs joining the hub, the pump and the relay; the real
`channel.HubLink` connects to the relay's socket. `ssh-keygen` and `ssh -G`
are the real programs (both machines have them) with throwaway keys and
configurations in a temporary directory. No test claims a property against
a process running as `pi` or as Jacob; tests of the checks below are tests
of honest misconfiguration and accidents.

| Area | Tests |
| --- | --- |
| Proof of possession | `test_good_proof_admits`; `test_link_without_the_listed_key_refused` (no key, an unrelated key, another host's listed key: honest misconfiguration); `test_proof_for_another_host_refused`; `test_old_nonce_replay_refused`; `test_proof_bound_to_epoch_version_and_machine`; `test_other_uid_refused`; `test_verifier_timeout_and_output_cap` (`verifier_timeout` at the 5 s deadline; the child then terminated, killed 2 s later when it ignores `SIGTERM`, and reaped); `test_verifier_gets_only_validated_signer_line`; `test_self_test_vector`; `test_verifier_unavailable_refuses_links_keeps_pi`; `test_publication_rechecks_signer_line` |
| Unattributed refusals | `test_version_refusal_counted_unattributed` (a hello naming a host whose link is up, with a bad version: that host's link stays `up`, its `since` unchanged); `test_unattributed_refusals_bounded` (hellos naming a thousand hosts: one entry per code, no new link records); `test_refusal_versions_validated_and_capped` |
| Relay ssh | `test_only_desk_identity_offered` (runs `ssh -G` with the relay's argv: one `identityfile`, `identityagent none`, `identitiesonly yes`, no `certificatefile`, password, keyboard-interactive, hostbased and GSSAPI off); `test_user_config_identity_would_be_added` (documents why `-F none`); `test_default_known_hosts_lists_parse` (the real `ssh -G -F none` output: two paths per known-hosts keyword, parsed, and not passed); `test_configured_known_hosts_list_passed_whole` (a throwaway configuration with two `UserKnownHostsFile` paths: both reach ssh in one `-o`); `test_hostkeyalias_passed_and_used` (the relay's argv under `ssh -G` reports the configured alias as `hostkeyalias`, so known-hosts lookups use the alias on every route); `test_hostkeyalias_omitted_when_unset`; `test_hostkeyalgorithms_passed_only_when_configured`; `test_proxy_and_known_hosts_command_refused`; `test_g_values_validated` (whitespace, quotes, control characters, a relative path, a port out of range); `test_stderr_classified_never_logged` |
| Handshake and children | `test_relay_handshake_deadline_reaps_ssh` (a fake ssh that keeps its pipes open and sends nothing; one that sends a partial line; one that answers the challenge but never `link_welcome`: each attempt fails with `handshake_timeout` at the 30 s deadline and backs off, and the child gets `SIGTERM` then and is reaped; a fake that also ignores `SIGTERM` is killed 2 s later and reaped, so every child is gone by 32 s plus a 1 s scheduling allowance); `test_every_child_reaped_on_every_path` (deadline, exception, normal exit: no zombie, `waitpid` done; the caller's timeout comes at the deadline, not after the cleanup) |
| Channel identity (Mac) | with a fake libproc: `test_other_uid_channel_refused`; `test_parent_not_installed_claude_refused`; `test_desk_flags_not_first_refused` (later in argv, after `--`, as another option's value); `test_stale_session_file_same_second_refused` (mtime before the parent's start); argv tail, exec path and `proc_pidpath` against the framework rule; a decoy second Python child; a reused parent pid (`p_puniqueid` mismatch); `procStart` with one- and two-digit days and either spacing; a session id mismatch; `/clear` between register and event -> `undeliverable` before any write; `test_channel_has_no_listener_check_on_either_os` (the channel connects to a listener of any argv, on Linux and with macOS faked) |
| Mixed versions | relay `[1]` vs hub `[2]` and the reverse: refused, counted unattributed with the relay's list, channels refused with the mismatch reason, the 10-minute backoff; a frame with another `v` after the challenge, an unknown type, a hello with a `v`: link closed. `test_new_cli_against_phase1_operator_handler`: the phase 1 desk package at commit `d024217`, exported with `git archive` into a temporary directory, serves a temporary home's operator socket through its own `OperatorServer` and `Hub.handle_operator` (no TLS or channel socket needed); `enroll --host mac`, `unenroll --host mac`, `clear-stopped` and `link-fence` each exit 1 with nothing changed in `enrolled.json` or `enrolled-hosts.json` (skipped, naming the reason, only when that commit isn't in the local repository); `test_phase1_op_with_host_refused_by_new_hub`; `test_phase1_route_with_host_refused`; `test_host_routes_404_on_phase1_http` (same archive); `test_phase1_channel_through_new_relay` (the channel from the same archive registers through the new relay, takes an event and reports it); `test_live_channel_with_relay_and_key_absent` (a running channel, no relay, no key: it retries on its backoff, its log lines stay bounded, and nothing reaches it) |
| Writers and outcomes | `test_writer_decides_after_final_byte_accepted` (the deadline passes after the kernel accepted the final `\n` and before the writer records it: `written`, decided by the writer); `test_partial_frame_is_unfinished` (some bytes accepted: `unfinished`, never `not_begun`, the socket shut down before the decision); `test_closed_after_begun_mark_before_first_write_is_unfinished` (the peer closes, or the link or channel retires, right after the begun mark and before the first successful write; and a socket full from the begun mark to the deadline: zero bytes accepted, `unfinished`, for the hub's link writer and for the relay's channel writer); `test_unbegun_frames_not_begun_on_retire`; `test_writer_never_begins_a_frame_with_an_outcome`; `test_waiter_guard_can_only_set_unfinished`; `test_hub_send_three_outcomes` (Pi `ChannelConnection` and `RemoteChannel`: each outcome's request state, lock and clear rule); `test_relay_channel_write_unfinished_sends_no_undeliverable` (some bytes and zero bytes accepted: only `unregistered {channel_write_failed}`, and the hub's request needs a presence check); `test_relay_sends_undeliverable_only_for_never_begun_events` (a channel closed by its idle rule, a failed recheck and a full queue, with one event begun and one queued: `undeliverable` only for the queued one); `test_retire_from_other_thread_shuts_down_writer_closes` |
| Locks and presence | `test_presence_from_link_with_other_key_is_unverifiable`; `test_presence_from_other_machine_is_unverifiable`; `test_presence_answer_bound_to_query_and_link`; `test_late_presence_after_timeout_ignored`; `test_query_id_above_highest_issued_closes_link`; `test_clear_stopped_requires_confirm_equal_to_lock_id`; `test_clear_stopped_refused_while_matching_link_up`; `test_clear_stopped_rechecks_lock_at_mutation`; `test_no_http_route_reaches_clear_stopped`; `test_lock_fields_by_host` (phase 1 fields for `pi`, plus `link_key` and `machine` otherwise) |
| Replacement and late messages | `test_replacement_with_event_write_waiting` (the old writer blocked mid-frame; the event's outcome is decided by the old writer, never on the new link); `test_old_reader_finalizer_spares_successor`; `test_late_registered_reply_result_unregistered_ignored`; `test_late_event_and_very_late_presence_keep_link` (an `event` for a closed `chan` answered `undeliverable {channel_closed}` and a `presence` for a query that timed out long ago, while another channel has a request outstanding: the link stays up and the other request is untouched); `test_unknown_chan_closes_link`; `test_chan_never_recurs_across_links`; `test_retired_proxy_write_fails_without_lookup` |
| Wire codes | `test_every_emitted_code_is_in_its_table` (each sender's code paths enumerated against `LINK_CODES`); `test_successful_reply_null_reason` (the real `_handle_reply` result over the link); `test_final_check_reasons_mapped_to_codes` (the real channel's guard reasons, apostrophes included, after a simulated `/clear`); `test_every_reply_refusal_has_a_code`; `test_bad_or_unsolicited_request_id_refused_at_relay` (nothing forwarded); `test_free_text_never_on_wire_or_in_logs` |
| Deadlines, heartbeats and slots | `test_relay_answers_channel_ping_locally`; `test_healthy_idle_channel_kept_by_relay_pings`; `test_channel_blocked_on_stdout_unregistered_within_45s` (the real channel with a stdout that never drains); `test_channel_write_deadline_includes_queue`; `test_event_wait_includes_queue_wait`; `test_link_waits_capped_leave_http_and_operator_slots` (a pump that stops reading; 4 Mac sends or clears hold their HTTP slots and 1 operator presence clear holds its slot, a fifth and a second are refused `busy` at once, and a Pi send, `desk status`, `desk revoke`, `link_drop` and a long poll each finish within their own bounds); `test_remote_close_never_blocks`; `test_register_wait_3_5s_then_refused` |
| Freshness and sleep | `test_freshness_from_time_read_before_ping_enqueued` (fake clocks); `test_suspension_between_kernel_accept_and_bookkeeping` (the ping's bytes are accepted by the kernel, the hub answers, and the relay is suspended for 50 s before its writer does anything more: `acked_at` is the time read before the ping was enqueued, so an `event` arriving after the suspension is not dispatched and the link is retired; the same for `link_proof` and `link_welcome`); `test_unmatched_ping_records_bounded_and_expire` (pongs withheld: at most 4 records, none older than 40 s); `test_pong_for_unsent_ping_closes_link`; `test_stale_pong_changes_nothing` (a pong for a matched or expired record leaves `acked_at` as it was); `test_relay_pings_every_15s_under_traffic`; `test_frames_not_dispatched_40s_after_ack` (an `event` and a `drop` arrive after a 50 s clock jump: neither acted on, the link retired); `test_short_jump_dispatches_in_order` (a 10 s jump); `test_awake_scheduling_delay_does_not_retire` (pongs delayed up to 20 s: frames still dispatched); `test_write_spanning_sleep_decided_by_writer` (the clock jumps while a channel write is in progress: its outcome comes from the bytes accepted, and its deadline on awake time doesn't expire from the jump) |
| Disconnect | a link lost at each row of the request table, with the request state, the lock and the clear outcome asserted; channel disconnect -> `unregistered` -> unknown -> presence `gone` clears; `link_drop`; the pump's EOF on either side, a partial line never forwarded |
| Hosts and enrollment | `test_registration_host_must_match_enrollment` (a Mac `register` for a `pi` id, a Pi channel for a Mac id); `test_enrollment_published_atomically` (no reader sees one file's change alone); `test_id_in_both_files_enrolled_nowhere`; `test_id_under_two_remote_hosts_unroutable` (both nested records well formed: the id is in `conflicts`, no ticket or handoff for it, the target shows `enrolled on two hosts`); `test_enroll_refused_when_either_file_unreadable` (online and offline, for `pi` and for a Mac host, each file in turn); `test_enroll_checks_owners_registrations_and_locks` (online and offline; offline, a FIFO at `locks.json` refuses at once); `test_unenroll_needs_only_its_file`; `test_ticket_bound_to_host`; two Mac registrations for one id are ambiguous; unenroll enqueues `drop` and stops routing before it returns; the page's `desk_logic` tests: history and speech filter by host and session id; reassignment never shows the old host's reply |
| Enroll now | `test_enroll_running_mac_candidate_reachable_without_relaunch`; `test_enroll_now_wording_page_and_cli`; candidates' cap, expiry, field cuts and the recently-unenrolled table |
| Revocation and rollback | `test_signer_removal_retires_established_link_without_link_drop` (the hub never receives `link_drop`; the link retires at the next housekeeping pass, whose signer check comes first); `test_signer_removal_after_publication_reread` (a publication paused after its reread under the links lock; `link-revoke` removes the line meanwhile: the link is published after step 1 returns, step 3's `link_drop` waits for the links lock, retires it and answers `signer: null, link: null`; with `link_drop` unanswered, the next housekeeping pass retires it and the command exits 1, cutoff not confirmed); `test_link_drop_answer_reflects_reread` (the answer's `signer` is the fingerprint the hub read in the same hold as its retirement); `test_authorize_and_revoke_same_host_interleaved` (with a hub and with none, the second command started at each gap of the first: before it takes the file locks, paused between its two edits (the second waits for both locks, then sees both edits), between releasing the locks and its confirmation, and between the answer and its report; whenever neither holds the locks, the signer line and the marked `authorized_keys` line name one key or are both absent; a revoke overtaken by the authorization exits 1 naming the new key, an authorization overtaken by the revoke exits 1 saying its key is not listed, and the command that ran last reports done; a competing edit started after a command's confirming answer and before it prints leaves that command's success, which holds as of the answer, and the next command run reports the edit; a run-lock refusal at the confirmation step changes nothing, and the command's credential edits already made stand); `test_link_fence_offline_refuses_while_gated_handler_paused` (the test holds the run lock as `desk serve` does; a Mac send paused inside the handoff gate; `hub.close()`, so the operator socket is gone and the CLI gets `HubAbsent`, while the handler thread still runs: the fence refuses with the starting-or-stopping message and reads nothing to decide; the send resumes and persists its Mac lock; once the test releases the run lock, as the process's exit would, the fence refuses naming that lock); `test_no_hub_outcomes_need_run_lock` (`link-revoke` step 3, `link-authorize` step 4 and the offline fence, with the operator socket absent and `hub.lock` held by another process: each exits 1 with the run lock's refusal and nothing changed; with it free, each decides from the files); `test_serve_takes_run_lock_before_constructing_hub` (a `desk serve` child started through the real CLI while the test holds `hub.lock`, with a FIFO without a writer at `locks.json`: it exits 3 after 5 s, never having opened the FIFO, so it read no hub state, and created no socket; a hub that read its state before the lock would hang in `LockStore.load`; a running one holds the lock until its process exits); `test_incoming_serve_loads_locks_left_by_outgoing_hub` (the test holds `hub.lock` as an outgoing hub would, with an empty `locks.json`; a `desk serve` child starts through the real CLI and waits; meanwhile the test adds a lock through a `LockStore`, as the outgoing hub's handoff would, and releases `hub.lock`; the incoming hub reports that target busy with that lock, refuses a send to it, and after a send to another target persists a second lock, `locks.json` holds both); `test_clear_stopped_has_no_offline_form`; `test_link_fence_lock_file_not_regular_refuses_promptly` (a FIFO with no writer and a symlink at `locks.json`, online and offline: the fence refuses within its bounds, and the hub's live store keeps its list and its error as they were); `test_link_fence_consistent_with_concurrent_pi_lock_write` (a Pi send's `LockStore.add` paused after its rename, under the state's lock, while the fence runs: the fence's read waits for that lock and sees the new file and list together; the fence passes, the Pi lock persists, and the fence changes nothing in the live store); `test_link_fence_after_replace_then_dir_fsync_failure` (a Mac `hand_off` whose `os.replace` lands and whose directory fsync fails: no event, the request expired; the fence refuses online on the store's error, and offline on the Mac lock in the file); `test_rotation_retires_old_key_link`; `test_unreadable_signers_retires_all_links`; `test_revoke_interrupted_between_files_rerun_completes` (the process killed after the signer write: no link, the key line still present, a second run removes it and reports `already so` for the first step); `test_authorize_interrupted_between_files`; `test_revoke_during_active_send`; `test_revoke_during_delayed_hello` (an admission past its proof at the revoke fails at publication); `test_link_authorize_drops_old_link`; `test_link_fence_requires_no_signer_no_link_no_mac_lock` (each condition failing in turn); `test_link_fence_refuses_handoff_between_recheck_and_lock_write` (a Mac send paused after its dispatch recheck and inside the handoff gate, before `LockStore.add` fsyncs; meanwhile its link retires and the signer line goes: the fence refuses, naming the handoff; the send resumes and persists its Mac lock; the fence then refuses, naming that lock); `test_handoff_gate_refuses_retiring_registration` (a send paused after its dispatch recheck and before the gate while its link retires and its registrations are not yet dropped: 409, the request expired, no lock written, and the fence passes); `test_link_fence_refuses_unreadable_or_corrupt_lock_store` (online with an empty in-memory list and offline: unreadable, oversize and malformed `locks.json`); `test_link_fence_refuses_unreadable_signers`; `test_link_fence_offline`; `test_rollback_order_leaves_no_remote_lock`; `link-authorize` keeps every other line, refuses `pi`, a bad name, a reused key blob and a fifth host; `test_authorize_non_ed25519_refused_before_any_write` (RSA and ECDSA keys: the Ed25519 message, exit 1, both files byte for byte unchanged); `test_refused_authorization_keeps_existing_signers` (with two hosts listed, each refusal (a non-Ed25519 key, a reused blob, a fifth host) leaves both lines and both links in place through the next housekeeping pass); `test_concurrent_authorizations_respect_host_cap` (three hosts listed, two new hosts at once: one listed, the other refused as a fifth); `test_written_signers_pass_the_hub_reader`; `link-authorize --self-test` changes nothing; `link-revoke` removes only its marker and signer line; `host-authorize` leaves the desk line; `relay-install --unload` boots out only its label, deletes its plist and reports done only once `relay.lock` is free (a relay that keeps the lock: exit 1 after 5 s); a phase 1 `LockStore` (KNOWN_HOSTS = {pi}) reading a Mac lock fails closed |
| Bounds | `test_pending_admissions_capped`; `test_accept_rate_capped`; `test_worst_case_status_fits_operator_line`; `test_unenrolled_channels_throttled_without_dropping_link` (16 channels retrying; established registrations and their requests untouched); `test_relay_launch_log_capped_across_crash_loop` (`bin/desk-relay` run repeatedly with a failing import); `test_launch_log_closed_after_logging_starts` (a relay that has started raises in a thread and writes to fd 2: the traceback lands in the rotating log, the launch log's size is unchanged); frame limits at the hub, relay and pump; a line exactly at the cap passes; bad field shapes refused; memory stays within the caps under a flood |
| Privacy | `test_marker_absent_from_desk_logs_and_files` (a marker phrase in a prompt and its reply through a whole exchange: absent from the hub's, relay's and pump's captured logs, the relay launch log, `locks.json`, `enrolled-hosts.json`, `link-signers` and the v1 hosts directory); `test_desk_adds_no_v1_frame_fields` (a v1 exchange's frames for a session have the same fields before and after a desk exchange); `test_desk_first_prompt_title_follows_v1_rule` (a transcript whose first user record is the record Claude Code wrote for a real desk event, captured from a desk session and scrubbed, committed as a fixture: the v1 row's title equals what `transcripts.prompt_text` and v1's title rule yield for that transcript, whatever that is, and the row carries no other prompt or reply text) |

**Checks that only run live**, before the review that gates install. First
with isolated, staged credentials, policy files and sessions (a separate
HOME on each machine, a throwaway desk key and signer line), then again after
the reviewed installation:

- **Pi**: the verifier self-test through `/dev/fd` pipes on the Pi's
  OpenSSH; the real `authorized_keys` line with `restrict` and the forced
  command, with a positive case (the desk key gets the pump and a link) and
  negative cases (a throwaway key not listed is refused by sshd; the v1 key
  runs `host-exchange`, never the pump; an ordinary `ssh` login that runs
  `bin/desk-link` by hand gets no link at the proof); `link-revoke` with the
  hub not answering `link_drop` (the command exits 1, cutoff not confirmed,
  and the link retires at the next housekeeping pass);
  `link-fence` on the staged hub.
- **Mac, adapter facts the build depends on**: `LOCAL_PEERPID` and
  `LOCAL_PEERCRED` on the relay's accepted connections, the `proc_bsdinfo`
  and `proc_uniqidentifierinfo` layouts, `KERN_PROCARGS2` argv, against a
  real `claudep --desk` session and the launchd relay; the
  framework-interpreter rule against the checkout's `.venv`; `procStart`
  for a session started on a day 1 to 9; the session file's mtime against
  the process start. Already checked on 2026-11-23, by the facts noted in
  place: Claude's executable path and argv shape, the session file being
  rewritten, `CLOCK_MONOTONIC` counting sleep, the `ssh -G` output shape.
- **Mac, ssh, on both routes** (on the van LAN, and off it over Tailscale),
  from the launchd job itself, so the environment is the real one: the
  relay connects with strict host-key checking intact; `ssh -v` with the
  relay's argv shows only the desk key offered and the host key found under
  the alias; with the desk line removed on the Pi, the login fails with no
  fallback to another key.
- **Mac, launch log**: after the relay has run with a forced thread
  exception, the launch log is unchanged and the rotating log has the
  traceback.
- A disposable `claudep --desk` session: the startup Enter, registration,
  the Test round trip through `reply`, a tapped exchange, **Enroll now** on a
  running candidate, unenroll from the tablet while it runs, `/clear` (the
  registration drops); the record Claude Code writes for the desk event,
  captured (scrubbed) as the privacy test's fixture.
- **Sleep, on the real lid**: lid closed about 10 s with a tapped send made
  while closed (the event waits in the stream): on wake the relay delivers
  it. Lid closed about 90 s with the same: on wake the relay retires the
  link without delivering (the session's transcript has no such prompt),
  the request stays unknown with its lock, and the link comes back. A
  closure between these may go either way; both outcomes are consistent with
  the hub's state.
- The relay unloaded and reloaded, a hub restart, `link-revoke`, a key
  rotation with a lock outstanding (presence refused, `clear-stopped`
  works).
- No marker text in the Mac's relay logs, the Pi's journal, or the v1
  snapshot after a full exchange.

### Decisions made here

- **Jacob's decision, 2026-11-23: the Pi's side has full authority over
  flagged Mac sessions through this link** (Who the design defends
  against). Rejected by him: a Mac-side restriction on desk-origin turns. A
  real restriction would need a Mac enforcement point before the event
  reaches the session or at tool execution, since the relay doesn't mediate
  what a submitted turn does.
- **Mac-side checks are for accidents; a same-user Mac process is out of
  scope.** Rejected: machinery against a process running as Jacob (a
  listener check, a pinned relay configuration, a relay-only credential),
  which can't hold against a process that can read his keys and type into
  his terminals, and which reviews showed always leaves another route.
- **No listener check on either machine.** Rejected: the Mac channel
  checking that the relay's pinned program listens on its socket. It only
  made sense against a same-user process; no honest process stands in for
  the relay by accident (`bind_private_socket` refuses while something
  listens, the relay's `flock` keeps one relay, a stale socket refuses
  connections). Removing it also leaves the bounded session-file read as
  the only difference between channel versions, which concerns accidents
  and is picked up when a session restarts, so upgrades and rollbacks need
  no channel gate.
- **The hub authenticates the link by proof of possession of the Mac's desk
  key, verified with `ssh-keygen -Y verify` against `link-signers`.** Its
  job is to tell the hub which Mac key a link belongs to and to keep out a
  peer without a listed key. Rejected: process ancestry (sshd parent and
  grandparent), which proves an SSH login of some kind, not which key, and
  depends on sshd's process layout; a secret in the key's `environment=`
  (needs `PermitUserEnvironment`, an sshd change); passing descriptors over
  `SCM_RIGHTS` (authenticates nothing); `from=` on the key (the Mac's address
  changes between the LAN and Tailscale). The uid check stays; argv and
  ancestry checks are dropped, since the proof makes them prove nothing more
  and they could refuse a legitimate login.
- **Established links follow `link-signers` at every housekeeping pass;
  `link_drop`'s answer confirms a cutoff.** A signer check and a publication
  are each one hold of the links lock, so a check that began after the
  file changed leaves no link published from the old line, and the commands
  report the cutoff only from such a check's answer. Rejected: relying on
  the revoking command's last step alone, which a crash, a failed write or
  an unanswered drop would skip, leaving the old key's link up;
  serializing publication with signer edits by taking `link-signers.lock`
  in the hub's admission path, which puts a cross-process lock a stopped
  command could hold in front of every admission, to close an overlap the
  confirmed answer already bounds; promising a fixed 5 s, which the
  housekeeping pass can't keep (it starts 5 s after the previous one ends
  and waits on file locks).
- **A rollback is gated by `desk link-fence`, built on the handoff gate's
  count and a fresh read of `locks.json`.** Rejected: the absence of the
  signer line alone, which says nothing about a link already up or a
  handoff past its recheck; the hub's in-memory lock list alone, which is
  empty after a lock file it couldn't read; draining handoffs by waiting
  (the fence refuses and is run again, so it holds nothing while a handoff
  finishes); a separate check for admissions between proof and publication
  (removed: publication rereads the signer file under the links lock, so it
  can't succeed once the fence has seen no line under that lock).
- **"No hub" is the run lock, taken by `desk serve` before it constructs
  the hub and held for its process's life; every offline decision that rests on there being no hub takes it
  non-blocking and holds it while it reads and decides.** `HubAbsent` only
  shows that nothing listens on the operator socket, which is also true of
  a hub starting or stopping while a request thread still runs. Rejected:
  `HubAbsent` alone (a handler paused in the handoff gate outlives the
  socket); joining the request threads in `Hub.close` (daemon threads
  can't be joined, and non-daemon ones would let one stuck request hold up
  every stop); a pid file (stale after a crash, and a reused pid looks
  alive); refusing every offline fence (a stopped hub is the normal state
  before a downgrade); putting the offline enroll, unenroll and `sends`
  edits under it too, which would add a refusal to phase 1's offline edits
  without closing anything, since they rest on the file locks every
  handoff holds; taking the lock in `Hub.serve`, after `Hub(config)` has
  built `HubState`, which loads the lock table (an incoming hub would load
  the table while the outgoing one could still add a lock, then overwrite
  that lock with its next whole-file write); deferring only the lock
  table's load to `Hub.start`, which leaves the rule resting on every
  constructor staying free of reads. It is the pattern `relay.lock` already
  gives `relay-install --unload`.
- **`link-revoke` holds both file locks, in the installer's order, across
  both edits, and `link_drop`'s answer carries the signer fingerprint the
  hub read with its retirement.** So the two files always show one serial
  order of authorizations and revocations, and each command checks that
  the signer state it intended is the one the hub checked. Rejected:
  releasing `link-signers.lock` between the edits (an authorization in the
  gap left a new signer with its key line removed, and both commands
  reported success); confirming from `link: null` alone, which can't tell
  "no link now" from "the revocation still holds"; rereading the signer
  file after the hub answers, which doesn't tie the answer to the file
  state the hub checked (an authorization and a second revocation can both
  land between the two reads).
- **The fence reads `locks.json` through `inspect_lock_file`, beside the
  hub's list in one hold of the state's lock.** Rejected: `LockStore.load`,
  whose blocking open hangs on a FIFO and which replaces the live store's
  list and broken flag outside the mutex, so it could publish an old
  snapshot over a concurrent Pi lock write; reading the file outside the
  state's lock and arguing from order (no Mac lock can be added after the
  links-lock check, and every write rewrites the file from the list), which
  holds but rests on every writer keeping that shape, while the state's
  lock is one every lock write already takes.
- **The relay runs ssh with `-F none` and the routing values from `ssh -G`,
  passing a value only when the configuration set it.** Rejected: Jacob's
  configuration plus `-i` and `IdentitiesOnly=yes`, which keeps every
  configured `IdentityFile` (shown with `ssh -G`); a pinned copy of the
  routing rules, which would drift from his configuration; passing every
  `-G` value (it would turn defaults into explicit settings, losing ssh's
  default host-key-algorithm ordering); rejecting list values (the real
  default `userknownhostsfile` is a list of two).
- **One outcome per frame, decided by its writer from the bytes the kernel
  accepted; every frame marked begun and not proven complete is
  `unfinished`, zero bytes included, and `not_begun` needs positive
  evidence that it never was.** Rejected: a waiter deciding at its deadline
  (it can't know whether the final byte went); treating a frame proven to
  lack its final `\n` as evidence of non-delivery (true for the pump's and
  the channel's whole-line readers, but only by relying on their parsing);
  reading "begun, zero bytes accepted" as `not_begun`, which would rest on
  every error path reporting an exact count, to save one presence check.
- **The relay acts on a hub frame only while its last acknowledged ping is
  under 40 s old on `CLOCK_MONOTONIC`, timed from before the ping was
  enqueued.** Rejected: the time the write finished, which a suspension
  after the kernel accepted the ping makes later than the hub's receipt, so
  an old pong could look fresh; detecting sleep as growth
  in `CLOCK_MONOTONIC − CLOCK_UPTIME_RAW` with a latched wake transition, a
  fresh ping/pong gate and frames held until it, which needs a noise
  threshold, a control path and a frame store, and only narrows the race
  between a hub retirement and dispatch that exists while awake too; the
  45 s arithmetic on the relay's last write alone, which a write still in a
  buffer when the Mac slept defeats.
- **Relay link timing on `CLOCK_MONOTONIC`, local write deadlines on
  awake time.** Rejected: `time.monotonic()` for the link (it stops while
  the Mac sleeps, while the Pi's clock doesn't); `CLOCK_MONOTONIC` for local
  writes (a sleep would expire them, though both ends slept).
- **The relay has its own 30 s handshake deadline and reaps ssh, with the
  child's cleanup (2 s of `SIGTERM` grace, then `SIGKILL`) a separate bound
  after it.** Rejected: relying on `ConnectTimeout` and
  `ServerAliveInterval`, which don't end a live transport whose far end
  never sends a frame; folding the cleanup into the deadline, which would
  shorten the time ssh gets to connect.
- **`link-authorize` accepts only Ed25519 keys and checks the whole
  proposed `link-signers` with the hub's own reader, inside its locks.**
  Rejected: `host_authorize.public_key` alone, which accepts RSA and ECDSA
  keys the reader refuses, so one such authorization would make the file
  unreadable to the hub and retire every Mac's link; checking duplicates
  and the host count before taking the lock, which two authorizations at
  once could both pass.
- **The off switches are described by what they stop, and none is said to
  make a session quiet.** Unloading the relay is the switch the Mac
  enforces; `link-revoke` withdraws the Pi's copy of the credential.
  Rejected: recalling held events (no message can withdraw a line from
  Claude's pipe or its queue); presenting revocation as a limit on the
  Pi's `pi` account, which the model accepts as authoritative.
- **Admission refusals are recorded per code, never per host.** Rejected:
  per-host `mismatch` state from the hello's `host`, which an unproven peer
  could set for a healthy Mac and which grew with every name sent.
- **Link waits are capped per surface (4 HTTP, 1 operator).** Rejected:
  separate thread pools for link calls (more threads, same bound needed);
  leaving the shared slots uncapped and claiming a slow link delays nothing
  else.
- **An enroll needs both enrollment files readable, online and offline, and
  an id with two owners anywhere is unroutable.** Rejected: allowing a Pi
  enroll while `enrolled-hosts.json` is unreadable (exclusivity unchecked);
  flattening owners and letting one win.
- **The Mac's desk flags must be claude's first four arguments.** Rejected:
  the phase 1 adjacent-pair test anywhere in argv (it accepts the strings
  after `--` or as another option's value); parsing Claude's whole option
  grammar (it changes between Claude versions).
- **Host-qualified enrollment uses new operations and routes.** Rejected: a
  `host` field on `enroll`, which a phase 1 hub ignores and applies to the
  Pi; a version handshake on the operator socket (more mechanism than a new
  op name, which the phase 1 dispatch already refuses).
- **Mac locks record the link's key and machine, and presence counts only
  from a matching link.** Rejected: refusing key replacement while a lock
  exists, which needs a fence between `link-authorize` and the hub's
  handoffs; trusting any link for the host name.
- **One writer thread and bounded queue per link; callers wait with total
  deadlines.** Rejected: synchronous writes under a shared mutex with a
  per-write timeout, whose waits add up across writers and which would block
  `close()` and reader threads on a slow Mac.
- **One code table; the relay turns codes back into the channel's text.**
  Rejected: forwarding the hub's and the channel's free-form reasons (one
  apostrophe or `null` broke the link grammar, and a code is the only safe
  thing to log); separate sender and receiver tables (a code legal at one
  end was illegal at the other).
- **Sequential `query_id`s per link, compared with a high-water mark.**
  Rejected: random ids, whose "never issued" check needs the whole history.
- **Enrolling a running Mac session takes effect at once (the Mac
  exception), said on the button, the confirmation, the CLI and the
  guide.** Rejected: enforcing the launch boundary by requiring enrollment
  before launch, which needs the Mac to learn enrollment and adds a
  Pi-to-Mac write.
- **The relay throttles refused channels; the hub refuses excess registers
  without dropping the link.** Rejected: dropping the link at a register
  rate, which let unenrolled sessions take down enrolled ones.
- **Revoke before reconciling Mac locks when rolling back.** Rejected:
  clearing locks first, which leaves time for a new handoff before the
  revoke.
- **Mac enrollment lives on the Pi, in `enrolled-hosts.json` under
  `enrolled.json`'s lock, published together with it.** Rejected: a key in
  `enrolled.json` (the phase 1 parser would read the file as malformed,
  unenrolling every Pi session after a rollback); a list on the Mac (a desk
  unenroll would then need a Pi-to-Mac write, and there would be two
  lists).
- **Targets stay keyed by session id, with `host` as an attribute and one
  owner per id enforced at registration, ticket, handoff, edit and on the
  page.** Rejected: rekeying every table by `(host, session_id)`, which
  rewrites most of `hub_state.py` (also being changed for conversation
  mode).
- **The Mac's opt-in is the `--desk` launch, recorded nowhere on the Mac.**
  Rejected: a Mac-local enrollment file; the argv check already keeps the
  opt-in per launch.
- **Discovery through bounded candidates.** Rejected: typing Mac session ids
  as the only route (kept through the host choice).
- **On link loss the relay closes its channels; they re-register.**
  Rejected: keeping them and re-registering them itself, which adds relay
  state and special handling of in-flight replies to save at most 30 s.
- **A newer link replaces the older one, rate-limited, and `chan` never
  recurs within a relay process.** Rejected: refusing the newer one, which
  would hold off a woken Mac until the Pi's idle timeout.
- **The pump only checks line lengths; the hub parses.** Rejected: a second
  parser in the forced command.
- **The Mac's interpreter is matched by the framework rule, worked out at
  each check.** Rejected: the phase 1 exact rule (can't hold on Homebrew's
  framework Python); calibrating from the relay's own process.
- **Mac labels come from the session file in `register`.** Rejected: the
  hub reading v1 snapshots (couples the desk to v1, reads of up to 4 MiB).
- **A late reply after a link loss is refused, as on the Pi** (the reviewed
  rule: same verified connection).
- **`relay-install --unload` deletes the plist.** Rejected: v1's bootout
  only, which reloads the job at the next login.
- **The relay's launch log is written only before logging starts, capped by
  its own entry point; launchd's output goes to `/dev/null`.** Rejected:
  truncating at relay startup, which a crash before startup never reaches;
  leaving stderr on the launch log for the relay's lifetime, which thread
  tracebacks grow without bound.
- **A separate `setup-mac-desk.sh` that checks both machines before
  changing either.** Rejected: adding steps to `setup-mac.sh`, which would
  install the desk link with every v1 setup.
- **`claudep --desk` as the first argument only; `claudepr` unchanged.**
  Rejected: parsing `--desk` anywhere in the arguments, and changing
  `claudepr`, which calls `claude` rather than `command claude`.
- **No power assertion on the Mac.** Rejected: keeping it awake for the
  desk; sleep shows as "link down".

### Open questions for Jacob

None open. The question of the Pi's authority over Mac sessions was decided
by Jacob on 2026-11-23 (Who the design defends against, and the first entry
under Decisions made here).
