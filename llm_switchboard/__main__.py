import sys

from .host_cli import COMMANDS

if len(sys.argv) > 1 and sys.argv[1] in COMMANDS:
    from .host_cli import main
else:
    from .cli import main

sys.exit(main())
