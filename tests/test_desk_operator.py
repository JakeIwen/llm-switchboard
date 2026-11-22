"""The running hub over its channel and operator sockets: registration and
refusal, dispatch, replies, rechecks, restarts with held locks, operator ops
and the desk CLI."""

import dataclasses
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from desk_fixtures import (
    SESSION_A, SESSION_B, SESSION_C, FakeIdentity, HubCase, desk_config, make_home, wait_until, write_fake_process,
)

from llm_switchboard.desk.enrollment import Enrollment
from llm_switchboard.desk.hub import INVALID_ID_MARKER
from llm_switchboard.desk.hub_state import CHANNEL_STILL_RUNNING
from llm_switchboard.desk.labels import SessionLabels
from llm_switchboard.desk.model import (
    LABEL_HEAD_BYTES, LABEL_TAIL_BYTES, MAX_OPERATOR_CONNECTIONS, MAX_OPERATOR_LINE_BYTES, DeskError,
)
from llm_switchboard.desk.operator import OperatorOp, SocketInUse, bind_private_socket, operator_request
from llm_switchboard.desk.peer import read_process
from llm_switchboard.desk.protocol import DELIVERY_SUFFIX, MAX_REPLY_TEXT_BYTES, MessageType, encode

REPO = Path(__file__).resolve().parents[1]


