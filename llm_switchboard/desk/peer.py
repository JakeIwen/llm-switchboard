"""Who is on the other end of the channel socket?

The hub trusts a channel connection only when the connecting process is the
pinned channel server, started by a claude whose command line carries the
pinned MCP config, whose session registry entry names the session the hello
claimed, and which Jacob enrolled. Rules (`check_*`) work on parsed records;
the adapter (`read_process`, `read_session_file`) reads /proc and the session
registry through `DeskConfig`, so tests can point both at a fake tree.

Linux only in phase 1: the peer pid comes from SO_PEERCRED.
Standard library only.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import struct
import sys
from typing import Callable

from .config import DeskConfig
from .model import CHANNEL_START_PATTERN, ChannelProcess


@dataclass(frozen=True)
class PeerIdentity:
    channel_pid: int
    channel_start: str
    claude_pid: int
    claude_start: str
    session_id: str


class PeerRefused(Exception):
    """args[0] is a short reason, with no secrets (it may go to the hub log)."""


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    ppid: int
    # /proc/<pid>/stat field 22, kept as the string the session registry stores.
    start: str
    argv: tuple[str, ...]
    # /proc/<pid>/exe resolved, or None when unreadable.
    exe: str | None
    # stat fields 5 and 8: the process group, and the foreground process group
    # of its controlling terminal (-1 without one). The startup ack reads them.
    pgrp: int = -1
    tpgid: int = -1


# --- adapter: /proc and the session registry ---------------------------------


def socket_peer_pid(sock) -> int:
    if not sys.platform.startswith("linux"):
        raise PeerRefused("peer checks need Linux in phase 1")
    try:
        pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    except OSError as exc:
        raise PeerRefused("peer credentials unavailable") from exc
    if uid != os.getuid():
        raise PeerRefused("peer is another user")
    if pid <= 0:
        raise PeerRefused("peer pid unavailable")
    return pid


def read_process(proc_dir: Path, pid: int) -> ProcessRecord | None:
    """None when the process is gone or its files can't be read."""
    proc = Path(proc_dir) / str(pid)
    try:
        stat = (proc / "stat").read_text()
        raw_argv = (proc / "cmdline").read_bytes()
        # The command name in stat may hold spaces and ")", so split after the last one.
        fields = stat.rsplit(")", 1)[1].split()
        ppid, pgrp, tpgid, start = int(fields[1]), int(fields[2]), int(fields[5]), fields[19]
    except (OSError, ValueError, IndexError):
        return None
    try:
        exe = os.path.realpath(os.readlink(proc / "exe"))
    except OSError:
        exe = None
    body = raw_argv[:-1] if raw_argv.endswith(b"\0") else raw_argv
    argv = tuple(part.decode(errors="surrogateescape") for part in body.split(b"\0")) if body else ()
    return ProcessRecord(pid=pid, ppid=ppid, start=start, argv=argv, exe=exe, pgrp=pgrp, tpgid=tpgid)


