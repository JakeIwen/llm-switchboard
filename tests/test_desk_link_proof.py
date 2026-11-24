"""desk/link_proof.py without a hub: the signed message, the bounded
`ssh-keygen -Y sign` and `-Y verify` calls (the real program, throwaway
keys in a temporary directory), the self-test vector and `link-signers`."""

import base64
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from llm_switchboard.desk import link_proof
from llm_switchboard.desk.child_process import TERM_GRACE_SECONDS
from llm_switchboard.desk.link_proof import (
    MAX_SIGNER_LINES, MAX_SIGNERS_FILE_BYTES, SELF_TEST_MESSAGE, SELF_TEST_SIGNATURE, SELF_TEST_SIGNER,
    ProofCheck, Signer, SignersUnreadable, format_signers, parse_signers, proof_message, read_signers,
    self_test, sign, verify, verify_proof,
)

ALLOWANCE = 10.0
FIELDS = {"version": 1, "host": "mac", "machine": "a" * 32, "epoch": "b" * 32, "nonce": "c" * 64}


def _blob(key_type: bytes, *parts: bytes) -> str:
    raw = b"".join(struct.pack(">I", len(p)) + p for p in (key_type, *parts))
    return base64.b64encode(raw).decode()


class KeyCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        os.chmod(cls.dir, 0o700)
        cls.keys = {}
        for name in ("mac", "other"):
            path = cls.dir / name
            subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(path)],
                           check=True, env={"PATH": "/usr/bin:/bin"}, timeout=30)
            cls.keys[name] = (path, (cls.dir / f"{name}.pub").read_text().split()[1])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def signer(self, host="mac", key="mac"):
        return Signer(host, self.keys[key][1])

    def fake_keygen(self, body: str) -> str:
        """An executable standing in for ssh-keygen, written in Python."""
        path = self.dir / f"fake-{self.id().rsplit('.', 1)[-1]}"
        path.write_text(f"#!{sys.executable} -I\n" + body)
        path.chmod(0o700)
        return str(path)


class ProofTest(KeyCase):
    def test_self_test_vector(self):
        self.assertTrue(self_test())
        self.assertIs(verify(SELF_TEST_SIGNER, SELF_TEST_MESSAGE, SELF_TEST_SIGNATURE), ProofCheck.PROOF_OK)
        for i in range(len(SELF_TEST_MESSAGE)):
            changed = bytearray(SELF_TEST_MESSAGE)
            changed[i] ^= 1
            if i % 17 == 0:  # a spread of single-byte changes, kept quick
                with self.subTest(byte=i):
                    self.assertIs(verify(SELF_TEST_SIGNER, bytes(changed), SELF_TEST_SIGNATURE),
                                  ProofCheck.PROOF_BAD)
        # A verifier that accepts everything fails the self-test too.
        accepts_all = self.fake_keygen(
            "import sys\nsys.stdin.read()\n"
            "print('Good \"llm-switchboard-desk-link\" signature for selftest with ED25519 key')\n")
        self.assertFalse(self_test(ssh_keygen=accepts_all))
        self.assertFalse(self_test(ssh_keygen=str(self.dir / "missing")))

    def test_good_proof_verifies(self):
        message = proof_message(**FIELDS)
        signature = sign(self.keys["mac"][0], message)
        self.assertLessEqual(len(signature), 4096)
        self.assertIs(verify(self.signer(), message, signature), ProofCheck.PROOF_OK)

    def test_proof_bound_to_epoch_version_machine_nonce_and_host(self):
        signature = sign(self.keys["mac"][0], proof_message(**FIELDS))
        for name, value in [("version", 2), ("machine", "d" * 32), ("epoch", "e" * 32), ("nonce", "f" * 64),
                            ("host", "mac2")]:
            with self.subTest(field=name):
                other = proof_message(**{**FIELDS, name: value})
                signer = self.signer(host=value if name == "host" else "mac")
                self.assertIs(verify(signer, other, signature), ProofCheck.PROOF_BAD)

    def test_other_key_or_principal_refused(self):
        message = proof_message(**FIELDS)
        signature = sign(self.keys["other"][0], message)
        self.assertIs(verify(self.signer(), message, signature), ProofCheck.PROOF_BAD)
        # ssh-keygen binds the key, not the name: a proof made for `mac` must
        # still fail for another host listed with the same key, which
        # verify_proof ensures by signing over the host it checks.
        signature = sign(self.keys["mac"][0], message)
        proof = {k: FIELDS[k] for k in ("version", "machine", "epoch", "nonce")}
        self.assertIs(verify_proof(self.signer(), signature=signature, **proof), ProofCheck.PROOF_OK)
        self.assertIs(verify_proof(self.signer(host="mac2"), signature=signature, **proof), ProofCheck.PROOF_BAD)
        self.assertIs(verify_proof(self.signer(), signature=signature, **{**proof, "machine": "bad"}),
                      ProofCheck.PROOF_BAD)

    def test_message_is_the_sections_exact_bytes(self):
        self.assertEqual(proof_message(**FIELDS),
                         b"llm-switchboard-desk-link-proof\nversion=1\nhost=mac\nmachine=" + b"a" * 32
                         + b"\nepoch=" + b"b" * 32 + b"\nnonce=" + b"c" * 64 + b"\n")
        for name, value in [("version", 0), ("host", "pi"), ("host", "a\nb"), ("machine", "A" * 32),
                            ("epoch", "b" * 31), ("nonce", "c" * 63)]:
            with self.subTest(field=name), self.assertRaises(ValueError):
                proof_message(**{**FIELDS, name: value})

    def test_signature_shape_checked_before_running_the_verifier(self):
        with mock.patch.object(link_proof, "run_bounded") as run:
            for bad in ("", "not armored", SELF_TEST_SIGNATURE * 20):
                self.assertIs(verify(SELF_TEST_SIGNER, SELF_TEST_MESSAGE, bad), ProofCheck.PROOF_BAD)
        run.assert_not_called()

    def test_sign_runs_without_an_agent_and_bounded(self):
        fake = self.fake_keygen("import os, sys, time\nsys.stdin.read()\nprint(dict(os.environ), file=sys.stderr)\n"
                                "sys.stdout.write('x' * 10000)\n")
        with self.assertRaises(link_proof.SigningFailed) as caught:
            sign(self.keys["mac"][0], proof_message(**FIELDS), ssh_keygen=fake)
        self.assertEqual(caught.exception.args[0], link_proof.SigningFailure.SIGN_FAILED)
        self.assertEqual(link_proof.CHILD_ENV, {"PATH": "/usr/bin:/bin"})


