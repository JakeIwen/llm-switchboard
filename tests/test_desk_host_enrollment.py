"""Mac enrollment on the hub (docs/voice-desk.md, "Enrollment for Mac
sessions", "Targets, tickets, requests, locks and replies"): the one
published view over `enrolled.json` and `enrolled-hosts.json`, one owner
per session id, the edits' checks online and offline, tickets bound to a
host, unenroll stopping routing at once, and Enroll now for a running Mac
session, with a real hub driven by the test-side relay in tests/link_peer.py."""

import json
import logging
import os
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

from desk_fixtures import (
    SESSION_A, SESSION_B, SESSION_C, Clock, desk_config, enroll_file, make_home, request_id, run_desk_cli,
    wait_until,
)
from link_peer import MAC, MAC2, MACHINE, write_host_enrollment
from test_desk_link_channels import SESSION_M, MacTargetCase

from llm_switchboard.desk.enrollment import DeskSettings, Enrollment
from llm_switchboard.session_names import MAX_TITLE_LEN
from llm_switchboard.desk.link_candidates import Candidates, candidate_folder
from llm_switchboard.desk.link_protocol import LinkType
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import (
    CANDIDATE_TTL_SECONDS, MAX_CANDIDATE_FOLDER, MAX_CANDIDATE_LABEL, MAX_CANDIDATES, DeskError, Lock,
)
from llm_switchboard.desk.protocol import MessageType
from llm_switchboard.desk.refusals import Refusal
from llm_switchboard.desk.remote_enrollment import ENROLL_NOW_NOTICE, HostEnrollment

SESSION_X = "eeeeeeee-0000-4000-8000-000000000005"
# Two ways a policy file can't be trusted: not JSON, and JSON of the wrong shape.
CORRUPTIONS = ("{not json", json.dumps({"other": {}}))


def two_owner_file(path: Path, session_id: str, *hosts: str) -> None:
    """`enrolled-hosts.json` listing `session_id` under each of `hosts`, every
    nested record well formed (a hand edit can do this; the desk never does)."""
    path.write_text(json.dumps({"hosts": {host: {session_id: {"since": 1}} for host in hosts}}))
    path.chmod(0o600)


def file_bytes(*paths: Path) -> list[bytes | None]:
    return [path.read_bytes() if path.exists() else None for path in paths]


def mac_lock(config, session_id: str, fingerprint: str, host: str = MAC) -> Lock:
    """A Mac lock written to `locks.json` as a stopped hub would have left it."""
    lock = Lock("0123456789abcdef", host, session_id, request_id(1), "f" * 64, 1_800_000_000.0, 4321, "777",
                fingerprint, MACHINE)
    store = LockStore(config.lock_file)
    store.load()
    store.add(lock)
    return lock


