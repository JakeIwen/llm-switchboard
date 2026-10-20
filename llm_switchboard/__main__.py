import sys

# These commands have entry points that import little. cli.py imports the web
# app, nearly a second on a busy Pi, which the SSH forced command, llmsearch
# and serve's 30 s indexer child would otherwise pay every time.
_LIGHT_COMMANDS = {
    "host-exchange": "host_cli", "host-authorize": "host_cli",
    "host-agent": "host_cli", "host-install": "host_cli",
    "search": "search_cli", "index": "search_cli",
}

module = _LIGHT_COMMANDS.get(sys.argv[1] if len(sys.argv) > 1 else "")
if module == "host_cli":
    from .host_cli import main
elif module == "search_cli":
    from .search_cli import main
else:
    from .cli import main

sys.exit(main())
