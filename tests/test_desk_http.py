"""The desk's HTTPS API through a real TLS client that trusts only a
throwaway leaf: guards, headers, pairing, sends, long-polls and refusals."""

import dataclasses
import hashlib
import http.client
import os
import re
import secrets
import socket
import ssl
import threading
import time
import unittest
from unittest import mock

from desk_fixtures import (
    SESSION_A, SESSION_B, ChannelClient, FakeIdentity, FakePeer, HubCase, desk_config, install_cert, make_home,
    throwaway_cert, wait_until,
)

from llm_switchboard.desk.http_server import STATIC_ROUTES, TlsSetupError
from llm_switchboard.desk.hub import Hub
from llm_switchboard.desk.hub_state import CHANNEL_UNVERIFIABLE
from llm_switchboard.desk.labels import SessionLabels
from llm_switchboard.desk.model import MAX_BODY_BYTES, LockStoreError
from llm_switchboard.desk.protocol import MessageType

CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def browser_post_origin(referrer_policy: str, page_origin: str, target_origin: str) -> str:
    """The Origin header a browser sends on the page's own fetch() POST, per the
    Fetch standard's "serialize a request origin" for a same-origin request
    (response tainting "basic"): the page's referrer policy decides whether it
    is the page's origin or "null". Only the last policy token counts."""
    tokens = [t.strip().lower() for t in referrer_policy.split(",") if t.strip()]
    policy = tokens[-1] if tokens else "strict-origin-when-cross-origin"
    downgrade = page_origin.startswith("https:") and not target_origin.startswith("https:")
    if policy == "no-referrer":
        return "null"
    if policy in ("no-referrer-when-downgrade", "strict-origin", "strict-origin-when-cross-origin") and downgrade:
        return "null"
    if policy == "same-origin" and page_origin != target_origin:
        return "null"
    return page_origin


class FailingLockStore:
    def load(self):
        return []

    def add(self, lock):
        raise LockStoreError("lock file unwritable")

    def remove(self, lock_id):
        raise LockStoreError("lock file unwritable")


class HttpCase(HubCase):
    def assertDeskHeaders(self, headers):
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "same-origin")
        self.assertEqual(headers["content-security-policy"], CSP)
        self.assertFalse([name for name in headers if name.startswith("access-control-")])

    def registered_channel(self, session_id=SESSION_A):
        client = ChannelClient(self.config.channel_socket, session_id)
        self.addCleanup(client.close)
        kind, welcome = client.first
        self.assertIs(kind, MessageType.WELCOME)
        return client, welcome["generation"]

    def ticket(self, token, generation, session_id=SESSION_A):
        status, _, body = self.call("POST", "/desk/api/ticket", {"session_id": session_id, "generation": generation,
                                                                 "epoch": self.hub.epoch}, token=token)
        self.assertEqual(status, 200, body)
        return body["ticket"]