def run_desk_cli(home, *args):
    """`llm-switchboard desk ...` as a separate process under this HOME."""
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": "", "XDG_STATE_HOME": "",
           "XDG_RUNTIME_DIR": "", "LLM_SWITCHBOARD_DESK_PORT": ""}
    return subprocess.run([sys.executable, "-m", "llm_switchboard", "desk", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=30)


class ChannelHubCase(HubCase):
    def operator(self, op, **fields):
        return operator_request(self.config.operator_socket, op, **fields)

    def registered(self, session_id):
        client = self.channel(session_id)
        kind, welcome = client.first
        self.assertIs(kind, MessageType.WELCOME, welcome)
        return client, welcome["generation"]

    def send(self, session_id, generation, text="what changed?"):
        ticket = self.hub.ticket({"session_id": session_id, "generation": generation, "epoch": self.hub.epoch},
                                 self.token)
        return self.hub.send({"ticket": ticket["ticket"], "text": text}, self.token)

    def assertRefused(self, status, reason, call, *args):
        with self.assertRaises(DeskError) as caught:
            call(*args)
        self.assertEqual((caught.exception.status, caught.exception.reason), (status, reason))


class RegistrationTest(ChannelHubCase):
    def test_welcome_carries_the_epoch_and_a_hub_issued_generation(self):
        _, welcome = self.channel(SESSION_A).first
        self.assertEqual(welcome["epoch"], self.hub.epoch)
        self.assertEqual(self.target(SESSION_A)["registration"], "registered")
        self.assertEqual(self.target(SESSION_A)["generation"], welcome["generation"])

    def test_spoofed_identity_unenrolled_and_bad_hellos_are_refused_and_closed(self):
        self.peer.refuse[SESSION_A] = "cmdline is not the pinned channel"
        cases = [(SESSION_A, "cmdline is not the pinned channel"), (SESSION_C, "not enrolled"),
                 ("../../etc", "bad hello")]
        for session_id, reason in cases:
            with self.subTest(reason=reason):
                client = self.channel(session_id)
                self.assertEqual(client.first, (MessageType.REFUSED, {"v": 1, "type": "refused", "reason": reason}))
                self.assertEqual(client.read(), (None, {}))
        self.assertEqual(self.target(SESSION_A)["registration"], "none")
        self.assertEqual(self.operator(OperatorOp.STATUS)["registrations"], [])

    def test_two_connections_for_one_session_are_ambiguous_until_one_leaves(self):
        first, generation = self.registered(SESSION_A)
        second, later = self.registered(SESSION_A)
        self.assertEqual(self.target(SESSION_A)["registration"], "ambiguous")
        for gen in (generation, later):
            self.assertRefused(409, "ambiguous", self.hub.ticket,
                               {"session_id": SESSION_A, "generation": gen, "epoch": self.hub.epoch}, self.token)
        first.close()
        wait_until(lambda: self.target(SESSION_A)["registration"] == "registered")
        self.assertEqual(self.send(SESSION_A, later)["state"], "handed_off")

    def test_labels_come_from_the_transcript_title_else_the_folder(self):
        project = self.home / ".claude" / "projects" / "-work-van"
        project.mkdir(parents=True)
        records = [{"type": "user", "cwd": "/work/van", "message": {"content": "first words"}},
                   {"type": "custom-title", "customTitle": "Van wiring", "sessionId": SESSION_A}]
        (project / f"{SESSION_A}.jsonl").write_text("".join(json.dumps(r, separators=(",", ":")) + "\n"
                                                           for r in records))
        self.hub.recheck_now()
        self.assertEqual((self.target(SESSION_A)["label"], self.target(SESSION_A)["cwd"]), ("Van wiring", "/work/van"))
        self.assertEqual(self.target(SESSION_B)["label"], SESSION_B[:8])

    def test_labels_read_only_a_bounded_head_and_tail_and_are_cached_by_file_identity(self):
        project = self.home / ".claude" / "projects" / "-work-van"
        project.mkdir(parents=True)
        path = project / f"{SESSION_A}.jsonl"

        def line(record):
            return json.dumps(record, separators=(",", ":")) + "\n"
        filler = line({"type": "assistant", "message": {"content": "x" * 1000}})
        middle = line({"type": "custom-title", "customTitle": "Middle title"})
        with open(path, "w") as f:
            f.write(line({"type": "user", "cwd": "/work/van", "message": {"content": "first"}}))
            f.write(filler * (LABEL_HEAD_BYTES // len(filler) + 2))
            f.write(middle)
            f.write(filler * (LABEL_TAIL_BYTES // len(filler) + 2))
            f.write(line({"type": "ai-title", "aiTitle": "Tail title"}))
        labels = SessionLabels(self.config)
        # The middle custom title would win a whole-file read; it is never read.
        self.assertEqual(labels.describe(SESSION_A), ("Tail title", "/work/van"))
        with mock.patch("llm_switchboard.desk.labels.read_title_and_cwd") as read:
            self.assertEqual(labels.describe(SESSION_A), ("Tail title", "/work/van"))
            read.assert_not_called()
        with open(path, "a") as f:
            f.write(line({"type": "custom-title", "customTitle": "Renamed"}))
        self.assertEqual(labels.describe(SESSION_A), ("Renamed", "/work/van"))

    def test_the_state_api_reads_no_transcripts(self):
        self.hub.recheck_now()
        with mock.patch.object(SessionLabels, "describe", side_effect=AssertionError("read on the request path")):
            view = self.hub.state_view()
        self.assertEqual({t["label"] for t in view["targets"]}, {SESSION_A[:8], SESSION_B[:8]})

    def test_the_real_peer_check_refuses_a_process_that_is_not_the_pinned_channel(self):
        self.hub.close()
        self.peer = None
        self.hub = self.start_hub()
        kind, refused = self.channel(SESSION_A).first
        self.assertIs(kind, MessageType.REFUSED)
        self.assertTrue(refused["reason"])
        self.assertEqual(self.target(SESSION_A)["registration"], "none")

    def test_ping_gets_pong_and_a_bad_frame_drops_the_connection(self):
        client, _ = self.registered(SESSION_A)
        client.send(MessageType.PING)
        self.assertEqual(client.read()[0], MessageType.PONG)
        client.send_raw(b"not json\n")
        self.assertEqual(client.read(), (None, {}))
        wait_until(lambda: self.target(SESSION_A)["registration"] == "none")

    def test_a_stale_socket_file_is_replaced_but_a_live_one_is_not(self):
        with self.assertRaises(SocketInUse):
            bind_private_socket(self.config.channel_socket)
        stale = self.config.runtime_dir / "stale.sock"
        leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        leftover.bind(str(stale))
        leftover.close()
        listener = bind_private_socket(stale)
        self.addCleanup(listener.close)
        self.assertEqual(stat.S_IMODE(os.stat(stale).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.config.channel_socket).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.config.operator_socket).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.config.runtime_dir).st_mode), 0o700)


class DispatchTest(ChannelHubCase):
    def test_send_writes_the_event_and_the_first_valid_reply_wins(self):
        client, generation = self.registered(SESSION_A)
        other, _ = self.registered(SESSION_B)
        sent = self.send(SESSION_A, generation, "what changed?")
        request_id = sent["request_id"]
        self.assertEqual(sent["state"], "handed_off")
        self.assertTrue(request_id.startswith(self.hub.epoch + "."))
        kind, event = client.read()
        self.assertIs(kind, MessageType.EVENT)
        self.assertEqual(event["content"], "what changed?" + DELIVERY_SUFFIX.format(request_id=request_id))
        client.send(MessageType.DELIVERED, request_id=request_id)
        replies = [
            (other, {"request_id": request_id, "text": "x", "spoken": "x"}, "wrong connection"),
            (client, {"request_id": "nope", "text": "x", "spoken": "x"}, "unknown request"),
            (client, {"request_id": request_id, "text": "y" * (MAX_REPLY_TEXT_BYTES + 1), "spoken": "x"},
             "reply too large"),
            (client, {"request_id": request_id, "text": "the answer", "spoken": "Short."}, None),
            (client, {"request_id": request_id, "text": "again", "spoken": "again"}, "request not outstanding"),
        ]
        for sender, fields, reason in replies:
            with self.subTest(reason=reason):
                sender.send(MessageType.REPLY, **fields)
                kind, result = sender.read()
                self.assertIs(kind, MessageType.REPLY_RESULT)
                self.assertEqual((result["ok"], result["reason"]), (reason is None, reason))
        [request] = [r for r in self.hub.state_view()["requests"] if r["request_id"] == request_id]
        self.assertEqual(request["reply"], {"text": "the answer", "spoken": "Short."})
        self.assertEqual(self.target(SESSION_A)["registration"], "replied")
        self.assertIsNone(self.target(SESSION_A)["lock"])

    def test_disconnect_after_handoff_leaves_an_unknown_locked_request(self):
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        client.close()
        wait_until(lambda: self.hub.state.request_state(request_id) == "unknown")
        self.assertEqual(self.target(SESSION_A)["lock"]["request_id"], request_id)
        _, generation = self.registered(SESSION_A)
        self.assertRefused(409, "busy", self.hub.ticket,
                           {"session_id": SESSION_A, "generation": generation, "epoch": self.hub.epoch}, self.token)

    def test_a_failed_recheck_turns_a_handed_off_request_unknown_and_keeps_its_lock(self):
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        client.read()
        # /clear: the session file now names another session.
        self.peer.recheck_failures[SESSION_A] = "session file names another session"
        self.hub.recheck_now()
        self.assertEqual(self.hub.state.request_state(request_id), "unknown")
        self.assertEqual(self.target(SESSION_A)["registration"], "none")
        self.assertEqual(self.target(SESSION_A)["lock"]["request_id"], request_id)
        self.assertEqual(client.read(), (None, {}))

    def test_a_failed_dispatch_recheck_writes_nothing_and_expires_the_request(self):
        client, generation = self.registered(SESSION_A)
        ticket = self.hub.ticket({"session_id": SESSION_A, "generation": generation, "epoch": self.hub.epoch},
                                 self.token)
        self.peer.recheck_failures[SESSION_A] = "parent start time changed"
        self.assertRefused(409, "target changed; not sent", self.hub.send,
                           {"ticket": ticket["ticket"], "text": "hi"}, self.token)
        [request] = self.hub.state_view()["requests"]
        self.assertEqual(request["state"], "expired")
        self.assertIsNone(self.target(SESSION_A)["lock"])
        self.assertEqual(client.read(), (None, {}))

    def test_unenroll_stops_routing_from_the_api_at_once_and_from_the_cli_at_the_next_recheck(self):
        client, _ = self.registered(SESSION_A)
        self.hub.unenroll({"session_id": SESSION_A}, self.token)
        self.assertNotIn(SESSION_A, [t["session_id"] for t in self.hub.state_view()["targets"]])
        self.assertEqual(client.read(), (None, {}))
        other, _ = self.registered(SESSION_B)
        Enrollment(self.config.enrolled_file).unenroll(SESSION_B)
        self.assertEqual(self.operator(OperatorOp.STATUS)["registrations"][0]["session_id"], SESSION_B)
        self.hub.recheck_now()
        self.assertEqual(self.operator(OperatorOp.STATUS)["registrations"], [])
        self.assertEqual(other.read(), (None, {}))


class DeliveryGuardTest(ChannelHubCase):
    def test_a_lock_stays_unclearable_until_the_channel_reports_delivery(self):
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        # The hub's socket write is done; the channel holds the event without
        # having written it to Claude yet.
        self.assertIs(client.read()[0], MessageType.EVENT)
        lock_id = self.target(SESSION_A)["lock"]["lock_id"]
        clear = {"session_id": SESSION_A, "lock_id": lock_id}
        self.assertEqual(self.operator(OperatorOp.CLEAR, **clear), {"ok": False, "error": "delivery in progress"})
        self.assertRefused(409, "busy", self.hub.ticket,
                           {"session_id": SESSION_A, "generation": generation, "epoch": self.hub.epoch}, self.token)
        client.send(MessageType.DELIVERED, request_id=request_id)
        wait_until(lambda: self.operator(OperatorOp.CLEAR, **clear)["ok"])

    def test_a_lost_delivery_report_resolves_through_disconnect_as_unknown(self):
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        client.read()
        lock_id = self.target(SESSION_A)["lock"]["lock_id"]
        client.close()
        wait_until(lambda: self.hub.state.request_state(request_id) == "unknown")
        self.assertEqual(self.operator(OperatorOp.CLEAR, session_id=SESSION_A, lock_id=lock_id),
                         {"ok": True, "cleared": True})


class StuckChannelTest(ChannelHubCase):
    """A channel blocked writing an event to Claude can't answer heartbeats;
    the hub drops it at the idle timeout, but the process may still write the
    event later, so its lock can't be cleared while that process runs."""

    # Well above the few milliseconds from registration to the send.
    IDLE_TIMEOUT = 2.0

    def stuck_after_handoff(self, identity):
        self.hub.close()
        self.hub = self.start_hub(idle_timeout_seconds=self.IDLE_TIMEOUT)
        self.peer.identities[SESSION_A] = identity
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        self.assertIs(client.read()[0], MessageType.EVENT)
        # The channel now stops reading and answering, its socket left open.
        wait_until(lambda: self.hub.state.request_state(request_id) == "unknown", timeout=10)
        self.assertEqual(self.target(SESSION_A)["registration"], "none")
        lock = self.target(SESSION_A)["lock"]
        self.assertEqual(lock["request_id"], request_id)
        return {"session_id": SESSION_A, "lock_id": lock["lock_id"]}

    def assertClearRefused(self, clear):
        self.assertEqual(self.operator(OperatorOp.CLEAR, **clear), {"ok": False, "error": CHANNEL_STILL_RUNNING})
        self.assertRefused(409, CHANNEL_STILL_RUNNING, self.hub.clear, clear, self.token)

    def test_a_clear_waits_for_the_timed_out_channel_process_to_be_gone(self):
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        # The process tree stands in for /proc; the child is the channel's pid.
        write_fake_process(self.config.proc_dir, child.pid, "4242")
        clear = self.stuck_after_handoff(FakeIdentity(child.pid, "4242", child.pid + 1, "2", SESSION_A))
        self.assertClearRefused(clear)
        # Another process reusing the pid (another start time) is not that channel.
        write_fake_process(self.config.proc_dir, child.pid, "9999")
        self.assertEqual(self.operator(OperatorOp.CLEAR, **clear), {"ok": True, "cleared": True})
        self.assertIsNone(self.target(SESSION_A)["lock"])

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc to see a real process's start time")
    def test_a_clear_waits_for_a_real_channel_process_to_exit(self):
        self.config = dataclasses.replace(self.config, proc_dir=Path("/proc"))
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        record = read_process(Path("/proc"), child.pid)
        clear = self.stuck_after_handoff(FakeIdentity(child.pid, record.start, child.pid + 1, "2", SESSION_A))
        self.assertClearRefused(clear)
        child.kill()
        child.wait(timeout=10)
        self.assertEqual(self.operator(OperatorOp.CLEAR, **clear), {"ok": True, "cleared": True})


class OnlineEnrollmentCliTest(ChannelHubCase):
    def test_cli_unenroll_goes_through_the_running_hub_and_stops_routing_at_once(self):
        client, generation = self.registered(SESSION_A)
        ticket = {"session_id": SESSION_A, "generation": generation, "epoch": self.hub.epoch}
        self.assertTrue(self.hub.ticket(ticket, self.token))
        done = run_desk_cli(self.home, "unenroll", SESSION_A)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("unenrolled", done.stdout)
        # No recheck ran: the hub changed the file and its state together.
        self.assertEqual(self.operator(OperatorOp.STATUS)["registrations"], [])
        self.assertEqual(client.read(), (None, {}))
        self.assertRefused(409, "not enrolled", self.hub.ticket, ticket, self.token)
        self.assertFalse(Enrollment(self.config.enrolled_file).is_enrolled(SESSION_A))
        again = run_desk_cli(self.home, "unenroll", SESSION_A)
        self.assertEqual((again.returncode, again.stdout.split()[1:]), (0, ["was", "not", "enrolled"]))

    def test_cli_enroll_goes_through_the_running_hub(self):
        done = run_desk_cli(self.home, "enroll", SESSION_C)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"{SESSION_C} enrolled", done.stdout)
        self.assertTrue(self.target(SESSION_C)["enrolled"])
        self.assertEqual(self.operator(OperatorOp.STATUS)["enrolled"], sorted([SESSION_A, SESSION_B, SESSION_C]))
        refused = run_desk_cli(self.home, "enroll", "bogus")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("bad session_id", refused.stderr)


class ReplyRecheckTest(ChannelHubCase):
    def test_a_reply_after_the_session_changed_is_refused_and_keeps_the_lock(self):
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        client.read()
        client.send(MessageType.DELIVERED, request_id=request_id)
        # /clear: the session file names another session; housekeeping hasn't run.
        self.peer.recheck_failures[SESSION_A] = "session file names another session"
        client.send(MessageType.REPLY, request_id=request_id, text="answer", spoken="answer")
        kind, result = client.read()
        self.assertEqual((kind, result["ok"], result["reason"]), (MessageType.REPLY_RESULT, False, "session changed"))
        # The hub refuses before it drops the registration: wait for the drop.
        self.assertEqual(client.read(), (None, {}))
        self.assertEqual(self.hub.state.request_state(request_id), "unknown")
        self.assertEqual(self.target(SESSION_A)["lock"]["request_id"], request_id)


class PreHandoffFailureTest(ChannelHubCase):
    def test_an_error_before_handoff_expires_the_request_and_frees_the_target(self):
        _, generation = self.registered(SESSION_A)
        with mock.patch.object(self.hub, "refresh_files", side_effect=OSError("settings unreadable")):
            with self.assertRaises(OSError):
                self.send(SESSION_A, generation)
        [request] = self.hub.state_view()["requests"]
        self.assertEqual(request["state"], "expired")
        self.assertIsNone(self.target(SESSION_A)["lock"])
        self.assertEqual(self.send(SESSION_A, generation)["state"], "handed_off")


class PolicyRaceTest(ChannelHubCase):
    def test_a_stale_settings_read_is_never_published_over_a_newer_sends_off(self):
        reading, go = threading.Event(), threading.Event()
        real = self.hub.settings.sends_enabled

        def slow_read():
            value = real()  # reads "on"
            reading.set()
            go.wait(5)
            return value
        with mock.patch.object(self.hub.settings, "sends_enabled", side_effect=slow_read):
            refresher = threading.Thread(target=self.hub.refresh_files)
            refresher.start()
            reading.wait(5)
            operator = threading.Thread(target=self.hub.handle_operator, args=({"op": "sends", "enabled": False},))
            operator.start()
            # Unserialized, the operator's change lands now and the refresh's
            # stale "on" overwrites it below; serialized, it waits.
            operator.join(0.5)
            go.set()
            refresher.join(5)
            operator.join(5)
        self.assertFalse(self.hub.state.snapshot()["sends_enabled"])
        self.assertEqual(json.loads(self.config.settings_file.read_text()), {"sends_enabled": False})


class LabelFailureTest(ChannelHubCase):
    def test_a_malformed_transcript_degrades_its_label_and_nothing_else(self):
        project = self.home / ".claude" / "projects" / "-work-van"
        project.mkdir(parents=True)
        records = [{"type": "user", "cwd": "/work/van", "message": "a bare string"},
                   {"type": "user", "message": {"content": [{"type": "text", "text": 5}]}}]
        (project / f"{SESSION_A}.jsonl").write_text("".join(json.dumps(r) .replace(" ", "") + "\n" for r in records))
        self.hub.close()
        self.hub = self.start_hub()
        self.assertEqual(self.target(SESSION_A)["label"], "van")
        self.assertTrue(self.operator(OperatorOp.STATUS)["ok"])

    def test_a_label_error_at_registration_leaves_the_connection_served(self):
        with mock.patch.object(SessionLabels, "describe", side_effect=RuntimeError("boom")):
            with self.assertLogs("llm_switchboard.desk", "WARNING"):
                client, _ = self.registered(SESSION_A)
            client.send(MessageType.PING)
            self.assertIs(client.read()[0], MessageType.PONG)
        self.assertEqual(self.target(SESSION_A)["label"], SESSION_A[:8])
        self.assertEqual(self.target(SESSION_A)["registration"], "registered")


class UndeliverableTest(ChannelHubCase):
    def test_undeliverable_makes_the_request_unknown_keeps_the_lock_and_rechecks_at_once(self):
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        self.assertIs(client.read()[0], MessageType.EVENT)
        # /clear: the channel's own check failed, and the hub's recheck fails too.
        self.peer.recheck_failures[SESSION_A] = "session file names another session"
        client.send(MessageType.UNDELIVERABLE, request_id=request_id, reason="session changed")
        wait_until(lambda: self.hub.state.request_state(request_id) == "unknown")
        self.assertEqual(self.target(SESSION_A)["lock"]["request_id"], request_id)
        self.assertEqual(client.read(), (None, {}))
        wait_until(lambda: self.target(SESSION_A)["registration"] == "none")

    def test_undeliverable_with_a_passing_recheck_keeps_the_connection(self):
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        client.read()
        client.send(MessageType.UNDELIVERABLE, request_id=request_id, reason="session changed")
        wait_until(lambda: self.hub.state.request_state(request_id) == "unknown")
        client.send(MessageType.PING)
        self.assertIs(client.read()[0], MessageType.PONG)
        self.assertEqual(self.target(SESSION_A)["registration"], "registered")
        self.assertEqual(self.target(SESSION_A)["lock"]["request_id"], request_id)

    def test_undeliverable_for_another_connections_request_changes_nothing(self):
        client, generation = self.registered(SESSION_A)
        other, _ = self.registered(SESSION_B)
        request_id = self.send(SESSION_A, generation)["request_id"]
        other.send(MessageType.UNDELIVERABLE, request_id=request_id, reason="session changed")
        other.send(MessageType.PING)
        self.assertIs(other.read()[0], MessageType.PONG)
        self.assertEqual(self.hub.state.request_state(request_id), "handed_off")


class JournalTest(ChannelHubCase):
    def test_an_invalid_request_id_from_the_channel_is_never_logged(self):
        client, generation = self.registered(SESSION_A)
        request_id = self.send(SESSION_A, generation)["request_id"]
        client.read()
        private = "my private words about the van"
        with self.assertLogs("llm_switchboard.desk", "INFO") as logs:
            client.send(MessageType.DELIVERED, request_id=private)
            client.send(MessageType.UNDELIVERABLE, request_id=private, reason=private + "!")
            client.send(MessageType.REPLY, request_id=private, text="t", spoken="s")
            self.assertIs(client.read()[0], MessageType.REPLY_RESULT)
            client.send(MessageType.REPLY, request_id=request_id, text="t", spoken="s")
            self.assertIs(client.read()[0], MessageType.REPLY_RESULT)
        journal = "\n".join(logs.output)
        self.assertNotIn("private", journal)
        self.assertIn(INVALID_ID_MARKER, journal)
        self.assertIn(request_id, journal)


class RestartTest(ChannelHubCase):
    def test_a_prompt_held_behind_a_dialog_survives_a_hub_restart_until_cleared_by_its_lock_id(self):
        client, generation = self.registered(SESSION_A)
        first = self.send(SESSION_A, generation)["request_id"]
        client.read()
        old_lock = self.target(SESSION_A)["lock"]["lock_id"]
        client.send(MessageType.REPLY, request_id=first, text="ok", spoken="ok")
        client.read()
        held = self.send(SESSION_A, generation)["request_id"]
        self.assertEqual(client.read()[0], MessageType.EVENT)
        held_lock = self.target(SESSION_A)["lock"]["lock_id"]
        # The event was written but never answered; the hub restarts.
        old_epoch = self.hub.epoch
        self.hub.close()
        client.close()
        self.hub = self.start_hub()
        self.assertNotEqual(self.hub.epoch, old_epoch)
        lock = self.target(SESSION_A)["lock"]
        self.assertEqual((lock["lock_id"], lock["request_id"], lock["earlier_run"]), (held_lock, held, True))
        client, generation = self.registered(SESSION_A)
        ticket = {"session_id": SESSION_A, "generation": generation, "epoch": self.hub.epoch}
        self.assertRefused(409, "busy", self.hub.ticket, ticket, self.token)
        # The channel answering the earlier run's request is not this connection's request.
        client.send(MessageType.REPLY, request_id=held, text="late", spoken="late")
        self.assertEqual(client.read()[1]["reason"], "unknown request")
        refused = self.operator(OperatorOp.CLEAR, session_id=SESSION_A, lock_id=old_lock)
        self.assertEqual(refused, {"ok": False, "error": "lock id does not match"})
        self.assertRefused(409, "busy", self.hub.ticket, ticket, self.token)
        self.assertEqual(self.operator(OperatorOp.CLEAR, session_id=SESSION_A, lock_id=held_lock),
                         {"ok": True, "cleared": True})
        self.assertEqual(self.send(SESSION_A, generation)["state"], "handed_off")


class OperatorTest(ChannelHubCase):
    def test_status_lists_ids_and_never_text(self):
        token = self.pair()
        client, generation = self.registered(SESSION_A)
        self.send(SESSION_A, generation, "secret prompt words")
        status = self.operator(OperatorOp.STATUS)
        self.assertTrue(status["ok"])
        self.assertEqual(status["epoch"], self.hub.epoch)
        self.assertEqual([r["session_id"] for r in status["registrations"]], [SESSION_A])
        self.assertEqual(len(status["locks"]), 1)
        # The tablet's token and the one this test calls the hub with.
        self.assertEqual(len(status["tokens"]), 2)
        self.assertEqual(status["enrolled"], sorted([SESSION_A, SESSION_B]))
        dumped = json.dumps(status)
        self.assertNotIn("secret prompt", dumped)
        self.assertNotIn(token, dumped)

    def test_sends_persist_and_revoke_one_or_all(self):
        self.assertEqual(self.operator(OperatorOp.SENDS, enabled=False), {"ok": True, "sends_enabled": False})
        self.assertEqual(json.loads(self.config.settings_file.read_text()), {"sends_enabled": False})
        self.assertFalse(self.hub.state_view()["sends_enabled"])
        self.assertFalse(self.operator(OperatorOp.SENDS, enabled="yes")["ok"])
        tokens = [self.pair(), self.pair()]
        first_id = self.operator(OperatorOp.STATUS)["tokens"][0]["token_id"]
        self.assertEqual(self.operator(OperatorOp.REVOKE, token_id=first_id)["revoked"], 1)
        self.assertFalse(self.hub.authenticate(tokens[0]))
        self.assertTrue(self.hub.authenticate(tokens[1]))
        self.assertEqual(self.operator(OperatorOp.REVOKE)["revoked"], 1)
        self.assertFalse(self.hub.authenticate(tokens[1]))

    def test_unknown_ops_and_bad_lines_are_refused(self):
        self.assertEqual(self.hub.handle_operator({"op": "shutdown"}), {"ok": False, "error": "unknown op"})
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(5)
        raw.connect(str(self.config.operator_socket))
        raw.sendall(b"[1,2]\n")
        self.assertEqual(json.loads(raw.makefile("rb").readline()), {"ok": False, "error": "bad request"})
        raw.close()
        self.assertEqual(self.operator(OperatorOp.PAIR, code="ZZZ-ZZZ"), {"ok": False, "error": "no such pairing"})


class OperatorAdmissionTest(ChannelHubCase):
    def test_connections_are_capped_and_a_busy_hub_answers_at_once(self):
        idle = []
        for _ in range(MAX_OPERATOR_CONNECTIONS):
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(str(self.config.operator_socket))
            idle.append(conn)
        self.addCleanup(lambda: [c.close() for c in idle])
        started = time.monotonic()
        self.assertEqual(wait_until(lambda: (r := self.operator(OperatorOp.STATUS, timeout=2)) and not r["ok"] and r),
                         {"ok": False, "error": "busy"})
        self.assertLess(time.monotonic() - started, 2)
        for conn in idle:
            conn.close()
        wait_until(lambda: self.operator(OperatorOp.STATUS)["ok"])

    def test_an_oversize_request_line_is_refused(self):
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(5)
        self.addCleanup(raw.close)
        raw.connect(str(self.config.operator_socket))
        line = json.dumps({"op": "status", "pad": "x" * MAX_OPERATOR_LINE_BYTES}).encode() + b"\n"
        raw.sendall(line)
        self.assertEqual(json.loads(raw.makefile("rb").readline()), {"ok": False, "error": "bad request"})
        with self.assertRaisesRegex(ValueError, "too large"):
            self.operator(OperatorOp.PAIR, code="x" * MAX_OPERATOR_LINE_BYTES)


class DeskCliTest(unittest.TestCase):
    def run_cli(self, home, *args):
        return run_desk_cli(home, *args)

    def test_file_commands_work_without_a_hub(self):
        home = make_home(self)
        config = desk_config(home)
        flags = self.run_cli(home, "flags")
        self.assertEqual(flags.returncode, 0, flags.stderr)
        self.assertEqual(flags.stdout.split(), [*config.launch_flags])
        installed = self.run_cli(home, "install-config")
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(config.mcp_config).st_mode), 0o600)
        argv = config.channel_argv
        self.assertEqual(json.loads(config.mcp_config.read_text()),
                         {"mcpServers": {"desk": {"command": argv[0], "args": list(argv[1:])}}})
        self.assertEqual(self.run_cli(home, "enroll", SESSION_A).returncode, 0)
        self.assertTrue(Enrollment(config.enrolled_file).is_enrolled(SESSION_A))
        self.assertEqual(self.run_cli(home, "enroll", "bogus").returncode, 1)
        self.assertEqual(self.run_cli(home, "unenroll", SESSION_A).returncode, 0)
        self.assertFalse(Enrollment(config.enrolled_file).is_enrolled(SESSION_A))
        status = self.run_cli(home, "status")
        self.assertEqual(status.returncode, 3)
        self.assertIn("not running", status.stderr)
        sends = self.run_cli(home, "sends", "on")
        self.assertEqual(sends.returncode, 0, sends.stderr)
        self.assertEqual(json.loads(config.settings_file.read_text()), {"sends_enabled": True})

    def test_serve_refuses_to_start_without_the_tls_leaf(self):
        home = make_home(self)
        served = self.run_cli(home, "serve")
        self.assertNotEqual(served.returncode, 0)
        self.assertIn("leaf.pem", served.stderr)
        self.assertFalse(desk_config(home).channel_socket.exists())


if __name__ == "__main__":
    unittest.main()
