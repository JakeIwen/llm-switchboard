"""Install one restricted public key, preserving every unrelated authorized_keys line."""

import base64
import os
from pathlib import Path
import shlex
import struct
import tempfile

from .host_protocol import host_name
from .json_file import JsonFile

KEY_TYPES = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}


def public_key(raw: bytes) -> tuple[str, str]:
    if len(raw) > 16384:
        raise ValueError("public key too long")
    line = raw.decode("ascii").removesuffix("\n")
    if "\n" in line or "\r" in line or any(ord(c) < 32 for c in line):
        raise ValueError("supply exactly one public-key line, without options")
    parts = line.split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise ValueError("unsupported public key or key options supplied")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        n = struct.unpack(">I", blob[:4])[0]
        if blob[4:4 + n].decode("ascii") != parts[0] or len(blob) <= n + 8:
            raise ValueError("key type does not match its blob")
        if parts[0] == "ssh-ed25519" and (len(blob) != n + 40 or blob[n + 4:n + 8] != struct.pack(">I", 32)):
            raise ValueError("invalid Ed25519 key length")
    except (ValueError, UnicodeError, struct.error) as e:
        raise ValueError("invalid SSH public key blob") from e
    return parts[0], parts[1]


def authorize(host: str, raw: bytes, home: Path | None = None, executable: Path | None = None) -> Path:
    host = host_name(host)
    kind, blob = public_key(raw)
    home = Path.home() if home is None else Path(home)
    executable = executable or Path(__file__).resolve().parents[1] / "bin" / "llm-switchboard"
    command = f"{shlex.quote(str(executable.resolve()))} host-exchange {host}"
    command = command.replace("\\", "\\\\").replace('"', '\\"')
    marker = f"llm-switchboard-host:{host}".encode()
    new = f'restrict,command="{command}" {kind} {blob} '.encode() + marker + b"\n"
    ssh = home / ".ssh"
    ssh.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ssh / "authorized_keys"
    if path.is_symlink():
        raise ValueError("authorized_keys must not be a symlink")
    with JsonFile(path).locked():
        original = path.read_bytes() if path.exists() else b""
        lines = original.splitlines(keepends=True)
        # Only our final comment is a marker; a substring in someone else's line isn't.
        kept = [line for line in lines if not line.rstrip(b"\r\n").endswith(b" " + marker)]
        content = b"".join(kept)
        if content and not content.endswith(b"\n"):
            content += b"\n"
        fd, tmp = tempfile.mkstemp(dir=ssh, prefix=".authorized_keys-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content + new)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    return path
