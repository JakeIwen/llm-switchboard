"""The Mac link's credential commands and the rollback fence on the Pi's
shell (docs/voice-desk.md, "SSH", "Switching it off, revocation and
rollback", "Hub side": the run lock).

`desk link-authorize <mac>` reads one Ed25519 public-key line on stdin and
runs four idempotent steps, one line each (`done`, `already so` or the
failure, which names the steps still to do and exits 1): the verifier's
self-test (`--self-test` runs only this, exit 2 on failure); the key and
the name, before any file is read or locked; the check and write of both
files inside `link-signers.lock` then the `authorized_keys` lock (the
proposed `link-signers` checked by the hub's reader before it is written,
then the marked `authorized_keys` line); and, holding no file lock, the
confirmation by the hub's `link_drop` answer. `desk link-revoke <mac>`
removes the signer line, then the marked line, in one hold of the same two
locks in the same order, then confirms the cutoff the same way. With no
hub answering, each confirmation applies the run lock rule: holding
`hub.lock` it rereads `link-signers`; with it held elsewhere it refuses,
and the credential edits already made stand.

`desk link-fence` asks a running hub (operator op `link_fence`); with no
hub answering (`HubAbsent`) it applies the run lock rule: it takes
`hub.lock` non-blocking (`run_lock.probe`) and, holding it, reads
`link-signers` through the hub's reader and `locks.json` through
`inspect_lock_file`, with the hub's refusals (`link_fence`); with the lock
held elsewhere (a hub starting or stopping) it refuses having read
nothing. It never waits for the run lock and holds nothing after it
answers; every refusal exits 1 naming what is left. Standard library only;
never imports the hub or the HTTP stack.
"""

import contextlib
import os
import shlex
import sys
import tempfile

from ..host_authorize import public_key
from ..ssh_authorized_keys import (
    authorized_keys_path, has_marker, locked, read_lines, remove_marked_line, replace_marked_line,
)
from .config import DeskConfig
from .enrollment import ensure_private_dir
from .link_fence import read_lock_view, refuse_lock_view, refuse_signers
from .link_proof import (
    KEY_TYPE, MAX_SIGNER_LINES, Signer, SignersUnreadable, format_signers, read_signers, self_test,
)
from .link_protocol import valid_link_host
from .locks import inspect_lock_file
from .model import DeskError, LockStoreError
from .operator import HubAbsent, HubUnavailable, OperatorOp, operator_request
from .refusals import Refusal
from .run_lock import RunLockHeld, RunLockUnusable, probe

EXIT_DONE = 0
EXIT_REFUSED = 1
EXIT_SETUP = 2


def run_lock_refusal(command: str) -> str:
    """The run lock rule's refusal: a hub process holds `hub.lock` while it
    starts or stops, and may still hold a link or a handoff."""
    return (f"a desk hub process is starting or stopping; run {command} again once the hub answers, or once "
            "`systemctl --user is-active llm-switchboard-desk` prints inactive")


def _print_refusal(prefix: str, reason: str, fields: dict) -> None:
    print(f"desk: {prefix}: {reason}", file=sys.stderr)
    for host in fields.get("hosts") or ():
        print(f"  host {host}", file=sys.stderr)
    for lock in fields.get("locks") or ():
        print(f"  lock {lock.get('lock_id')}: host {lock.get('host')}, session {lock.get('session_id')}",
              file=sys.stderr)
    if fields.get("detail"):
        print(f"  {fields['detail']}", file=sys.stderr)


# -- the rollback fence --------------------------------------------------------------

def fence_offline(config: DeskConfig) -> None:
    """The fence's decision with no hub, under the run lock: DeskError for
    a refusal, RunLockHeld when a hub process holds the lock (nothing read)."""
    with probe(config.hub_run_lock):
        try:
            signers = read_signers(config.link_signers_file)
        except SignersUnreadable:
            raise DeskError(409, Refusal.FENCE_SIGNERS_UNREADABLE) from None
        refuse_signers(signers)
        refuse_lock_view(read_lock_view(lambda: inspect_lock_file(config.lock_file)))


