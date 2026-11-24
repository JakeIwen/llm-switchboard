"""Mac enrollment on the Pi (docs/voice-desk.md, "Enrollment for Mac
sessions"): `config_dir/enrolled-hosts.json`, `{"hosts": {<host>: {<session
id>: {"since": <unix s>}}}}`, 0600, at most MAX_ENROLLED_HOSTS hosts and
MAX_HOST_ENROLLED sessions across them, read through `read_bounded` and
validated as strictly as `enrolled.json` (one bad entry makes the whole
file malformed). It is a separate file because the phase 1 supervisor,
picker and hub read any unknown key in `enrolled.json` as a malformed file,
which would unenroll every Pi session. It is read and written only under
`enrolled.json`'s lock, so the lock order everywhere stays phase 1's. Like
`enrolled.json`, it is a control for honest operation on the Pi, not a
limit the Mac can verify ("Who the design defends against").

One owner per session id, enforced: `owners` maps every id in either file
to the hosts listing it, and `published_view` routes an id with one owner
to that host and puts an id with more in `conflicts`, routed nowhere until
an unenroll leaves one owner. An unreadable `enrolled-hosts.json` publishes
no Mac enrollment; Pi enrollment stays.

Edits, from the hub and offline, take one rule (`enroll_checked`): under
`enrolled.json`'s lock both files are read fresh and validated before
either is written; the enroll is refused when either can't be read
(exclusivity can't then be checked) and when the id has an owner on
another host in either file, or a registration or a lock on another host
(the hub's state online, `locks.json` through `inspect_lock_file`
offline). An unenroll needs only its own file, since removing an owner
can't create a conflict.
"""

from pathlib import Path
from typing import Callable

from ..session_id import is_session_id
from .enrollment import CachedJson, Enrollment, check_session_id, valid_since_record
from .locks import inspect_lock_file
from .model import HOST, MAX_ENROLLED_HOSTS, MAX_HOST_ENROLLED, DeskError, LockStoreError, is_desk_host
from .refusals import Refusal

HOSTS_KEY = "hosts"
# What enrolling a Mac session means, which the CLI prints and the page's
# confirmation says: the session was launched with the desk flags already.
ENROLL_NOW_NOTICE = "Enroll now: this running Mac session becomes reachable from the desk immediately"


def valid_host_enrollment(data: dict) -> bool:
    if not set(data) <= {HOSTS_KEY}:
        return False
    hosts = data.get(HOSTS_KEY, {})
    if not isinstance(hosts, dict) or len(hosts) > MAX_ENROLLED_HOSTS:
        return False
    total = 0
    for host, sessions in hosts.items():
        if host == HOST or not is_desk_host(host) or not isinstance(sessions, dict):
            return False
        if not all(is_session_id(sid) and valid_since_record(record) for sid, record in sessions.items()):
            return False
        total += len(sessions)
    return total <= MAX_HOST_ENROLLED


def check_mac_host(host) -> str:
    """A Mac's host name for a host-qualified edit (never `pi`, which the
    phase 1 `enroll` edits); 400 otherwise."""
    if host == HOST or not is_desk_host(host):
        raise DeskError(400, Refusal.BAD_HOST)
    return host


class HostEnrollment:
    """`enrolled-hosts.json`, locked by `enrolled.json`'s lock."""

    def __init__(self, path: Path, enrolled_file: Path):
        self._json = CachedJson(path, valid_host_enrollment, lock_for=enrolled_file)

    def hosts(self) -> dict[str, frozenset[str]]:
        """{host: enrolled session ids} from the cache; an unreadable or
        malformed file reads as none."""
        return {host: frozenset(sessions) for host, sessions in self._json.read().get(HOSTS_KEY, {}).items()}

    def hosts_fresh(self) -> dict[str, frozenset[str]]:
        """Opened and validated now, for an edit's checks; 503 when it can't be."""
        data, unreadable = self._json.read_fresh()
        if unreadable:
            raise DeskError(503, Refusal.ENROLLMENT_FILES_UNREADABLE)
        return {host: frozenset(sessions) for host, sessions in data.get(HOSTS_KEY, {}).items()}

    def enroll(self, host: str, session_id: str, now: float) -> bool:
        """True when newly enrolled; the caller holds the lock and has checked."""
        check_session_id(session_id)
        with self._json.updating() as data:
            hosts = data.setdefault(HOSTS_KEY, {})
            if session_id in hosts.get(host, {}):
                return False
            if (host not in hosts and len(hosts) >= MAX_ENROLLED_HOSTS) or \
                    sum(len(sessions) for sessions in hosts.values()) >= MAX_HOST_ENROLLED:
                raise DeskError(409, Refusal.HOST_ENROLLMENT_FULL)
            hosts.setdefault(host, {})[session_id] = {"since": int(now)}
            return True

    def unenroll(self, host: str, session_id: str) -> bool:
        """True when it was enrolled for `host`; a host left with no
        session leaves the file."""
        check_session_id(session_id)
        with self._json.updating() as data:
            hosts = data.get(HOSTS_KEY, {})
            if session_id not in hosts.get(host, {}):
                return False
            del hosts[host][session_id]
            if not hosts[host]:
                del hosts[host]
            return True


