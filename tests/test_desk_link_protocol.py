"""desk/link_protocol.py: frame limits, exact shapes, the code table and
round trips (docs/voice-desk.md, "Framing")."""

import json
import unittest

from llm_switchboard.desk import protocol
from llm_switchboard.desk.channel import UndeliverableReason
from llm_switchboard.desk.link_protocol import (
    LINK_CODES, MAX_LINK_LINE_BYTES, PRESENCE_CODES, PRESENCE_PROCESS, REPLY_REFUSAL_CODES, REPLY_REFUSAL_TEXT,
    VERSION_FREE, LineSplitter, LinkFrameError, LinkType, PresenceState, ReplyResultCode, Side,
    UndeliverableCode, decode_frame, encode_frame, sender_of, undeliverable_code,
)
from llm_switchboard.desk.model import ChannelProcess, ReplyRefusal

SESSION = "aaaaaaaa-0000-4000-8000-000000000001"
REQUEST = "a" * 32 + "." + "b" * 16
SIGNATURE = "-----BEGIN SSH SIGNATURE-----\n" + "QUJD\n" * 4 + "-----END SSH SIGNATURE-----\n"

VALID = {
    LinkType.LINK_HELLO: {"versions": [1], "relay_build": "abc1234", "host": "mac", "machine": "a" * 32},
    LinkType.LINK_CHALLENGE: {"version": 1, "epoch": "b" * 32, "nonce": "c" * 64},
    LinkType.LINK_REFUSED: {"reason": "hub_not_running", "versions": [1]},
    LinkType.LINK_PROOF: {"signature": SIGNATURE},
    LinkType.LINK_WELCOME: {"link_id": "d" * 32, "heartbeat_seconds": 15, "idle_timeout_seconds": 45,
                            "max_line_bytes": MAX_LINK_LINE_BYTES},
    LinkType.REGISTER: {"chan": 1, "session_id": SESSION, "channel_pid": 4242, "channel_start": "1700000000123456",
                        "claude_pid": 4241, "claude_start": "1700000000000001", "label": "Fix the é bug",
                        "cwd": "/Users/me/dev"},
    LinkType.REGISTERED: {"chan": 1, "generation": 7},
    LinkType.REGISTER_REFUSED: {"chan": 1, "reason": "not_enrolled"},
    LinkType.UNREGISTERED: {"chan": 1, "reason": "channel_closed"},
    LinkType.DROP: {"chan": 1, "reason": "unenrolled"},
    LinkType.EVENT: {"chan": 1, "request_id": REQUEST, "content": "what changed?\n \U0001f600"},
    LinkType.DELIVERED: {"chan": 1, "request_id": REQUEST},
    LinkType.UNDELIVERABLE: {"chan": 1, "request_id": REQUEST, "reason": "guard_session_file"},
    LinkType.REPLY: {"chan": 1, "request_id": REQUEST, "text": "the answer", "spoken": "Short."},
    LinkType.REPLY_RESULT: {"chan": 1, "request_id": REQUEST, "ok": False, "reason": "unknown_request"},
    LinkType.PRESENCE_QUERY: {"query_id": 1, "channel_pid": 4242, "channel_start": "1700000000123456"},
    LinkType.PRESENCE: {"query_id": 1, "state": "running"},
    LinkType.PING: {"n": 1},
    LinkType.PONG: {"n": 1},
}