class GuardTest(HttpCase):
    def test_every_response_carries_the_security_headers_and_no_cors(self):
        token = self.pair()
        responses = [
            self.call("GET", "/desk"),
            self.call("GET", "/desk/nope"),
            self.call("GET", "/desk/api/state", token=token),
            self.call("GET", "/desk/api/state"),
            self.call("POST", "/desk/pair/start", {"secret_hash": "bad"},
                      headers={"Origin": "https://evil.example"}),
            self.call("OPTIONS", "/desk/api/send", headers={"Origin": f"https://localhost:{self.port}",
                                                           "Access-Control-Request-Method": "POST"}),
        ]
        for status, headers, _ in responses:
            with self.subTest(status=status):
                self.assertDeskHeaders(headers)

    def test_host_origin_content_type_and_size_guards(self):
        token = self.pair()
        ok_body = {"secret_hash": "a" * 64}
        cases = [
            (("GET", "/desk"), {"headers": {"Host": f"127.0.0.1:{self.port}"}}, 403, "wrong host"),
            (("GET", "/desk"), {"headers": {"Host": "localhost:1"}}, 403, "wrong host"),
            (("POST", "/desk/pair/start", ok_body), {"headers": {"Origin": None}}, 403, "wrong origin"),
            (("POST", "/desk/pair/start", ok_body), {"headers": {"Origin": f"http://localhost:{self.port}"}},
             403, "wrong origin"),
            (("POST", "/desk/pair/start", ok_body), {"headers": {"Content-Type": "text/plain"}},
             400, "content type must be application/json"),
            (("POST", "/desk/pair/start"), {"raw": b"x" * (MAX_BODY_BYTES + 1)}, 413, "body too large"),
            (("POST", "/desk/pair/start"), {"raw": b"[1, 2]"}, 400, "bad json"),
            (("POST", "/desk/api/ticket", {}), {}, 401, "unauthorized"),
            (("POST", "/desk/api/ticket", {}), {"token": "not-a-token"}, 401, "unauthorized"),
            (("GET", "/desk/api/state"), {"headers": {"Authorization": f"Basic {token}"}}, 401, "unauthorized"),
            (("GET", "/desk/api/state?after=x"), {"token": token}, 400, "bad after"),
        ]
        for args, options, status, reason in cases:
            with self.subTest(reason=reason, args=args[:2], options=str(options)[:60]):
                got, _, body = self.call(*args, **options)
                self.assertEqual((got, body), (status, {"error": reason}))

    def test_static_files_are_a_fixed_list_with_their_content_types(self):
        for path, (file, content_type) in STATIC_ROUTES.items():
            with self.subTest(path=path):
                self.assertTrue(file.is_file(), f"{file} is missing")
                status, headers, body = self.call("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], content_type)
        for path in ("/desk/", "/desk/hub.py", "/desk/static/app.js", "/desk/../desk/app.js", "/desk/index.html"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path)[0], 404)

    def test_every_file_the_page_loads_is_a_static_route(self):
        index = STATIC_ROUTES["/desk"][0].read_text()
        loaded = re.findall(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"', index)
        self.assertIn("/desk/conversation_view.js", loaded)
        for path in loaded:
            with self.subTest(path=path):
                self.assertIn(path, STATIC_ROUTES)

    def test_plain_http_gets_no_answer(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            raw.sendall(f"GET /desk HTTP/1.1\r\nHost: localhost:{self.port}\r\n\r\n".encode())
            data = b""
            try:
                while chunk := raw.recv(4096):
                    data += chunk
            except (ConnectionResetError, TimeoutError):
                pass
        self.assertNotIn(b"HTTP/1", data)

    def test_the_listener_is_loopback_only(self):
        self.assertEqual(self.hub.http._server.server_address[0], "127.0.0.1")


class ReferrerPolicyTest(HttpCase):
    def test_the_page_s_own_pairing_post_carries_the_origin_the_guard_needs(self):
        """A browser derives the POST's Origin from the page's referrer policy;
        under no-referrer it would send "null" and pairing would fail."""
        status, headers, _ = self.call("GET", "/desk")
        self.assertEqual(status, 200)
        origin = f"https://localhost:{self.port}"
        sent = browser_post_origin(headers["referrer-policy"], origin, origin)
        self.assertEqual(sent, origin)
        body = {"secret_hash": hashlib.sha256(b"s").hexdigest()}
        status, _, started = self.call("POST", "/desk/pair/start", body, headers={"Origin": sent})
        self.assertEqual(status, 200, started)
        self.assertEqual(browser_post_origin("no-referrer", origin, origin), "null")

    def test_origin_null_is_still_refused(self):
        body = {"secret_hash": hashlib.sha256(b"s").hexdigest()}
        got = self.call("POST", "/desk/pair/start", body, headers={"Origin": "null"})
        self.assertEqual(got[::2], (403, {"error": "wrong origin"}))


class RevokedInFlightTest(HttpCase):
    def test_a_token_revoked_after_the_header_check_cannot_mutate(self):
        """The header check passes (as for a request already in flight at the
        revoke), then the token is revoked: the mutation itself refuses."""
        token = self.pair()
        client, generation = self.registered_channel()
        ticket = self.ticket(token, generation)
        _, _, sent = self.call("POST", "/desk/api/send", {"ticket": ticket, "text": "hi"}, token=token)
        lock = self.target(SESSION_A)["lock"]
        other = "dddddddd-0000-4000-8000-000000000004"
        self.hub.handle_operator({"op": "revoke"})
        routes = [
            ("/desk/api/ticket", {"session_id": SESSION_B, "generation": 1, "epoch": self.hub.epoch}),
            ("/desk/api/send", {"ticket": ticket, "text": "again"}),
            ("/desk/api/clear", {"session_id": SESSION_A, "lock_id": lock["lock_id"]}),
            ("/desk/api/enroll", {"session_id": other}),
            ("/desk/api/unenroll", {"session_id": SESSION_B}),
        ]
        with mock.patch.object(self.hub, "authenticate", return_value=True):
            for path, body in routes:
                with self.subTest(path=path):
                    got = self.call("POST", path, body, token=token)
                    self.assertEqual(got[::2], (401, {"error": "unauthorized"}))
        self.assertEqual(self.target(SESSION_A)["lock"], lock)
        self.assertFalse(self.hub.enrollment.is_enrolled(other))
        self.assertTrue(self.hub.enrollment.is_enrolled(SESSION_B))


class TlsSetupTest(unittest.TestCase):
    def test_refuses_to_start_without_the_leaf_or_with_a_readable_key(self):
        config = desk_config(make_home(self))
        hub = Hub(config, peer=FakePeer())
        with self.assertRaisesRegex(TlsSetupError, "leaf.pem"):
            hub.start()
        install_cert(config)
        os.chmod(config.tls_key, 0o644)
        with self.assertRaisesRegex(TlsSetupError, "0600"):
            Hub(config, peer=FakePeer()).start()
        self.assertFalse(config.channel_socket.exists())


class PairingHttpTest(HttpCase):
    def test_claim_waits_for_the_operator_and_hands_out_the_token_once(self):
        secret = secrets.token_hex(32)
        _, _, started = self.call("POST", "/desk/pair/start", {"secret_hash": hashlib.sha256(secret.encode()).hexdigest()})
        claim = {"pair_id": started["pair_id"], "secret": secret, "build": self.hub.http.page_build}
        self.assertEqual(self.call("POST", "/desk/pair/claim", claim)[::2], (409, {"error": "not confirmed"}))
        self.hub.handle_operator({"op": "pair", "code": started["code"]})
        status, _, claimed = self.call("POST", "/desk/pair/claim", claim)
        self.assertEqual(status, 200)
        self.assertEqual(set(claimed), {"token", "expires_at"})
        self.assertEqual(self.call("POST", "/desk/pair/claim", claim)[0], 404)
        status, _, view = self.call("GET", "/desk/api/state", token=claimed["token"])
        self.assertEqual(status, 200)
        self.assertEqual(set(view), {"version", "epoch", "sends_enabled", "voice_confirm_enabled", "lock_store_error",
                                     "targets", "requests", "links", "admission_refusals"})


class SendHttpTest(HttpCase):
    def test_round_trip_over_https_and_the_channel(self):
        token = self.pair()
        client, generation = self.registered_channel()
        status, _, view = self.call("GET", "/desk/api/state", token=token)
        [target] = [t for t in view["targets"] if t["session_id"] == SESSION_A]
        self.assertEqual({k: target[k] for k in ("host", "enrolled", "registration", "generation", "lock")},
                         {"host": "pi", "enrolled": True, "registration": "registered", "generation": generation,
                          "lock": None})
        self.assertEqual(target["label"], SESSION_A[:8])
        ticket = self.ticket(token, generation)
        for text, reason in (("/clear", "starts with a command character"), ("", "empty"),
                             ("a‮b", "control character")):
            got = self.call("POST", "/desk/api/send", {"ticket": ticket, "text": text}, token=token)
            self.assertEqual(got[::2], (400, {"error": reason}))
        status, _, sent = self.call("POST", "/desk/api/send", {"ticket": ticket, "text": "how is the build?"},
                                    token=token)
        self.assertEqual((status, sent["state"]), (200, "handed_off"))
        kind, event = client.read()
        self.assertTrue(event["content"].startswith("how is the build?"))
        client.send(MessageType.REPLY, request_id=sent["request_id"], text="Green.", spoken="It is green.")
        self.assertTrue(client.read()[1]["ok"])
        _, _, view = self.call("GET", "/desk/api/state", token=token)
        [request] = view["requests"]
        self.assertEqual(request, {"request_id": sent["request_id"], "host": "pi", "session_id": SESSION_A, "state": "answered",
                                   "text": "how is the build?", "created": request["created"], "confirm": "tap",
                                   "reply": {"text": "Green.", "spoken": "It is green."}})
        duplicate = self.call("POST", "/desk/api/send", {"ticket": ticket, "text": "how is the build?"}, token=token)
        self.assertEqual(duplicate[::2], (409, {"error": "ticket already used"}))

    def test_clear_enroll_and_unenroll_routes(self):
        token = self.pair()
        client, generation = self.registered_channel()
        ticket = self.ticket(token, generation)
        _, _, sent = self.call("POST", "/desk/api/send", {"ticket": ticket, "text": "hi"}, token=token)
        lock = self.target(SESSION_A)["lock"]
        client.read()
        client.send(MessageType.DELIVERED, request_id=sent["request_id"])
        client.send(MessageType.PING)
        client.read()
        self.assertEqual(self.call("POST", "/desk/api/clear", {"session_id": SESSION_A, "lock_id": "0" * 16},
                                   token=token)[::2], (409, {"error": "lock id does not match"}))
        self.assertEqual(self.call("POST", "/desk/api/clear", {"session_id": SESSION_A, "lock_id": lock["lock_id"]},
                                   token=token)[::2], (200, {"cleared": True}))
        other = "dddddddd-0000-4000-8000-000000000004"
        self.assertEqual(self.call("POST", "/desk/api/enroll", {"session_id": other}, token=token)[::2],
                         (200, {"session_id": other, "enrolled": True}))
        self.assertTrue(self.target(other)["enrolled"])
        self.assertEqual(self.call("POST", "/desk/api/unenroll", {"session_id": other}, token=token)[0], 200)
        self.assertEqual(self.call("POST", "/desk/api/enroll", {"session_id": "nope"}, token=token)[0], 400)


class SendsOffTest(HttpCase):
    sends_enabled = False

    def test_sends_are_refused_while_off(self):
        token = self.pair()
        _, generation = self.registered_channel()
        ticket = self.ticket(token, generation)
        got = self.call("POST", "/desk/api/send", {"ticket": ticket, "text": "hi"}, token=token)
        self.assertEqual(got[::2], (403, {"error": "sends off"}))


class ProcessTreeHttpTest(HttpCase):
    def test_a_missing_process_tree_is_no_evidence_the_channel_is_gone(self):
        token = self.pair()
        client, generation = self.registered_channel()
        status, _, sent = self.call("POST", "/desk/api/send", {"ticket": self.ticket(token, generation), "text": "hi"},
                                    token=token)
        self.assertEqual(status, 200, sent)
        # The channel goes away without reporting on the event.
        client.close()
        wait_until(lambda: self.hub.state.request_state(sent["request_id"]) == "unknown")
        lock = next(t for t in self.hub.state_view()["targets"] if t["session_id"] == SESSION_A)["lock"]
        clear = {"session_id": SESSION_A, "lock_id": lock["lock_id"]}
        tree = self.config.proc_dir
        for root, why in ((self.home / "no-proc", "no process tree at all"), (self.home / "empty-proc", "not mounted")):
            with self.subTest(why):
                if why == "not mounted":
                    root.mkdir()
                self.hub.close()
                self.config = dataclasses.replace(self.config, proc_dir=root)
                self.hub = self.start_hub()
                status, _, body = self.call("POST", "/desk/api/clear", clear, token=self.pair())
                self.assertEqual((status, body), (503, {"error": CHANNEL_UNVERIFIABLE}))
        # A mounted tree without that pid: the channel is gone.
        self.hub.close()
        self.config = dataclasses.replace(self.config, proc_dir=tree)
        self.hub = self.start_hub()
        status, _, body = self.call("POST", "/desk/api/clear", clear, token=self.pair())
        self.assertEqual((status, body), (200, {"cleared": True}))


class LockStoreFailureHttpTest(HttpCase):
    def setUp(self):
        super().setUp()
        self.hub.close()
        self.hub = self.start_hub(lock_store=FailingLockStore())

    def test_a_lock_write_failure_refuses_the_send_with_503_and_writes_nothing(self):
        token = self.pair()
        client, generation = self.registered_channel()
        ticket = self.ticket(token, generation)
        got = self.call("POST", "/desk/api/send", {"ticket": ticket, "text": "hi"}, token=token)
        self.assertEqual(got[::2], (503, {"error": "lock file unwritable"}))
        _, _, view = self.call("GET", "/desk/api/state", token=token)
        self.assertEqual(view["lock_store_error"], "lock file unwritable")
        self.assertEqual(view["requests"][0]["state"], "expired")
        client.send(MessageType.PING)
        self.assertIs(client.read()[0], MessageType.PONG)


class LongPollTest(HttpCase):
    long_poll_seconds = 20.0

    def poll_async(self, token, after):
        result = {}

        def run():
            result["response"] = self.call("GET", f"/desk/api/state?after={after}", token=token)
            result["at"] = time.monotonic()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    def test_a_long_poll_returns_at_the_next_change(self):
        token = self.pair()
        _, _, view = self.call("GET", "/desk/api/state", token=token)
        status, _, unchanged = self.call("GET", f"/desk/api/state?after={view['version'] + 100}", token=token)
        self.assertEqual(unchanged["version"], view["version"])
        thread, result = self.poll_async(token, view["version"])
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())
        self.registered_channel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        status, _, changed = result["response"]
        self.assertEqual(status, 200)
        self.assertGreater(changed["version"], view["version"])

    def test_a_revoked_tokens_waiting_long_poll_returns_401_promptly(self):
        token = self.pair()
        _, _, view = self.call("GET", "/desk/api/state", token=token)
        thread, result = self.poll_async(token, view["version"])
        time.sleep(0.2)
        revoked_at = time.monotonic()
        self.assertEqual(self.hub.handle_operator({"op": "revoke"}), {"ok": True, "revoked": 1})
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["response"][::2], (401, {"error": "unauthorized"}))
        self.assertLess(result["at"] - revoked_at, 2)
        self.assertEqual(self.call("GET", "/desk/api/state", token=token)[0], 401)


