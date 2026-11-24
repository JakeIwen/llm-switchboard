"""The Mac's `machine` for the link hello (docs/voice-desk.md, "Framing"):
the first 32 hex characters of the SHA-256 of `llm-switchboard-desk-machine:`
plus the Mac's hardware UUID, so the raw UUID never leaves the Mac.

The UUID comes from `gethostuuid` through `ctypes`, loaded on first use so
the module imports on Linux for tests. It is written as `ioreg` shows
`IOPlatformUUID` (upper case, hyphenated), so the value is stable across
relay restarts and Python versions. Standard library only.
"""

import ctypes
import hashlib
import uuid

from .model import MACHINE_HEX_LENGTH

PREFIX = "llm-switchboard-desk-machine:"
# gethostuuid blocks at most this long for the kernel's answer.
_WAIT_SECONDS = 5


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def machine_for(hardware_uuid: str) -> str:
    """The link's `machine` for a hardware UUID as text."""
    return hashlib.sha256((PREFIX + hardware_uuid).encode("ascii")).hexdigest()[:MACHINE_HEX_LENGTH]


def hardware_uuid() -> str:
    """This Mac's hardware UUID; OSError where `gethostuuid` is missing or fails."""
    try:
        call = ctypes.CDLL(None, use_errno=True).gethostuuid
    except (AttributeError, OSError) as exc:
        raise OSError("gethostuuid is unavailable") from exc
    call.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Timespec)]
    call.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(16)
    if call(buffer, ctypes.byref(_Timespec(_WAIT_SECONDS, 0))) != 0:
        raise OSError(ctypes.get_errno(), "gethostuuid failed")
    return str(uuid.UUID(bytes=buffer.raw)).upper()


def machine_id() -> str:
    return machine_for(hardware_uuid())
