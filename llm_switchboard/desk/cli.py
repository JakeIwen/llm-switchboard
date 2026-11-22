"""`llm-switchboard desk ...`: run the hub and operate it.

A light command (`__main__._LIGHT_COMMANDS`): it never imports the HTTP
stack. `pair`, `revoke`, `status`, `clear` and `sends` talk to the running
hub over the operator socket; `enroll`, `unenroll`, `flags` and
`install-config` work on files and need no hub.
"""

import argparse
import json
import logging
import os
import sys
import time

from .config import DeskConfig
from .enrollment import DeskSettings, Enrollment, ensure_private_dir
from .model import DeskError
from .operator import HubUnavailable, OperatorOp, operator_request
from .protocol import SERVER_NAME

EXIT_REFUSED = 1
EXIT_SETUP = 2
EXIT_NO_HUB = 3


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
    for name, text in (("enroll", "let the desk route to a session"), ("unenroll", "stop routing to a session")):
        commands.add_parser(name, help=text).add_argument("session_id")
    commands.add_parser("flags", help="print the claude launch flags for an enrolled session")
    commands.add_parser("install-config", help="write the pinned MCP config (mcp.json)")
    args = parser.parse_args(argv)
    config = DeskConfig.from_env()
    try:
        return _COMMANDS[args.desk_command](args, config)
    except DeskError as exc:
        print(f"desk: {exc.reason}", file=sys.stderr)
        return EXIT_REFUSED
    except HubUnavailable as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return EXIT_NO_HUB


def _serve(args, config: DeskConfig) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from .http_server import TlsSetupError
    from .hub import Hub
    from .operator import SocketInUse
    try:
        return Hub(config).serve()
    except (TlsSetupError, SocketInUse) as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return EXIT_SETUP


def _operator(config: DeskConfig, op: OperatorOp, **fields) -> int:
    response = operator_request(config.operator_socket, op, **fields)
    if not response.get("ok"):
        print(f"desk: {response.get('error', 'refused')}", file=sys.stderr)
        return EXIT_REFUSED
    response.pop("ok", None)
    print(json.dumps(response, indent=2))
    return 0


def _pair(args, config):
    return _operator(config, OperatorOp.PAIR, code=args.code)


def _revoke(args, config):
    return _operator(config, OperatorOp.REVOKE, token_id=args.token_id)


def _status(args, config):
    return _operator(config, OperatorOp.STATUS)


def _clear(args, config):
    return _operator(config, OperatorOp.CLEAR, session_id=args.session_id, lock_id=args.lock_id)


def _sends(args, config):
    enabled = args.value == "on"
    try:
        return _operator(config, OperatorOp.SENDS, enabled=enabled)
    except HubUnavailable:
        DeskSettings(config.settings_file).set_sends_enabled(enabled)
        print(f"sends {args.value} (saved; the hub is not running and reads it at start)")
        return 0


def _enroll(args, config):
    added = Enrollment(config.enrolled_file).enroll(args.session_id, time.time())
    print(f"{args.session_id} {'enrolled' if added else 'was already enrolled'}; "
          "it takes effect at the session's next launch with the desk flags")
    return 0


def _unenroll(args, config):
    removed = Enrollment(config.enrolled_file).unenroll(args.session_id)
    print(f"{args.session_id} {'unenrolled' if removed else 'was not enrolled'}")
    return 0


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
    "sends": _sends, "enroll": _enroll, "unenroll": _unenroll, "flags": _flags,
    "install-config": _install_config,
}