class UnencodableLabelTest(HttpCase):
    """A folder name holding a lone surrogate (a JSON escape in a transcript
    or session file) must not break the snapshot every tablet reads."""

    # `\udc80` stays a JSON escape in the file, as Claude Code would write it.
    BAD_CWD_JSON = '"/work/\\udc80"'

    def assertStateServed(self):
        status, _, view = self.call("GET", "/desk/api/state", token=self.pair())
        self.assertEqual(status, 200, view)
        [target] = [t for t in view["targets"] if t["session_id"] == SESSION_A]
        target["cwd"].encode("utf-8")
        target["label"].encode("utf-8")
        return target

    def test_a_transcript_cwd_with_a_lone_surrogate_is_served(self):
        project = self.home / ".claude" / "projects" / "-work-x"
        project.mkdir(parents=True)
        (project / f"{SESSION_A}.jsonl").write_text('{"type":"system","cwd":' + self.BAD_CWD_JSON + '}\n')
        self.assertEqual(SessionLabels(self.config).describe(SESSION_A), ("?", "/work/?"))
        self.hub.recheck_now()
        self.assertEqual(self.assertStateServed()["cwd"], "/work/?")

    def test_a_session_file_cwd_with_a_lone_surrogate_is_served(self):
        # No transcript: the folder comes from the session registry entry.
        self.peer.identities[SESSION_A] = FakeIdentity(7001, "1", 7002, "2", SESSION_A)
        self.config.claude_sessions_dir.mkdir(parents=True)
        (self.config.claude_sessions_dir / "7002.json").write_text('{"cwd":' + self.BAD_CWD_JSON + '}')
        self.assertEqual(SessionLabels(self.config).describe(SESSION_A, 7002), ("?", "/work/?"))
        self.registered_channel()
        self.assertEqual(self.assertStateServed()["cwd"], "/work/?")

    def test_the_hub_cleans_whatever_the_label_reader_returns(self):
        with mock.patch.object(SessionLabels, "describe", return_value=("bad \udc80 title", "/work/\udc80")):
            self.hub.recheck_now()
        target = self.assertStateServed()
        self.assertEqual((target["label"], target["cwd"]), ("bad ? title", "/work/?"))


if __name__ == "__main__":
    unittest.main()
