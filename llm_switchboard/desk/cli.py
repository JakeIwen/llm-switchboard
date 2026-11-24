"""`llm-switchboard desk ...`: run the hub and operate it.

A light command (`__main__._LIGHT_COMMANDS`): it never imports the HTTP
stack. `pair`, `revoke`, `status`, `clear` and `ack` talk to the running hub
over the operator socket (`ack` only through the hub, which sends its one
Enter). `sends`, `voice`, `enroll` and `unenroll` go through the hub
when it runs (so its sends see the change in order) and edit the file
directly only when the connect proved no hub listens; once connected, a
missing answer is reported as unknown and nothing is written. `enroll
--host` and `unenroll --host` (Mac sessions) go only through the
host-qualified ops, never the phase 1 ones, so a phase 1 hub refuses them
(`unknown op`) instead of editing the Pi's file; offline, both forms check
both enrollment files and `locks.json` first (remote_enrollment.py).
`clear-stopped` has no offline form: a hub rewrites `locks.json` whole from
its own list. `link-authorize`, `link-revoke` and `link-fence` (link_authorize.py) ask
the hub, and with none answering decide from the files under the run lock
(`hub.lock`). `flags` and `install-config` work on files only.
"""

import argparse
import json
import logging
import os
import sys
import time

from .config import DeskConfig
from .enrollment import DeskSettings, ensure_private_dir
from .model import HOST, DeskError
from .operator import HubAbsent, HubResultUnknown, HubUnavailable, OperatorOp, operator_request
from .protocol import SERVER_NAME
from .refusals import Refusal
from .remote_enrollment import ENROLL_NOW_NOTICE, check_mac_host, enroll_offline, unenroll_offline

EXIT_REFUSED = 1
EXIT_SETUP = 2
EXIT_NO_HUB = 3
# `desk serve` found the run lock held past its start wait; systemd starts it
# again 5 s later.
EXIT_RUN_LOCK_HELD = 3
# The request may or may not have been applied (for `revoke`: the tokens
# are gone, but an acknowledgement on one may still press its Enter).
EXIT_UNKNOWN = 4


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="llm-switchboard")
    sub = parser.add_subparsers(required=True)
    desk = sub.add_parser("desk", help="the voice desk hub and its operator commands")
    commands = desk.add_subparsers(dest="desk_command", required=True)
    commands.add_parser("serve", help="run the hub (HTTPS on localhost, channel and operator sockets)")
    pair = commands.add_parser("pair", help="confirm the pairing code the tablet shows")
    pair.add_argument("code")
    revoke = commands.add_parser("revoke", help="revoke one tablet token, or all of them")
    revoke.add_argument("--token", dest="token_id", metavar="ID")
    commands.add_parser("status", help="epoch, registrations, locks, tokens, sends flag")
    clear = commands.add_parser("clear", help="release a target lock after checking the session")
    clear.add_argument("session_id")
    clear.add_argument("lock_id")
    sends = commands.add_parser("sends", help="turn sends on or off (persisted)")
    sends.add_argument("value", choices=("on", "off"))
    voice = commands.add_parser("voice", help="allow or refuse sends confirmed by voice in a conversation (persisted)")
    voice.add_argument("value", choices=("on", "off"))
    for name, text in (("enroll", "let the desk route to a session"), ("unenroll", "stop routing to a session")):
        command = commands.add_parser(name, help=text)
        command.add_argument("session_id")
        command.add_argument("--host", metavar="MAC", help="a Mac session, enrolled for that Mac's host name")
    stopped = commands.add_parser("clear-stopped", help="release a Mac lock its Mac can't answer for, after "
                                  "seeing the session exit there")
    stopped.add_argument("session_id")
    stopped.add_argument("lock_id")
    stopped.add_argument("--confirm", metavar="LOCK_ID", help="off a terminal: the lock id again")
    authorize = commands.add_parser("link-authorize", help="list a Mac's desk link key (one public-key line on "
                                    "stdin) and let it log in to the forced command")
    authorize.add_argument("host", nargs="?", metavar="MAC")
    authorize.add_argument("--self-test", action="store_true", help="only check that ssh-keygen can verify "
                           "signatures; changes nothing")
    link_revoke = commands.add_parser("link-revoke", help="withdraw a Mac's desk link credential and confirm the "
                                      "cutoff")
    link_revoke.add_argument("host", metavar="MAC")
    commands.add_parser("link-fence", help="before rolling the Pi back: confirm no Mac signer, link, handoff or "
                        "lock is left")
    ack = commands.add_parser("ack", help="show a session's startup warning, then press Enter once to accept it")
    ack.add_argument("session_id")
    ack.add_argument("--digest", metavar="HEX", help="the digest a previous `desk ack` printed; sends without asking")
    commands.add_parser("flags", help="print the claude launch flags for an enrolled session")
    commands.add_parser("install-config", help="write the pinned MCP config (mcp.json)")
    args = parser.parse_args(argv)
    config = DeskConfig.from_env()
    try:
        return _COMMANDS[args.desk_command](args, config)
    except DeskError as exc:
        print(f"desk: {exc.reason}", file=sys.stderr)
        return EXIT_REFUSED
    except HubResultUnknown as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    except HubUnavailable as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return EXIT_NO_HUB


