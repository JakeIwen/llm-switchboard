"""The relay's `relay_build` (docs/voice-desk.md, "Framing": the checkout's
git short hash or ''), read from git's files in throwaway repositories:
a plain checkout, a worktree, packed refs, a detached HEAD, and no
repository at all."""

import os
from pathlib import Path
import shutil
import subprocess
import unittest

from desk_fixtures import make_home
from llm_switchboard.desk.checkout_build import SHORT_HEX_DIGITS, checkout_build
from llm_switchboard.desk.link_protocol import LinkType, Side, decode_frame, encode_frame


@unittest.skipIf(shutil.which("git") is None, "git is not installed; the fixtures are git repositories")
class CheckoutBuildTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "commit", "-q", "--allow-empty", "-m", "first")

    def git(self, where: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(where), *args], env=self.env, check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()

    def head(self, where: Path) -> str:
        return self.git(where, "rev-parse", "HEAD")[:SHORT_HEX_DIGITS]

    def test_plain_checkout_loose_and_packed(self):
        self.assertEqual(checkout_build(self.repo), self.head(self.repo))
        self.git(self.repo, "pack-refs", "--all")
        self.assertFalse((self.repo / ".git" / "refs" / "heads" / "main").exists())
        self.assertEqual(checkout_build(self.repo), self.head(self.repo))

    def test_worktree_and_detached_head(self):
        tree = self.home / "tree"
        self.git(self.repo, "worktree", "add", "-q", "-b", "side", str(tree))
        self.git(tree, "commit", "-q", "--allow-empty", "-m", "second")
        self.assertEqual(checkout_build(tree), self.head(tree))
        self.assertNotEqual(checkout_build(tree), checkout_build(self.repo))
        self.git(tree, "checkout", "-q", "--detach", "HEAD~1")
        self.assertEqual(checkout_build(tree), self.head(self.repo))

    def test_unknown_build_is_empty(self):
        self.assertEqual(checkout_build(self.home), "")
        (self.repo / ".git" / "HEAD").write_text("ref: ../../outside\n")
        self.assertEqual(checkout_build(self.repo), "")
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/missing\n")
        self.assertEqual(checkout_build(self.repo), "")

    def test_build_fits_the_hello(self):
        build = checkout_build(self.repo)
        frame = encode_frame(LinkType.LINK_HELLO, Side.RELAY, versions=[1], relay_build=build, host="mac",
                             machine="ab" * 16)
        self.assertEqual(decode_frame(frame, Side.RELAY, v=None)[1]["relay_build"], build)


if __name__ == "__main__":
    unittest.main()