# Values no field accepts but the ones named.
GENERIC_BAD = [None, 1.5, [], {}, 2**63, -1, "x\n"]
SPECIFIC_BAD = {
    "versions": [[], [1, 1], [0], [1, 2, 3, 4, 5], [True], ["1"]],
    "relay_build": ["ABC", "g" * 7, "a" * 41],
    "host": ["pi", "Mac", "a" * 33, "1mac", "mac name"],
    "machine": ["a" * 31, "A" * 32, "g" * 32],
    "version": [0, True, 2**31],
    "epoch": ["b" * 31, "B" * 32],
    "nonce": ["c" * 63, "c" * 65],
    "signature": ["", SIGNATURE.replace("END", "FIN"), SIGNATURE[:-1] + " \n",
                  "-----BEGIN SSH SIGNATURE-----\n" + ("A" * 76 + "\n") * 60 + "-----END SSH SIGNATURE-----\n"],
    "link_id": ["d" * 31, "D" * 32],
    "heartbeat_seconds": [0, True, 3601],
    "idle_timeout_seconds": [0],
    "max_line_bytes": [0],
    "chan": [0, 2**31, True],
    "query_id": [0, 2**31],
    "generation": [True],
    "n": [0, True],
    "session_id": ["not-a-session", SESSION.upper()],
    "channel_pid": [0, 2**31, True],
    "claude_pid": [0],
    "channel_start": ["", "12a", "1" * 21, 12],
    "claude_start": ["-1"],
    "label": ["a" * 121, "tab\there", "\x7f", "line sep"],
    "cwd": ["relative/path", "/" + "a" * 4096, "/a\nb"],
    "request_id": ["nope", REQUEST.upper()],
    "content": [1, b"bytes"],
    "text": [None, 1],
    "spoken": [None, 1],
    "ok": [None, 1, "true"],
}
# Fields of free text: every string is valid there.
STRING_FIELDS = {"content", "text", "spoken"}


def sender(kind):
    return next(iter(sender_of(kind)))


def version_for(kind):
    return None if kind in VERSION_FREE else 1


def raw_line(kind, fields, **head):
    message = {"type": kind.value, **({} if kind in VERSION_FREE else {"v": 1}), **head, **fields}
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


class RoundTripTest(unittest.TestCase):
    def test_every_type_has_a_sample_and_round_trips(self):
        self.assertEqual(set(VALID), set(LinkType))
        for kind, fields in VALID.items():
            with self.subTest(kind=kind):
                line = encode_frame(kind, sender(kind), v=version_for(kind), **fields)
                self.assertEqual(decode_frame(line, sender(kind), v=version_for(kind)), (kind, fields))
                self.assertEqual("v" in json.loads(line), kind not in VERSION_FREE)

    def test_successful_reply_result_has_a_null_reason(self):
        fields = dict(VALID[LinkType.REPLY_RESULT], ok=True, reason=None)
        line = encode_frame(LinkType.REPLY_RESULT, Side.HUB, v=1, **fields)
        self.assertEqual(decode_frame(line, Side.HUB, v=1), (LinkType.REPLY_RESULT, fields))
        for bad in (dict(fields, reason="bad_reply"), dict(fields, ok=False, reason=None)):
            with self.subTest(bad=bad), self.assertRaises(LinkFrameError):
                encode_frame(LinkType.REPLY_RESULT, Side.HUB, v=1, **bad)

    def test_pump_refusal_shape_is_version_free(self):
        line = encode_frame(LinkType.LINK_REFUSED, Side.HUB, reason="hub_not_running", versions=[1])
        self.assertEqual(json.loads(line), {"type": "link_refused", "reason": "hub_not_running", "versions": [1]})