class VerifierBoundsTest(KeyCase):
    def test_verifier_timeout_and_output_cap(self):
        hang = self.fake_keygen("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                "import sys\nsys.stdout.write('ready'); sys.stdout.flush()\ntime.sleep(60)\n")
        results = []
        real_run = link_proof.run_bounded

        def recording(*args, **kwargs):
            results.append(real_run(*args, **kwargs))
            return results[-1]
        with mock.patch.object(link_proof, "run_bounded", side_effect=recording):
            started = time.monotonic()
            outcome = verify(SELF_TEST_SIGNER, SELF_TEST_MESSAGE, SELF_TEST_SIGNATURE, ssh_keygen=hang,
                             deadline_seconds=2)
            elapsed = time.monotonic() - started
        self.assertIs(outcome, ProofCheck.VERIFIER_TIMEOUT)
        self.assertGreaterEqual(elapsed, 2)
        [result] = results
        self.assertEqual(result.stdout, b"ready")  # it had ignored SIGTERM before the deadline
        # Terminated at the deadline, killed after the grace, reaped.
        self.assertTrue(result.child.reaped.wait(TERM_GRACE_SECONDS + ALLOWANCE))
        self.assertGreaterEqual(time.monotonic() - started, 2 + TERM_GRACE_SECONDS)
        self.assertEqual(result.child.popen.returncode, -9)
        self.assertEqual(link_proof.VERIFY_DEADLINE_SECONDS, 5)

        flood = self.fake_keygen(
            "import sys\nsys.stdin.read()\n"
            "sys.stdout.write('Good \"llm-switchboard-desk-link\" signature for selftest with ' + 'x' * 5000)\n")
        self.assertIs(verify(SELF_TEST_SIGNER, SELF_TEST_MESSAGE, SELF_TEST_SIGNATURE, ssh_keygen=flood),
                      ProofCheck.PROOF_BAD)
        self.assertIs(verify(SELF_TEST_SIGNER, SELF_TEST_MESSAGE, SELF_TEST_SIGNATURE,
                             ssh_keygen=str(self.dir / "missing")), ProofCheck.VERIFIER_UNAVAILABLE)

    def test_verifier_gets_only_validated_signer_line(self):
        record = self.dir / "record.json"
        spy = self.fake_keygen(f"""
import json, os, sys
argv = sys.argv[1:]
def arg(flag):
    return argv[argv.index(flag) + 1]
def read(path):
    with open(path, "rb") as f:
        return f.read().decode()
open_fds = []
for fd in range(3, 256):
    try:
        os.fstat(fd)
        open_fds.append(fd)
    except OSError:
        pass
fds = sorted(int(arg(flag).rsplit("/", 1)[1]) for flag in ("-f", "-s"))
json.dump({{"argv": argv, "env": dict(os.environ), "signers": read(arg("-f")), "signature": read(arg("-s")),
           "stdin": sys.stdin.read(), "open_fds": open_fds, "fds": fds}}, open({str(record)!r}, "w"))
""")
        signer = self.signer()
        message = proof_message(**FIELDS)
        signature = sign(self.keys["mac"][0], message)
        self.assertIs(verify(signer, message, signature, ssh_keygen=spy), ProofCheck.PROOF_BAD)
        seen = json.loads(record.read_text())
        self.assertEqual(seen["signers"], signer.line + "\n")
        self.assertEqual(seen["signature"], signature)
        self.assertEqual(seen["stdin"], message.decode())
        self.assertEqual(seen["open_fds"], seen["fds"])  # only the two pipes, nothing else inherited
        self.assertEqual(set(seen["env"]) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}, {"PATH"})
        self.assertEqual(seen["argv"][:4], ["-Y", "verify", "-f", seen["argv"][3]])
        self.assertEqual(seen["argv"][4:8], ["-I", "mac", "-n", "llm-switchboard-desk-link"])
        self.assertTrue(all(a.startswith("/dev/fd/") for a in (seen["argv"][3], seen["argv"][9])))


