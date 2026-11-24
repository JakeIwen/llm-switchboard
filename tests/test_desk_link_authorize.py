"""`desk link-authorize` and `desk link-revoke` (docs/voice-desk.md, "SSH":
Installer and Revocation; "Switching it off, revocation and rollback"):
the commands run in-process against a real hub driven by the test-side
relay in tests/link_peer.py, or with no hub under the run lock, and as
child processes through the real CLI. Every `authorized_keys` and
`link-signers` path is under a temporary HOME."""

import subprocess
import threading
import unittest
from unittest.mock import patch

from desk_fixtures import HubCase, desk_config, make_home, run_desk_cli, wait_until
from link_peer import MAC, MAC2, KeyPair, LinkHubCase, fake_signature, write_signers
from test_desk_link_channels import SESSION_M, MacTargetCase
from test_desk_link_fence import run_lock_held_by_child

from llm_switchboard import host_authorize
from llm_switchboard.desk import link_authorize, link_hub
from llm_switchboard.desk.link_authorize import (
    ED25519_REFUSAL, UNCONFIRMED, key_line, marker, run_authorize, run_lock_refusal, run_revoke,
)
from llm_switchboard.desk.link_proof import Signer, SignersUnreadable, read_signers
from llm_switchboard.desk.link_protocol import LinkType, RefusedCode
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.operator import HubResultUnknown
from llm_switchboard.ssh_authorized_keys import has_marker

V1_LINE = b"restrict,command=\"/x host-exchange mac\" ssh-ed25519 AAAAV1 llm-switchboard-host:mac\n"


class Output(list):
    def __call__(self, line, error):
        self.append(line)

    def text(self) -> str:
        return "\n".join(self)


def pub(key: KeyPair) -> bytes:
    return key.path.with_suffix(".pub").read_bytes()


def other_type_key(directory, kind: str) -> bytes:
    path = directory / f"key-{kind}"
    subprocess.run(["ssh-keygen", "-q", "-t", kind, "-N", "", "-f", str(path)], check=True, capture_output=True,
                   timeout=60)
    return path.with_suffix(".pub").read_bytes()


class CommandsMixin:
    """Runs the commands in-process with a verifier that passes."""

    def authorize(self, host, raw):
        out = Output()
        return run_authorize(self.config, host, raw, verifier_self_test=lambda: True, out=out), out

    def revoke(self, host):
        out = Output()
        return run_revoke(self.config, host, out=out), out

    def keys_file(self):
        return self.config.home / ".ssh" / "authorized_keys"

    def signer_blob(self, host):
        return {s.host: s.blob for s in read_signers(self.config.link_signers_file)}.get(host)

    def key_blob(self, host):
        lines = [line for line in self.keys_file().read_bytes().splitlines(keepends=True)
                 if has_marker(line, marker(host))] if self.keys_file().exists() else []
        self.assertLessEqual(len(lines), 1)
        return lines[0].split()[-2].decode() if lines else None

    def assert_one_key(self, host):
        """Whenever neither command holds the locks, both lines name one key or are both absent."""
        self.assertEqual(self.signer_blob(host), self.key_blob(host))


