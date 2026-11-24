"""The rollback fence's rules (docs/voice-desk.md, "Switching it off,
revocation and rollback", step 3), shared by the hub's `link_fence`
(`LinkHub.fence_check` for the links, `HubState.fence_lock_view` for the
locks) and the offline half under the run lock (`link_authorize`).

The fence passes only when no Mac lock can exist or appear: `link-signers`
readable with no line (no link can be admitted), no live link and no
handoff past the gate (the hub's half), and `locks.json`, read from disk
through `locks.inspect_lock_file`, together with the hub's list and its
store's error, holding no lock whose host isn't `pi`. Each refusal is a
`DeskError` naming what is left. Standard library only.
"""

from dataclasses import dataclass

from .model import HOST, DeskError, Lock, LockStoreError
from .refusals import Refusal


@dataclass(frozen=True)
class FenceLockView:
    """One moment of the lock table: the hub's in-memory list and its store's
    error (both empty offline), and the file on disk or why it can't be read."""
    listed: tuple[Lock, ...]
    store_error: str | None
    disk: tuple[Lock, ...] | None
    disk_error: str | None


def read_lock_view(inspect, listed=(), store_error: str | None = None) -> FenceLockView:
    """`inspect()` (a bounded read of `locks.json`) taken into a view."""
    try:
        return FenceLockView(tuple(listed), store_error, tuple(inspect()), None)
    except LockStoreError as exc:
        return FenceLockView(tuple(listed), store_error, None, str(exc.args[0] if exc.args else "lock file error"))


def refuse_signers(signers) -> None:
    """`signers` as `link_proof.read_signers` returned them: none may be left."""
    if signers:
        raise DeskError(409, Refusal.FENCE_SIGNERS_LISTED, {"hosts": sorted(s.host for s in signers)})


def refuse_lock_view(view: FenceLockView) -> None:
    if view.disk_error is not None:
        raise DeskError(409, Refusal.FENCE_LOCK_FILE, {"detail": view.disk_error})
    if view.store_error is not None:
        raise DeskError(409, Refusal.FENCE_STORE_ERROR, {"detail": view.store_error})
    mac = {lock.lock_id: lock for lock in (*view.disk, *view.listed) if lock.host != HOST}
    if mac:
        raise DeskError(409, Refusal.FENCE_MAC_LOCK, {"locks": [
            {"host": lock.host, "session_id": lock.session_id, "lock_id": lock.lock_id}
            for lock in sorted(mac.values(), key=lambda lock: (lock.host, lock.session_id, lock.lock_id))]})
