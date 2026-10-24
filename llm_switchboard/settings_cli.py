"""Local settings commands; usable even when the web service is stopped."""

import argparse
import json
import sys

from . import settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="llm-switchboard")
    add_parsers(parser.add_subparsers(required=True))
    args = parser.parse_args(argv)
    return args.func(args)


def add_parsers(sub) -> None:
    parser = sub.add_parser("settings", help="show or change this install's settings")
    parser.set_defaults(func=_show)
    setters = parser.add_subparsers()
    setter = setters.add_parser("set", help="set one key (for example folders.depth)")
    setter.add_argument("key", metavar="KEY")
    setter.add_argument("value", metavar="VALUE", help="folders.hidden takes a comma-separated list")
    setter.set_defaults(func=_set)


def _show(args) -> int:
    print(json.dumps(settings.load(), indent=2))
    return 0


def _set(args) -> int:
    try:
        current = settings.save(settings.cli_update(args.key, args.value))
    except ValueError as exc:
        print(f"settings: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(current, indent=2))
    return 0
