"""Darwin libproc/sysctl adapter for a socket peer and its parent; no native calls on import."""

from dataclasses import dataclass
import errno
import os
import socket
import struct
import sys

from .peer_credentials import socket_peer_uid

# Darwin sys/proc_info.h and proc_info_private.h. Fixed-width fields, native byte order.
_BSD = struct.Struct("=12I48s6I2Q")
_UNIQUE = struct.Struct("=16s5Q")
_PROC_PIDTBSDINFO = 3
_PROC_PIDUNIQIDENTIFIERINFO = 17
_PROC_PIDPATHINFO_MAXSIZE = 4096
_CTL_KERN, _KERN_PROCARGS2 = 1, 49
_SOL_LOCAL, _LOCAL_PEERPID = 0, 2
_USEC_PER_SECOND = 1_000_000


@dataclass(frozen=True)
class ProcessStamp:
    pid: int
    ppid: int
    uid: int
    sec: int
    usec: int

    @property
    def micros(self) -> int:
        return self.sec * _USEC_PER_SECOND + self.usec


@dataclass(frozen=True)
class MacProcess:
    stamp: ProcessStamp
    unique: int
    parent_unique: int
    exe: str
    exec_path: str
    argv: tuple[str, ...]


def _exec_args(raw: bytes) -> tuple[str, tuple[str, ...]]:
    # argc, exec path, NUL padding, then exactly argc NUL-terminated strings.
    # The environment follows; don't interpret it, expose it or keep it in the record.
    count_size = struct.calcsize("i")
    if len(raw) <= count_size:
        raise OSError(errno.EIO, "short argument record")
    count, = struct.unpack_from("i", raw)
    end = raw.find(b"\0", count_size)
    if end <= count_size or not 0 < count <= len(raw) - end:
        raise OSError(errno.EIO, "invalid argument record")
    path = os.fsdecode(raw[count_size:end])
    pos = end
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    argv = []
    for _ in range(count):
        end = raw.find(b"\0", pos)
        if end < 0:
            raise OSError(errno.EIO, "truncated argument record")
        argv.append(os.fsdecode(raw[pos:end]))
        pos = end + 1
    return path, tuple(argv)


class LibprocReader:
    """Read only a named process. ESRCH is distinct from every other read failure."""

    def __init__(self):
        if sys.platform != "darwin":
            raise OSError(errno.ENOSYS, "Darwin process information unavailable")
        import ctypes as c
        self.c = c
        lib = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._pidinfo = lib.proc_pidinfo
        self._pidinfo.argtypes = [c.c_int, c.c_int, c.c_uint64, c.c_void_p, c.c_int]
        self._pidinfo.restype = c.c_int
        self._pidpath = lib.proc_pidpath
        self._pidpath.argtypes = [c.c_int, c.c_void_p, c.c_uint32]
        self._pidpath.restype = c.c_int
        self._sysctl = c.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).sysctl
        self._sysctl.argtypes = [c.POINTER(c.c_int), c.c_uint, c.c_void_p, c.POINTER(c.c_size_t),
                                 c.c_void_p, c.c_size_t]
        self._sysctl.restype = c.c_int
        self._argmax = os.sysconf("SC_ARG_MAX")
        if self._argmax <= 0:
            raise OSError(errno.EIO, "argument limit unavailable")

    def credentials(self, sock: socket.socket) -> tuple[int, int]:
        return sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID), socket_peer_uid(sock)

    def _info(self, pid, flavor, layout):
        buf = self.c.create_string_buffer(layout.size)
        self.c.set_errno(0)
        length = self._pidinfo(pid, flavor, 0, buf, len(buf))
        if length != layout.size:
            raise OSError((self.c.get_errno() if length <= 0 else 0) or errno.EIO, "process info unavailable")
        return layout.unpack(buf.raw)

    def stamp(self, pid: int) -> ProcessStamp:
        fields = self._info(pid, _PROC_PIDTBSDINFO, _BSD)
        if fields[3] != pid or not fields[-2] or fields[-1] >= _USEC_PER_SECOND:
            raise OSError(errno.EIO, "invalid process stamp")
        return ProcessStamp(pid, fields[4], fields[5], fields[-2], fields[-1])

    def _args(self, pid: int) -> tuple[str, tuple[str, ...]]:
        c = self.c
        mib = (c.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        buf = c.create_string_buffer(self._argmax)
        size = c.c_size_t(len(buf))
        if self._sysctl(mib, len(mib), buf, c.byref(size), None, 0) != 0:
            raise OSError(c.get_errno() or errno.EIO, "process arguments unavailable")
        return _exec_args(buf[:size.value])

    def process(self, pid: int) -> MacProcess:
        stamp = self.stamp(pid)
        _, unique, parent_unique, *_ = self._info(pid, _PROC_PIDUNIQIDENTIFIERINFO, _UNIQUE)
        buf = self.c.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
        self.c.set_errno(0)
        if self._pidpath(pid, buf, len(buf)) <= 0:
            raise OSError(self.c.get_errno() or errno.EIO, "executable unavailable")
        path, argv = self._args(pid)
        # Several syscalls make up a snapshot; don't join two processes or parentages.
        if not unique or self.stamp(pid) != stamp:
            raise OSError(errno.EIO, "process changed while reading")
        return MacProcess(stamp, unique, parent_unique, os.fsdecode(buf.value), path, argv)
