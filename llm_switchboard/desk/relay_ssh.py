"""Prepare the Mac desk link's isolated OpenSSH invocation, without connecting.

The configured `ssh -G` may run the operator's Match exec route probe. Only
validated route and host-key values survive; authentication and multiplexing
come entirely from this module. No ssh output is a diagnostic or log message.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re

from ..paths import LOCAL_ENV, MAC_JOB_PATH
from ..local_config import MAC_NAME, PI_SSH, load
from .child_process import run_bounded
from .link_proof import KEY_TYPE
from .link_protocol import valid_link_host

SSH = "/usr/bin/ssh"
MAX_SSH_OUTPUT_BYTES = 64 * 1024
_CONFIG_SECONDS = 5
_CONFIG_CODE = "ssh_config_unsupported"
_ALIASED_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_HOST_ALGORITHMS = re.compile(r"[A-Za-z0-9@._+,\-]+")
_FILES = {"userknownhostsfile": "UserKnownHostsFile", "globalknownhostsfile": "GlobalKnownHostsFile"}
_UNSUPPORTED = {"proxycommand", "proxyjump", "knownhostscommand"}
_ROUTING = {"hostname": "HostName", "port": "Port", "user": "User"}
_TRACKED = _FILES.keys() | _UNSUPPORTED | _ROUTING.keys() | {"hostkeyalias", "hostkeyalgorithms"}
_FIXED = ("IdentitiesOnly=yes", "IdentityAgent=none", "PubkeyAuthentication=yes",
          f"PubkeyAcceptedAlgorithms={KEY_TYPE}", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
          "HostbasedAuthentication=no", "GSSAPIAuthentication=no", "BatchMode=yes", "StrictHostKeyChecking=yes",
          "UpdateHostKeys=no", "ConnectTimeout=15", "ServerAliveInterval=15", "ServerAliveCountMax=3",
          "ControlMaster=no", "ControlPath=none", "ClearAllForwardings=yes", "ForwardAgent=no", "RequestTTY=no")


class ConfigUnsupported(ValueError):
    def __init__(self):
        super().__init__(_CONFIG_CODE)


class SshStderr(StrEnum):
    PERMISSION_DENIED = "permission_denied"
    HOST_KEY = "host_key"
    CONNECTION_REFUSED = "connection_refused"
    TIMEOUT = "timeout"
    NAME_RESOLUTION = "name_resolution"
    OTHER = "other"


_STDERR_MARKERS = {
    SshStderr.HOST_KEY: (b"host key verification failed", b"remote host identification has changed",
                         b"host key is known", b"no matching host key type"),
    SshStderr.PERMISSION_DENIED: (b"permission denied", b"authentication failed"),
    SshStderr.CONNECTION_REFUSED: (b"connection refused",),
    SshStderr.TIMEOUT: (b"timed out", b"operation timeout", b"timeout, server"),
    SshStderr.NAME_RESOLUTION: (b"could not resolve hostname", b"name or service not known",
                                b"temporary failure in name resolution"),
}


def classify_stderr(data: bytes) -> SshStderr:
    data = data[:MAX_SSH_OUTPUT_BYTES].lower()
    return next((code for code, markers in _STDERR_MARKERS.items() if any(m in data for m in markers)),
                SshStderr.OTHER)


def _parse(data: bytes) -> dict[str, tuple[str, ...]]:
    values = {}
    for line in data.split(b"\n"):
        raw_key, sep, raw_value = line.partition(b" ")
        key = raw_key.decode("ascii", "replace").lower()
        if key not in _TRACKED:
            continue
        if not sep or key in values:
            raise ConfigUnsupported()
        try:
            value = raw_value.decode("utf-8")
        except UnicodeDecodeError:
            raise ConfigUnsupported() from None
        if any(not ch.isprintable() or (ch.isspace() and ch != " ") for ch in value):
            raise ConfigUnsupported()
        tokens = tuple(value.split())
        if not tokens or any(ch in value for ch in "\"'\\#"):
            raise ConfigUnsupported()
        values[key] = tokens
    return values


def _single(values, key):
    parts = values.get(key, ())
    if len(parts) != 1:
        raise ConfigUnsupported()
    return parts[0]


def _route_options(config, defaults):
    if any(key in config and config[key] != ("none",) for key in _UNSUPPORTED):
        raise ConfigUnsupported()
    options = []
    for key, name in _ROUTING.items():
        word = _single(config, key)
        if key == "port" and not (re.fullmatch(r"[0-9]{1,5}", word) and 0 < int(word) <= 65535):
            raise ConfigUnsupported()
        options.append(f"{name}={word}")
    if "hostkeyalias" in config:
        alias = _single(config, "hostkeyalias")
        if not _ALIASED_HOST.fullmatch(alias):
            raise ConfigUnsupported()
        options.append(f"HostKeyAlias={alias}")
    for key, name in _FILES.items():
        paths = config.get(key, ())
        if not 1 <= len(paths) <= 4 or not all(Path(p).is_absolute() for p in paths):
            raise ConfigUnsupported()
        if paths != defaults.get(key):
            options.append(f"{name}={' '.join(paths)}")
    algos = _single(config, "hostkeyalgorithms")
    if not _HOST_ALGORITHMS.fullmatch(algos):
        raise ConfigUnsupported()
    if config["hostkeyalgorithms"] != defaults.get("hostkeyalgorithms"):
        options.append(f"HostKeyAlgorithms={algos}")
    return options


@dataclass(frozen=True)
class SshInvocation:
    argv: tuple[str, ...]
    home: Path
    key: Path
    host: str

    @property
    def env(self) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": MAC_JOB_PATH}


def _read_config(argv, env, run):
    try:
        result = run(argv, env=env, deadline_seconds=_CONFIG_SECONDS,
                     stdout_limit=MAX_SSH_OUTPUT_BYTES, stderr_limit=MAX_SSH_OUTPUT_BYTES)
    except OSError:
        raise ConfigUnsupported() from None
    if result.returncode != 0 or result.timed_out or result.stdout_overflow or result.stderr_overflow:
        raise ConfigUnsupported()
    return _parse(result.stdout)


def prepare_ssh(config, *, environ=None, ssh_config: Path | None = None, run=run_bounded) -> SshInvocation:
    """Fresh route on each attempt. `ssh_config` substitutes an isolated config for tests;
    the *connection* always uses `-F none`, never the file passed here.
    """
    local = config.repo / LOCAL_ENV.name
    values = load(local) if environ is None else load(local, environ=environ)
    alias, host = values[PI_SSH], values[MAC_NAME]
    if not _ALIASED_HOST.fullmatch(alias) or not valid_link_host(host):
        raise ConfigUnsupported()
    env = {"HOME": str(config.home), "PATH": MAC_JOB_PATH}
    configured = () if ssh_config is None else ("-F", str(ssh_config))
    route = _read_config((SSH, "-G", *configured, alias), env, run)
    defaults = _read_config((SSH, "-G", "-F", "none", alias), env, run)
    _route_options(defaults, defaults)  # An incomplete baseline is not evidence of a configured override.
    key = config.home / ".ssh" / f"llm-switchboard-desk-{host}"
    if not key.is_absolute() or any(not c.isprintable() or c in '\"\\%$' for c in str(key)):
        raise ConfigUnsupported()
    # `-i` stats the key and ignores a missing one, restoring all default identities.
    # IdentityFile stays set even if the file disappears just before ssh starts.
    options = [f'IdentityFile="{key}"', *_FIXED, *_route_options(route, defaults)]
    argv = (SSH, "-F", "none", "-T", *(word for option in options for word in ("-o", option)), alias, "desk-link")
    return SshInvocation(argv, config.home, key, host)
