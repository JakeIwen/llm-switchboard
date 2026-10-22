import threading
import unittest

from llm_switchboard.listen import TAILSCALE, Listeners, allowed_peer


class FakeServer:
    def __init__(self, host):
        self.host, self.closed = host, False
        self._done = threading.Event()

    def serve_forever(self):
        self._done.wait()

    def shutdown(self):
        self._done.set()

    def server_close(self):
        self.closed = True


class AllowedPeerTest(unittest.TestCase):
    def test_localhost_and_tailnet_only(self):
        for ip in ["127.0.0.1", "::1", "100.100.1.2", "100.64.0.1", "fd7a:115c:a1e0::401:430a", "::ffff:100.101.1.2"]:
            self.assertTrue(allowed_peer(ip), ip)
        for ip in ["192.168.6.50", "10.0.0.2", "100.128.0.1", "8.8.8.8", "fe80::1", "::ffff:192.168.6.50", "junk"]:
            self.assertFalse(allowed_peer(ip), ip)


class ListenersTest(unittest.TestCase):
    def run_listeners(self, hosts, resolve):
        made = []

        def make(host, port):
            if host == "bad":
                raise OSError("cannot assign requested address")
            made.append(FakeServer(host))
            return made[-1]

        listeners = Listeners(hosts, 8777, make, resolve_tailscale=resolve, retry_s=0.01)
        thread = threading.Thread(target=listeners.serve, daemon=True)
        thread.start()
        return listeners, thread, made

    def wait_for(self, cond):
        for _ in range(500):
            if cond():
                return
            threading.Event().wait(0.01)
        self.fail("timed out")

    def test_binds_tailscale_once_it_has_an_address(self):
        answers = [[], [], ["100.100.1.2", "fd7a::1"]]
        listeners, thread, made = self.run_listeners(["127.0.0.1", TAILSCALE], lambda: answers.pop(0) if answers else [])
        self.wait_for(lambda: len(made) == 3)
        self.assertEqual([s.host for s in made], ["127.0.0.1", "100.100.1.2", "fd7a::1"])
        listeners.shutdown()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(all(s.closed for s in made))

    def test_retries_until_an_address_binds(self):
        answers = [["bad"], ["bad", "100.100.1.2"]]
        listeners, thread, made = self.run_listeners([TAILSCALE], lambda: answers.pop(0) if answers else [])
        self.wait_for(lambda: len(made) == 1)
        self.assertEqual(made[0].host, "100.100.1.2")
        listeners.shutdown()
        thread.join(2)

    def test_fixed_address_failure_fails_the_start(self):
        with self.assertRaises(OSError):
            Listeners(["bad"], 8777, lambda h, p: (_ for _ in ()).throw(OSError("in use")))


if __name__ == "__main__":
    unittest.main()
