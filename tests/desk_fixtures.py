"""Shared fixtures for the desk hub tests: a temporary HOME, a throwaway TLS
leaf, a fake peer verifier (no /proc needed), a manual clock and a channel
client speaking the real socket protocol."""

import atexit
from dataclasses import dataclass
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from llm_switchboard.desk.config import PORT_ENV, DeskConfig
from llm_switchboard.desk.protocol import MessageType, decode, encode, read_line

SESSION_A = "aaaaaaaa-0000-4000-8000-000000000001"
SESSION_B = "bbbbbbbb-0000-4000-8000-000000000002"
SESSION_C = "cccccccc-0000-4000-8000-000000000003"

_CERT_CONFIG = """[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = localhost
[v3]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost,IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
"""
_cert_dir: Path | None = None
_cert_lock = threading.Lock()


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def make_home(case: unittest.TestCase) -> Path:
    # Short path under /tmp: macOS caps Unix socket paths at 104 bytes.
    home = Path(tempfile.mkdtemp(prefix="desk", dir="/tmp"))
    case.addCleanup(shutil.rmtree, home, True)
    return home


def desk_config(home: Path) -> DeskConfig:
    return DeskConfig.from_env(home=home, environ={PORT_ENV: "0"})


def throwaway_cert() -> Path:
    """A self-signed leaf for localhost, made once per test run. Raises
    unittest.SkipTest when the openssl CLI is missing."""
    global _cert_dir
    with _cert_lock:
        if _cert_dir is None:
            if shutil.which("openssl") is None:
                raise unittest.SkipTest("openssl CLI not installed; HTTPS tests need a throwaway cert")
            directory = Path(tempfile.mkdtemp(prefix="desk-cert", dir="/tmp"))
            atexit.register(shutil.rmtree, directory, True)
            (directory / "openssl.cnf").write_text(_CERT_CONFIG)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-keyout", str(directory / "leaf.key"), "-out", str(directory / "leaf.pem"),
                            "-days", "2", "-config", str(directory / "openssl.cnf")],
                           check=True, capture_output=True, timeout=30)
            _cert_dir = directory
        return _cert_dir


def install_cert(config: DeskConfig) -> Path:
    """Copy the throwaway leaf into this HOME; returns the cert to trust."""
    source = throwaway_cert()
    config.tls_cert.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source / "leaf.pem", config.tls_cert)
    shutil.copyfile(source / "leaf.key", config.tls_key)
    os.chmod(config.tls_key, 0o600)
    return config.tls_cert


def client_context(cert: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(cert))


class Clock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class FakeIdentity:
    channel_pid: int
    channel_start: str
    claude_pid: int
    claude_start: str
    session_id: str


class FakePeer:
    """Stands in for desk.peer: the test decides who is refused and when a
    recheck fails."""

    class PeerRefused(Exception):
        pass

    def __init__(self):
        self.refuse: dict[str, str] = {}
        self.recheck_failures: dict[str, str] = {}
        self._pids = iter(range(5000, 6000))

    def socket_peer_pid(self, sock) -> int:
        return next(self._pids)

    def verify_peer(self, pid, hello_session_id, config, is_enrolled):
        if hello_session_id in self.refuse:
            raise self.PeerRefused(self.refuse[hello_session_id])
        if not is_enrolled(hello_session_id):
            raise self.PeerRefused("not enrolled")
        return FakeIdentity(pid, "1", pid + 1, "2", hello_session_id)

    def recheck(self, identity, config, is_enrolled):
        if identity.session_id in self.recheck_failures:
            return self.recheck_failures[identity.session_id]
        return None if is_enrolled(identity.session_id) else "not enrolled"


class ChannelClient:
    """A channel server's side of the hub socket."""

    def __init__(self, path: Path, session_id: str, timeout: float = 5.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.stream = self.sock.makefile("rb")
        self.send(MessageType.HELLO, session_id=session_id)
        self.first = self.read()

    def send(self, kind: MessageType, **fields) -> None:
        self.sock.sendall(encode(kind, **fields))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read(self) -> tuple[MessageType | None, dict]:
        """The next message other than a heartbeat ping; (None, {}) at EOF."""
        while True:
            line = read_line(self.stream)
            if not line:
                return None, {}
            kind, message = decode(line)
            if kind is not MessageType.PING:
                return kind, message

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.stream.close()
        with contextlib.suppress(OSError):
            self.sock.close()


def enroll_file(config: DeskConfig, *session_ids: str) -> None:
    config.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"sessions": {sid: {"since": 1} for sid in session_ids}}
    config.enrolled_file.write_text(json.dumps(payload))


class HubCase(unittest.TestCase):
    """A running hub over HTTPS and real sockets, with a fake peer verifier.
    The 5 s loop is parked; tests call `hub.recheck_now()` themselves."""

    sends_enabled = True
    long_poll_seconds = 5.0

    def setUp(self):
        from llm_switchboard.desk.enrollment import DeskSettings
        self.home = make_home(self)
        self.config = desk_config(self.home)
        self.cert = install_cert(self.config)
        enroll_file(self.config, SESSION_A, SESSION_B)
        DeskSettings(self.config.settings_file).set_sends_enabled(self.sends_enabled)
        self.peer = FakePeer()
        self.hub = self.start_hub()
        self.tls = client_context(self.cert)

    def start_hub(self, **options):
        from llm_switchboard.desk.hub import Hub
        hub = Hub(self.config, peer=self.peer, recheck_seconds=3600, long_poll_seconds=self.long_poll_seconds,
                  **options)
        hub.start()
        self.addCleanup(hub.close)
        return hub

    @property
    def port(self) -> int:
        return self.hub.http.port

    def call(self, method, path, body=None, *, token=None, headers=None, raw=None, timeout=30):
        import http.client
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.tls, timeout=timeout)
        sent = {"Host": f"localhost:{self.port}"}
        if method == "POST":
            sent.update({"Origin": f"https://localhost:{self.port}", "Content-Type": "application/json"})
        if token:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        sent = {k: v for k, v in sent.items() if v is not None}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            payload = response.read()
            headers_seen = {k.lower(): v for k, v in response.getheaders()}
        finally:
            conn.close()
        if headers_seen.get("content-type") == "application/json":
            payload = json.loads(payload)
        return response.status, headers_seen, payload

    def pair(self) -> str:
        import hashlib
        import secrets
        from llm_switchboard.desk.operator import OperatorOp, operator_request
        secret = secrets.token_hex(32)
        status, _, started = self.call("POST", "/desk/pair/start",
                                       {"secret_hash": hashlib.sha256(secret.encode()).hexdigest()})
        self.assertEqual(status, 200, started)
        confirmed = operator_request(self.config.operator_socket, OperatorOp.PAIR, code=started["code"])
        self.assertTrue(confirmed["ok"], confirmed)
        status, _, claimed = self.call("POST", "/desk/pair/claim", {"pair_id": started["pair_id"], "secret": secret})
        self.assertEqual(status, 200, claimed)
        return claimed["token"]

    def channel(self, session_id: str) -> ChannelClient:
        client = ChannelClient(self.config.channel_socket, session_id)
        self.addCleanup(client.close)
        return client

    def target(self, session_id: str) -> dict:
        return next(t for t in self.hub.state_view()["targets"] if t["session_id"] == session_id)
