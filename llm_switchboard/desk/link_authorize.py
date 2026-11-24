"""The Mac link's credential commands and the rollback fence on the Pi's
shell (docs/voice-desk.md, "SSH", "Switching it off, revocation and
rollback", "Hub side": the run lock).

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

import sys

from .config import DeskConfig
from .link_fence import read_lock_view, refuse_lock_view, refuse_signers
from .link_proof import SignersUnreadable, read_signers
from .locks import inspect_lock_file
from .model import DeskError
from .operator import HubAbsent, HubUnavailable, OperatorOp, operator_request
from .refusals import Refusal
from .run_lock import RunLockHeld, RunLockUnusable, probe

EXIT_DONE = 0
EXIT_REFUSED = 1


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
