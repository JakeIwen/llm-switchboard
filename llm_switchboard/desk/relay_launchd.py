"""The Mac relay's launchd job, `dev.llm-switchboard.desk-relay`
(docs/voice-desk.md, "Processes, sockets and files" and "Switching it off,
revocation and rollback").

`install` checks before it changes anything (macOS, the link key, the
checkout's interpreter and `bin/desk-relay`), writes the plist and loads it
the way v1's jobs are loaded (`host_launchd.bootstrap`: bootout, then
bootstrap retried while launchd finishes removing a running job).

`unload` is the off switch the Mac enforces: bootout of this one label, the
plist deleted so the next login doesn't load it again, and done only once
`launchctl print` no longer finds the label and `relay.lock` is free, so no
relay process is left; it waits up to 5 s for a relay that is exiting.

Every `launchctl` call and the LaunchAgents directory go through `Launchd`,
which tests replace. Standard library only.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from typing import Callable

from ..host_launchd import bootstrap
from ..paths import MAC_JOB_PATH
from .config import DeskConfig
from .relay_ssh import ConfigUnsupported, desk_key
from .run_lock import RunLockHeld, RunLockUnusable, probe

LABEL = "dev.llm-switchboard.desk-relay"
UNLOAD_WAIT_SECONDS = 5
POLL_SECONDS = 0.1
# `launchctl print` exits 113 ("Could not find service") for a label the
# domain doesn't have; any other failure is no evidence that the job is gone.
SERVICE_NOT_FOUND = 113


class RelayInstallRefused(Exception):
    """Refused before anything changed; the message is for the operator."""


class RelayUnloadUnconfirmed(Exception):
    """Couldn't prove the relay was stopped within the unload deadline."""


@dataclass(frozen=True)
class Launchd:
    """The user's launchd domain and LaunchAgents directory. `run` has
    `subprocess.run`'s signature; tests pass a fake and a temporary
    directory, so no test calls launchctl or writes under ~/Library."""
    agents_dir: Path
    domain: str
    # None: subprocess.run, looked up at each call (so a test's patch of it holds).
    run: Callable[..., subprocess.CompletedProcess] | None = None

    @classmethod
    def for_user(cls, home: Path) -> "Launchd":
        return cls(Path(home) / "Library" / "LaunchAgents", f"gui/{os.getuid()}")

    @property
    def plist(self) -> Path:
        return self.agents_dir / f"{LABEL}.plist"

    @property
    def service(self) -> str:
        return f"{self.domain}/{LABEL}"

    @property
    def runner(self) -> Callable[..., subprocess.CompletedProcess]:
        return self.run or subprocess.run

    def launchctl(self, *args: str, timeout: float | None = None) -> int:
        argv = ["launchctl", *args]
        if timeout is None:
            return self.runner(argv, check=False, capture_output=True).returncode
        # No output is needed for the confirmation. subprocess.run kills and
        # reaps on timeout without the desk link child's separate TERM grace.
        return self.runner(argv, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout).returncode


def job(config: DeskConfig) -> dict:
    """Pin the config home even if the GUI domain has its own XDG setting.
    Output goes to /dev/null: bin/desk-relay keeps its own capped launch log."""
    return {"Label": LABEL, "ProgramArguments": list(config.relay_argv), "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 10, "ProcessType": "Standard",
            "EnvironmentVariables": {"HOME": str(config.home), "PATH": MAC_JOB_PATH,
                                     "XDG_CONFIG_HOME": str(config.mac_relay_config_home)},
            "StandardOutPath": os.devnull, "StandardErrorPath": os.devnull}


def install(config: DeskConfig, *, launchd: Launchd | None = None, platform: str = sys.platform,
            environ=None) -> Path:
    """Write the plist and load the job; the plist's path. RelayInstallRefused
    (nothing changed) off macOS, without the link key, or without the
    checkout's interpreter or bin/desk-relay; CalledProcessError when
    launchctl never accepted the bootstrap (the plist is written)."""
    _require_mac_config(platform, config)
    try:
        key = desk_key(config, environ=environ)
    except ConfigUnsupported:
        raise RelayInstallRefused("LLM_SWITCHBOARD_MAC_NAME in local.env is not a Mac host name the desk link "
                                  "can use; nothing changed") from None
    if not key.is_file():
        raise RelayInstallRefused(f"the desk link key {key} is missing (scripts/setup-mac-desk.sh makes it); "
                                  "nothing changed")
    for program in (config.relay_argv[0], config.relay_argv[-1]):
        if not Path(program).is_file():
            raise RelayInstallRefused(f"{program} is missing (scripts/setup-mac.sh sets up the checkout); "
                                      "nothing changed")
    launchd = launchd or Launchd.for_user(config.home)
    launchd.agents_dir.mkdir(parents=True, exist_ok=True)
    _write_plist(launchd.plist, job(config))
    launchd.launchctl("bootout", launchd.service)
    bootstrap(launchd.runner, launchd.domain, launchd.plist)
    return launchd.plist


def unload(config: DeskConfig, *, launchd: Launchd | None = None, platform: str = sys.platform,
           wait_seconds: float = UNLOAD_WAIT_SECONDS, clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> Path:
    """Boot out the job, delete its plist and confirm no relay is left; the
    plist's path. Idempotent: an unloaded job is confirmed the same way.
    RelayUnloadUnconfirmed when it can't confirm within `wait_seconds`."""
    _require_mac_config(platform, config)
    launchd = launchd or Launchd.for_user(config.home)
    deadline = clock() + wait_seconds
    if _unload_call(launchd, "bootout", deadline, clock) is None:
        raise RelayUnloadUnconfirmed("`launchctl bootout` timed out; the plist was left in place")
    launchd.plist.unlink(missing_ok=True)
    left = "confirmation deadline elapsed"
    while clock() < deadline:
        status = _unload_call(launchd, "print", deadline, clock)
        if status is None:
            left = "`launchctl print` timed out"
            break
        left = _still_running(config, status)
        remaining = deadline - clock()
        if remaining <= 0:
            break
        if left is None:
            return launchd.plist
        sleep(min(POLL_SECONDS, remaining))
    raise RelayUnloadUnconfirmed(f"the plist was deleted, but {left or 'confirmation deadline elapsed'}")


def _unload_call(launchd: Launchd, verb: str, deadline: float, clock: Callable[[], float]) -> int | None:
    remaining = deadline - clock()
    if remaining <= 0:
        return None
    try:
        return launchd.launchctl(verb, launchd.service, timeout=remaining)
    except subprocess.TimeoutExpired:
        return None


def _still_running(config: DeskConfig, status: int) -> str | None:
    """What still runs, or None when the label is gone and the lock is free."""
    if status == 0:
        return f"launchd still lists {LABEL}"
    if status != SERVICE_NOT_FOUND:
        return f"`launchctl print` exited {status}, so it can't confirm that {LABEL} is gone"
    try:
        with probe(config.relay_lock):
            return None
    except RunLockHeld:
        return f"a desk relay is still running: it holds {config.relay_lock}"
    except RunLockUnusable as exc:
        return f"{exc}; it can't confirm that no relay is running"


def _require_mac_config(platform: str, config: DeskConfig) -> None:
    if platform != "darwin":
        raise RelayInstallRefused("desk relay-install runs only on the Mac; nothing changed")
    if reason := config.mac_relay_location_error():
        raise RelayInstallRefused(f"{reason}; nothing changed")


def _write_plist(path: Path, spec: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as f:
        plistlib.dump(spec, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