def run_fence(config: DeskConfig) -> int:
    try:
        response = operator_request(config.operator_socket, OperatorOp.LINK_FENCE)
    except HubAbsent:
        try:
            fence_offline(config)
        except RunLockHeld:
            print(f"desk: link-fence refused: {run_lock_refusal('link-fence')}", file=sys.stderr)
            return EXIT_REFUSED
        except RunLockUnusable as exc:
            print(f"desk: link-fence refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        except DeskError as exc:
            _print_refusal("link-fence refused", exc.reason, exc.fields)
            return EXIT_REFUSED
        print("link-fence passed (no hub running): no Mac signer line and no Mac lock; downgrade now")
        return EXIT_DONE
    except HubUnavailable as exc:
        print(f"desk: link-fence not confirmed: {exc}; run link-fence again", file=sys.stderr)
        return EXIT_REFUSED
    if not response.get("ok"):
        error = response.get("error", "refused")
        if error == Refusal.UNKNOWN_OP:
            error = "the running desk hub is older and has no link-fence; nothing was checked"
        _print_refusal("link-fence refused", error, response)
        return EXIT_REFUSED
    print("link-fence passed: no Mac signer line, link, handoff or lock; downgrade now")
    return EXIT_DONE


# -- link-authorize and link-revoke ---------------------------------------------------

MARKER_PREFIX = "llm-switchboard-desk-link:"
SELF_TEST_REFUSAL = ("the Pi's ssh-keygen can't verify SSH signatures (OpenSSH 8.1 or later is needed); "
                     "nothing changed")
ED25519_REFUSAL = "the desk link needs an Ed25519 key (ssh-keygen -t ed25519); nothing changed"
SIGNERS_REFUSAL = ("link-signers is unreadable or malformed; nothing changed; move it aside and run "
                   "link-authorize again for each Mac")
MAX_KEY_LINE_BYTES = 16 * 1024


def marker(host: str) -> bytes:
    """The final comment that marks the desk link's `authorized_keys` line."""
    return f"{MARKER_PREFIX}{host}".encode()


def key_line(config: DeskConfig, signer: Signer) -> bytes:
    """`restrict,command="<link_argv>" ssh-ed25519 <blob> <marker>`, newline-ended."""
    command = shlex.join(config.link_argv).replace("\\", "\\\\").replace('"', '\\"')
    return f'restrict,command="{command}" {KEY_TYPE} {signer.blob} '.encode() + marker(signer.host) + b"\n"


class Steps:
    """One line per step (`done`, `already so`, or the failure); a failure
    names the steps still to do and exits 1 (or `code`)."""

    def __init__(self, command: str, names: tuple[str, ...], out):
        self._command, self._names, self._out, self._at = command, names, out, 0

    def done(self, detail: str = "done") -> None:
        self._out(f"{self._command} step {self._at + 1} ({self._names[self._at]}): {detail}", False)
        self._at += 1

    def fail(self, message: str, code: int = EXIT_REFUSED) -> int:
        self._out(f"desk: {self._command} step {self._at + 1} ({self._names[self._at]}): {message}", True)
        left = ", ".join(f"{n} ({name})" for n, name in enumerate(self._names, 1) if n > self._at)
        self._out(f"desk: {self._command}: still to do: {left}; run the command again to complete them", True)
        return code


def _print(line: str, error: bool) -> None:
    print(line, file=sys.stderr if error else sys.stdout)


def _write_signers(path, data: bytes) -> None:
    """The whole file (temporary file, fsync, rename), 0600."""
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".link-signers-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _replace_signers(path, listed: tuple[Signer, ...], proposed: list[Signer]) -> bool:
    """Write `proposed` (checked by the hub's reader first) when it differs
    from `listed`; True when the file changed."""
    data = format_signers(proposed)
    if tuple(proposed) == listed:
        return False
    _write_signers(path, data)
    return True


def run_self_test(*, verifier_self_test=self_test, out=_print) -> int:
    steps = Steps("link-authorize", ("verifier self-test",), out)
    if not verifier_self_test():
        return steps.fail(SELF_TEST_REFUSAL, EXIT_SETUP)
    steps.done()
    return EXIT_DONE


def run_authorize(config: DeskConfig, host: str, raw: bytes, *, verifier_self_test=self_test, out=_print) -> int:
    steps = Steps("link-authorize", ("verifier self-test", "key and name", "link-signers and authorized_keys",
                                     "confirmation"), out)
    if not verifier_self_test():
        return steps.fail(SELF_TEST_REFUSAL, EXIT_SETUP)
    steps.done()
    # The key and the name, before any file is read or locked.
    if not valid_link_host(host):
        return steps.fail(f"{host!r} is not a Mac host name (never pi); nothing changed")
    try:
        kind, blob = public_key(raw)
    except (ValueError, UnicodeDecodeError):
        return steps.fail("supply exactly one public-key line on stdin; nothing changed")
    if kind != KEY_TYPE:
        return steps.fail(ED25519_REFUSAL)
    signer = Signer(host, blob)
    steps.done()
    try:
        keys = authorized_keys_path(config.home)
    except ValueError as exc:
        return steps.fail(f"{exc}; nothing changed")
    with locked(config.link_signers_file), locked(keys):
        try:
            listed = read_signers(config.link_signers_file)
        except SignersUnreadable:
            return steps.fail(SIGNERS_REFUSAL)
        others = [line for line in read_lines(keys) if not has_marker(line, marker(host))]
        if any(blob.encode() in line.split() for line in others):
            return steps.fail("this key is already on another authorized_keys line (the v1 key or another "
                              "host's); nothing changed")
        if any(s.blob == blob and s.host != host for s in listed):
            return steps.fail("this key is already listed for another Mac in link-signers; nothing changed")
        if all(s.host != host for s in listed) and len(listed) >= MAX_SIGNER_LINES:
            return steps.fail(f"link-signers already lists {MAX_SIGNER_LINES} Macs, the most it holds; run "
                              "link-revoke for one first; nothing changed")
        proposed = [signer if s.host == host else s for s in listed]
        if signer not in proposed:
            proposed.append(signer)
        signers_changed = _replace_signers(config.link_signers_file, listed, proposed)
        keys_changed = replace_marked_line(keys, marker(host), key_line(config, signer))
    steps.done(_edits(signers_changed, keys_changed))
    return _confirm_authorized(config, signer, steps, out)


def _edits(signers_changed: bool, keys_changed: bool) -> str:
    if not (signers_changed or keys_changed):
        return "already so"
    return (f"done (link-signers {'written' if signers_changed else 'already so'}, authorized_keys "
            f"{'written' if keys_changed else 'already so'})")


def _not_listed(host: str) -> str:
    return (f"a later link-revoke or link-authorize changed {host}'s signer line; the key this command "
            "installed is not listed")


def _overtaken(host: str, fingerprint: str) -> str:
    return (f"a later link-authorize listed key {fingerprint} for {host}; this revocation was overtaken and the "
            "credential is not withdrawn; run link-revoke again to withdraw it")


UNCONFIRMED = ("cutoff not confirmed; the hub retires the link at its next housekeeping pass; run link-revoke "
               "again to confirm")


def _link_drop(config: DeskConfig, host: str):
    """The hub's answer to `link_drop`, or None with no hub (HubAbsent)."""
    try:
        return operator_request(config.operator_socket, OperatorOp.LINK_DROP, host=host)
    except HubAbsent:
        return None


def _offline_signers(config: DeskConfig):
    """No hub answered: under the run lock, `link-signers` reread through the
    hub's reader and `locks.json`; RunLockHeld when a hub process holds it."""
    with probe(config.hub_run_lock):
        listed = {s.host: s for s in read_signers(config.link_signers_file)}
        try:
            held = inspect_lock_file(config.lock_file)
        except LockStoreError:
            held = []
    return listed, held


def _confirm_authorized(config: DeskConfig, signer: Signer, steps: Steps, out) -> int:
    host, edits_stand = signer.host, "; the credential edits already made stand"
    try:
        answer = _link_drop(config, host)
    except HubUnavailable as exc:
        return steps.fail(f"not confirmed: {exc}{edits_stand}")
    if answer is None:
        try:
            listed, held = _offline_signers(config)
        except RunLockHeld:
            return steps.fail(run_lock_refusal(f"link-authorize {host}") + edits_stand)
        except (RunLockUnusable, SignersUnreadable) as exc:
            return steps.fail(f"not confirmed: {exc or 'link-signers unreadable'}{edits_stand}")
        if listed.get(host) != signer:
            return steps.fail(_not_listed(host))
        steps.done("no hub running: no link to drop")
        _stale_locks(held, signer, out)
        return EXIT_DONE
    if not answer.get("ok"):
        error = answer.get("error", "refused")
        if error == Refusal.UNKNOWN_OP:
            error = "the running desk hub is older and has no link_drop"
        return steps.fail(f"not confirmed: {error}{edits_stand}")
    listed, live = answer.get("signer"), answer.get("link")
    if not isinstance(listed, dict) or listed.get("fingerprint") != signer.fingerprint:
        return steps.fail(_not_listed(host))
    if live is not None and (not isinstance(live, dict) or live.get("fingerprint") != signer.fingerprint):
        return steps.fail(f"not confirmed: the hub still holds a link for {host} with another key")
    steps.done()
    try:
        status = operator_request(config.operator_socket, OperatorOp.STATUS)
    except HubUnavailable:
        status = {}
    _stale_locks([_StatusLock(row) for row in status.get("locks") or () if isinstance(row, dict)], signer, out)
    return EXIT_DONE


class _StatusLock:
    """A `desk status` lock row with the fields the stale-lock notice reads."""

    def __init__(self, row: dict):
        self.lock_id, self.host = row.get("lock_id"), row.get("host")
        self.session_id, self.link_key = str(row.get("session_id")), row.get("link_key")


def _stale_locks(held, signer: Signer, out) -> None:
    """Locks of the host handed over a link with another key: no presence
    answer counts for them any more."""
    for lock in held:
        if lock.host == signer.host and lock.link_key != signer.fingerprint:
            out(f"lock {lock.lock_id} of session {lock.session_id[:8]} was handed over {signer.host}'s link with "
                "another key; once you have seen that session exit, release it with desk clear-stopped", False)


def run_revoke(config: DeskConfig, host: str, *, out=_print) -> int:
    steps = Steps("link-revoke", ("link-signers", "authorized_keys", "confirmation"), out)
    if not valid_link_host(host):
        return steps.fail(f"{host!r} is not a Mac host name (never pi); nothing changed")
    try:
        keys = authorized_keys_path(config.home)
    except ValueError as exc:
        return steps.fail(f"{exc}; nothing changed")
    with locked(config.link_signers_file), locked(keys):
        try:
            listed = read_signers(config.link_signers_file)
        except SignersUnreadable:
            return steps.fail(SIGNERS_REFUSAL)
        changed = _replace_signers(config.link_signers_file, listed, [s for s in listed if s.host != host])
        steps.done("done" if changed else "already so")
        steps.done("done" if remove_marked_line(keys, marker(host)) else "already so")
    return _confirm_revoked(config, host, steps)


def _confirm_revoked(config: DeskConfig, host: str, steps: Steps) -> int:
    edits_stand = "; the credential edits steps 1 and 2 made stand"
    try:
        answer = _link_drop(config, host)
    except HubUnavailable:
        return steps.fail(UNCONFIRMED)
    if answer is None:
        try:
            listed, _ = _offline_signers(config)
        except RunLockHeld:
            return steps.fail(run_lock_refusal(f"link-revoke {host}") + edits_stand)
        except (RunLockUnusable, SignersUnreadable) as exc:
            return steps.fail(f"{UNCONFIRMED} ({exc or 'link-signers unreadable'})")
        if host in listed:
            return steps.fail(_overtaken(host, listed[host].fingerprint))
        steps.done("no hub running: no link to drop")
        return EXIT_DONE
    if not answer.get("ok"):
        return steps.fail(f"{UNCONFIRMED} ({answer.get('error', 'refused')})")
    listed, live = answer.get("signer"), answer.get("link")
    if listed is not None:
        return steps.fail(_overtaken(host, listed.get("fingerprint") if isinstance(listed, dict) else "?"))
    if live is not None:
        return steps.fail(UNCONFIRMED)
    steps.done()
    return EXIT_DONE