class ShapeTest(unittest.TestCase):
    def assertRefused(self, kind, fields, **head):
        with self.assertRaises(LinkFrameError):
            encode_frame(kind, sender(kind), v=version_for(kind), **fields)
        with self.assertRaises(LinkFrameError):
            decode_frame(raw_line(kind, fields, **head), sender(kind), v=version_for(kind))

    def test_bad_field_shapes_refused(self):
        for kind, fields in VALID.items():
            for name in fields:
                if name in ("reason", "state"):
                    continue
                bad_values = SPECIFIC_BAD[name] + [v for v in GENERIC_BAD
                                                   if not (name in STRING_FIELDS and isinstance(v, str))]
                for value in bad_values:
                    with self.subTest(kind=kind, field=name, value=repr(value)[:40]):
                        if isinstance(value, bytes):
                            with self.assertRaises(LinkFrameError):
                                encode_frame(kind, sender(kind), v=version_for(kind), **{**fields, name: value})
                            continue
                        self.assertRefused(kind, {**fields, name: value})

    def test_missing_and_extra_fields_refused(self):
        for kind, fields in VALID.items():
            with self.subTest(kind=kind):
                self.assertRefused(kind, {**fields, "extra": 1})
                missing = dict(fields)
                missing.pop(next(iter(missing)))
                self.assertRefused(kind, missing)

    def test_wrong_side_refused(self):
        for kind, fields in VALID.items():
            others = set(Side) - sender_of(kind)
            for side in others:
                with self.subTest(kind=kind), self.assertRaises(LinkFrameError):
                    decode_frame(raw_line(kind, fields), side, v=version_for(kind))

    def test_v_rules(self):
        event = VALID[LinkType.EVENT]
        hello = VALID[LinkType.LINK_HELLO]
        cases = [
            (raw_line(LinkType.LINK_HELLO, hello, v=1), None),  # a hello with a v
            (raw_line(LinkType.EVENT, event, v=2), 1),  # another v after the challenge
            (raw_line(LinkType.EVENT, event, v=1.0), 1),
            (raw_line(LinkType.EVENT, event, v=True), 1),
            (raw_line(LinkType.EVENT, event), None),  # before any version is agreed
            (json.dumps({"type": "event", **event}).encode() + b"\n", 1),  # no v at all
        ]
        for line, version in cases:
            with self.subTest(line=line[:60]), self.assertRaises(LinkFrameError):
                decode_frame(line, Side.HUB, v=version)
        with self.assertRaises(LinkFrameError):
            encode_frame(LinkType.LINK_HELLO, Side.RELAY, v=1, **hello)
        with self.assertRaises(LinkFrameError):
            encode_frame(LinkType.EVENT, Side.HUB, **event)

    def test_not_frames_refused(self):
        for line in (b"not json\n", b"[1]\n", b'"x"\n', b'{"type":"nope","v":1}\n', b"{}\n",
                     b'{"type":"ping","v":1,"n":1}', b'{"type":"ping","v":1,"n":1}\n\n',
                     b'{"type":"ping","v":1,"n":[[1]]}\n', ("[" * 30000 + "]" * 30000 + "\n").encode(),
                     b'{"type":"ping","v":1,"n":1,"\xff":1}\n'):
            with self.subTest(line=line[:40]), self.assertRaises(LinkFrameError):
                decode_frame(line, Side.HUB, v=1)


class CodeTableTest(unittest.TestCase):
    def test_every_code_in_its_table_round_trips_and_others_are_refused(self):
        all_codes = set().union(*LINK_CODES.values())
        for kind, codes in LINK_CODES.items():
            key = "state" if kind is LinkType.PRESENCE else "reason"
            for code in codes:
                with self.subTest(kind=kind, code=code):
                    fields = {**VALID[kind], key: code}
                    line = encode_frame(kind, sender(kind), v=version_for(kind), **fields)
                    self.assertEqual(decode_frame(line, sender(kind), v=version_for(kind))[1][key], code)
            for code in (all_codes - codes) | {"Not_a_code", "a" * 41, "", "free text", None, 1}:
                with self.subTest(kind=kind, outside=code), self.assertRaises(LinkFrameError):
                    encode_frame(kind, sender(kind), v=version_for(kind), **{**VALID[kind], key: code})

    def test_every_reply_refusal_has_a_code(self):
        self.assertEqual(set(REPLY_REFUSAL_CODES), set(ReplyRefusal))
        self.assertEqual(set(REPLY_REFUSAL_CODES.values()), set(ReplyResultCode))
        self.assertEqual({REPLY_REFUSAL_TEXT[code] for code in ReplyResultCode}, set(ReplyRefusal))

    def test_every_undeliverable_reason_classified(self):
        for reason in UndeliverableReason:
            code = undeliverable_code(str(reason))
            self.assertIn(code, LINK_CODES[LinkType.UNDELIVERABLE])
            self.assertIsNot(code, UndeliverableCode.CHANNEL_OTHER)
        self.assertEqual(len({undeliverable_code(r) for r in UndeliverableReason}), len(UndeliverableReason))
        for other in ("something new", "", None, 3):
            self.assertIs(undeliverable_code(other), UndeliverableCode.CHANNEL_OTHER)

    def test_every_channel_process_state_has_a_code(self):
        self.assertEqual(set(PRESENCE_CODES), set(ChannelProcess))
        self.assertEqual(set(PRESENCE_CODES.values()), set(PresenceState))
        self.assertEqual({PRESENCE_PROCESS[code] for code in PresenceState}, set(ChannelProcess))


