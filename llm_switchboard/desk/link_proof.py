"""Proof of possession for the desk link (docs/voice-desk.md, "Admission
and proof of possession"): the message the relay signs, the namespace,
the bounded `ssh-keygen -Y sign` (Mac) and `-Y verify` (Pi) calls, the
self-test vector, and the one reader of `link-signers`.

The signed message binds the protocol version, the host, the machine, the
hub's epoch and a fresh nonce, so a proof can't be replayed into another
admission, hub run or version. `verify` hands ssh-keygen only what this
module validated: the one host's signer line and the signature, each
through its own pipe (`/dev/fd/<n>`), the message on stdin, nothing on
disk, environment `PATH=/usr/bin:/bin` only. `parse_signers` is the rule
every reader and writer of `link-signers` goes through: at most
`MAX_SIGNER_LINES` lines and `MAX_SIGNERS_FILE_BYTES`, each exactly
`<host> namespaces="llm-switchboard-desk-link" ssh-ed25519 <blob>`, hosts
and blobs unique. Standard library only.
"""

import base64
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from pathlib import Path
import re

from ..ssh_public_key import blob_valid
from .bounded_read import read_bounded
from .child_process import data_pipes, run_bounded
from .link_protocol import (
    valid_epoch, valid_link_host, valid_machine, valid_nonce, valid_signature_armor, valid_version,
)

NAMESPACE = "llm-switchboard-desk-link"
MESSAGE_TITLE = "llm-switchboard-desk-link-proof"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
# No agent socket and no configuration from the environment reaches it.
CHILD_ENV = {"PATH": "/usr/bin:/bin"}
SIGN_DEADLINE_SECONDS = 5
VERIFY_DEADLINE_SECONDS = 5
MAX_OUTPUT_BYTES = 4 * 1024
MAX_SIGNER_LINES = 4
MAX_SIGNERS_FILE_BYTES = 4 * 1024
KEY_TYPE = "ssh-ed25519"

_SIGNER_LINE = re.compile(rf'([a-z][a-z0-9-]{{0,31}}) namespaces="{NAMESPACE}" {KEY_TYPE} ([A-Za-z0-9+/]+={{0,2}})')


class ProofCheck(StrEnum):
    """A verification's result, also its log code."""
    PROOF_OK = "proof_ok"
    PROOF_BAD = "proof_bad"
    VERIFIER_TIMEOUT = "verifier_timeout"
    VERIFIER_UNAVAILABLE = "verifier_unavailable"


class SigningFailure(StrEnum):
    SIGN_TIMEOUT = "sign_timeout"
    SIGN_FAILED = "sign_failed"
    SIGNER_UNAVAILABLE = "signer_unavailable"


class SigningFailed(Exception):
    """args[0] is a `SigningFailure`; never carries ssh-keygen's output."""


class SignersUnreadable(Exception):
    """`link-signers` can't be read or breaks the format; every link is refused."""


def fingerprint(blob: str) -> str:
    """OpenSSH's SHA-256 fingerprint of a key blob: `SHA256:<base64, unpadded>`."""
    digest = hashlib.sha256(base64.b64decode(blob, validate=True)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


@dataclass(frozen=True)
class Signer:
    host: str
    blob: str

    @property
    def line(self) -> str:
        """The allowed-signers line, without its newline."""
        return f'{self.host} namespaces="{NAMESPACE}" {KEY_TYPE} {self.blob}'

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.blob)


def parse_signers(data: bytes) -> tuple[Signer, ...]:
    """The signers in file order; SignersUnreadable for anything else than
    the format above (an empty file is valid and lists none)."""
    if len(data) > MAX_SIGNERS_FILE_BYTES:
        raise SignersUnreadable("too large")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SignersUnreadable("not ASCII") from exc
    if not text:
        return ()
    if not text.endswith("\n"):
        raise SignersUnreadable("no final newline")
    lines = text[:-1].split("\n")
    if len(lines) > MAX_SIGNER_LINES:
        raise SignersUnreadable("too many lines")
    signers: list[Signer] = []
    for line in lines:
        match = _SIGNER_LINE.fullmatch(line)
        if match is None or not valid_link_host(match[1]) or not blob_valid(KEY_TYPE, match[2]):
            raise SignersUnreadable("malformed line")
        signers.append(Signer(match[1], match[2]))
    if len({s.host for s in signers}) != len(signers) or len({s.blob for s in signers}) != len(signers):
        raise SignersUnreadable("duplicate host or key")
    return tuple(signers)


def read_signers(path: Path) -> tuple[Signer, ...]:
    """`link-signers` through `read_bounded` (a missing file lists none)."""
    try:
        data = read_bounded(Path(path), MAX_SIGNERS_FILE_BYTES)
    except OSError as exc:
        raise SignersUnreadable("unreadable") from exc
    return () if data is None else parse_signers(data)


def format_signers(signers) -> bytes:
    """The whole file for `signers`, checked by `parse_signers` before it is
    returned, so no writer can produce a file the hub's reader refuses."""
    data = "".join(signer.line + "\n" for signer in signers).encode("ascii")
    if parse_signers(data) != tuple(signers):
        raise SignersUnreadable("the proposed file does not read back as written")
    return data


def proof_message(*, version: int, host: str, machine: str, epoch: str, nonce: str) -> bytes:
    """The exact bytes the relay signs and the hub verifies."""
    if not (valid_version(version) and valid_link_host(host) and valid_machine(machine)
            and valid_epoch(epoch) and valid_nonce(nonce)):
        raise ValueError("invalid proof field")
    return (f"{MESSAGE_TITLE}\nversion={version}\nhost={host}\nmachine={machine}\n"
            f"epoch={epoch}\nnonce={nonce}\n").encode("ascii")


