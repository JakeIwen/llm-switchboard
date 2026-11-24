"""Install one restricted public key, preserving every unrelated authorized_keys line."""

from pathlib import Path
import shlex

from . import ssh_authorized_keys
from .host_protocol import host_name
from .ssh_public_key import KEY_TYPES, blob_valid


def public_key(raw: bytes) -> tuple[str, str]:
    if len(raw) > 16384:
        raise ValueError("public key too long")
    line = raw.decode("ascii").removesuffix("\n")
    if "\n" in line or "\r" in line or any(ord(c) < 32 for c in line):
        raise ValueError("supply exactly one public-key line, without options")
    parts = line.split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise ValueError("unsupported public key or key options supplied")
    if not blob_valid(parts[0], parts[1]):
        raise ValueError("invalid SSH public key blob")
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
    path = ssh_authorized_keys.authorized_keys_path(home)
    with ssh_authorized_keys.locked(path):
        ssh_authorized_keys.replace_marked_line(path, marker, new)
    return path