def owners(pi_sessions, hosts: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """{session id: the hosts that list it}, from both files."""
    found: dict[str, set[str]] = {session_id: {HOST} for session_id in pi_sessions}
    for host, sessions in hosts.items():
        for session_id in sessions:
            found.setdefault(session_id, set()).add(host)
    return {session_id: frozenset(hosts_) for session_id, hosts_ in found.items()}


def published_view(owner_map: dict[str, frozenset[str]]) -> tuple[dict[str, str], frozenset[str]]:
    """({session id: its one host}, the ids with more than one owner)."""
    enrollment = {sid: next(iter(found)) for sid, found in owner_map.items() if len(found) == 1}
    return enrollment, frozenset(sid for sid, found in owner_map.items() if len(found) > 1)


def enroll_checked(enrollment: Enrollment, host_enrollment: HostEnrollment, host: str, session_id,
                   now: float, held_elsewhere: Callable[[], bool]) -> bool:
    """Enroll `session_id` for `host` (`pi` included) under `enrolled.json`'s
    lock after reading both files fresh: 503 when either can't be read, 409
    when another host owns the id in either file or `held_elsewhere()`
    (a registration or a lock on another host) says so. True when added."""
    check_session_id(session_id)
    with enrollment.locked():
        try:
            pi_sessions = enrollment.sessions(strict=True)
        except DeskError as exc:
            raise DeskError(503, Refusal.ENROLLMENT_FILES_UNREADABLE) from exc
        hosts = host_enrollment.hosts_fresh()
        if owners(pi_sessions, hosts).get(session_id, frozenset()) - {host} or held_elsewhere():
            raise DeskError(409, Refusal.ENROLLED_ON_ANOTHER_HOST)
        if host == HOST:
            return enrollment.enroll(session_id, now)
        return host_enrollment.enroll(host, session_id, now)


def unenroll_edit(enrollment: Enrollment, host_enrollment: HostEnrollment, host: str, session_id) -> bool:
    """Remove the id from `host`'s file only; True when it was there."""
    with enrollment.locked():
        if host == HOST:
            return enrollment.unenroll(session_id)
        return host_enrollment.unenroll(host, session_id)


def lock_file_held_elsewhere(lock_file: Path, session_id: str, host: str) -> Callable[[], bool]:
    """The offline lock check: a lock in `locks.json` for `session_id` on
    another host. A file `inspect_lock_file` refuses refuses the edit."""
    def held() -> bool:
        try:
            locks = inspect_lock_file(lock_file)
        except LockStoreError as exc:
            raise DeskError(503, Refusal.LOCKS_UNREADABLE) from exc
        return any(lock.session_id == session_id and lock.host != host for lock in locks)
    return held


def offline_files(config) -> tuple[Enrollment, HostEnrollment]:
    return Enrollment(config.enrolled_file), HostEnrollment(config.remote_enrolled_file, config.enrolled_file)


def enroll_offline(config, host: str, session_id: str, now: float) -> bool:
    """`desk enroll [--host]` when the connect proved no hub listens; no run
    lock: a stopping hub's handoff holds the same file lock from its reread
    through its lock write."""
    enrollment, host_enrollment = offline_files(config)
    return enroll_checked(enrollment, host_enrollment, host, session_id, now,
                          lock_file_held_elsewhere(config.lock_file, session_id, host))


def unenroll_offline(config, host: str, session_id: str) -> bool:
    return unenroll_edit(*offline_files(config), host, session_id)
