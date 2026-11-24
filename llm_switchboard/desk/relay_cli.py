"""The Mac relay's `llm-switchboard desk` commands, kept beside cli.py so the
relay's imports load only when one of them runs.

`desk relay` runs the relay in the foreground (relay.run_process): the same
process the launchd job runs, logging to the terminal as well as to its
rotating log, until Ctrl-C or SIGTERM. Exit 3 when another relay holds
`relay.lock`, 2 when it can't start (the lock file, the channel socket, the
Mac's hardware UUID), 1 when its link loop died.

`desk relay-install` writes and loads the relay's launchd job;
`relay-install --unload` boots it out, deletes its plist and confirms that
no relay is left (relay_launchd.py). Exit 2 when refused before anything
changed (off macOS, no link key, no checkout interpreter), 1 when launchctl
refused the bootstrap or the unload couldn't be confirmed within 5 s.
"""

import subprocess
import sys

EXIT_FAILED = 1
EXIT_SETUP = 2


def relay(args, config) -> int:
    from .relay import run_process
    return run_process(config, launched=False)


def relay_install(args, config, *, launchd=None, platform: str = sys.platform, environ=None,
                  timing: dict | None = None) -> int:
    """`launchd`, `platform`, `environ` and `timing` (unload's wait_seconds,
    clock and sleep) are seams for tests."""
    from .relay_launchd import (LABEL, UNLOAD_WAIT_SECONDS, RelayInstallRefused, RelayUnloadUnconfirmed,
                                install, unload)
    try:
        if args.unload:
            plist = unload(config, launchd=launchd, platform=platform, **(timing or {}))
            print(f"{LABEL} unloaded and {plist} deleted; no desk relay is running, and this Mac opens no "
                  "desk link until `desk relay-install` runs again")
            return 0
        plist = install(config, launchd=launchd, platform=platform, environ=environ)
    except RelayInstallRefused as exc:
        print(f"desk: {exc}", file=sys.stderr)
        return EXIT_SETUP
    except RelayUnloadUnconfirmed as exc:
        print(f"desk: {LABEL} was booted out and its plist deleted, but after {UNLOAD_WAIT_SECONDS} s {exc}; "
              "run relay-install --unload again", file=sys.stderr)
        return EXIT_FAILED
    except subprocess.CalledProcessError as exc:
        print(f"desk: launchctl bootstrap of {LABEL} failed (exit {exc.returncode}); its plist is written; "
              "run relay-install again", file=sys.stderr)
        return EXIT_FAILED
    print(f"{LABEL} loaded from {plist}")
    return 0
