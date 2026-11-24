"""The fields of an OpenSSH public key blob, checked against its key type.

Pure and standard library only, so both machines import it: v1's
`host-authorize` (Pi) and the desk link's proof and signer checks
(`desk/link_proof.py`, both machines) share this one rule.
"""

import base64
import struct

KEY_TYPES = frozenset({"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                       "ecdsa-sha2-nistp521"})
_ECDSA_POINT_LENGTH = {b"nistp256": 65, b"nistp384": 97, b"nistp521": 133}


def blob_fields(blob: str) -> list[bytes]:
    """The length-prefixed fields of a base64 key blob; ValueError when it
    isn't strict base64 or a field runs past the end."""
    try:
        raw = base64.b64decode(blob, validate=True)
        fields = []
        while raw:
            size = struct.unpack(">I", raw[:4])[0]
            if size > len(raw) - 4:
                raise ValueError("truncated key")
            fields.append(raw[4:4 + size])
            raw = raw[4 + size:]
    except (ValueError, struct.error) as exc:
        raise ValueError("invalid SSH public key blob") from exc
    return fields


def blob_valid(key_type: str, blob: str) -> bool:
    """True when `blob` is a well-formed public key of `key_type`, one of
    KEY_TYPES: its first field names that type and the rest have the type's
    shape (Ed25519: one 32-byte key)."""
    if key_type not in KEY_TYPES:
        return False
    try:
        fields = blob_fields(blob)
    except ValueError:
        return False
    if not fields or fields[0] != key_type.encode("ascii"):
        return False
    if key_type == "ssh-ed25519":
        return len(fields) == 2 and len(fields[1]) == 32
    if key_type == "ssh-rsa":
        return len(fields) == 3 and all(int.from_bytes(f, "big") > 0 for f in fields[1:])
    curve = key_type.removeprefix("ecdsa-sha2-").encode("ascii")
    return (len(fields) == 3 and fields[1] == curve and len(fields[2]) == _ECDSA_POINT_LENGTH[curve]
            and fields[2][0] == 4)
