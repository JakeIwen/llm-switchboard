"""Mac desk-channel identity and presence. Only the uid check separates users;
process and registry checks keep honest launches apart, not hostile same-uid code.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cache
import os
from pathlib import Path
import stat

from ..session_id import is_session_id
from .config import DeskConfig
from .mac_process import LibprocReader, MacProcess
from .model import ChannelProcess, MAX_PID, valid_channel_identity
from .session_identity import installed_claude_path, read_session_file


@dataclass(frozen=True)
class MacIdentity:
    channel_pid: int
    channel_start: str
    claude_pid: int
    claude_start: str
    session_id: str
    channel_unique: int
    claude_unique: int


class PeerRefused(Exception):
    """Fixed local diagnostic; never includes argv, a registry value or path."""


@cache
def _native_reader():
    return LibprocReader()


def socket_peer_pid(sock, *, reader=None) -> int:
    """Check socket credentials before allocating a hello reader."""
    try:
        pid, uid = (reader or _native_reader()).credentials(sock)
    except OSError:
        raise PeerRefused("peer credentials unavailable") from None
    if uid != os.getuid() or not 0 < pid <= MAX_PID:
        raise PeerRefused("peer credentials refused")
    return pid


def runs_pinned(process: MacProcess, argv: tuple[str, ...]) -> bool:
    target = Path(os.path.realpath(argv[0]))
    paths = {str(target)}
    version = target.parent.parent
    if (target.parent.name == "bin" and target.name == "python" + version.name
            and version.parent.name == "Versions" and version.parent.parent.name == "Python.framework"):
        paths.add(str(version / "Resources/Python.app/Contents/MacOS/Python"))
    return (bool(process.argv) and process.argv[1:] == argv[1:] and
            process.argv[0] == process.exec_path == process.exe and process.exe in paths)


def _session_matches(parent: MacProcess, session_id: str, config: DeskConfig) -> bool:
    stamp = parent.stamp
    entry = read_session_file(config.claude_sessions_dir, stamp.pid, min_mtime_ns=stamp.micros * 1000)
    if (not entry or type(entry.get("pid")) is not int or entry["pid"] != stamp.pid
            or entry.get("sessionId") != session_id or not isinstance(entry.get("procStart"), str)):
        return False
    try:
        value = " ".join(entry["procStart"].split())
        start = datetime.strptime(value, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
        return start.timestamp() == stamp.sec
    except (ValueError, OSError, OverflowError):
        return False


def verify_peer(pid: int, hello_session_id: str, config: DeskConfig, *, reader=None) -> MacIdentity:
    """Verify the pid returned by socket_peer_pid, never an environment or hello pid."""
    if (type(pid) is not int or not 0 < pid <= MAX_PID or not isinstance(hello_session_id, str)
            or not is_session_id(hello_session_id)):
        raise PeerRefused("invalid channel identity")
    try:
        kernel = reader or _native_reader()
        peer = kernel.process(pid)
        if (peer.stamp.pid != pid or peer.stamp.uid != os.getuid() or not runs_pinned(peer, config.channel_argv)
                or peer.stamp.ppid <= 1):
            raise PeerRefused("not the pinned channel")
        parent = kernel.process(peer.stamp.ppid)
        if (parent.stamp.pid != peer.stamp.ppid or parent.stamp.uid != os.getuid()
                or not parent.unique or parent.unique != peer.parent_unique):
            raise PeerRefused("channel parent changed")
        if (not installed_claude_path(parent.exe, config.claude_versions_dir.resolve())
                or not stat.S_ISREG(os.lstat(parent.exe).st_mode)):
            raise PeerRefused("not an installed claude")
        if parent.argv[1:1 + len(config.launch_flags)] != config.launch_flags:
            raise PeerRefused("desk flags not first")
        if not _session_matches(parent, hello_session_id, config):
            raise PeerRefused("session file does not match")
    except OSError:
        raise PeerRefused("process or session unavailable") from None
    return MacIdentity(pid, str(peer.stamp.micros), parent.stamp.pid, str(parent.stamp.micros),
                       hello_session_id, peer.unique, parent.unique)


def recheck(identity: MacIdentity, config: DeskConfig, *, reader=None) -> str | None:
    """None only while the pinned launch, both kernel identities and the registry still agree."""
    try:
        current = verify_peer(identity.channel_pid, identity.session_id, config, reader=reader)
    except PeerRefused as exc:
        return str(exc)
    return None if current == identity else "process identity changed"


def process_presence(pid: int, start: str, *, reader=None) -> ChannelProcess:
    """Only ESRCH or a different start proves a recorded process gone; no argv read."""
    if not valid_channel_identity(pid, start):
        return ChannelProcess.UNVERIFIABLE
    try:
        stamp = (reader or _native_reader()).stamp(pid)
    except ProcessLookupError:
        return ChannelProcess.GONE
    except OSError:
        return ChannelProcess.UNVERIFIABLE
    return ChannelProcess.RUNNING if str(stamp.micros) == start else ChannelProcess.GONE
