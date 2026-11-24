"""Available operator/HTTP capacity while Mac peers stop reading; worst-case status."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from queue import Queue
from unittest.mock import patch

from desk_fixtures import HubCase, REPO, SESSION_A, enroll_file, request_id, run_desk_cli, wait_until
from link_peer import MAC, MAC2, MACHINE, LinkHubCase, LinkPeer, fake_signature, numbered_session
from test_desk_link_channels import MacTargetCase, SESSION_M

from llm_switchboard.desk.link_protocol import MAX_COUNTER, LinkType, RefusedCode
from llm_switchboard.desk.link_session import LinkSession
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import MAX_CANDIDATES, MAX_LOCKS, MAX_REGISTRATIONS, Lock
from llm_switchboard.session_names import MAX_TITLE_LEN
from llm_switchboard.desk.operator import OperatorOp, operator_request
from llm_switchboard.desk.refusals import Refusal


class LinkWaitTest(MacTargetCase):
    link_options = {'presence_seconds': 60, 'tick_seconds': 3600}
    long_poll_seconds = 60

    def pump_peer(self):
        relay, ssh = socket.socketpair()
        with ssh:
            proc = subprocess.Popen([sys.executable, '-I', '-B', REPO / 'bin/desk-link'], stdin=ssh, stdout=ssh,
                                    stderr=subprocess.PIPE, env={**os.environ, 'HOME': str(self.home),
                                    'XDG_CONFIG_HOME': '', 'XDG_STATE_HOME': '', 'XDG_RUNTIME_DIR': ''})
        def close():
            if proc.poll() is None:
                proc.send_signal(signal.SIGCONT)
                proc.terminate()
            proc.communicate(timeout=10)
        self.addCleanup(close)
        peer = LinkPeer(sock=relay)
        self.addCleanup(peer.close)
        self.assertEqual(peer.handshake(lambda _: fake_signature(self.key('mac').signer))['type'], 'link_welcome')
        return proc, peer

    def test_link_waits_capped_leave_http_and_operator_slots(self):
        first = self.unknown_after_disconnect()
        pump, self.mac = self.pump_peer()
        other_sid = numbered_session(2)
        self.remote[other_sid] = MAC2
        self.hub.refresh_files()
        other = self.connect('mac2', MAC2)
        gen = other.register(1, other_sid)['generation']
        ticket = self.hub.ticket({'session_id': other_sid, 'generation': gen, 'epoch': self.hub.epoch}, self.token)
        second = self.hub.send({'ticket': ticket['ticket'], 'text': 'two'}, self.token)['request_id']
        other.expect(LinkType.EVENT)
        other.send(LinkType.UNREGISTERED, chan=1, reason='channel_closed')
        wait_until(lambda: self.hub.state.request_state(second) == 'unknown')
        first_body = {'session_id': SESSION_M, 'lock_id': self.lock_m()['lock_id']}
        other_body = {'session_id': other_sid, 'lock_id': self.target(other_sid)['lock']['lock_id']}
        arrived, polling = Queue(), threading.Event()
        original_submit, original_wait = LinkSession.submit, self.hub.wait_for_change
        def observe(link, kind, **fields):
            frame = original_submit(link, kind, **fields)
            if kind is LinkType.PRESENCE_QUERY:
                arrived.put(link.host)
            return frame
        def observe_poll(*args):
            polling.set()
            return original_wait(*args)
        def http_clear(body):
            return self.call('POST', '/desk/api/clear', body, token=self.token, timeout=65)
        pump.send_signal(signal.SIGSTOP)
        with ThreadPoolExecutor(6) as pool, patch.object(LinkSession, 'submit', observe), \
                patch.object(self.hub, 'wait_for_change', observe_poll):
            try:
                clears = [pool.submit(http_clear, b) for b in (first_body, first_body, first_body, other_body)]
                op = pool.submit(operator_request, self.config.operator_socket, OperatorOp.CLEAR, 65, **first_body)
                self.assertCountEqual([arrived.get(timeout=10) for _ in range(5)], [MAC]*4 + [MAC2])
                status, _, error = http_clear(first_body)
                self.assertEqual((status, error['error']), (503, Refusal.LINK_BUSY))
                self.assertEqual(operator_request(self.config.operator_socket, OperatorOp.CLEAR, **first_body),
                                 {'ok': False, 'error': Refusal.LINK_OPERATOR_BUSY})
                pi = self.channel(SESSION_A)
                target = self.target(SESSION_A)
                _, _, ticket = self.call('POST', '/desk/api/ticket', {'session_id': SESSION_A,
                                  'generation': target['generation'], 'epoch': self.hub.epoch}, token=self.token)
                status, _, sent = self.call('POST', '/desk/api/send', {'ticket': ticket['ticket'], 'text': 'pi'},
                                            token=self.token)
                self.assertEqual((status, sent['state']), (200, 'handed_off'))
                self.assertEqual(pi.read()[0].value, 'event')
                self.assertEqual(run_desk_cli(self.home, 'status').returncode, 0)
                dropped = operator_request(self.config.operator_socket, OperatorOp.LINK_DROP, host=MAC2)
                self.assertEqual(dropped['signer'], {'fingerprint': self.key('mac2').signer.fingerprint})
                version = self.hub.state_view()['version']
                poll = pool.submit(self.call, 'GET', f'/desk/api/state?after={version}', token=self.token)
                self.assertTrue(polling.wait(10))
                self.assertEqual(run_desk_cli(self.home, 'revoke').returncode, 0)
                self.assertEqual(poll.result(10)[0], 401)
                self.assertFalse(op.done() or any(f.done() for f in clears))
            finally:
                pump.send_signal(signal.SIGCONT)
                self.mac.close()
                other.close()
            self.assertEqual(op.result(10)['error'], Refusal.CHANNEL_UNVERIFIABLE)
            self.assertEqual([f.result(10)[0] for f in clears], [401]*4)
        self.assertEqual(self.lock_m()['request_id'], first)


class StatusBoundTest(LinkHubCase, HubCase):
    def test_worst_case_status_fits_operator_line(self):
        self.hub.close()
        store = LockStore(self.config.lock_file)
        store.load()
        names = [f'm{i:031d}' for i in range(MAX_LOCKS)]
        for i, host in enumerate(names):
            store.add(Lock(f'{i:016x}', host, numbered_session(i), request_id(i), 'f'*64, 1800000000.125, 1234, '100',
                           self.key('mac').signer.fingerprint, MACHINE))
        enroll_file(self.config, *(numbered_session(i+100) for i in range(64)))
        self.hub = self.start_hub()
        # The most candidates, each label as long as a frame allows; a
        # non-BMP character expands to two escapes in the operator's JSON.
        candidate_peer = self.connect()
        for i in range(16):
            reply = candidate_peer.register(i+1, numbered_session(i+200), label='\U00010000'*MAX_TITLE_LEN,
                                            cwd='/candidate-folder/' + 'f'*120)
            self.assertEqual(reply['reason'], 'not_enrolled')
        for i in range(MAX_REGISTRATIONS):
            self.hub.state.register(1000+i, numbered_session(i+100), None, host=names[i])
        for code in RefusedCode:
            self.hub.links.refusals.add(code, [MAX_COUNTER-i for i in range(4)])
        self.hub.state.set_admission_refusals(self.hub.links.refusals.view())
        status = operator_request(self.config.operator_socket, OperatorOp.STATUS)
        self.assertTrue(status['ok'])
        self.assertEqual(len(status['locks']), MAX_LOCKS)
        self.assertEqual(len(status['registrations']), MAX_REGISTRATIONS)
        self.assertEqual(len(status['admission_refusals']), len(RefusedCode))
        self.assertEqual(len(status['candidates']), MAX_CANDIDATES)
        self.assertTrue(all('folder' not in candidate for candidate in status['candidates']))
        self.assertTrue(all(row['link_key'] == self.key('mac').signer.fingerprint and row['machine'] == MACHINE[:8]
                            for row in status['locks']))
        self.assertTrue(set(names).issubset({row['host'] for row in status['links']}))
        self.assertLessEqual(len(json.dumps(status, separators=(',', ':')).encode())+1, 64*1024)