def _serve(args, config: DeskConfig) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from .http_server import TlsSetupError
    from .hub import Hub
    from .operator import SocketInUse
    from .run_lock import START_WAIT_SECONDS, RunLockHeld, RunLockUnusable, hold_for_process
    # The run lock comes before anything of the hub is constructed, so no hub
    # state (the lock table first) is read while another hub could still
    # write it, and an offline command holding it knows no hub runs.
    try:
        run_lock = hold_for_process(config.hub_run_lock, wait_seconds=START_WAIT_SECONDS)
    except RunLockHeld:
        print("desk: another desk hub or a desk command holds hub.lock", file=sys.stderr)
        return EXIT_RUN_LOCK_HELD
    except RunLockUnusable as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return EXIT_SETUP
    try:
        return Hub(config).serve(run_lock)
    except (TlsSetupError, SocketInUse) as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return EXIT_SETUP


def _operator(config: DeskConfig, op: OperatorOp, **fields) -> int:
    return _print_response(operator_request(config.operator_socket, op, **fields))


def _print_response(response: dict) -> int:
    if not response.get("ok"):
        print(f"desk: {response.get('error', 'refused')}", file=sys.stderr)
        return EXIT_REFUSED
    response.pop("ok", None)
    print(json.dumps(response, indent=2))
    return 0


def _pair(args, config):
    return _operator(config, OperatorOp.PAIR, code=args.code)


def _revoke(args, config):
    response = operator_request(config.operator_socket, OperatorOp.REVOKE, token_id=args.token_id)
    if not response.get("ok") and response.get("in_progress") is True:
        # The tokens are gone, but an Enter authorized before may still follow.
        again = " ".join(["llm-switchboard desk revoke", *(["--token", args.token_id] if args.token_id else [])])
        print(f"desk: {response.get('error')}; run `{again}` again to wait for it", file=sys.stderr)
        return EXIT_UNKNOWN
    return _print_response(response)


def _status(args, config):
    return _operator(config, OperatorOp.STATUS)


def _clear(args, config):
    return _operator(config, OperatorOp.CLEAR, session_id=args.session_id, lock_id=args.lock_id)


def _switch(args, config, op: OperatorOp, write) -> int:
    """A `desk-settings.json` switch: through the hub, else (no hub, proved at
    connect) one locked read-modify-write of the file that keeps the other key."""
    enabled = args.value == "on"
    try:
        return _operator(config, op, enabled=enabled)
    except HubAbsent:
        write(DeskSettings(config.settings_file), enabled)
        print(f"{op.value} {args.value} (saved; the hub is not running and reads it at start)")
        return 0