class OwnershipTest(MacTargetCase):
    """SESSION_A and SESSION_B are the Pi's; SESSION_M is `mac`'s, registered on chan 1."""

    def ticket(self, session_id: str, **extra) -> dict:
        return self.hub.ticket({"session_id": session_id, "generation": self.target(session_id)["generation"],
                                "epoch": self.hub.epoch, **extra}, self.token)

    def refused(self, call, *args, **kwargs) -> DeskError:
        with self.assertRaises(DeskError) as caught:
            call(*args, **kwargs)
        return caught.exception

    def test_registration_host_must_match_enrollment(self):
        # A Mac register for a Pi id, and a Pi channel for a Mac id.
        self.assertEqual(self.mac.register(2, SESSION_A)["reason"], "enrolled_on_another_host")
        self.assertEqual(self.channel(SESSION_M).first, (MessageType.REFUSED,
                                                         {"v": 1, "type": "refused", "reason": "not enrolled"}))
        self.assertEqual(self.channel(SESSION_A).first[0], MessageType.WELCOME)
        # The state's own check: a registration whose host isn't the view's never routes.
        self.hub.state.register(9000, SESSION_B, None, host=MAC)
        self.assertEqual(self.refused(self.ticket, SESSION_B).reason, Refusal.ENROLLED_ON_ANOTHER_HOST)
        self.assertIn(SESSION_A, {r.session_id for r in self.hub.state.registrations() if r.host == "pi"})

    def test_enrollment_published_atomically(self):
        """Moving an id from the Pi's file to the Mac's under enrolled.json's
        lock: a refresh that has read one file can't publish before the edit
        of the other, and every view names exactly one host for the id."""
        seen, stop = [], threading.Event()

        def watch():
            while not stop.is_set():
                target = self.target(SESSION_B)
                seen.append((target["enrolled"], target["enrolled_on_two_hosts"], self.hub.remote_owner(SESSION_B)))
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        self.addCleanup(stop.set)
        paused, resume = threading.Event(), threading.Event()
        real_hosts = self.hub.host_enrollment.hosts

        def hosts_then_pause():
            found = real_hosts()
            paused.set()
            resume.wait(30)
            return found
        edited = threading.Event()

        def move():
            with Enrollment(self.config.enrolled_file).locked():
                enroll_file(self.config, SESSION_A)
                write_host_enrollment(self.config.remote_enrolled_file, {SESSION_M: MAC, SESSION_B: MAC})
            edited.set()
        with mock.patch.object(self.hub.host_enrollment, "hosts", hosts_then_pause):
            refresher = threading.Thread(target=self.hub.refresh_files, daemon=True)
            refresher.start()
            self.assertTrue(paused.wait(30))
            editor = threading.Thread(target=move, daemon=True)
            editor.start()
            # The edit waits for the lock the refresh holds across both reads.
            self.assertFalse(edited.wait(0.5))
            resume.set()
            refresher.join(30)
        editor.join(30)
        self.assertTrue(edited.is_set())
        self.assertEqual(self.hub.remote_owner(SESSION_B), "pi")
        self.hub.refresh_files()
        self.assertEqual(self.hub.remote_owner(SESSION_B), MAC)
        wait_until(lambda: len(seen) > 2 and seen[-1][2] == MAC, timeout=30)
        stop.set()
        watcher.join(30)
        self.assertEqual({entry for entry in seen}, {(True, False, "pi"), (True, False, MAC)})

    def test_id_in_both_files_enrolled_nowhere(self):
        channel = self.channel(SESSION_A)
        self.assertEqual(channel.first[0], MessageType.WELCOME)
        ticket = self.ticket(SESSION_A)["ticket"]
        self.remote[SESSION_A] = MAC
        self.hub.refresh_files()
        target = self.target(SESSION_A)
        self.assertEqual((target["enrolled"], target["enrolled_on_two_hosts"]), (False, True))
        self.assertIsNone(self.hub.remote_owner(SESSION_A))
        # Nothing routes to it: not the Pi registration still connected,
        # not the Mac, not a new Pi channel, not a ticket issued before.
        self.assertEqual(self.refused(self.ticket, SESSION_A).reason, Refusal.ENROLLED_ON_TWO_HOSTS)
        self.assertEqual(self.refused(self.hub.send, {"ticket": ticket, "text": "x"}, self.token).reason,
                         Refusal.ENROLLED_ON_TWO_HOSTS)
        self.assertEqual(self.mac.register(2, SESSION_A)["reason"], "enrolled_on_another_host")
        self.assertEqual(self.channel(SESSION_A).first[0], MessageType.REFUSED)
        self.assertEqual(LockStore(self.config.lock_file).load(), [])
        # An unenroll that leaves one owner makes it routable again.
        self.hub.handle_operator({"op": "unenroll_host", "session_id": SESSION_A, "host": MAC})
        self.assertEqual(self.hub.remote_owner(SESSION_A), "pi")
        self.assertIn("ticket", self.ticket(SESSION_A))

    def test_id_under_two_remote_hosts_unroutable(self):
        ticket = self.ticket(SESSION_M)["ticket"]
        two_owner_file(self.config.remote_enrolled_file, SESSION_M, MAC, MAC2)
        self.hub.refresh_files()
        self.assertIn(SESSION_M, self.hub.state._conflicts)
        target = self.target(SESSION_M)
        self.assertEqual((target["enrolled"], target["enrolled_on_two_hosts"]), (False, True))
        self.assertEqual(self.refused(self.ticket, SESSION_M).reason, Refusal.ENROLLED_ON_TWO_HOSTS)
        self.assertEqual(self.refused(self.hub.send, {"ticket": ticket, "text": "x"}, self.token).reason,
                         Refusal.ENROLLED_ON_TWO_HOSTS)
        self.assertEqual(self.mac.register(2, SESSION_M)["reason"], "enrolled_on_another_host")
        mac2 = self.connect("mac2", MAC2)
        self.assertEqual(mac2.register(1, SESSION_M)["reason"], "enrolled_on_another_host")
        self.assertEqual(LockStore(self.config.lock_file).load(), [])
        # The recheck drops the registration the view no longer routes.
        self.hub.recheck_now()
        self.assertEqual(self.mac.expect(LinkType.DROP)["chan"], 1)

    def test_two_mac_registrations_for_one_id_are_ambiguous(self):
        self.assertEqual(self.mac.register(2, SESSION_M)["type"], "registered")
        self.assertEqual(len([r for r in self.hub.state.registrations() if r.session_id == SESSION_M]), 2)
        with self.assertRaises(DeskError) as caught:
            self.hub.ticket({"session_id": SESSION_M, "generation": 1, "epoch": self.hub.epoch}, self.token)
        self.assertEqual(caught.exception.reason, Refusal.AMBIGUOUS)

    def test_ticket_bound_to_host(self):
        # `host`, when given, must be the target's.
        self.assertEqual(self.refused(self.ticket, SESSION_M, host="pi").reason, Refusal.WRONG_HOST)
        self.assertEqual(self.refused(self.ticket, SESSION_M, host="Not A Host").reason, Refusal.BAD_HOST)
        self.assertIn("ticket", self.ticket(SESSION_M, host=MAC))
        # A ticket issued for the Mac's registration never sends to the
        # session once it is the Pi's, whichever registration then exists.
        ticket = self.ticket(SESSION_M)["ticket"]
        self.hub.handle_operator({"op": "unenroll_host", "session_id": SESSION_M, "host": MAC})
        self.assertTrue(self.hub.handle_operator({"op": "enroll", "session_id": SESSION_M})["ok"])
        self.assertEqual(self.channel(SESSION_M).first[0], MessageType.WELCOME)
        self.assertEqual(self.target(SESSION_M)["host"], "pi")
        error = self.refused(self.hub.send, {"ticket": ticket, "text": "x"}, self.token)
        self.assertEqual(error.status, 409)
        self.assertEqual(self.hub.state_view()["requests"], [])
        self.assertIn("ticket", self.ticket(SESSION_M, host="pi"))

    def test_unenroll_enqueues_drop_and_stops_routing_before_it_returns(self):
        answer = self.hub.handle_operator({"op": "unenroll_host", "session_id": SESSION_M, "host": MAC})
        self.assertEqual((answer["ok"], answer["changed"]), (True, True))
        # Before anything else runs: no registration, nothing routes.
        self.assertEqual(self.hub.state.registrations(), [])
        self.assertIsNone(self.hub.remote_owner(SESSION_M))
        with self.assertRaises(DeskError) as caught:
            self.hub.ticket({"session_id": SESSION_M, "generation": 1, "epoch": self.hub.epoch}, self.token)
        self.assertEqual(caught.exception.reason, Refusal.NOT_ENROLLED)
        self.assertEqual(self.mac.expect(LinkType.DROP)["chan"], 1)
        self.assertEqual(json.loads(self.config.remote_enrolled_file.read_text()), {"hosts": {}})
        # The same running channel retries and is refused; it isn't offered
        # as a candidate while it was just unenrolled.
        self.assertEqual(self.mac.register(2, SESSION_M)["reason"], "not_enrolled")
        self.assertEqual(self.hub.state_view()["candidates"], [])

    def test_mac_unenroll_keeps_the_pi_registration_of_a_pi_id(self):
        self.assertEqual(self.channel(SESSION_A).first[0], MessageType.WELCOME)
        self.assertFalse(self.hub.handle_operator({"op": "unenroll_host", "session_id": SESSION_A,
                                                   "host": MAC})["changed"])
        self.assertEqual([r.session_id for r in self.hub.state.registrations() if r.host == "pi"], [SESSION_A])