def sign(key_path: Path, message: bytes, *, ssh_keygen: str = SSH_KEYGEN,
         deadline_seconds: float = SIGN_DEADLINE_SECONDS) -> str:
    """The armored signature of `message` by the private key at `key_path`."""
    argv = [ssh_keygen, "-Y", "sign", "-f", str(key_path), "-n", NAMESPACE]
    try:
        result = run_bounded(argv, env=CHILD_ENV, input=message, deadline_seconds=deadline_seconds,
                             stdout_limit=MAX_OUTPUT_BYTES, stderr_limit=MAX_OUTPUT_BYTES)
    except OSError as exc:
        raise SigningFailed(SigningFailure.SIGNER_UNAVAILABLE) from exc
    if result.timed_out:
        raise SigningFailed(SigningFailure.SIGN_TIMEOUT)
    try:
        signature = result.stdout.decode("ascii")
    except UnicodeDecodeError:
        signature = ""
    if result.returncode != 0 or result.stdout_overflow or not valid_signature_armor(signature):
        raise SigningFailed(SigningFailure.SIGN_FAILED)
    return signature


def verify(signer: Signer, message: bytes, signature: str, *, ssh_keygen: str = SSH_KEYGEN,
           deadline_seconds: float = VERIFY_DEADLINE_SECONDS) -> ProofCheck:
    """Whether `signature` is `signer`'s over `message` in NAMESPACE. Its
    output is read to MAX_OUTPUT_BYTES per stream and never returned."""
    if not valid_signature_armor(signature):
        return ProofCheck.PROOF_BAD
    with data_pipes((signer.line + "\n").encode("ascii"), signature.encode("ascii")) as (signers_fd, sig_fd):
        argv = [ssh_keygen, "-Y", "verify", "-f", f"/dev/fd/{signers_fd}", "-I", signer.host,
                "-n", NAMESPACE, "-s", f"/dev/fd/{sig_fd}"]
        try:
            result = run_bounded(argv, env=CHILD_ENV, input=message, deadline_seconds=deadline_seconds,
                                 stdout_limit=MAX_OUTPUT_BYTES, stderr_limit=MAX_OUTPUT_BYTES,
                                 pass_fds=(signers_fd, sig_fd))
        except OSError:
            return ProofCheck.VERIFIER_UNAVAILABLE
    if result.timed_out:
        return ProofCheck.VERIFIER_TIMEOUT
    good = f'Good "{NAMESPACE}" signature for {signer.host} with '.encode("ascii")
    if result.returncode == 0 and not result.stdout_overflow and result.stdout.startswith(good):
        return ProofCheck.PROOF_OK
    return ProofCheck.PROOF_BAD


def verify_proof(signer: Signer, *, version: int, machine: str, epoch: str, nonce: str,
                 signature: str) -> ProofCheck:
    """A relay's proof for an admission, checked against the signer line the
    hello's host named. The message is built with that line's host, because
    ssh-keygen binds only the key and namespace: the same key listed under
    two names would verify for either, and the host the proof was made for
    is decided here, by the bytes signed. PROOF_BAD for fields of the wrong
    shape (they can't have been signed by an honest relay)."""
    try:
        message = proof_message(version=version, host=signer.host, machine=machine, epoch=epoch, nonce=nonce)
    except ValueError:
        return ProofCheck.PROOF_BAD
    return verify(signer, message, signature)


# The self-test vector: a throwaway key's signature over a fixed proof
# message (its private key was discarded once this was made).
SELF_TEST_SIGNER = Signer("selftest", "AAAAC3NzaC1lZDI1NTE5AAAAIPGj6gmO/KdgRJ2xs+lBBdSU+lDkaAb2h4yLMvl5SQiv")
SELF_TEST_MESSAGE = proof_message(version=1, host="selftest", machine="0" * 32, epoch="0" * 32, nonce="0" * 64)
SELF_TEST_SIGNATURE = (
    "-----BEGIN SSH SIGNATURE-----\n"
    "U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg8aPqCY78p2BEnbGz6UEF1JT6UO\n"
    "RoBvaHjIsy+XlJCK8AAAAZbGxtLXN3aXRjaGJvYXJkLWRlc2stbGluawAAAAAAAAAGc2hh\n"
    "NTEyAAAAUwAAAAtzc2gtZWQyNTUxOQAAAEBh/0E9AGY7IJneneuGWMeNa14kJyLqviGkEZ\n"
    "H7KtLxkxUxz1fzMTL7+eawJE8U+8zsSiu+cW40IzzwFfHUuEAF\n"
    "-----END SSH SIGNATURE-----\n"
)


def self_test(*, ssh_keygen: str = SSH_KEYGEN, deadline_seconds: float = VERIFY_DEADLINE_SECONDS) -> bool:
    """True when this machine's verifier accepts the vector and refuses it
    with one byte of the message changed, through `verify` itself. False
    disables links (an old OpenSSH, no `/dev/fd` pipes, a broken binary)."""
    changed = SELF_TEST_MESSAGE.replace(b"nonce=0", b"nonce=1", 1)
    return (verify(SELF_TEST_SIGNER, SELF_TEST_MESSAGE, SELF_TEST_SIGNATURE, ssh_keygen=ssh_keygen,
                   deadline_seconds=deadline_seconds) is ProofCheck.PROOF_OK
            and verify(SELF_TEST_SIGNER, changed, SELF_TEST_SIGNATURE, ssh_keygen=ssh_keygen,
                       deadline_seconds=deadline_seconds) is ProofCheck.PROOF_BAD)
