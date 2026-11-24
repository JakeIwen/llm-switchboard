"""Admission on the hub's `link.sock`, steps 2 to 6 (docs/voice-desk.md,
"Admission and proof of possession"): the peer's uid, the hello and
version negotiation, the signer line the hello's host names, the
challenge and the proof. Publication (step 7, the reread under the links
lock) is `link_hub.LinkHub`'s.

Nothing here is attributed to a host: the hello's `host` only says which
signer line to try. Every refusal is a `link_refused` code, counted in
`AdmissionRefusals`, one entry per code whatever names hellos carry, so a
refused hello can't change any host's link state. Standard library only.
"""

from dataclasses import dataclass
import os
import socket
import threading
from typing import Callable

from .link_proof import ProofCheck, Signer, SignersUnreadable, read_signers
from .link_protocol import (
    LINK_VERSIONS, MAX_VERSIONS, NONCE_BYTES, LineSplitter, LinkFrameError, LinkType, RefusedCode, Side,
    decode_frame, encode_frame,
)

_READ_BYTES = 64 * 1024


class AdmissionClosed(Exception):
    """Close without a refusal frame: another uid, EOF, a bad frame or one
    out of order. args[0] is a short log word, never peer text."""


class AdmissionRefused(Exception):
    """Answer `link_refused {code}` and close. `relay_versions` is the
    hello's validated version list, kept only for `version`."""

    def __init__(self, code: RefusedCode, relay_versions: list[int] | None = None):
        super().__init__(code)
        self.code = code
        self.relay_versions = relay_versions


@dataclass
class Admitted:
    """A hello whose proof verified against `signer`: not yet published."""
    signer: Signer
    machine: str
    version: int
    # Bytes the peer sent after its proof (a whole-line reader's remainder).
    splitter: LineSplitter


# How a verification's result becomes the refusal the relay sees. A
# verifier that ran out of time says nothing about the key, so the relay
# retries on its normal backoff rather than the fixed 10 minutes.
_PROOF_REFUSALS = {
    ProofCheck.PROOF_BAD: RefusedCode.PROOF_FAILED,
    ProofCheck.VERIFIER_TIMEOUT: RefusedCode.ADMISSION_TIMEOUT,
    ProofCheck.VERIFIER_UNAVAILABLE: RefusedCode.VERIFIER_UNAVAILABLE,
}


def refusal_frame(code: RefusedCode) -> bytes:
    """The version-free `link_refused`, naming the versions this hub speaks."""
    return encode_frame(LinkType.LINK_REFUSED, Side.HUB, reason=code.value, versions=list(LINK_VERSIONS))


class _DeadlineIO:
    """Whole lines and writes on the admission socket, every wait within
    the one deadline from accept."""

    def __init__(self, sock: socket.socket, deadline: float, clock: Callable[[], float]):
        self._sock = sock
        self._deadline = deadline
        self._clock = clock
        self.splitter = LineSplitter()
        self._lines: list[bytes] = []

    def remaining(self) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise AdmissionRefused(RefusedCode.ADMISSION_TIMEOUT)
        return remaining

    def read_frame(self, v: int | None) -> tuple[LinkType, dict]:
        while not self._lines:
            self._sock.settimeout(self.remaining())
            try:
                data = self._sock.recv(_READ_BYTES)
            except TimeoutError:
                raise AdmissionRefused(RefusedCode.ADMISSION_TIMEOUT) from None
            except OSError:
                raise AdmissionClosed("read failed") from None
            if not data:
                raise AdmissionClosed("eof")
            try:
                self._lines.extend(self.splitter.feed(data))
            except LinkFrameError:
                raise AdmissionClosed("bad frame") from None
        line = self._lines.pop(0)
        if self._lines:
            raise AdmissionClosed("frame before its answer")
        try:
            return decode_frame(line, Side.RELAY, v=v)
        except LinkFrameError:
            raise AdmissionClosed("bad frame") from None

    def send(self, data: bytes) -> None:
        self._sock.settimeout(self.remaining())
        try:
            self._sock.sendall(data)
        except TimeoutError:
            raise AdmissionRefused(RefusedCode.ADMISSION_TIMEOUT) from None
        except OSError:
            raise AdmissionClosed("write failed") from None


def admit(sock: socket.socket, *, epoch: str, signers_path, verifier_available: bool,
          verify: Callable[..., ProofCheck], peer_uid: Callable[[socket.socket], int],
          nonce_bytes: Callable[[int], bytes], clock: Callable[[], float], deadline_seconds: float) -> Admitted:
    """Steps 2 to 6 on one accepted connection, within `deadline_seconds`
    of now. Raises AdmissionRefused or AdmissionClosed."""
    io = _DeadlineIO(sock, clock() + deadline_seconds, clock)
    try:
        if peer_uid(sock) != os.getuid():
            raise AdmissionClosed("another uid")
    except OSError:
        raise AdmissionClosed("no peer credentials") from None
    kind, hello = io.read_frame(v=None)
    if kind is not LinkType.LINK_HELLO:
        raise AdmissionClosed("not a hello")
    common = set(hello["versions"]) & set(LINK_VERSIONS)
    if not common:
        raise AdmissionRefused(RefusedCode.VERSION, list(hello["versions"]))
    if not verifier_available:
        raise AdmissionRefused(RefusedCode.VERIFIER_UNAVAILABLE)
    try:
        signers = read_signers(signers_path)
    except SignersUnreadable:
        raise AdmissionRefused(RefusedCode.SIGNERS_UNREADABLE) from None
    signer = next((s for s in signers if s.host == hello["host"]), None)
    if signer is None:
        raise AdmissionRefused(RefusedCode.NOT_AUTHORIZED)
    version = max(common)
    nonce = nonce_bytes(NONCE_BYTES).hex()
    io.send(encode_frame(LinkType.LINK_CHALLENGE, Side.HUB, version=version, epoch=epoch, nonce=nonce))
    kind, proof = io.read_frame(v=version)
    if kind is not LinkType.LINK_PROOF:
        raise AdmissionClosed("not a proof")
    check = verify(signer, version=version, machine=hello["machine"], epoch=epoch, nonce=nonce,
                   signature=proof["signature"])
    io.remaining()  # a proof verified after the deadline is not admitted
    if check is not ProofCheck.PROOF_OK:
        raise AdmissionRefused(_PROOF_REFUSALS[check])
    return Admitted(signer, hello["machine"], version, io.splitter)


class AdmissionRefusals:
    """The unattributed admission refusals: one entry per `link_refused`
    code, `{code, count, last, versions}`, `versions` only for `version`
    (the refused relay's list as validated, at most MAX_VERSIONS)."""

    def __init__(self, now: Callable[[], float]):
        self._now = now
        self._lock = threading.Lock()
        self._entries: dict[RefusedCode, dict] = {}

    def add(self, code: RefusedCode, relay_versions: list[int] | None = None) -> None:
        with self._lock:
            entry = self._entries.setdefault(code, {"code": code.value, "count": 0, "last": None, "versions": None})
            entry["count"] += 1
            entry["last"] = self._now()
            if code is RefusedCode.VERSION and relay_versions is not None:
                entry["versions"] = list(relay_versions)[:MAX_VERSIONS]

    def view(self) -> list[dict]:
        with self._lock:
            return [dict(self._entries[code]) for code in sorted(self._entries)]