def unreadable(path: Path):
    """Takes read permission away; returns the restore."""
    path.chmod(0)
    return lambda: path.chmod(0o600)


def corrupt_keeping_stat_key(path: Path):
    """Rewrites `path` in place as same-length garbage with its mtime put
    back, so its mtime, size and inode are what they were; returns the restore."""
    original, before = path.read_bytes(), path.stat()
    path.write_bytes(b"x" + original[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    return lambda: path.write_bytes(original)


def wrong_shape(path: Path):
    """Rewrites `path` as JSON that isn't an object; returns the restore."""
    original = path.read_bytes()
    path.write_text("[]")
    return lambda: path.write_bytes(original)


ROOT_READS_ANY_MODE = os.geteuid() == 0, "root reads a file whatever its mode"


def request_ids(hub) -> set[str]:
    return {r["request_id"] for r in hub.state_view()["requests"]}


def new_request_states(hub, seen: set[str]) -> list[str]:
    """States of the requests created since `seen`; the snapshot lists newest
    first, so asking by id keeps a check on the send's own request."""
    return [r["state"] for r in hub.state_view()["requests"] if r["request_id"] not in seen]


class RoutingReadTest(MacTargetCase):
    """Routing reads both enrollment files now, never content kept from an
    earlier read: a file that fails its read routes nothing it governs, and
    the next good read routes again without a restart."""

    def ticket(self, session_id: str) -> str:
        return self.hub.ticket({"session_id": session_id, "generation": self.target(session_id)["generation"],
                                "epoch": self.hub.epoch}, self.token)["ticket"]

    def refused_send(self, ticket: str) -> DeskError:
        with self.assertRaises(DeskError) as caught:
            self.hub.send({"ticket": ticket, "text": "x"}, self.token)
        return caught.exception

    def test_a_file_that_fails_its_read_routes_nothing_it_governs_until_read_again(self):
        self.assertEqual(self.channel(SESSION_A).first[0], MessageType.WELCOME)
        governed = ((self.config.remote_enrolled_file, SESSION_M), (self.config.enrolled_file, SESSION_A))
        for path, session_id in governed:
            for breaker in (unreadable, corrupt_keeping_stat_key):
                with self.subTest(file=path.name, how=breaker.__name__):
                    if breaker is unreadable and ROOT_READS_ANY_MODE[0]:
                        self.skipTest(ROOT_READS_ANY_MODE[1])
                    self.hub.refresh_files()
                    ticket = self.ticket(session_id)
                    restore = breaker(path)
                    try:
                        seen = request_ids(self.hub)
                        error = self.refused_send(ticket)
                        self.assertEqual((error.status, error.reason), (409, Refusal.NOT_ENROLLED))
                        self.assertEqual(new_request_states(self.hub, seen), ["expired"])
                        self.assertEqual(LockStore(self.config.lock_file).load(), [])
                        self.assertIsNone(self.hub.remote_owner(session_id))
                        with self.assertRaises(DeskError) as caught:
                            self.ticket(session_id)
                        self.assertEqual(caught.exception.reason, Refusal.NOT_ENROLLED)
                    finally:
                        restore()
                    # The next good read routes again, with no restart.
                    self.hub.refresh_files()
                    self.assertIn(self.hub.remote_owner(session_id), ("pi", MAC))
                    self.ticket(session_id)
        self.assertEqual(self.send_m()["state"], "handed_off")
        self.assertEqual(self.mac.expect(LinkType.EVENT)["chan"], 1)

    def test_an_unreadable_pi_file_routes_no_id_to_a_mac(self):
        """Without `enrolled.json` no Mac id's single owner can be checked:
        an id listed in both files must not become the Mac's."""
        self.remote[SESSION_A] = MAC
        self.hub.refresh_files()
        self.assertTrue(self.hub.enrolled_on_two_hosts(SESSION_A))
        ticket = self.ticket(SESSION_M)
        self.config.enrolled_file.write_text(CORRUPTIONS[0])
        self.hub.refresh_files()
        self.assertEqual((self.hub.remote_owner(SESSION_A), self.hub.remote_owner(SESSION_M)), (None, None))
        self.assertEqual(self.mac.register(2, SESSION_A)["reason"], "not_enrolled")
        self.assertEqual(self.refused_send(ticket).reason, Refusal.NOT_ENROLLED)
        enroll_file(self.config, SESSION_A, SESSION_B)
        self.hub.refresh_files()
        self.assertTrue(self.hub.enrolled_on_two_hosts(SESSION_A))
        self.assertEqual(self.hub.remote_owner(SESSION_M), MAC)

    @unittest.skipIf(*ROOT_READS_ANY_MODE)
    def test_a_failed_read_keeps_the_lock_of_a_prompt_that_may_still_run(self):
        sent = self.send_m()
        self.assertEqual(self.mac.expect(LinkType.EVENT)["request_id"], sent["request_id"])
        [lock] = LockStore(self.config.lock_file).load()
        restore = unreadable(self.config.remote_enrolled_file)
        try:
            self.hub.recheck_now()
            # The registration goes as an unenroll's would; the lock stays.
            self.assertEqual(self.mac.expect(LinkType.DROP)["chan"], 1)
            self.assertEqual(LockStore(self.config.lock_file).load(), [lock])
            self.assertNotIn(self.hub.state.request_state(sent["request_id"]), ("expired", None))
        finally:
            restore()
        self.hub.refresh_files()
        self.assertEqual(LockStore(self.config.lock_file).load(), [lock])
        self.assertEqual(self.target(SESSION_M)["lock"]["lock_id"], lock.lock_id)


class SendsReadTest(MacTargetCase):
    """The sends switch is established at each handoff from a read of
    `desk-settings.json` that succeeded now (docs/voice-desk.md, Files:
    anything unreadable reads as both off), for every route a prompt takes."""

    def ticket(self, session_id: str) -> str:
        return self.hub.ticket({"session_id": session_id, "generation": self.target(session_id)["generation"],
                                "epoch": self.hub.epoch}, self.token)["ticket"]

    def send_body(self, session_id: str, confirm: str) -> dict:
        body = {"ticket": self.ticket(session_id), "text": "x"}
        if confirm == "voice":
            grant = self.hub.conversation({"session_id": session_id, "epoch": self.hub.epoch,
                                           "generation": self.target(session_id)["generation"]}, self.token)
            body.update(confirm="voice", grant=grant["grant"])
        return body

    def switches(self) -> tuple[bool, bool, bool]:
        """What the page's snapshot and `desk status` show, and what a
        handoff's own read would decide."""
        return (self.hub.state_view()["sends_enabled"],
                self.hub.handle_operator({"op": "status"})["sends_enabled"],
                self.hub.settings.sends_enabled())

    def test_a_settings_file_that_fails_its_read_is_sends_off_at_handoff_until_read_again(self):
        self.assertEqual(self.channel(SESSION_A).first[0], MessageType.WELCOME)
        self.hub.handle_operator({"op": "voice", "enabled": True})
        routes = [(session_id, confirm) for session_id in (SESSION_A, SESSION_M) for confirm in ("tap", "voice")]
        for breaker in (unreadable, corrupt_keeping_stat_key):
            for session_id, confirm in routes:
                with self.subTest(how=breaker.__name__, session=session_id[:8], confirm=confirm):
                    if breaker is unreadable and ROOT_READS_ANY_MODE[0]:
                        self.skipTest(ROOT_READS_ANY_MODE[1])
                    self.hub.refresh_files()
                    self.assertEqual(self.switches(), (True, True, True))
                    body = self.send_body(session_id, confirm)
                    restore = breaker(self.config.settings_file)
                    try:
                        seen = request_ids(self.hub)
                        with self.assertRaises(DeskError) as caught:
                            self.hub.send(body, self.token)
                        self.assertEqual((caught.exception.status, caught.exception.reason),
                                         (403, Refusal.SENDS_OFF))
                        self.assertEqual(new_request_states(self.hub, seen), ["expired"])
                        self.assertEqual(LockStore(self.config.lock_file).load(), [])
                        self.assertEqual(self.switches(), (False, False, False))
                    finally:
                        restore()
                    # The next good read turns sends back on, with no restart.
                    self.hub.refresh_files()
                    self.assertEqual(self.switches(), (True, True, True))
        self.assertEqual(self.send_m()["state"], "handed_off")
        self.assertEqual(self.mac.expect(LinkType.EVENT)["chan"], 1)


class FailedReadLogTest(unittest.TestCase):
    """Every read of a policy file is fresh, but a file that stays broken
    logs one warning when it breaks and one line when it reads again, per
    file and per process, however often it is read in between: the hub
    rereads every 5 s and at each handoff, the picker at each poll and the
    supervisor at each launch, and the Pi's log is on an SD card."""

    def setUp(self):
        self.config = desk_config(make_home(self))
        enroll_file(self.config, SESSION_A)
        write_host_enrollment(self.config.remote_enrolled_file, {SESSION_M: MAC})
        DeskSettings(self.config.settings_file).set_sends_enabled(True)

    def readers(self):
        """(file, a read as each of its readers makes it): the hub's long-lived
        object and a new one per read, as the supervisor builds them."""
        config = self.config
        hub_enrollment, hub_hosts, hub_settings = (
            Enrollment(config.enrolled_file), HostEnrollment(config.remote_enrolled_file, config.enrolled_file),
            DeskSettings(config.settings_file))
        return [
            (config.enrolled_file, lambda: hub_enrollment.sessions()),
            (config.enrolled_file, lambda: Enrollment(config.enrolled_file).is_enrolled(SESSION_A)),
            (config.remote_enrolled_file, lambda: hub_hosts.hosts()),
            (config.remote_enrolled_file,
             lambda: HostEnrollment(config.remote_enrolled_file, config.enrolled_file).hosts()),
            (config.settings_file, lambda: hub_settings.sends_enabled()),
            (config.settings_file, lambda: hub_settings.voice_confirm_enabled()),
        ]

    def logged(self, reads) -> list[str]:
        with self.assertLogs("llm_switchboard.desk", "INFO") as logs:
            for read in reads:
                read()
            logging.getLogger("llm_switchboard.desk").info("end")
        return [f"{r.levelname} {r.getMessage()}" for r in logs.records][:-1]

    def test_a_file_that_stays_broken_logs_once_until_it_reads_again(self):
        for breaker in (unreadable, corrupt_keeping_stat_key, wrong_shape):
            for path in (self.config.enrolled_file, self.config.remote_enrolled_file, self.config.settings_file):
                with self.subTest(how=breaker.__name__, file=path.name):
                    if breaker is unreadable and ROOT_READS_ANY_MODE[0]:
                        self.skipTest(ROOT_READS_ANY_MODE[1])
                    reads = [read for file, read in self.readers() if file == path]
                    restore = breaker(path)
                    try:
                        lines = self.logged(reads * 50)
                    finally:
                        restore()
                    self.assertEqual(len(lines), 1, lines)
                    self.assertRegex(lines[0], rf"^WARNING desk: {path.name} (unreadable|malformed)")
                    self.assertEqual(self.logged(reads * 50), [f"INFO desk: {path.name} reads again"])
                    # Breaking again is a new break, and logs again, once.
                    restore = breaker(path)
                    try:
                        self.assertEqual(len(self.logged(reads * 3)), 1)
                    finally:
                        restore()
                    self.logged(reads)


class HubFailedReadLogTest(MacTargetCase):
    def test_the_hub_s_rereads_of_a_file_that_stays_broken_log_once(self):
        for path in (self.config.enrolled_file, self.config.remote_enrolled_file, self.config.settings_file):
            with self.subTest(file=path.name):
                self.hub.refresh_files()
                restore = corrupt_keeping_stat_key(path)
                try:
                    with self.assertLogs("llm_switchboard.desk", "INFO") as logs:
                        for _ in range(20):
                            self.hub.recheck_now()
                            self.hub.refresh_files()
                finally:
                    restore()
                with self.assertLogs("llm_switchboard.desk", "INFO") as recovered:
                    self.hub.refresh_files()
                    self.hub.refresh_files()
                self.assertEqual([r.levelname for r in logs.records if path.name in r.getMessage()], ["WARNING"])
                self.assertEqual([r.getMessage() for r in recovered.records if path.name in r.getMessage()],
                                 [f"desk: {path.name} reads again"])


class EnrollEditTest(MacTargetCase):
    """The edit rule, online through the hub and offline through the CLI."""

    def operator(self, op: str, session_id: str, host: str | None = None) -> dict:
        return self.hub.handle_operator({"op": op, "session_id": session_id, **({"host": host} if host else {})})

    def offline_home(self) -> tuple[Path, object]:
        home = make_home(self)
        config = desk_config(home)
        enroll_file(config, SESSION_A)
        write_host_enrollment(config.remote_enrolled_file, {SESSION_M: MAC})
        return home, config

    def enroll_cli(self, home, session_id: str, host: str | None = None):
        return run_desk_cli(home, "enroll", session_id, *(["--host", host] if host else []))

    def test_enroll_refused_when_either_file_unreadable(self):
        files = (self.config.enrolled_file, self.config.remote_enrolled_file)
        for broken in files:
            for corruption in CORRUPTIONS:
                for host, op in ((None, "enroll"), (MAC, "enroll_host")):
                    with self.subTest("online", file=broken.name, corruption=corruption, host=host):
                        saved = file_bytes(*files)
                        broken.write_text(corruption)
                        before = file_bytes(*files)
                        answer = self.operator(op, SESSION_C, host)
                        self.assertEqual(answer, {"ok": False, "error": Refusal.ENROLLMENT_FILES_UNREADABLE})
                        self.assertEqual(file_bytes(*files), before)
                        for path, content in zip(files, saved):
                            path.write_bytes(content)
        home, config = self.offline_home()
        offline_files = (config.enrolled_file, config.remote_enrolled_file)
        for broken in offline_files:
            for host in (None, MAC):
                with self.subTest("offline", file=broken.name, host=host):
                    saved = file_bytes(*offline_files)
                    broken.write_text(CORRUPTIONS[0])
                    before = file_bytes(*offline_files)
                    done = self.enroll_cli(home, SESSION_C, host)
                    self.assertEqual(done.returncode, 1, done.stderr)
                    self.assertIn(Refusal.ENROLLMENT_FILES_UNREADABLE, done.stderr)
                    self.assertEqual(file_bytes(*offline_files), before)
                    for path, content in zip(offline_files, saved):
                        path.write_bytes(content)
        # With both readable, both forms go through.
        self.assertEqual(self.enroll_cli(home, SESSION_C).returncode, 0)
        self.assertEqual(self.enroll_cli(home, SESSION_X, MAC).returncode, 0)

    def test_enroll_checks_owners_registrations_and_locks(self):
        # An owner on another host in either file.
        self.assertEqual(self.operator("enroll_host", SESSION_A, MAC)["error"], Refusal.ENROLLED_ON_ANOTHER_HOST)
        self.assertEqual(self.operator("enroll", SESSION_M)["error"], Refusal.ENROLLED_ON_ANOTHER_HOST)
        self.assertEqual(self.operator("enroll_host", SESSION_M, MAC2)["error"], Refusal.ENROLLED_ON_ANOTHER_HOST)
        # A live registration from another host: the Pi channel of an id
        # whose line was removed by hand, before any refresh drops it.
        self.assertEqual(self.channel(SESSION_B).first[0], MessageType.WELCOME)
        enroll_file(self.config, SESSION_A)
        self.assertEqual(self.operator("enroll_host", SESSION_B, MAC)["error"], Refusal.ENROLLED_ON_ANOTHER_HOST)
        # A lock on another host: a Mac send's lock outlives its unenroll.
        self.send_m()
        self.mac.expect(LinkType.EVENT)
        self.assertTrue(self.operator("unenroll_host", SESSION_M, MAC)["ok"])
        self.assertIsNotNone(self.lock_m())
        self.assertEqual(self.operator("enroll", SESSION_M)["error"], Refusal.ENROLLED_ON_ANOTHER_HOST)
        self.assertEqual(self.operator("enroll_host", SESSION_M, MAC2)["error"], Refusal.ENROLLED_ON_ANOTHER_HOST)
        self.assertEqual(json.loads(self.config.remote_enrolled_file.read_text()), {"hosts": {}})
        # The lock's own host may enroll it again.
        self.assertTrue(self.operator("enroll_host", SESSION_M, MAC)["ok"])

        home, config = self.offline_home()
        files = (config.enrolled_file, config.remote_enrolled_file)
        before = file_bytes(*files)
        for session_id, host in ((SESSION_A, MAC), (SESSION_M, None), (SESSION_M, MAC2)):
            with self.subTest("offline owner", session_id=session_id, host=host):
                done = self.enroll_cli(home, session_id, host)
                self.assertEqual(done.returncode, 1)
                self.assertIn(Refusal.ENROLLED_ON_ANOTHER_HOST, done.stderr)
        mac_lock(config, SESSION_X, self.key("mac").signer.fingerprint)
        for host in (None, MAC2):
            with self.subTest("offline lock", host=host):
                done = self.enroll_cli(home, SESSION_X, host)
                self.assertEqual(done.returncode, 1)
                self.assertIn(Refusal.ENROLLED_ON_ANOTHER_HOST, done.stderr)
        self.assertEqual(file_bytes(*files), before)
        # A FIFO at locks.json (no writer) refuses at once, never opening it to wait.
        config.lock_file.unlink()
        os.mkfifo(config.lock_file, 0o600)
        started = time.monotonic()
        done = self.enroll_cli(home, SESSION_X, MAC)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(Refusal.LOCKS_UNREADABLE, done.stderr)
        self.assertLess(time.monotonic() - started, 25)
        self.assertEqual(file_bytes(*files), before)

    def test_unenroll_needs_only_its_file(self):
        self.config.remote_enrolled_file.write_text(CORRUPTIONS[0])
        self.assertTrue(self.operator("unenroll", SESSION_A)["changed"])
        write_host_enrollment(self.config.remote_enrolled_file, {SESSION_M: MAC})
        self.config.enrolled_file.write_text(CORRUPTIONS[1])
        self.assertTrue(self.operator("unenroll_host", SESSION_M, MAC)["changed"])
        # Its own file unreadable refuses, changing nothing.
        self.assertFalse(self.operator("unenroll", SESSION_B)["ok"])

        home, config = self.offline_home()
        config.remote_enrolled_file.write_text(CORRUPTIONS[0])
        done = run_desk_cli(home, "unenroll", SESSION_A)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Enrollment(config.enrolled_file).sessions(), {})
        write_host_enrollment(config.remote_enrolled_file, {SESSION_M: MAC})
        config.enrolled_file.write_text(CORRUPTIONS[0])
        done = run_desk_cli(home, "unenroll", SESSION_M, "--host", MAC)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"{SESSION_M} unenrolled on {MAC}", done.stdout)
        self.assertEqual(HostEnrollment(config.remote_enrolled_file, config.enrolled_file).hosts(), {})

    def test_phase1_op_with_host_refused_by_new_hub(self):
        files = (self.config.enrolled_file, self.config.remote_enrolled_file)
        before = file_bytes(*files)
        for op, session_id in (("enroll", SESSION_C), ("unenroll", SESSION_A), ("unenroll", SESSION_M)):
            with self.subTest(op=op):
                self.assertEqual(self.operator(op, session_id, MAC), {"ok": False, "error": Refusal.USE_ENROLL_HOST})
                self.assertEqual(self.operator(op, session_id, "pi")["error"], Refusal.USE_ENROLL_HOST)
        self.assertEqual(file_bytes(*files), before)
        self.assertEqual(self.mac.register(2, SESSION_C)["reason"], "not_enrolled")
        # `pi` never rides the host-qualified ops.
        self.assertEqual(self.operator("enroll_host", SESSION_C, "pi")["error"], Refusal.BAD_HOST)

    def test_phase1_route_with_host_refused(self):
        token = self.pair()
        files = (self.config.enrolled_file, self.config.remote_enrolled_file)
        before = file_bytes(*files)
        for path, session_id in (("/desk/api/enroll", SESSION_C), ("/desk/api/unenroll", SESSION_A)):
            with self.subTest(path=path):
                status, _, body = self.call("POST", path, {"session_id": session_id, "host": MAC}, token=token)
                self.assertEqual((status, body), (400, {"error": Refusal.USE_ENROLL_HOST}))
        self.assertEqual(file_bytes(*files), before)
        # The host-qualified routes are the way.
        status, _, body = self.call("POST", "/desk/api/enroll-host", {"session_id": SESSION_C, "host": MAC},
                                    token=token)
        self.assertEqual((status, body), (200, {"session_id": SESSION_C, "host": MAC, "enrolled": True}))
        status, _, body = self.call("POST", "/desk/api/unenroll-host", {"session_id": SESSION_C, "host": MAC},
                                    token=token)
        self.assertEqual((status, body), (200, {"session_id": SESSION_C, "host": MAC, "enrolled": False}))
        self.assertEqual(self.call("POST", "/desk/api/enroll-host", {"session_id": SESSION_C, "host": MAC})[0], 401)


