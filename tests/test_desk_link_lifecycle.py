"""What crosses a retired link, and what loss proves about the target lock."""

from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import threading
from unittest.mock import patch

from desk_fixtures import Clock, SESSION_A, request_id, wait_until
from link_peer import MAC, numbered_session, write_signers
from test_desk_link_channels import MacTargetCase, SESSION_M

from llm_switchboard.desk.link_admission import refusal_frame
from llm_switchboard.desk.link_protocol import (
    LINK_CODES, MAX_LINK_REGISTRATIONS, MAX_REGISTERS_PER_MINUTE, LinkType, RefusedCode, Side,
    UndeliverableCode, decode_frame,
)
from llm_switchboard.desk.link_pump import no_hub_frame
from llm_switchboard.desk.link_writer import SocketTarget
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import DeskError, ReplyRefusal
from llm_switchboard.desk.protocol import MessageType
from llm_switchboard.desk.refusals import Refusal


@contextlib.contextmanager
def partial_event_writer():
    """Let the real old socket accept a prefix, then suspend that writer alone."""
    accepted, resume = threading.Event(), threading.Event()
    original = SocketTarget.send

    def send(target, view):
        if b'"event"' not in view[:64].tobytes():
            return original(target, view)
        count = original(target, view[:3])
        accepted.set()
        if not resume.wait(30):
            raise AssertionError('event writer never released')
        return count

    with patch.object(SocketTarget, 'send', send), patch('llm_switchboard.desk.link_session.SEND_GUARD_SECONDS', 60):
        try:
            yield accepted
        finally:
            resume.set()


class FixedLinkClockCase(MacTargetCase):
    def setUp(self):
        self.link_clock = Clock()
        self.link_options = {'clock': self.link_clock, 'tick_seconds': 3600}
        super().setUp()


class LinkFencingTest(FixedLinkClockCase):
    def test_replacement_with_event_write_waiting(self):
        with ThreadPoolExecutor(1) as pool:
            with partial_event_writer() as prefix:
                pending = pool.submit(self.send_m)
                self.assertTrue(prefix.wait(10))
                self.assertEqual(len(self.mac.sock.recv(3)), 3)
                successor = self.connect()
                self.assertEqual(successor.register(2, SESSION_M)['type'], 'registered')
            sent = pending.result(10)
        self.assertEqual(sent['state'], 'unknown')
        self.assertEqual(self.lock_m()['request_id'], sent['request_id'])
        # A ping fences the successor's writer: it never replayed the old event.
        successor.send(LinkType.PING, n=1)
        self.assertEqual(successor.expect(LinkType.PONG), {'n': 1})
        pending = self.clear_in_thread()
        q = successor.expect(LinkType.PRESENCE_QUERY)
        successor.send(LinkType.PRESENCE, query_id=q['query_id'], state='running')
        pending['thread'].join(10)
        self.assertEqual(pending['error'].reason, Refusal.CHANNEL_STILL_RUNNING)

    def test_remote_close_never_blocks(self):
        reg, = self.hub.state.registrations()
        proxy = self.hub.links.live_link(MAC).channel(reg.identity.chan)
        with ThreadPoolExecutor(2) as pool:
            with partial_event_writer() as prefix:
                write = pool.submit(self.send_m)
                self.assertTrue(prefix.wait(10))
                # Must return before the preceding event can finish writing.
                pool.submit(proxy.close, 'unenrolled').result(10)
                self.assertFalse(write.done())
            self.assertEqual(write.result(10)['state'], 'handed_off')
        self.mac.expect(LinkType.EVENT)
        self.assertEqual(self.mac.expect(LinkType.DROP), {'chan': 1, 'reason': 'unenrolled'})


