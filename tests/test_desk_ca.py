"""scripts/desk-ca.sh: single-use CA, leaf issuance, CA key destruction, cleanup."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "desk-ca.sh"
CANDIDATES = ("/opt/homebrew/opt/openssl@3/bin/openssl", "/opt/homebrew/bin/openssl",
              "/usr/local/opt/openssl@3/bin/openssl")


def find_openssl() -> str | None:
    for candidate in (os.environ.get("OPENSSL"), shutil.which("openssl"), *CANDIDATES):
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        out = subprocess.run([candidate, "version"], capture_output=True, text=True, check=False).stdout
        if re.match(r"OpenSSL ([3-9]|[1-9]\d)\.", out):
            return candidate
    return None


OPENSSL = find_openssl()


@unittest.skipIf(OPENSSL is None, "needs OpenSSL 3+ (system LibreSSL/1.1 can't issue name-constrained certs)")
class DeskCaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.env = dict(os.environ, TMPDIR=str(self.tmpdir), OPENSSL=OPENSSL)

    def run_ca(self, *args):
        return subprocess.run([str(SCRIPT), *args], env=self.env, capture_output=True,
                              text=True, timeout=120, check=False)

    def text(self, path):
        return subprocess.run([OPENSSL, "x509", "-in", str(path), "-noout", "-text"],
                              capture_output=True, text=True, check=True).stdout

    def test_real_run_writes_three_files_and_cleans_up(self):
        out = self.root / "out"
        result = self.run_ca(str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        files = {p.name: stat.S_IMODE(p.stat().st_mode) for p in out.iterdir()}
        self.assertEqual(files, {"desk-ca.crt": 0o644, "leaf.pem": 0o600, "leaf.key": 0o600})
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertEqual(list(self.tmpdir.iterdir()), [], "work dir must be removed")
        self.assertRegex(result.stdout, r"(?i)sha256 fingerprint=([0-9A-F]{2}:){31}[0-9A-F]{2}")
        self.assertNotIn("PRIVATE KEY", result.stdout + result.stderr)

        for path in out.iterdir():
            content = path.read_text()
            if path.name == "leaf.key":
                self.assertEqual(content.count("PRIVATE KEY"), 2)  # one key: BEGIN + END
                continue
            self.assertNotIn("PRIVATE KEY", content)

        ca = self.text(out / "desk-ca.crt")
        self.assertIn("CA:TRUE, pathlen:0", ca)
        self.assertIn("X509v3 Name Constraints: critical", ca)
        self.assertIn("DNS:localhost", ca)
        self.assertIn("IP:127.0.0.1/255.255.255.255", ca)
        self.assertIn("Certificate Sign, CRL Sign", ca)
        leaf = self.text(out / "leaf.pem")
        self.assertIn("CA:FALSE", leaf)
        self.assertRegex(leaf, r"DNS:localhost, IP Address:127\.0\.0\.1")
        self.assertIn("TLS Web Server Authentication", leaf)

        verify = subprocess.run([OPENSSL, "verify", "-CAfile", str(out / "desk-ca.crt"),
                                 "-purpose", "sslserver", str(out / "leaf.pem")],
                                capture_output=True, text=True, check=False)
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

        # The leaf key matches the leaf certificate.
        pub_from_cert = subprocess.run([OPENSSL, "x509", "-in", str(out / "leaf.pem"), "-pubkey", "-noout"],
                                       capture_output=True, text=True, check=True).stdout
        pub_from_key = subprocess.run([OPENSSL, "pkey", "-in", str(out / "leaf.key"), "-pubout"],
                                      capture_output=True, text=True, check=True).stdout
        self.assertEqual(pub_from_cert, pub_from_key)

    def test_constraint_test_leaf_is_outside_the_ca_constraints(self):
        out = self.root / "ct"
        result = self.run_ca("--constraint-test", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["constraint-test-ca.crt", "constraint-test-leaf.key", "constraint-test-leaf.pem"])
        self.assertEqual(list(self.tmpdir.iterdir()), [])
        self.assertIn("outside.example", self.text(out / "constraint-test-leaf.pem"))
        self.assertIn("DNS:localhost", self.text(out / "constraint-test-ca.crt"))
        verify = subprocess.run([OPENSSL, "verify", "-CAfile", str(out / "constraint-test-ca.crt"),
                                 str(out / "constraint-test-leaf.pem")], capture_output=True, text=True, check=False)
        self.assertNotEqual(verify.returncode, 0)

    def test_existing_out_dir_is_refused_untouched(self):
        out = self.root / "exists"
        out.mkdir()
        (out / "keep").write_text("x")
        result = self.run_ca(str(out))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual([p.name for p in out.iterdir()], ["keep"])
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_missing_argument_fails_with_usage(self):
        result = self.run_ca()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