def _sends(args, config):
    return _switch(args, config, OperatorOp.SENDS, DeskSettings.set_sends_enabled)


def _voice(args, config):
    return _switch(args, config, OperatorOp.VOICE, DeskSettings.set_voice_confirm_enabled)


def _enrollment_change(config: DeskConfig, op: OperatorOp, edit_file, **fields) -> bool | None:
    """Through the running hub, else (no hub, proved at connect) by editing
    the file; whether anything changed, or None when the hub refused
    (already printed)."""
    try:
        response = operator_request(config.operator_socket, op, **fields)
    except HubAbsent:
        return edit_file()
    if not response.get("ok"):
        error = response.get("error", "refused")
        if error == Refusal.UNKNOWN_OP:
            error = "the running desk hub is older and can't enroll Mac sessions; nothing changed"
        print(f"desk: {error}", file=sys.stderr)
        return None
    return bool(response.get("changed"))


def _host(args) -> str:
    """HOST without --host; a Mac's host name with it (never `pi`)."""
    return HOST if args.host is None else check_mac_host(args.host)


def _enroll(args, config):
    host, session_id = _host(args), args.session_id
    if host == HOST:
        added = _enrollment_change(config, OperatorOp.ENROLL,
                                   lambda: enroll_offline(config, HOST, session_id, time.time()),
                                   session_id=session_id)
    else:
        added = _enrollment_change(config, OperatorOp.ENROLL_HOST,
                                   lambda: enroll_offline(config, host, session_id, time.time()),
                                   session_id=session_id, host=host)
    if added is None:
        return EXIT_REFUSED
    if host == HOST:
        print(f"{session_id} {'enrolled' if added else 'was already enrolled'}; "
              "it takes effect at the session's next launch with the desk flags")
    else:
        print(f"{session_id} {'enrolled' if added else 'was already enrolled'} on {host}. {ENROLL_NOW_NOTICE}")
    return 0


def _unenroll(args, config):
    host, session_id = _host(args), args.session_id
    if host == HOST:
        removed = _enrollment_change(config, OperatorOp.UNENROLL, lambda: unenroll_offline(config, HOST, session_id),
                                     session_id=session_id)
    else:
        removed = _enrollment_change(config, OperatorOp.UNENROLL_HOST,
                                     lambda: unenroll_offline(config, host, session_id),
                                     session_id=session_id, host=host)
    if removed is None:
        return EXIT_REFUSED
    where = "" if host == HOST else f" on {host}"
    print(f"{session_id} {'unenrolled' if removed else 'was not enrolled'}{where}")
    return 0


CLEAR_STOPPED_STATEMENT = "I have seen this Mac session exit and its desk channel is not running"


