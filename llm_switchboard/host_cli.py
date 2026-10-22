"""Lightweight entry points for the restricted SSH command and Mac publisher."""

import argparse
import logging
from pathlib import Path
import subprocess
import sys

from .host_protocol import host_name
from . import local_config


def add_parsers(sub) -> None:
    for name in ("host-exchange", "host-authorize"):
        parser = sub.add_parser(name, help="exchange host metadata over SSH" if name == "host-exchange" else "authorize a dedicated host public key from stdin")
        parser.add_argument("host", type=host_name)
        parser.set_defaults(func=dispatch, host_command=name)
    local = local_config.load()
    pi, mac = local[local_config.PI_SSH], local[local_config.MAC_NAME]
    for name in ("host-agent", "host-install"):
        parser = sub.add_parser(name, help="publish Mac sessions outward" if name == "host-agent" else "install the Mac launchd publisher")
        parser.add_argument("--pi", default=pi, help=f"existing SSH host alias (default {pi}, from local.env)")
        parser.add_argument("--host-name", default=mac, type=host_name, help=f"default {mac}, from local.env")
        parser.add_argument("--identity", type=Path, help="default ~/.ssh/llm-switchboard-<host-name>")
        if name == "host-agent":
            parser.add_argument("--once", action="store_true", help="publish once and exit")
        else:
            parser.add_argument("--unload", action="store_true", help="unload the launchd job")
        parser.set_defaults(func=dispatch, host_command=name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="llm-switchboard")
    add_parsers(parser.add_subparsers(required=True))
    args = parser.parse_args(argv)
    return dispatch(args)


def dispatch(args) -> int:
    if args.host_command in ("host-agent", "host-install") and args.identity is None:
        args.identity = Path.home() / ".ssh" / f"llm-switchboard-{args.host_name}"
    try:
        if args.host_command == "host-exchange":
            from .host_exchange import exchange
            exchange(sys.stdin.buffer, sys.stdout, args.host)
        elif args.host_command == "host-authorize":
            from .host_authorize import authorize
            print(authorize(args.host, sys.stdin.buffer.read(16385)))
        elif args.host_command == "host-install":
            from .host_launchd import install
            print(install(args.pi, args.host_name, args.identity.expanduser().resolve(), unload=args.unload))
        else:
            from .host_agent import HostAgent
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            agent = HostAgent(args.pi, args.host_name, args.identity)
            if args.once:
                agent.cycle()
            else:
                agent.run_forever()
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        print(f"{args.host_command}: {e}", file=sys.stderr)
        return 1
    return 0