class LateFrameTest(FixedLinkClockCase):
    def test_late_event_and_very_late_presence_keep_link(self):
        old = self.unknown_after_disconnect()
        self.hub.links.presence_seconds = 0
        with self.assertRaises(DeskError) as refused:
            self.clear_m()
        self.assertEqual(refused.exception.reason, Refusal.CHANNEL_UNVERIFIABLE)
        q = self.mac.expect(LinkType.PRESENCE_QUERY)
        other = numbered_session(2)
        self.remote[other] = MAC
        self.hub.refresh_files()
        registered = self.mac.register(2, other)
        ticket = self.hub.ticket({'session_id': other, 'generation': registered['generation'],
                                  'epoch': self.hub.epoch}, self.token)
        current = self.hub.send({'ticket': ticket['ticket'], 'text': 'second'}, self.token)['request_id']
        self.mac.expect(LinkType.EVENT)
        self.mac.send(LinkType.UNDELIVERABLE, chan=1, request_id=old, reason='channel_closed')
        # Advance the query high-water mark well beyond this expired ID: no tombstone is needed.
        for _ in range(20):
            with self.assertRaises(DeskError):
                self.clear_m()
            self.mac.expect(LinkType.PRESENCE_QUERY)
        self.mac.send(LinkType.PRESENCE, query_id=q['query_id'], state='gone')
        self.mac.send(LinkType.PING, n=1)
        self.mac.expect(LinkType.PONG)
        self.assertEqual(self.hub.state.request_state(current), 'handed_off')
        self.assertEqual(self.target(other)['lock']['request_id'], current)
        self.assertEqual(self.lock_m()['request_id'], old)

    def test_unenrolled_channels_throttled_without_dropping_link(self):
        current = self.send_m()['request_id']
        self.mac.expect(LinkType.EVENT)
        generation = self.target(SESSION_M)['generation']
        ids = [numbered_session(n) for n in range(MAX_LINK_REGISTRATIONS)]
        for chan in range(2, MAX_REGISTERS_PER_MINUTE + len(ids) + 1):
            reply = self.mac.register(chan, ids[chan % len(ids)])
            reason = 'not_enrolled' if chan <= MAX_REGISTERS_PER_MINUTE else 'too_many_registers'
            self.assertEqual((reply['chan'], reply['reason']), (chan, reason))
        self.assertEqual(self.target(SESSION_M)['generation'], generation)
        self.assertEqual(self.hub.state.request_state(current), 'handed_off')
        self.mac.send(LinkType.REPLY, chan=1, request_id=current, text='done', spoken='')
        self.assertTrue(self.mac.expect(LinkType.REPLY_RESULT)['ok'])


class WireCodeTest(FixedLinkClockCase):
    def test_every_emitted_code_is_in_its_table(self):
        # Exercise the actual senders (the pump shares the admission wire kind).
        seen = {decode_frame(no_hub_frame(), Side.HUB, v=None)[1]['reason']}
        seen.update(decode_frame(refusal_frame(c), Side.HUB, v=None)[1]['reason'] for c in RefusedCode)
        self.assertEqual(seen, LINK_CODES[LinkType.LINK_REFUSED])
        proxy = self.hub.links.live_link(MAC).channel(1)
        seen = set()
        for reason in ReplyRefusal:
            proxy.send(MessageType.REPLY_RESULT, request_id=request_id(1), ok=False, reason=reason)
            seen.add(self.mac.expect(LinkType.REPLY_RESULT)['reason'])
        self.assertEqual(seen, LINK_CODES[LinkType.REPLY_RESULT])
        drops = set()
        for chan, reason in enumerate(('unenrolled', 'arbitrary local reason'), 1):
            if chan > 1:
                self.mac.register(chan, SESSION_M)
            self.hub.links.live_link(MAC).channel(chan).close(reason)
            drops.add(self.mac.expect(LinkType.DROP)['reason'])
        self.assertEqual(drops, LINK_CODES[LinkType.DROP])
        seen = {self.mac.register(3, numbered_session(1))['reason'], self.mac.register(4, SESSION_A)['reason']}
        for chan in range(5, 5 + MAX_LINK_REGISTRATIONS):
            self.mac.register(chan, SESSION_M)
        for chan in range(5 + MAX_LINK_REGISTRATIONS, MAX_REGISTERS_PER_MINUTE + 2):
            seen.add(self.mac.register(chan, SESSION_M)['reason'])
        self.assertEqual(seen, LINK_CODES[LinkType.REGISTER_REFUSED])

    def test_free_text_never_on_wire_or_in_logs(self):
        # Text is intentional in event/reply/registration payloads, never diagnostics.
        secret = 'private-diagnostic-canary'
        with self.assertLogs('llm_switchboard.desk', level='DEBUG') as logs:
            response = self.mac.register(2, numbered_session(1), label=secret, cwd='/'+secret)
            self.assertNotIn(secret, json.dumps(response))
            current = self.send_m(secret)['request_id']
            self.assertIn(secret, self.mac.expect(LinkType.EVENT)['content'])
            self.mac.send(LinkType.REPLY, chan=1, request_id=current, text=secret, spoken=secret)
            self.assertNotIn(secret, json.dumps(self.mac.expect(LinkType.REPLY_RESULT)))
            self.mac.send_raw(json.dumps({'type': secret, 'v': 1}).encode() + b'\n')
            self.assertTrue(self.mac.closed())
            wait_until(lambda: self.links()[MAC]['state'] == 'down')
        self.assertNotIn(secret, '\n'.join(logs.output))