def _clear_stopped(args, config):
    """Shows the lock and the statement it records, takes the confirmation,
    then asks the hub, which checks everything again at the mutation."""
    try:
        status = operator_request(config.operator_socket, OperatorOp.STATUS)
    except HubAbsent:
        print("desk: the desk hub is not running; start it and run clear-stopped again", file=sys.stderr)
        return EXIT_REFUSED
    lock = next((found for found in status.get("locks", []) if found.get("lock_id") == args.lock_id
                 and found.get("session_id") == args.session_id), None) if status.get("ok") else None
    if lock is None or lock.get("host") in (None, HOST):
        print("desk: no Mac lock with that session id and lock id; nothing changed", file=sys.stderr)
        return EXIT_REFUSED
    print(f"lock {args.lock_id}: host {lock['host']}, session {args.session_id}, since {lock.get('since')}, "
          f"key {lock.get('link_key')}, machine {lock.get('machine')}...")
    print(f"this records: {CLEAR_STOPPED_STATEMENT}")
    confirm = args.confirm
    if confirm is None:
        if not sys.stdin.isatty():
            print("desk: off a terminal, pass --confirm <lock id>; nothing changed", file=sys.stderr)
            return EXIT_REFUSED
        if input("Type the session id's first 8 characters to confirm: ").strip() != args.session_id[:8]:
            print("nothing changed")
            return EXIT_REFUSED
        confirm = args.lock_id
    response = operator_request(config.operator_socket, OperatorOp.CLEAR_STOPPED, session_id=args.session_id,
                                lock_id=args.lock_id, confirm=confirm)
    if not response.get("ok"):
        error = response.get("error", "refused")
        if error == Refusal.UNKNOWN_OP:
            error = "the running desk hub is older and has no clear-stopped; nothing changed"
        print(f"desk: {error}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"{args.lock_id} cleared")
    return 0


def _link_authorize(args, config):
    from .link_authorize import MAX_KEY_LINE_BYTES, run_authorize, run_self_test
    if args.self_test:
        if args.host is not None:
            print("desk: link-authorize --self-test takes no host and reads no key", file=sys.stderr)
            return EXIT_SETUP
        return run_self_test()
    if args.host is None:
        print("desk: link-authorize needs the Mac's host name, and its public key on stdin", file=sys.stderr)
        return EXIT_SETUP
    return run_authorize(config, args.host, sys.stdin.buffer.read(MAX_KEY_LINE_BYTES + 1))


def _link_revoke(args, config):
    from .link_authorize import run_revoke
    return run_revoke(config, args.host)


def _link_fence(args, config):
    from .link_authorize import run_fence
    return run_fence(config)


def _ack(args, config):
    digest = args.digest
    if digest is None:
        response = operator_request(config.operator_socket, OperatorOp.STARTUP_VIEW, session_id=args.session_id)
        if not response.get("ok"):
            print(f"desk: {response.get('error', 'refused')}", file=sys.stderr)
            return EXIT_REFUSED
        print(_printable(response.get("capture") or "").rstrip("\n"))
        if response.get("refusal") or not response.get("digest"):
            print(f"desk: {response.get('refusal') or 'not acknowledgeable'}", file=sys.stderr)
            return EXIT_REFUSED
        digest = response["digest"]
        print(f"digest {digest}")
        if not sys.stdin.isatty():
            print(f"nothing sent; to press Enter once: llm-switchboard desk ack {args.session_id} --digest {digest}")
            return 0
        if input("Press Enter once in this session to accept the warning? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing sent")
            return 0
    response = operator_request(config.operator_socket, OperatorOp.STARTUP_ACK, session_id=args.session_id,
                                digest=digest)
    if not response.get("ok"):
        print(f"desk: {response.get('error', 'refused')}", file=sys.stderr)
        # The hub handed tmux the key but can't say whether it arrived.
        return EXIT_UNKNOWN if response.get("delivery") == "unknown" else EXIT_REFUSED
    print(f"{args.session_id}: Enter sent; the session continues starting")
    return 0


def _printable(text: str) -> str:
    """Only matched warning text reaches here; keep the terminal safe anyway."""
    return "".join(c for c in text if c == "\n" or c.isprintable())


def _flags(args, config):
    print(" ".join(config.launch_flags))
    return 0


def _install_config(args, config):
    argv = config.channel_argv
    payload = {"mcpServers": {SERVER_NAME: {"command": argv[0], "args": list(argv[1:])}}}
    ensure_private_dir(config.mcp_config.parent)
    tmp = config.mcp_config.with_name(f".{config.mcp_config.name}.tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, config.mcp_config)
    print(config.mcp_config)
    return 0


_COMMANDS = {
    "serve": _serve, "pair": _pair, "revoke": _revoke, "status": _status, "clear": _clear,
    "sends": _sends, "voice": _voice, "enroll": _enroll, "unenroll": _unenroll, "ack": _ack, "flags": _flags,
    "install-config": _install_config, "clear-stopped": _clear_stopped, "link-fence": _link_fence,
    "link-authorize": _link_authorize, "link-revoke": _link_revoke,
}
