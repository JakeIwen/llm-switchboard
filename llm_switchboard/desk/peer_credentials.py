"""The uid of a Unix socket's peer, on Linux (`SO_PEERCRED`) and macOS
(`LOCAL_PEERCRED`), for the Mac link's sockets: the hub's `link.sock` and
the relay's channel socket. It repeats what a 0600 socket in a 0700
directory already ensures (docs/voice-desk.md, "Who the design defends
against"). Standard library only."""

import socket
import struct
import sys

# macOS: `struct xucred` is {u_int cr_version; uid_t cr_uid; short
# cr_ngroups; gid_t cr_groups[16]}, read at level 0 (SOL_LOCAL).
_XUCRED_BYTES = 76
_SOL_LOCAL = 0


class PeerCredentialsUnavailable(OSError):
    """The kernel gave no credentials for this socket."""


def socket_peer_uid(sock: socket.socket) -> int:
    try:
        if sys.platform.startswith("linux"):
            _pid, uid, _gid = struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            return uid
        if sys.platform == "darwin":
            data = sock.getsockopt(_SOL_LOCAL, socket.LOCAL_PEERCRED, _XUCRED_BYTES)
            _version, uid = struct.unpack_from("Ii", data)
            return uid
    except (OSError, struct.error) as exc:
        raise PeerCredentialsUnavailable("peer credentials unavailable") from exc
    raise PeerCredentialsUnavailable(f"no peer credentials on {sys.platform}")