class LinkCommandsTest(CommandsMixin, MacTargetCase):
    def test_link_authorize_drops_old_link(self):
        code, out = self.authorize(MAC, pub(self.key("mac-rotated")))
        self.assertEqual(code, 0, out.text())
        self.assertTrue(self.mac.closed())
        self.assertEqual(self.links()[MAC]["state"], "down")
        self.assertIn("step 4 (confirmation): done", out.text())
        self.assert_one_key(MAC)
        self.connect("mac-rotated")

    def test_revoke_during_active_send(self):
        sent = self.send_m()
        self.mac.expect(LinkType.EVENT)
        code, out = self.revoke(MAC)
        self.assertEqual(code, 0, out.text())
        self.assertTrue(self.mac.closed())
        self.assertEqual(self.hub.state.request_state(sent["request_id"]), "unknown")
        self.assertEqual(self.lock_m()["request_id"], sent["request_id"])
        self.assert_one_key(MAC)
        self.assertIsNone(self.signer_blob(MAC))

    def test_revoke_during_delayed_hello(self):
        entered, resume = threading.Event(), threading.Event()
        verify = self.hub.links._verify

        def delayed(*args, **kwargs):
            result = verify(*args, **kwargs)
            entered.set()
            resume.wait(30)
            return result
        self.hub.links._verify = delayed
        peer = self.link_peer()
        result = {}
        hello = threading.Thread(target=lambda: result.update(peer.handshake(
            lambda _: fake_signature(Signer(MAC, self.key("mac").signer.blob)))), daemon=True)
        hello.start()
        self.assertTrue(entered.wait(30))
        code, out = self.revoke(MAC)
        self.assertEqual(code, 0, out.text())
        resume.set()
        hello.join(30)
        self.assertEqual((result["type"], result["reason"]), (LinkType.LINK_REFUSED.value, RefusedCode.NOT_AUTHORIZED))

    def test_signer_removal_after_publication_reread(self):
        self.mac.close()
        wait_until(lambda: self.links()[MAC]["state"] == "down", timeout=30)
        entered, resume, once = threading.Event(), threading.Event(), []
        reread = link_hub.read_signers

        def paused(path):
            listed = reread(path)
            if threading.current_thread().name == "desk-link" and not once:
                once.append(True)
                entered.set()
                resume.wait(30)
            return listed
        result, revoked = {}, {}
        with patch.object(link_hub, "read_signers", paused):
            peer = self.link_peer()
            hello = threading.Thread(target=lambda: result.update(peer.handshake(
                lambda _: fake_signature(Signer(MAC, self.key("mac").signer.blob)))), daemon=True)
            hello.start()
            self.assertTrue(entered.wait(30))
            # The revoke's steps 1 and 2 land; its link_drop waits for the links lock.
            revoke = threading.Thread(target=lambda: revoked.update(zip(("code", "out"), self.revoke(MAC))),
                                      daemon=True)
            revoke.start()
            wait_until(lambda: self.signer_blob(MAC) is None and self.key_blob(MAC) is None)
            revoke.join(0.5)
            self.assertTrue(revoke.is_alive())
            resume.set()
            hello.join(30)
            revoke.join(30)
        self.assertEqual(result["type"], LinkType.LINK_WELCOME.value)  # published after step 1 returned
        self.assertEqual(revoked["code"], 0, revoked["out"].text())
        self.assertTrue(peer.closed())
        # With link_drop unanswered, the next housekeeping pass retires it.
        write_signers(self.config.link_signers_file, self.key("mac").signer, self.key("mac2").signer)
        peer = self.connect()

        def unanswered(path, op, *args, **fields):
            raise HubResultUnknown("no answer")
        with patch.object(link_authorize, "operator_request", unanswered):
            code, out = self.revoke(MAC)
        self.assertEqual(code, 1)
        self.assertIn(UNCONFIRMED, out.text())
        self.assertEqual(self.links()[MAC]["state"], "up")
        self.hub.recheck_now()
        self.assertTrue(peer.closed())

    def test_unreadable_signer_check_is_not_a_confirmation(self):
        # The hub's signer check can't read link-signers, so its answer has
        # readable false and null signer and link: neither command confirms.
        unreadable = patch.object(link_hub, "read_signers", side_effect=SignersUnreadable("bad"))
        with unreadable:
            code, out = self.revoke(MAC)
        self.assertEqual(code, 1, out.text())
        self.assertIn(f"step 3 (confirmation): {UNCONFIRMED} (link-signers unreadable)", out.text())
        self.assertNotIn("(confirmation): done", out.text())
        self.assertIsNone(self.signer_blob(MAC))  # steps 1 and 2 stand
        self.assertIsNone(self.key_blob(MAC))
        with unreadable:
            code, out = self.authorize(MAC, pub(self.key("mac-rotated")))
        self.assertEqual(code, 1, out.text())
        self.assertIn("step 4 (confirmation): not confirmed: link-signers unreadable; the credential edits already "
                      "made stand", out.text())
        self.assertNotIn("(confirmation): done", out.text())
        self.assert_one_key(MAC)
        self.assertEqual(self.signer_blob(MAC), self.key("mac-rotated").signer.blob)

    def test_revoke_interrupted_between_files_rerun_completes(self):
        code, out = self.authorize(MAC, pub(self.key("mac")))
        self.assertEqual(code, 0, out.text())
        with patch.object(link_authorize, "remove_marked_line", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.revoke(MAC)
        self.assertIsNone(self.signer_blob(MAC))
        self.assertEqual(self.key_blob(MAC), self.key("mac").signer.blob)
        self.hub.recheck_now()
        self.assertTrue(self.mac.closed())
        code, out = self.revoke(MAC)
        self.assertEqual(code, 0, out.text())
        self.assertIn("step 1 (link-signers): already so", out.text())
        self.assertIn("step 2 (authorized_keys): done", out.text())
        self.assertIsNone(self.key_blob(MAC))

    def test_authorize_interrupted_between_files(self):
        with patch.object(link_authorize, "replace_marked_line", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.authorize(MAC, pub(self.key("mac-rotated")))
        self.assertEqual(self.signer_blob(MAC), self.key("mac-rotated").signer.blob)
        self.assertIsNone(self.key_blob(MAC))
        self.hub.recheck_now()  # the old key's link is retired at the next pass
        self.assertTrue(self.mac.closed())
        code, out = self.authorize(MAC, pub(self.key("mac-rotated")))
        self.assertEqual(code, 0, out.text())
        self.assertIn("link-signers already so, authorized_keys written", out.text())
        self.assert_one_key(MAC)

    def test_refused_authorization_keeps_existing_signers(self):
        mac2 = self.connect("mac2", MAC2)
        before = self.config.link_signers_file.read_bytes()
        cases = [("mac3", other_type_key(self.home, "rsa")), ("mac3", other_type_key(self.home, "ecdsa")),
                 ("mac3", pub(self.key("mac2")))]
        for host, raw in cases:
            with self.subTest(host=host, raw=raw[:12]):
                code, out = self.authorize(host, raw)
                self.assertEqual(code, 1, out.text())
                self.assertEqual(self.config.link_signers_file.read_bytes(), before)
        full = [KeyPair(self.home, f"mac{n}") for n in (3, 4)]
        write_signers(self.config.link_signers_file, self.key("mac").signer, self.key("mac2").signer,
                      *(k.signer for k in full))
        before = self.config.link_signers_file.read_bytes()
        code, out = self.authorize("mac5", pub(KeyPair(self.home, "mac5")))
        self.assertEqual(code, 1)
        self.assertIn("already lists 4 Macs", out.text())
        self.assertEqual(self.config.link_signers_file.read_bytes(), before)
        self.hub.recheck_now()
        self.assertEqual((self.links()[MAC]["state"], self.links()[MAC2]["state"]), ("up", "up"))
        mac2.send(LinkType.PING, n=1)
        self.assertEqual(mac2.expect(LinkType.PONG), {"n": 1})

    def test_concurrent_authorizations_respect_host_cap(self):
        write_signers(self.config.link_signers_file, self.key("mac").signer, self.key("mac2").signer,
                      KeyPair(self.home, "mac3").signer)
        keys = {host: pub(KeyPair(self.home, host)) for host in ("mac4", "mac5")}
        results = {}
        threads = [threading.Thread(target=lambda h=host: results.update({h: self.authorize(h, keys[h])}))
                   for host in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        codes = sorted(code for code, _ in results.values())
        self.assertEqual(codes, [0, 1], {h: o.text() for h, (_, o) in results.items()})
        self.assertEqual(len(read_signers(self.config.link_signers_file)), 4)
        refused = next(out for code, out in results.values() if code)
        self.assertIn("already lists 4 Macs", refused.text())

    def test_written_signers_pass_the_hub_reader(self):
        for host in ("mac3", "mac4"):
            self.assertEqual(self.authorize(host, pub(KeyPair(self.home, host)))[0], 0)
        self.assertEqual(self.revoke("mac3")[0], 0)
        self.assertEqual(sorted(s.host for s in read_signers(self.config.link_signers_file)), [MAC, MAC2, "mac4"])
        self.assertEqual(self.config.link_signers_file.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.hub.links.check_signers()["readable"])
        self.assertEqual(self.links()[MAC]["state"], "up")

    def test_authorize_keeps_other_lines_and_refuses_bad_input(self):
        self.keys_file().parent.mkdir(mode=0o700, exist_ok=True)
        self.keys_file().write_bytes(V1_LINE + b"ssh-ed25519 AAAAOTHER someone\n")
        code, out = self.authorize(MAC, pub(self.key("mac")))
        self.assertEqual(code, 0, out.text())
        lines = self.keys_file().read_bytes().splitlines(keepends=True)
        self.assertEqual(lines[:2], [V1_LINE, b"ssh-ed25519 AAAAOTHER someone\n"])
        self.assertEqual(lines[2], key_line(self.config, self.key("mac").signer))
        self.assertTrue(lines[2].startswith(b'restrict,command="'))
        before = (self.keys_file().read_bytes(), self.config.link_signers_file.read_bytes())
        reused = self.keys_file().read_bytes().replace(b"AAAAOTHER", self.key("mac2").signer.blob.encode())
        self.keys_file().write_bytes(reused)
        before = (reused, before[1])
        for host, raw in (("pi", pub(self.key("mac"))), ("Bad Name", pub(self.key("mac"))),
                          ("mac3", pub(self.key("mac2"))), (MAC, b"not a key\n")):
            with self.subTest(host=host):
                code, out = self.authorize(host, raw)
                self.assertEqual(code, 1, out.text())
                self.assertEqual((self.keys_file().read_bytes(), self.config.link_signers_file.read_bytes()), before)

    def test_revoke_removes_only_its_lines_and_host_authorize_leaves_desk_line(self):
        self.keys_file().parent.mkdir(mode=0o700, exist_ok=True)
        self.keys_file().write_bytes(V1_LINE)
        self.assertEqual(self.authorize(MAC, pub(self.key("mac")))[0], 0)
        self.assertEqual(self.authorize(MAC2, pub(self.key("mac2")))[0], 0)
        host_authorize.authorize(MAC, pub(self.key("stranger")), home=self.config.home)
        self.assertEqual(self.key_blob(MAC), self.key("mac").signer.blob)
        self.assertEqual(self.revoke(MAC)[0], 0)
        self.assertIsNone(self.key_blob(MAC))
        self.assertEqual(self.key_blob(MAC2), self.key("mac2").signer.blob)
        self.assertEqual([s.host for s in read_signers(self.config.link_signers_file)], [MAC2])
        self.assertIn(b"llm-switchboard-host:mac\n", self.keys_file().read_bytes())

    def test_rollback_order_leaves_no_remote_lock(self):
        self.send_m()
        self.mac.expect(LinkType.EVENT)  # written, never reported: it needs presence or clear-stopped
        for host in (MAC, MAC2):
            code, out = self.revoke(host)
            self.assertEqual(code, 0, out.text())
        lock = self.lock_m()
        self.assertEqual(self.hub.handle_operator({"op": "clear_stopped", "session_id": SESSION_M,
                                                   "lock_id": lock["lock_id"], "confirm": lock["lock_id"]}),
                         {"ok": True, "cleared": True})
        self.assertEqual(self.hub.handle_operator({"op": "link_fence"}), {"ok": True, "fenced": True})
        self.assertTrue(all(found.host == "pi" for found in LockStore(self.config.lock_file).load()))


class InterleaveCase(CommandsMixin):
    """Two commands for one host, the second run at a gap of the first."""

    GAPS = ("before_locks", "between_edits", "before_confirm", "after_answer")

    def start_paused(self, first, gap):
        """Run `first` in a thread paused at `gap`; returns (result, resume, thread)."""
        entered, resume, result = threading.Event(), threading.Event(), {}
        first_thread = []

        def pause():
            if threading.current_thread() in first_thread:
                entered.set()
                resume.wait(60)
        patches = []
        if gap == "before_locks":
            original = link_authorize.locked
            patches.append(patch.object(link_authorize, "locked", lambda path: (pause(), original(path))[1]))
        elif gap == "between_edits":
            original_r, original_m = link_authorize.remove_marked_line, link_authorize.replace_marked_line
            patches += [patch.object(link_authorize, "remove_marked_line", lambda *a: (pause(), original_r(*a))[1]),
                        patch.object(link_authorize, "replace_marked_line", lambda *a: (pause(), original_m(*a))[1])]
        else:
            original_drop = link_authorize._link_drop

            def drop(config, host):
                if gap == "before_confirm":
                    pause()
                answer = original_drop(config, host)
                if gap == "after_answer" and answer is not None:
                    pause()
                return answer
            patches.append(patch.object(link_authorize, "_link_drop", drop))
            if not self.with_hub:
                original_offline = link_authorize._offline_signers

                def offline(config):
                    answer = original_offline(config)
                    if gap == "after_answer":
                        pause()
                    return answer
                patches.append(patch.object(link_authorize, "_offline_signers", offline))
        for p in patches:
            p.start()
        thread = threading.Thread(target=lambda: result.update(zip(("code", "out"), first())), daemon=True)
        first_thread.append(thread)
        thread.start()
        self.assertTrue(entered.wait(60))
        return result, resume, thread, patches

    def check_interleaved(self):
        new = pub(self.key("mac-rotated"))
        commands = {"authorize": lambda: self.authorize(MAC, new), "revoke": lambda: self.revoke(MAC)}
        for first_name, second_name in (("authorize", "revoke"), ("revoke", "authorize")):
            for gap in self.GAPS:
                with self.subTest(first=first_name, gap=gap):
                    # Start from the old key listed on both files.
                    self.assertEqual(self.authorize(MAC, pub(self.key("mac")))[0], 0)
                    first, second = commands[first_name], commands[second_name]
                    result, resume, thread, patches = self.start_paused(first, gap)
                    for p in patches:
                        self.addCleanup(p.stop)
                    if gap == "between_edits":
                        later = {}
                        waiter = threading.Thread(target=lambda: later.update(zip(("code", "out"), second())),
                                                  daemon=True)
                        waiter.start()
                        waiter.join(0.3)
                        self.assertTrue(waiter.is_alive())  # it waits for both locks
                        resume.set()
                        thread.join(60)
                        waiter.join(60)
                        # Either may confirm first; the one confirming after both edits is the last one.
                        last = second_name
                        second_code = later["code"]
                    else:
                        second_code, second_out = second()
                        self.assert_one_key(MAC)
                        self.assertEqual(second_code, 0, second_out.text())
                        resume.set()
                        thread.join(60)
                        last = first_name if gap == "before_locks" else second_name
                    self.assert_one_key(MAC)
                    final = "absent" if self.signer_blob(MAC) is None else "listed"
                    self.assertEqual(final, "absent" if last == "revoke" else "listed")
                    if gap in ("before_locks", "after_answer"):
                        self.assertEqual(result["code"], 0, result["out"].text())
                    elif gap == "before_confirm":
                        self.assertEqual(result["code"], 1)
                        text = result["out"].text()
                        if first_name == "revoke":
                            self.assertIn(f"listed key {self.key('mac-rotated').signer.fingerprint}", text)
                        else:
                            self.assertIn("the key this command installed is not listed", text)
                    else:
                        self.assertEqual(second_code, 0)
                    for p in patches:
                        p.stop()


class InterleavedWithHubTest(InterleaveCase, LinkHubCase, HubCase):
    with_hub = True

    def test_authorize_and_revoke_same_host_interleaved(self):
        self.check_interleaved()


class InterleavedWithoutHubTest(InterleaveCase, unittest.TestCase):
    with_hub = False

    def setUp(self):
        self.home = make_home(self)
        self.config = desk_config(self.home)

    def key(self, name):
        if not hasattr(InterleavedWithoutHubTest, "_own"):
            directory = make_home(self)
            InterleavedWithoutHubTest._own = {n: KeyPair(directory, MAC, n) for n in ("mac", "mac-rotated")}
        return InterleavedWithoutHubTest._own[name]

    def test_authorize_and_revoke_same_host_interleaved(self):
        self.check_interleaved()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.config = desk_config(self.home)
        self.key = KeyPair(self.home, MAC)

    def test_self_test_changes_nothing(self):
        before = sorted(p.relative_to(self.home) for p in self.home.rglob("*"))
        done = run_desk_cli(self.home, "link-authorize", "--self-test", input="")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("verifier self-test", done.stdout)
        self.assertEqual(sorted(p.relative_to(self.home) for p in self.home.rglob("*")), before)
        self.assertNotEqual(run_desk_cli(self.home, "link-authorize", "--self-test", MAC, input="").returncode, 0)

    def test_authorize_non_ed25519_refused_before_any_write(self):
        (self.home / ".ssh").mkdir(mode=0o700, exist_ok=True)
        keys = self.home / ".ssh" / "authorized_keys"
        keys.write_bytes(V1_LINE)
        write_signers(self.config.link_signers_file)
        before = (keys.read_bytes(), self.config.link_signers_file.read_bytes())
        for kind in ("rsa", "ecdsa"):
            with self.subTest(kind=kind):
                raw = other_type_key(self.home, kind)
                done = run_desk_cli(self.home, "link-authorize", MAC, input=raw.decode())
                self.assertEqual(done.returncode, 1)
                self.assertIn(ED25519_REFUSAL, done.stderr)
                self.assertEqual((keys.read_bytes(), self.config.link_signers_file.read_bytes()), before)

    def test_no_hub_outcomes_need_run_lock(self):
        raw = pub(self.key).decode()
        with run_lock_held_by_child(self.config):
            done = run_desk_cli(self.home, "link-authorize", MAC, input=raw)
            self.assertEqual(done.returncode, 1)
            self.assertIn(run_lock_refusal(f"link-authorize {MAC}"), done.stderr)
            listed = self.config.link_signers_file.read_bytes()
            done = run_desk_cli(self.home, "link-revoke", MAC, input="")
            self.assertEqual(done.returncode, 1)
            self.assertIn(run_lock_refusal(f"link-revoke {MAC}"), done.stderr)
            self.assertNotEqual(self.config.link_signers_file.read_bytes(), listed)  # the edits stand
            done = run_desk_cli(self.home, "link-fence", input="")
            self.assertEqual(done.returncode, 1)
            self.assertIn(run_lock_refusal("link-fence"), done.stderr)
        done = run_desk_cli(self.home, "link-authorize", MAC, input=raw)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no hub running: no link to drop", done.stdout)
        done = run_desk_cli(self.home, "link-revoke", MAC, input="")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no hub running: no link to drop", done.stdout)
        self.assertEqual(run_desk_cli(self.home, "link-fence", input="").returncode, 0)


if __name__ == "__main__":
    unittest.main()
