"""The relay's half of the Mac link for tests, written from link_protocol
alone, so the hub's tests (and the Mac stage's) drive a real hub over its
`link.sock` (or through the real pump) without a relay.

`LinkPeer` speaks whole frames: `handshake` runs hello, challenge, proof
and welcome; `read` decodes the hub's frames (answering its pings unless
told not to). `KeyPair` makes a throwaway Ed25519 key with the real
`ssh-keygen` in a temporary directory and signs proofs with it;
`fake_signature` and `fake_verify` stand in for ssh-keygen where a test is
about something other than the proof itself.
"""

import base64
import hashlib
from pathlib import Path
import socket
import subprocess

from llm_switchboard.desk.link_proof import ProofCheck, Signer, format_signers, proof_message, sign
from llm_switchboard.desk.link_protocol import (
    LINK_VERSIONS, LineSplitter, LinkType, Side, decode_frame, encode_frame,
)

MAC = "mac"
MAC2 = "mac2"
MACHINE = "ab" * 16
OTHER_MACHINE = "cd" * 16


class KeyPair:
    """A throwaway Ed25519 link key in `directory`."""

    def __init__(self, directory: Path, host: str = MAC, name: str | None = None):
        self.path = Path(directory) / (name or f"link-{host}")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(self.path)],
                       check=True, capture_output=True, timeout=30)
        blob = self.path.with_suffix(".pub").read_text().split()[1]
        self.signer = Signer(host, blob)

    def sign(self, message: bytes) -> str:
        return sign(self.path, message)


def write_signers(path: Path, *signers: Signer) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(format_signers(signers))
    path.chmod(0o600)


def fake_signature(signer: Signer) -> str:
    """A well-formed armor that only `fake_verify` accepts, for `signer`."""
    digest = base64.b64encode(hashlib.sha256(f"{signer.host} {signer.blob}".encode()).digest()).decode()
    return f"-----BEGIN SSH SIGNATURE-----\n{digest}\n-----END SSH SIGNATURE-----\n"


def fake_verify(signer: Signer, *, version, machine, epoch, nonce, signature) -> ProofCheck:
    return ProofCheck.PROOF_OK if signature == fake_signature(signer) else ProofCheck.PROOF_BAD