class EnrollNowTest(MacTargetCase):
    def test_enroll_running_mac_candidate_reachable_without_relaunch(self):
        token = self.pair()
        reply = self.mac.register(2, SESSION_X, channel_pid=4500, channel_start="800", label="deploy notes",
                                  cwd="/Users/me/dev/project")
        self.assertEqual(reply["reason"], "not_enrolled")
        status, _, view = self.call("GET", "/desk/api/state", token=token)
        [candidate] = view["candidates"]
        self.assertEqual({k: candidate[k] for k in ("host", "session_id", "label", "folder")},
                         {"host": MAC, "session_id": SESSION_X, "label": "deploy notes", "folder": "dev/project"})
        [listed] = self.hub.handle_operator({"op": "status"})["candidates"]
        self.assertNotIn("folder", listed)
        status, _, body = self.call("POST", "/desk/api/enroll-host", {"session_id": SESSION_X, "host": MAC},
                                    token=token)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.hub.state_view()["candidates"], [])
        # The same running channel's next attempt, nothing done on the Mac.
        self.assertEqual(self.mac.register(3, SESSION_X, channel_pid=4500, channel_start="800",
                                           label="deploy notes")["type"], "registered")
        target = self.target(SESSION_X)
        ticket = self.hub.ticket({"session_id": SESSION_X, "generation": target["generation"],
                                  "epoch": self.hub.epoch}, self.token)["ticket"]
        sent = self.hub.send({"ticket": ticket, "text": "ship it?"}, self.token)
        event = self.mac.expect(LinkType.EVENT)
        self.assertEqual((event["chan"], event["request_id"]), (3, sent["request_id"]))

    def test_enroll_now_wording_page_and_cli(self):
        """The CLI half: online and offline, `enroll --host` prints the
        Enroll now notice; the Pi's enroll keeps the next-launch wording."""
        done = run_desk_cli(self.home, "enroll", SESSION_X, "--host", MAC)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(ENROLL_NOW_NOTICE, done.stdout)
        self.assertEqual(self.hub.remote_owner(SESSION_X), MAC)
        home = make_home(self)
        done = run_desk_cli(home, "enroll", SESSION_C, "--host", MAC)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(ENROLL_NOW_NOTICE, done.stdout)
        done = run_desk_cli(home, "enroll", SESSION_B)
        self.assertIn("next launch", done.stdout)
        self.assertNotIn(ENROLL_NOW_NOTICE, done.stdout)
        done = run_desk_cli(home, "enroll", SESSION_B, "--host", "pi")
        self.assertEqual(done.returncode, 1)
        self.assertIn(Refusal.BAD_HOST, done.stderr)


