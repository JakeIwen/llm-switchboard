"""The Mac relay's `llm-switchboard desk` commands, kept beside cli.py so the
relay's imports load only when one of them runs.

`desk relay` runs the relay in the foreground (relay.run_process): the same
process the launchd job runs, logging to the terminal as well as to its
rotating log, until Ctrl-C or SIGTERM. Exit 3 when another relay holds
`relay.lock`, 2 when it can't start (the lock file, the channel socket, the
Mac's hardware UUID), 1 when its link loop died.
"""


def relay(args, config) -> int:
    from .relay import run_process
    return run_process(config, launched=False)