class LimitTest(unittest.TestCase):
    def event_line(self, size):
        """An event line of exactly `size` bytes."""
        base = encode_frame(LinkType.EVENT, Side.HUB, v=1, **{**VALID[LinkType.EVENT], "content": ""})
        return raw_line(LinkType.EVENT, {**VALID[LinkType.EVENT], "content": "x" * (size - len(base))})

    def test_a_line_exactly_at_the_cap_passes(self):
        line = self.event_line(MAX_LINK_LINE_BYTES)
        self.assertEqual(len(line), MAX_LINK_LINE_BYTES)
        kind, fields = decode_frame(line, Side.HUB, v=1)
        self.assertEqual(encode_frame(kind, Side.HUB, v=1, **fields), line)
        self.assertEqual(LineSplitter().feed(line), [line])

    def test_one_byte_over_the_cap_refused_everywhere(self):
        line = self.event_line(MAX_LINK_LINE_BYTES + 1)
        with self.assertRaises(LinkFrameError):
            decode_frame(line, Side.HUB, v=1)
        _, fields = decode_frame(self.event_line(MAX_LINK_LINE_BYTES), Side.HUB, v=1)
        with self.assertRaises(LinkFrameError):
            encode_frame(LinkType.EVENT, Side.HUB, v=1, **{**fields, "content": fields["content"] + "x"})
        with self.assertRaises(LinkFrameError):
            LineSplitter().feed(line)
        # Refused before its newline arrives: the reader never buffers more.
        with self.assertRaises(LinkFrameError):
            LineSplitter().feed(line[:-1])

    def test_splitter_keeps_lines_whole_and_drops_a_partial_tail(self):
        splitter = LineSplitter()
        a, b = encode_frame(LinkType.PING, Side.HUB, v=1, n=1), encode_frame(LinkType.PONG, Side.HUB, v=1, n=2)
        stream = a + b + b'{"type":"pi'
        out = []
        for i in range(len(stream)):
            out.extend(splitter.feed(stream[i:i + 1]))
        self.assertEqual(out, [a, b])
        self.assertEqual(splitter.pending, len(b'{"type":"pi'))

    def test_largest_valid_channel_frames_fit_once_rewrapped(self):
        # Escapes that cost the most per character in the channel's own
        # encoding, filled to its 64 KiB line, both ways across the link.
        for filler in ('"', "é", "\U0001f600", "\\"):
            with self.subTest(filler=filler):
                text = _fill(lambda t: protocol.encode(protocol.MessageType.REPLY, request_id=REQUEST,
                                                       text=t, spoken="s"), filler)
                reply = {"chan": 2**31 - 1, "request_id": REQUEST, "text": text, "spoken": "s"}
                line = encode_frame(LinkType.REPLY, Side.RELAY, v=1, **reply)
                self.assertEqual(decode_frame(line, Side.RELAY, v=1), (LinkType.REPLY, reply))
                content = _fill(lambda t: protocol.encode(protocol.MessageType.EVENT, request_id=REQUEST,
                                                          content=t), filler)
                event = {"chan": 2**31 - 1, "request_id": REQUEST, "content": content}
                line = encode_frame(LinkType.EVENT, Side.HUB, v=1, **event)
                self.assertEqual(decode_frame(line, Side.HUB, v=1), (LinkType.EVENT, event))


def _fill(encode, filler):
    """The longest run of `filler` that `encode` accepts in one channel line."""
    low, high = 0, protocol.MAX_LINE_BYTES
    while low < high:
        mid = (low + high + 1) // 2
        try:
            encode(filler * mid)
            low = mid
        except protocol.FrameError:
            high = mid - 1
    return filler * low


if __name__ == "__main__":
    unittest.main()
