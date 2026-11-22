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
| `desk/locks.py` | adapter | The target lock file (fsync before handoff; refuses on unreadable/full) |
| `desk/enrollment.py` | adapter | `enrolled.json` (JsonFile) and `desk-settings.json` (`sends_enabled`) |
| `desk/peer.py` | rules + adapter | Channel identity: peer PID from the socket, `/proc` walk, session-file check and recheck |
| `desk/channel.py` | runtime | The channel server: MCP over stdio plus the hub socket client |
| `desk/hub.py` | runtime | Wires the state, the channel socket, the operator socket and HTTPS |
| `desk/http_server.py` | adapter | HTTPS (stdlib `ssl` + `ThreadingHTTPServer`), Host/Origin/token guards, long-poll |
| `desk/operator.py` | adapter | The 0600 operator socket and its client (`pair`, `revoke`, `sends`, `status`, `clear`) |
| `desk/cli.py` | entry | `llm-switchboard desk serve|pair|revoke|status|enroll|unenroll|sends|flags|install-config` |
| `desk/static/` | page | The tablet page: plain HTML/CSS/JS, no build step, Chrome 119 |
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
| `config_dir/enrolled.json` | `{"sessions": {<session id>: {"since": <unix s>}}}` | 64 sessions |
| `config_dir/desk-settings.json` | `{"sends_enabled": false}` | fixed |
| `config_dir/mcp.json` | The pinned MCP config (`desk install-config` writes it) | fixed |
| `config_dir/tls/leaf.pem`, `leaf.key` | The hub's TLS leaf (from `scripts/desk-ca.sh`) | fixed |
| `state_dir/locks.json` | Target locks, no prompt or reply content | 64 entries |
| `runtime_dir/channel.sock`, `operator.sock` | Unix sockets | n/a |

No prompt or reply text is ever written to disk or logged.

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
  -> `unknown` (the connection dropped, the recheck failed, or the hub
  restarted, after handoff). `pending` -> `expired` when the dispatch recheck
  fails before handoff (nothing was written; safe to send again). One
  outstanding request per target: a target is **busy** while it has a lock.
- **Target lock** (`locks.json`): `{lock_id, host, session_id, request_id,
  ticket_hash, since}`. Written and fsynced before handoff. Cleared by the
  verified answer, or by Jacob's explicit clear naming that `lock_id` (a clear
  naming an older lock id is refused). Locks survive hub restarts; a locked
  target stays busy in the new epoch, shown as `unknown (earlier hub run)`,
  until cleared. An unreadable, corrupt, full or unwritable lock file refuses
  every send and shows a lock-store error.
- **Replies** count only from the connection the request was handed to,
  while it is `handed_off`, with its request id, under the size caps. The
  first valid one wins; late, duplicate, oversize or mismatched replies are
  dropped and logged without content. A ring keeps the last 20 answered
  replies (and at most 256 KiB of text) in memory only.
- **Verified reply**: a registration shows `verified` once one request
  handed to it has been answered through `reply`; a new registration starts
  unverified.
- **Pairing**: `pair_start(secret_hash)` returns `pair_id` and a 6-character
  code (Crockford base32, shown as `XXX-XXX`), expiring after 5 minutes, at
  most 3 pending, at most 10 starts a minute. The operator's `desk pair
  <code>` confirms it. `pair_claim(pair_id, secret)` hands out the token once,
  only when `sha256(secret) == secret_hash` and it is confirmed; 5 wrong
  secrets kill the pairing.
- **Tokens**: 32 random bytes, base64url; the hub keeps only their SHA-256.
  Expire after 7 days, at most 4 (pairing a fifth drops the oldest). Each has a
  short id for `desk status` and `desk revoke --token <id>`. A revoked or
  dropped token's long-polls return 401 at once.

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
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and
`Content-Security-Policy: default-src 'self'; frame-ancestors 'none';
base-uri 'none'; form-action 'none'`. No CORS headers, ever. POST bodies are
JSON (`Content-Type: application/json`), at most 16 KiB, and need `Origin:
https://localhost:<port>` exactly. Errors are `{"error": "<reason>"}` with
400 (bad input), 401 (no or bad token), 403 (refused: sends off, wrong origin),
404, 409 (state conflict: busy, stale ticket or generation), 413, 429 (rate
limit) or 503 (lock store error).

Unauthenticated:
- `GET /desk`, `/desk/app.js`, `/desk/app.css`, `/desk/text_rule.js`,
  `/desk/text_rule_cases.json`: the page.
- `POST /desk/pair/start {secret_hash}` -> `{pair_id, code, expires_in}`.
- `POST /desk/pair/claim {pair_id, secret}` -> `{token, expires_at}`; 409
  `{"error": "not confirmed"}` while waiting (the page polls every 2 s).

Protected (`Authorization: Bearer <token>`):
- `GET /desk/api/state?after=<version>`: returns at once when the state
  version differs from `after` (or `after` is absent), else holds up to 25 s
  and returns the unchanged state. Body:
  `{version, epoch, sends_enabled, lock_store_error, targets: [Target], requests: [Request]}`.
  `Target = {host: "pi", session_id, label, cwd, enrolled, registration:
  "verified"|"registered"|"none"|"ambiguous", generation, lock: {lock_id,
  request_id, since, earlier_run} | null}`. `label` is the session's title
  from its transcript, else its folder name. Targets are the enrolled
  sessions plus any live registration. `Request = {request_id, session_id,
  state, text, created, reply: {text, spoken} | null}` for the last 20.
- `POST /desk/api/ticket {session_id, generation, epoch}` -> `{ticket, expires_at}`.
- `POST /desk/api/send {ticket, text}` -> `{request_id, state}`.
- `POST /desk/api/clear {session_id, lock_id}` -> `{cleared: true}`.
- `POST /desk/api/enroll {session_id}`, `POST /desk/api/unenroll {session_id}`.

## Operator socket and CLI

`llm-switchboard desk <command>` runs as `pi` and talks to the hub over
`operator.sock` (same framing as the channel socket, request `{op, ...}`,
response `{ok, ...}`): `pair <code>`, `revoke [--token <id>]` (all tokens when
no id), `status` (epoch, registrations, locks, token ids and expiry, sends
flag), `clear <session id> <lock id>`, `sends on|off` (persisted in
`desk-settings.json`). `enroll|unenroll <session id>` and `flags` (prints the
launch flags) and `install-config` (writes `mcp.json`) work without the hub.
`serve` runs the hub.

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

`bin/desk-adb-link <serial> <port>` loops: `adb -s <serial> wait-for-device`;
refuses (logs, exits 3) an unauthorized or offline device; reads `adb -s
<serial> reverse --list`: an identical `tcp:<port> tcp:<port>` mapping is
adopted, a different mapping on that remote port is refused (exit 4);
otherwise `adb -s <serial> reverse --no-rebind tcp:<port> tcp:<port>`; then
`adb -s <serial> wait-for-disconnect`. It never runs `kill-server`, `am`,
`input`, or `reverse --remove`. The unit doesn't restart after exit 3 or 4.

## Enrollment and launch

Phase 1 enrolls sessions Jacob starts by hand: `claudepd $(llm-switchboard
desk flags) --resume <id>`. Launching enrolled kept sessions from the
supervisor waits on Jacob's decision about the dev-channels startup warning.