def process_presence(proc_dir: Path, pid: int, start: str) -> ChannelProcess:
    """Whether a process recorded by pid and start time (a lock's channel, a
    startup acknowledgement's claude) may still run, from its start time
    alone (/proc/<pid>/stat field 22, parsed as `read_process` does). Only a missing entry in a mounted process
    tree (`<proc>/self` present) or another start time (a reused pid) is
    evidence it is gone; anything unreadable is not."""
    root = Path(proc_dir)
    try:
        stat = (root / str(pid) / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        # A missing entry means a missing process only in a mounted process
        # tree; with no tree at all (or no /proc mounted) nothing is known.
        return ChannelProcess.GONE if (root / "self").is_dir() else ChannelProcess.UNVERIFIABLE
    except (OSError, ValueError):
        return ChannelProcess.UNVERIFIABLE
    fields = stat.rsplit(")", 1)[1].split() if ")" in stat else []
    if len(fields) < 20 or not CHANNEL_START_PATTERN.fullmatch(fields[19]):
        return ChannelProcess.UNVERIFIABLE
    return ChannelProcess.RUNNING if fields[19] == start else ChannelProcess.GONE


def read_session_file(sessions_dir: Path, pid: int) -> dict | None:
    try:
        entry = json.loads((Path(sessions_dir) / f"{pid}.json").read_text())
    except (OSError, ValueError, RecursionError):
        return None
    return entry if isinstance(entry, dict) else None


# --- rules over parsed records -----------------------------------------------


def check_channel_process(peer: ProcessRecord, config: DeskConfig) -> None:
    if peer.argv != tuple(config.channel_argv):
        raise PeerRefused("peer is not the desk channel server")
    expected_exe = os.path.realpath(config.channel_argv[0])
    if peer.exe is None or peer.exe != expected_exe:
        raise PeerRefused("peer executable is not the channel interpreter")


def has_adjacent_flag(argv: tuple[str, ...], flag: str, value: str) -> bool:
    """The flag and its value as two adjacent arguments, as the launch flags give them."""
    return any(left == flag and right == value for left, right in zip(argv, argv[1:]))


def has_mcp_config_flag(argv: tuple[str, ...], path: Path) -> bool:
    return has_adjacent_flag(argv, "--mcp-config", str(path))


def has_channel_enablement(argv: tuple[str, ...], config: DeskConfig) -> bool:
    """`--dangerously-load-development-channels server:desk` from the launch flags; without it
    the session would not receive channel events, so registering it would be misleading."""
    flags = config.launch_flags
    return has_adjacent_flag(argv, flags[2], flags[3])


def check_claude_parent(parent: ProcessRecord, entry: dict | None, hello_session_id: str, config: DeskConfig) -> None:
    if entry is None:
        raise PeerRefused("no session file for the channel's parent")
    if entry.get("pid") != parent.pid:
        raise PeerRefused("session file names another process")
    if str(entry.get("procStart")) != parent.start:
        raise PeerRefused("session file is from an earlier process")
    if not hello_session_id or entry.get("sessionId") != hello_session_id:
        raise PeerRefused("session id does not match the parent's session")
    if not has_mcp_config_flag(parent.argv, config.mcp_config):
        raise PeerRefused("parent was not launched with the pinned MCP config")
    if not has_channel_enablement(parent.argv, config):
        raise PeerRefused("parent was not launched with the desk channel enabled")


# --- public entry points ------------------------------------------------------


def verify_peer(pid: int, hello_session_id: str, config: DeskConfig,
                is_enrolled: Callable[[str], bool]) -> PeerIdentity:
    peer = read_process(config.proc_dir, pid)
    if peer is None:
        raise PeerRefused("peer process is gone")
    check_channel_process(peer, config)
    if peer.ppid <= 1:
        raise PeerRefused("peer has no claude parent")
    parent = read_process(config.proc_dir, peer.ppid)
    if parent is None:
        raise PeerRefused("peer's parent is gone")
    check_claude_parent(parent, read_session_file(config.claude_sessions_dir, parent.pid), hello_session_id, config)
    if not is_enrolled(hello_session_id):
        raise PeerRefused("session is not enrolled")
    return PeerIdentity(channel_pid=peer.pid, channel_start=peer.start, claude_pid=parent.pid,
                        claude_start=parent.start, session_id=hello_session_id)


def recheck(identity: PeerIdentity, config: DeskConfig, is_enrolled: Callable[[str], bool]) -> str | None:
    """None while the identity still holds, else the reason it no longer does."""
    peer = read_process(config.proc_dir, identity.channel_pid)
    if peer is None or peer.start != identity.channel_start:
        return "channel process is gone"
    if peer.ppid != identity.claude_pid:
        return "channel process was reparented"
    parent = read_process(config.proc_dir, identity.claude_pid)
    if parent is None or parent.start != identity.claude_start:
        return "claude process is gone"
    entry = read_session_file(config.claude_sessions_dir, identity.claude_pid)
    if entry is None or entry.get("pid") != parent.pid or str(entry.get("procStart")) != parent.start:
        return "session file is gone or replaced"
    if entry.get("sessionId") != identity.session_id:
        return "session id changed"
    if not is_enrolled(identity.session_id):
        return "session is not enrolled"
    return None
