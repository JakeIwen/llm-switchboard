"""OpenSSH's actual effective options, offline; no probe reads the user's configuration."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import subprocess
import unittest

from desk_fixtures import desk_config, make_home
from link_peer import KeyPair
from llm_switchboard.desk.child_process import run_bounded
from llm_switchboard.desk.relay_ssh import ConfigUnsupported, MAX_SSH_OUTPUT_BYTES, SSH, SshStderr, classify_stderr, prepare_ssh
from llm_switchboard.local_config import MAC_NAME, PI_SSH


def effective(argv, env):
    # Insert -G before all other arguments so ssh cannot open a connection.
    result = subprocess.run([argv[0], "-G", *argv[1:]], env=env, capture_output=True, check=True, timeout=15)
    fields = {}
    for line in result.stdout.decode().splitlines():
        key, _, value = line.partition(" ")
        fields.setdefault(key, []).append(value)
    return fields


class SshTests(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.config = replace(desk_config(self.home), repo=self.home)
        self.file = self.home / "config"
        self.file.write_text("")
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}

    def prepare(self, directives="", **kwargs):
        self.file.write_text("Host pi\n" + directives)
        return prepare_ssh(self.config, environ={}, ssh_config=self.file, **kwargs)

    def check(self, directives=""):
        cmd = self.prepare(directives)
        return cmd, effective(cmd.argv, cmd.env)

    def test_only_desk_identity_offered(self):
        cred = self.home / ".ssh"
        cred.mkdir(mode=0o700)
        other = KeyPair(cred, name="other").path
        desk = KeyPair(cred, name="llm-switchboard-desk-mac").path
        directives = (f"IdentityFile {other}\nCertificateFile {other}.pub\nIdentityAgent /tmp/other\n"
                      "ForwardAgent yes\nControlMaster auto\nPasswordAuthentication yes\n"
                      "HostName 127.0.0.1\nPort 2200\nUser remote\n")
        cmd, fields = self.check(directives)
        self.assertEqual(set(cmd.env), {"HOME", "PATH"})
        self.assertEqual(fields["identityfile"], [str(desk)])
        for key, value in {"identityagent": "none", "identitiesonly": "yes", "batchmode": "yes",
                           "pubkeyauthentication": "true", "pubkeyacceptedalgorithms": "ssh-ed25519",
                           "hostname": "127.0.0.1", "port": "2200", "user": "remote"}.items():
            self.assertIn(fields[key][0], {value, {"true": "yes"}.get(value, value)}, key)
        for key in ("passwordauthentication", "kbdinteractiveauthentication", "hostbasedauthentication",
                    "gssapiauthentication", "controlmaster", "forwardagent", "requesttty", "updatehostkeys"):
            self.assertIn(fields[key][0], {"no", "false"}, key)
        self.assertIn(fields["stricthostkeychecking"][0], {"true", "yes"})
        self.assertIn(fields["clearallforwardings"][0], {"true", "yes"})
        self.assertNotIn("certificatefile", fields)
        self.assertEqual(cmd.argv[-2:], ("pi", "desk-link"))

    def test_missing_desk_key_never_reintroduces_default_identity_files(self):
        cmd, fields = self.check()
        self.assertFalse(cmd.key.exists())
        self.assertEqual(fields["identityfile"], [str(cmd.key)])

    def test_user_config_identity_would_be_added(self):
        desk = KeyPair(self.home, name="desk").path
        other = KeyPair(self.home, name="other").path
        self.file.write_text(f"Host pi\nIdentityFile {other}\n")
        fields = effective((SSH, "-F", str(self.file), "-i", str(desk), "-o", "IdentitiesOnly=yes", "pi"), self.env)
        self.assertCountEqual(fields["identityfile"], [str(desk), str(other)])

    def test_default_known_hosts_lists_parse(self):
        cmd, fields = self.check()
        defaults = effective((SSH, "-F", "none", "pi"), self.env)
        for key in ("userknownhostsfile", "globalknownhostsfile"):
            self.assertEqual(len(defaults[key][0].split()), 2)
            self.assertEqual(fields[key], defaults[key])
            self.assertFalse(any(arg.lower().startswith(key + "=") for arg in cmd.argv))

    def test_configured_known_hosts_list_passed_whole(self):
        paths = f"{self.home}/a {self.home}/b"
        cmd, fields = self.check(f"UserKnownHostsFile {paths}\nGlobalKnownHostsFile {paths}\n")
        for key in ("UserKnownHostsFile", "GlobalKnownHostsFile"):
            self.assertIn(f"{key}={paths}", cmd.argv)
            self.assertEqual(fields[key.lower()], [paths])

    def test_hostkeyalias_passed_and_used(self):
        _, fields = self.check("HostName 127.0.0.1\nHostKeyAlias trust-name\n")
        self.assertEqual(fields["hostname"], ["127.0.0.1"])
        self.assertEqual(fields["hostkeyalias"], ["trust-name"])

    def test_hostkeyalias_omitted_when_unset(self):
        _, fields = self.check()
        self.assertNotIn("hostkeyalias", fields)

    def test_hostkeyalgorithms_passed_only_when_configured(self):
        cmd, _ = self.check()
        self.assertFalse(any(arg.startswith("HostKeyAlgorithms=") for arg in cmd.argv))
        cmd, fields = self.check("HostKeyAlgorithms ssh-ed25519\n")
        self.assertIn("HostKeyAlgorithms=ssh-ed25519", cmd.argv)
        self.assertEqual(fields["hostkeyalgorithms"], ["ssh-ed25519"])

    def test_proxy_and_known_hosts_command_refused(self):
        for opt in ("ProxyJump jump", "ProxyCommand false", "KnownHostsCommand false"):
            with self.subTest(opt=opt), self.assertRaisesRegex(ConfigUnsupported, "^ssh_config_unsupported$"):
                self.prepare(opt + "\n")

    def test_g_values_validated(self):
        baseline = subprocess.check_output([SSH, "-G", "-F", "none", "-T", "pi"], env=self.env)
        cases = [(b"user", v) for v in [b"a b", b'"a"', b"a\tb", b"a\x7fb", b"a\x01b", b"a\\b"]]
        cases += [(b"port", v) for v in [b"0", b"65536", b"-80", b"one", b"9 " * 2048]]
        cases += [(b"userknownhostsfile", v) for v in [b"relative", b"/a b", b"", b"/a " * 5]]
        cases += [(b"hostkeyalias", b"bad@host"), (b"hostkeyalgorithms", b"one:two")]
        for key, val in cases:
            with self.subTest(key=key, val=val[:20]):
                def altered(argv, **kwargs):
                    result = run_bounded(argv, **kwargs)
                    data = b"\n".join(ln for ln in baseline.splitlines() if not ln.startswith(key + b" "))
                    return replace(result, stdout=data + b"\n" + key + b" " + val + b"\n")
                with self.assertRaisesRegex(ConfigUnsupported, "^ssh_config_unsupported$"):
                    self.prepare(run=altered)

    def test_unusable_dump_does_not_produce_connection_argv(self):
        for change in ({"timed_out": True}, {"returncode": 1}, {"stdout": b""},
                       {"stdout_overflow": True}, {"stderr_overflow": True}):
            def unreadable(argv, **kwargs):
                self.assertEqual(set(kwargs["env"]), {"HOME", "PATH"})
                self.assertEqual(kwargs["deadline_seconds"], 5)
                self.assertEqual(kwargs["stdout_limit"], MAX_SSH_OUTPUT_BYTES)
                return replace(run_bounded(argv, **kwargs), **change)
            with self.subTest(change=change), self.assertRaises(ConfigUnsupported):
                self.prepare(run=unreadable)

    def test_settings_read_again_and_environment_override(self):
        local = self.home / "local.env"
        local.write_text(f"{PI_SSH}=first\n{MAC_NAME}=mac2\n")
        self.assertEqual(self.prepare().argv[-2], "first")
        local.write_text(f"{PI_SSH}=second\n{MAC_NAME}=mac3\n")
        again = self.prepare()
        self.assertEqual((again.host, again.argv[-2]), ("mac3", "second"))
        override = prepare_ssh(self.config, environ={PI_SSH: "overridden"}, ssh_config=self.file)
        self.assertEqual(override.argv[-2], "overridden")
        for env in ({PI_SSH: "-V"}, {MAC_NAME: "../other"}):
            with self.assertRaises(ConfigUnsupported):
                prepare_ssh(self.config, environ=env, ssh_config=self.file)

    def test_stderr_classified_never_logged(self):
        messages = {b"Permission denied (publickey).": SshStderr.PERMISSION_DENIED,
                    b"WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!": SshStderr.HOST_KEY,
                    b"Connection refused": SshStderr.CONNECTION_REFUSED, b"Connection timed out": SshStderr.TIMEOUT,
                    b"Could not resolve hostname": SshStderr.NAME_RESOLUTION, b"arbitrary": SshStderr.OTHER}
        captured = io.StringIO()
        with self.assertNoLogs(), redirect_stdout(captured), redirect_stderr(captured):
            for message, code in messages.items():
                self.assertEqual(classify_stderr(b"private marker: " + message), code)
            self.assertEqual(classify_stderr(b"x" * MAX_SSH_OUTPUT_BYTES + b"permission denied"), SshStderr.OTHER)
        self.assertEqual(captured.getvalue(), "")