class CandidatesThroughHubTest(MacTargetCase):
    def test_candidates_capped_and_cut_through_the_hub(self):
        sessions = [f"{n:08x}-0000-4000-8000-{n:012x}" for n in range(1, 19)]
        for chan, session in enumerate(sessions, start=10):
            self.assertEqual(self.mac.register(chan, session, label="L" * MAX_TITLE_LEN, cwd="/a/b/" + "c" * 200)["reason"],
                             "not_enrolled")
        listed = self.hub.state_view()["candidates"]
        self.assertEqual(len(listed), 16)
        # The two seen first are the ones evicted.
        self.assertEqual({c["session_id"] for c in listed}, set(sessions[2:]))
        self.assertTrue(all(len(c["label"]) == 80 and len(c["folder"]) == 120 for c in listed))
        self.assertTrue(all(c["folder"].startswith("b/") for c in listed))
        # Never a candidate for any other refusal.
        self.assertEqual(self.mac.register(40, SESSION_A)["reason"], "enrolled_on_another_host")
        self.assertNotIn(SESSION_A, {c["session_id"] for c in self.hub.state_view()["candidates"]})


def numbered(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


class CandidatesTest(unittest.TestCase):
    """The table itself, on a fake wall clock."""

    def setUp(self):
        self.clock = Clock()
        self.candidates = Candidates(self.clock)

    def ids(self) -> list[str]:
        return [entry["session_id"] for entry in self.candidates.view()]

    def test_cap_evicts_the_least_recently_seen(self):
        for n in range(MAX_CANDIDATES):
            self.candidates.seen(MAC, numbered(n), "", "")
            self.clock.advance(1)
        self.candidates.seen(MAC, numbered(0), "", "")  # refreshed: now the most recent
        self.candidates.seen(MAC, numbered(99), "", "")
        self.assertEqual(len(self.ids()), MAX_CANDIDATES)
        self.assertNotIn(numbered(1), self.ids())
        self.assertIn(numbered(0), self.ids())
        self.assertEqual(self.ids(), sorted(self.ids()))

    def test_entries_expire_after_their_last_attempt(self):
        self.candidates.seen(MAC, numbered(1), "a", "")
        self.candidates.seen(MAC2, numbered(2), "b", "")
        self.clock.advance(CANDIDATE_TTL_SECONDS - 1)
        self.candidates.seen(MAC, numbered(1), "a again", "")
        self.clock.advance(1)
        self.assertEqual([(e["host"], e["session_id"], e["label"]) for e in self.candidates.view()],
                         [(MAC, numbered(1), "a again")])
        self.assertEqual(self.candidates.view()[0]["seen"], self.clock() - 1)
        self.clock.advance(CANDIDATE_TTL_SECONDS)
        self.assertEqual(self.candidates.view(), [])

    def test_fields_are_cut(self):
        self.candidates.seen(MAC, numbered(1), "\U00010000" * 200, "/Users/me/" + "x" * 50 + "/" + "y" * 200)
        [entry] = self.candidates.view()
        self.assertEqual(set(entry), {"host", "session_id", "label", "folder", "seen"})
        self.assertEqual(len(entry["label"]), MAX_CANDIDATE_LABEL)
        self.assertEqual(entry["folder"], ("x" * 50 + "/" + "y" * 200)[:MAX_CANDIDATE_FOLDER])
        self.assertEqual(candidate_folder("/Users/me/dev/project/"), "dev/project")
        self.assertEqual(candidate_folder("/top"), "top")
        self.assertEqual(candidate_folder(""), "")

    def test_recently_unenrolled_not_listed(self):
        self.candidates.seen(MAC, numbered(1), "", "")
        self.candidates.unenrolled(MAC, numbered(1))
        self.assertEqual(self.ids(), [])
        self.candidates.seen(MAC, numbered(1), "", "")
        self.assertEqual(self.ids(), [])
        # Only for that host.
        self.candidates.seen(MAC2, numbered(1), "", "")
        self.assertEqual([e["host"] for e in self.candidates.view()], [MAC2])
        self.clock.advance(CANDIDATE_TTL_SECONDS)
        self.candidates.seen(MAC, numbered(1), "", "")
        self.assertEqual([e["host"] for e in self.candidates.view()], [MAC])

    def test_unenrolled_table_is_capped_oldest_evicted(self):
        for n in range(MAX_CANDIDATES + 1):
            self.candidates.unenrolled(MAC, numbered(n))
            self.clock.advance(1)
        self.candidates.seen(MAC, numbered(0), "", "")
        self.candidates.seen(MAC, numbered(1), "", "")
        self.assertEqual(self.ids(), [numbered(0)])

    def test_enrolled_removes_the_entry(self):
        self.candidates.seen(MAC, numbered(1), "", "")
        self.candidates.seen(MAC2, numbered(1), "", "")
        self.candidates.enrolled(MAC, numbered(1))
        self.assertEqual([e["host"] for e in self.candidates.view()], [MAC2])