class SignersTest(KeyCase):
    def test_signers_round_trip(self):
        signers = (self.signer("mac"), self.signer("mac2", "other"))
        data = format_signers(signers)
        self.assertEqual(parse_signers(data), signers)
        self.assertEqual(data.decode().splitlines()[0],
                         f'mac namespaces="llm-switchboard-desk-link" ssh-ed25519 {self.keys["mac"][1]}')
        self.assertEqual(parse_signers(b""), ())
        self.assertTrue(signers[0].fingerprint.startswith("SHA256:"))
        keygen = subprocess.run(["/usr/bin/ssh-keygen", "-l", "-f", str(self.keys["mac"][0]) + ".pub"],
                                capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
        self.assertIn(signers[0].fingerprint, keygen.stdout)

    def test_malformed_signers_refused(self):
        good = format_signers((self.signer(),)).decode()
        mac, other = self.keys["mac"][1], self.keys["other"][1]
        line = 'mac{n} namespaces="llm-switchboard-desk-link" ssh-ed25519 {blob}\n'
        five = "".join(line.format(n=n, blob=_blob(b"ssh-ed25519", bytes([n]) * 32)) for n in range(5))
        cases = {
            "five lines": five,
            "duplicate host": good + good.replace(mac, other),
            "duplicate key": good + good.replace("mac ", "mac2 "),
            "rsa key": f'mac namespaces="llm-switchboard-desk-link" ssh-rsa {mac}\n',
            "rsa blob under an ed25519 label": line.format(n="", blob=_blob(b"ssh-rsa", b"\x01\x00\x01", b"\x07" * 64)),
            "short key": line.format(n="", blob=_blob(b"ssh-ed25519", b"\x01" * 31)),
            "trailing field": line.format(n="", blob=_blob(b"ssh-ed25519", b"\x01" * 32, b"")),
            "other namespace": good.replace("llm-switchboard-desk-link", "git"),
            "no final newline": good[:-1],
            "crlf": good.replace("\n", "\r\n"),
            "double space": good.replace(" ssh", "  ssh"),
            "comment": good[:-1] + " mac@host\n",
            "host pi": good.replace("mac ", "pi "),
            "upper host": good.replace("mac ", "Mac "),
            "blank line": good + "\n",
            "not ascii": good.replace("mac ", "méc "),
            "too large": good + "#" * MAX_SIGNERS_FILE_BYTES,
        }
        self.assertEqual(MAX_SIGNER_LINES, 4)
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(SignersUnreadable):
                parse_signers(text.encode())
        four = "".join(five.splitlines(keepends=True)[:4])
        self.assertEqual(len(parse_signers(four.encode())), 4)

    def test_format_refuses_what_the_reader_would(self):
        with self.assertRaises(SignersUnreadable):
            format_signers((self.signer("mac"), self.signer("mac", "other")))
        with self.assertRaises(SignersUnreadable):
            format_signers(tuple(Signer(f"mac{n}", _blob(b"ssh-ed25519", bytes([n]) * 32)) for n in range(5)))

    def test_read_signers_bounded_and_strict(self):
        path = self.dir / "link-signers"
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.assertEqual(read_signers(path), ())
        path.write_bytes(format_signers((self.signer(),)))
        self.assertEqual(read_signers(path), (self.signer(),))
        path.unlink()
        path.symlink_to(self.dir / "mac.pub")
        with self.assertRaises(SignersUnreadable):
            read_signers(path)
        path.unlink()
        os.mkfifo(path)
        started = time.monotonic()
        with self.assertRaises(SignersUnreadable):
            read_signers(path)
        self.assertLess(time.monotonic() - started, ALLOWANCE)
        self.assertTrue(stat.S_ISFIFO(os.lstat(path).st_mode))


if __name__ == "__main__":
    unittest.main()