class DisconnectTest(FixedLinkClockCase):
    def lost(self, request, *, clearable):
        self.mac.close()
        wait_until(lambda: self.links()[MAC]['state'] == 'down')
        self.assertEqual(self.hub.state.request_state(request), 'unknown')
        self.assertEqual([lock.request_id for lock in LockStore(self.config.lock_file).load()], [request])
        if clearable:
            self.assertEqual(self.clear_m(), {'cleared': True})
        else:
            with self.assertRaises(DeskError) as refused:
                self.clear_m()
            self.assertEqual(refused.exception.reason, Refusal.MAC_NOT_CONNECTED)

    def test_disconnect_after_each_writer_report(self):
        for i, code in enumerate([None, *UndeliverableCode]):
            with self.subTest(code=code):
                if i:
                    self.link_clock.advance(61)
                    self.mac = self.connect()
                    self.mac.register(1, SESSION_M)
                request = self.send_m()['request_id']
                self.mac.expect(LinkType.EVENT)
                kind, extra = (LinkType.DELIVERED, {}) if code is None else (LinkType.UNDELIVERABLE, {'reason': code})
                self.mac.send(kind, chan=1, request_id=request, **extra)
                self.mac.send(LinkType.PING, n=1)
                self.mac.expect(LinkType.PONG)
                self.lost(request, clearable=True)

    def test_disconnect_before_handoff(self):
        prepare = self.hub._prepare_handoff
        def disconnect(request):
            self.mac.close()
            wait_until(lambda: self.links()[MAC]['state'] == 'down')
            return prepare(request)
        with patch.object(self.hub, '_prepare_handoff', disconnect), self.assertRaises(DeskError):
            self.send_m()
        self.assertEqual([r['state'] for r in self.hub.state_view()['requests']], ['expired'])
        self.assertEqual(LockStore(self.config.lock_file).load(), [])
        answer = self.hub.handle_operator({'op': 'clear', 'session_id': SESSION_M, 'lock_id': 'f'*32})
        self.assertEqual(answer['error'], Refusal.NO_LOCK)

    def test_channel_write_unfinished_then_link_lost(self):
        request = self.send_m()['request_id']
        self.mac.expect(LinkType.EVENT)
        self.mac.send(LinkType.UNREGISTERED, chan=1, reason='channel_write_failed')
        self.mac.send(LinkType.PING, n=1)
        self.mac.expect(LinkType.PONG)
        self.lost(request, clearable=False)

    def test_sleep_and_reply_on_successor(self):
        request = self.send_m()['request_id']
        self.mac.expect(LinkType.EVENT)
        self.link_clock.advance(self.hub.links.idle_seconds)
        self.hub.links.tick()
        self.lost(request, clearable=False)
        self.mac = self.connect()
        self.mac.register(2, SESSION_M)
        self.mac.send(LinkType.REPLY, chan=2, request_id=request, text='late', spoken='')
        self.assertEqual(self.mac.expect(LinkType.REPLY_RESULT)['reason'], 'wrong_connection')
        self.assertEqual(self.lock_m()['request_id'], request)
        pending = self.clear_in_thread()
        q = self.mac.expect(LinkType.PRESENCE_QUERY)
        self.mac.send(LinkType.PRESENCE, query_id=q['query_id'], state='gone')
        pending['thread'].join(10)
        self.assertTrue(pending['value']['cleared'])

    def test_hub_restart_forgets_reports_not_locks(self):
        request = self.send_m()['request_id']
        self.mac.expect(LinkType.EVENT)
        self.mac.send(LinkType.DELIVERED, chan=1, request_id=request)
        self.mac.send(LinkType.PING, n=1)
        self.mac.expect(LinkType.PONG)
        self.hub.close()
        self.hub = self.start_hub()
        self.assertIsNone(self.hub.state.request_state(request))
        self.assertTrue(self.lock_m()['earlier_run'])
        with self.assertRaises(DeskError) as refused:
            self.clear_m()
        self.assertEqual(refused.exception.reason, Refusal.MAC_NOT_CONNECTED)

    def test_reply_in_flight_when_link_lost(self):
        request = self.send_m()['request_id']
        self.mac.expect(LinkType.EVENT)
        self.mac.send(LinkType.REPLY, chan=1, request_id=request, text='done', spoken='')
        # Close before reading reply_result. The only evidence is the decision on the Pi.
        self.mac.close()
        wait_until(lambda: self.links()[MAC]['state'] == 'down')
        self.assertEqual(self.hub.state.request_state(request), 'answered')
        self.assertEqual(LockStore(self.config.lock_file).load(), [])
        self.assertIsNone(self.lock_m())

    def test_revocation_does_not_clear_an_unreported_write(self):
        request = self.send_m()['request_id']
        self.mac.expect(LinkType.EVENT)
        write_signers(self.config.link_signers_file, self.key('mac2').signer)
        self.hub.recheck_now()
        self.lost(request, clearable=False)