class LinkPeer:
    def __init__(self, path: Path | None = None, *, sock: socket.socket | None = None, host: str = MAC,
                 machine: str = MACHINE, timeout: float = 10.0):
        if sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(path))
        self.sock = sock
        self.sock.settimeout(timeout)
        self.host = host
        self.machine = machine
        self.version: int | None = None
        self.epoch: str | None = None
        self.link_id: str | None = None
        self._splitter = LineSplitter()
        self._lines: list[bytes] = []
        self._pings = 0

    # -- raw frames ------------------------------------------------------------

    def send(self, kind: LinkType, **fields) -> None:
        self.send_raw(encode_frame(kind, Side.RELAY, v=None if kind in (LinkType.LINK_HELLO,) else self.version,
                                   **fields))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_line(self) -> bytes:
        """One whole line from the hub; b"" at EOF."""
        while not self._lines:
            data = self.sock.recv(65536)
            if not data:
                return b""
            self._lines.extend(self._splitter.feed(data))
        return self._lines.pop(0)

    def read(self, *, answer_pings: bool = True) -> tuple[LinkType | None, dict]:
        """The hub's next frame, (None, {}) at EOF."""
        while True:
            line = self.read_line()
            if not line:
                return None, {}
            kind, fields = decode_frame(line, Side.HUB, v=self.version)
            if kind is LinkType.PING and answer_pings:
                self.send(LinkType.PONG, n=fields["n"])
                continue
            return kind, fields

    def expect(self, kind: LinkType) -> dict:
        got, fields = self.read()
        if got is not kind:
            raise AssertionError(f"expected {kind.value}, got {got and got.value}: {fields}")
        return fields

    def closed(self) -> bool:
        """True when the hub has closed the link (anything still queued is skipped)."""
        while True:
            try:
                kind, _ = self.read()
            except OSError:
                return True
            if kind is None:
                return True

    # -- the handshake ---------------------------------------------------------------

    def hello(self, *, versions=None, relay_build: str = "") -> tuple[LinkType | None, dict]:
        self.send(LinkType.LINK_HELLO, versions=list(versions or LINK_VERSIONS), relay_build=relay_build,
                  host=self.host, machine=self.machine)
        return self.read()

    def handshake(self, signer, *, versions=None) -> dict:
        """hello -> challenge -> proof -> welcome. `signer` is a KeyPair, or a
        callable from the proof message to an armored signature."""
        kind, challenge = self.hello(versions=versions)
        if kind is not LinkType.LINK_CHALLENGE:
            raise AssertionError(f"no challenge: {kind and kind.value} {challenge}")
        self.version, self.epoch = challenge["version"], challenge["epoch"]
        message = proof_message(version=self.version, host=self.host, machine=self.machine,
                                epoch=self.epoch, nonce=challenge["nonce"])
        make = signer.sign if isinstance(signer, KeyPair) else signer
        self.send(LinkType.LINK_PROOF, signature=make(message))
        kind, fields = self.read()
        if kind is LinkType.LINK_WELCOME:
            self.link_id = fields["link_id"]
        return {"type": kind and kind.value, **fields}

    # -- channels --------------------------------------------------------------------

    def register(self, chan: int, session_id: str, *, channel_pid: int = 4000, channel_start: str = "100",
                 claude_pid: int = 3000, claude_start: str = "90", label: str = "", cwd: str = "") -> dict:
        self.send(LinkType.REGISTER, chan=chan, session_id=session_id, channel_pid=channel_pid,
                  channel_start=channel_start, claude_pid=claude_pid, claude_start=claude_start, label=label,
                  cwd=cwd)
        kind, fields = self.read()
        return {"type": kind and kind.value, **fields}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class LinkHubCase:
    """Mixed into desk_fixtures.HubCase: a hub with its link socket up, the
    test's own throwaway keys listed for `mac` and `mac2`, Mac enrollment
    from `self.remote` ({session id: host}), and a fake verifier unless
    `real_verifier` is set. `self.link_options` adds LinkHub options."""

    real_verifier = False
    verifier_ok = True
    _keys: dict = {}

    @classmethod
    def setUpClass(cls):
        import tempfile
        super().setUpClass()
        if not LinkHubCase._keys:
            import atexit
            import shutil
            directory = Path(tempfile.mkdtemp(prefix="desk-link-keys-"))
            atexit.register(shutil.rmtree, directory, True)
            LinkHubCase._keys = {name: KeyPair(directory, host, name) for name, host in
                                 (("mac", MAC), ("mac2", MAC2), ("mac-rotated", MAC), ("stranger", MAC))}

    def key(self, name: str) -> KeyPair:
        return LinkHubCase._keys[name]

    def setUp(self):
        self.remote: dict[str, str] = {}
        self.link_options: dict = getattr(self, "link_options", {})
        self._signers_written = False
        super().setUp()

    def start_hub(self, **options):
        if not self._signers_written:
            write_signers(self.config.link_signers_file, self.key("mac").signer, self.key("mac2").signer)
            self._signers_written = True
        link = {"verifier_self_test": lambda: self.verifier_ok}
        if not self.real_verifier:
            link["verify"] = fake_verify
        link.update(self.link_options)
        options.setdefault("remote_enrollment", lambda: dict(self.remote))
        return super().start_hub(link_options=link, **options)

    def link_peer(self, host: str = MAC, machine: str = MACHINE) -> LinkPeer:
        peer = LinkPeer(self.config.link_socket, host=host, machine=machine)
        self.addCleanup(peer.close)
        return peer

    def connect(self, key: str = "mac", host: str = MAC, machine: str = MACHINE) -> LinkPeer:
        """A published link for `host`, its proof made with `key`."""
        peer = self.link_peer(host, machine)
        pair = self.key(key)
        signer = pair if self.real_verifier else (lambda message: fake_signature(Signer(host, pair.signer.blob)))
        welcome = peer.handshake(signer)
        if welcome["type"] != LinkType.LINK_WELCOME.value:
            raise AssertionError(f"not admitted: {welcome}")
        return peer

    def links(self) -> dict:
        return {view["host"]: view for view in self.hub.state_view()["links"]}

    def refusals(self) -> dict:
        return {entry["code"]: entry for entry in self.hub.state_view()["admission_refusals"]}
