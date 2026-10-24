"""Isolated old/new process fixtures for scripts.api_parity (never a live HOME)."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

BASELINE = "647e4a6"
PORTS = (8794, 8795)
MISSING = "ffffffff-ffff-4fff-8fff-ffffffffffff"
REMOTE_IDS = {kind: str(uuid.uuid5(uuid.NAMESPACE_URL, "switchboard-parity-" + kind))
              for kind in ("rename", "delete", "restore", "move", "fork", "fork-pi", "readonly", "codex")}
# Only the connectivity probe is replaced: no fixture may launch Claude or use
# account credentials. Both real supervisor loops, tmux and indexers still run.
SERVE = "from llm_switchboard.connectivity import Connectivity; Connectivity._probe=lambda self:False; from llm_switchboard.cli import main; raise SystemExit(main())"


def environment(repo, home, socket):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "LLM_SWITCHBOARD_"))}
    return {**env, "HOME": str(home), "PYTHONPATH": str(repo), "LLM_SWITCHBOARD_CODEX_SOCKET": "codex.sock",
            "LLM_SWITCHBOARD_TMUX_SOCKET": socket, "LLM_SWITCHBOARD_NOTIFY": ""}


def fixture_homes(repo: Path, workspace: Path):
    old, new = workspace / "old-home", workspace / "new-home"
    subprocess.run([sys.executable, str(repo / "tests/browser/fixture.py"), str(old)], check=True)
    files = sorted((old / ".claude/projects").glob("*/*.jsonl"))
    for position, path in enumerate(files):
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + position / 1000))
    records = [json.loads(p.read_text().splitlines()[-1]) for p in files]
    ids = {r["aiTitle"]: p.stem for p, r in zip(files, records)}
    _extra_records(old, files[-1])
    search = runpy.run_path(str(repo / "tests/search_fixture.py"))
    for row in json.loads((old / "codex-seed.json").read_text()):
        search["append_jsonl"](old / ".codex/sessions" / (row["tid"] + ".jsonl"),
                               search["codex_records"](row["tid"], row["cwd"]))
    shutil.copytree(old, new)
    # Equivalent path lengths preserve byte sizes, project sort order and hashes.
    encoded_old = re.sub(r"[^a-zA-Z0-9]", "-", str(old))
    encoded_new = re.sub(r"[^a-zA-Z0-9]", "-", str(new))
    for path in list(new.rglob("*")):
        if path.is_file():
            stat = path.stat()
            path.write_bytes(path.read_bytes().replace(str(old).encode(), str(new).encode())
                             .replace(encoded_old.encode(), encoded_new.encode()))
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    for path in list((new / ".claude/projects").iterdir()):
        path.rename(path.with_name(path.name.replace(encoded_old, encoded_new)))
    return (old, new), ids, files[-1].stem


def _extra_records(home, trashed):
    now = time.time()
    root = home / ".local/share/llm-switchboard"
    hosts = root / "hosts"
    hosts.mkdir(parents=True)
    rows = [{"id": sid, "tool": "codex" if kind in ("codex", "readonly") else "claude",
             "title": f"Remote {kind}", "cwd": "/Users/me/dev/alpha", "mtime": now - 500,
             "size": 42, "state": "closed", "mutable": kind != "readonly"}
            for kind, sid in REMOTE_IDS.items()]
    trash = [{**row, "deleted_at": now - 100} for row in rows if row["id"] == REMOTE_IDS["restore"]]
    snapshot = {"version": 1, "rows": [row for row in rows if row["id"] != REMOTE_IDS["restore"]],
                "trash": trash, "last_seen": now}
    (hosts / "mac.json").write_text(json.dumps(snapshot))
    (hosts / "offline.json").write_text(json.dumps({**snapshot, "last_seen": 0}))
    entry = root / "trash" / trashed.stem
    entry.mkdir(parents=True)
    title = json.loads(trashed.read_text().splitlines()[-1])["aiTitle"]
    (entry / "meta.json").write_text(json.dumps({"title": title, "project": "~/scratch",
                                                "original": str(trashed), "deleted_at": now - 200}))
    shutil.move(trashed, entry / trashed.name)
    # Supply a Codex conversation as well as its list metadata.
    seed = home / "codex-seed.json"
    rows = json.loads(seed.read_text())
    for row in rows:
        row["turns"] = [{"user": "fixture message", "agent": "fixture reply"}]
    seed.write_text(json.dumps(rows))


@dataclass
class FixtureServer:
    repo: Path
    home: Path
    port: int
    socket: str
    python: str
    processes: list = field(default_factory=list)
    logs: list = field(default_factory=list)
    startup_s: float = 0
    rss_kb: int = 0
    stopped_code: int | None = None
    sentinel_survived: bool = False

    @property
    def env(self):
        return environment(self.repo, self.home, self.socket)

    def _start(self, command, name):
        log = (self.home.parent / f"{self.home.name}-{name}.log").open("w")
        self.logs.append(log)
        process = subprocess.Popen(command, cwd=self.home, env=self.env, stdout=log, stderr=log)
        self.processes.append(process)
        return process

    def start(self):
        self.codex = self._start([self.python, str(self.repo / "tests/fake_codex.py"), "codex.sock", "codex-seed.json"], "codex")
        self._wait(lambda: (self.home / "codex.sock").exists())
        subprocess.run([self.python, "-m", "llm_switchboard", "index"], cwd=self.home, env=self.env,
                       check=True, capture_output=True, timeout=30)
        subprocess.run(["tmux", "-L", self.socket, "new-session", "-d", "-s", "sentinel", "sleep 300"],
                       env=self.env, check=True, capture_output=True)
        start = time.perf_counter()
        launched_at = time.time()
        self.server = self._start([self.python, "-c", SERVE, "--port", str(self.port), "serve", "--host", "127.0.0.1"], "server")
        self._wait(self.ready)
        self.startup_s = time.perf_counter() - start
        self.rss_kb = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(self.server.pid)], text=True).strip())
        def indexed():
            status = self.request("GET", "/api/search/status")[3]["index"]
            return status["state"] == "ready" and (status["last_updated"] or 0) >= launched_at
        self._wait(indexed)

    def _wait(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in self.processes):
                raise RuntimeError(f"fixture process exited; inspect logs in {self.home.parent}")
            if predicate():
                return
            time.sleep(0.01)
        raise TimeoutError(f"fixture did not become ready: {self.home}")

    def ready(self):
        try:
            return self.request("GET", "/api/settings")[0] == 200
        except OSError:
            return False

    def request(self, method, path, body=None, content_type="application/json", raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if method == "POST" else None)
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                         headers={"Content-Type": content_type})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read()
            if response.headers.get("Content-Type") == "application/json":
                content = json.loads(content)
            return (response.status, response.headers.get("Content-Type"), response.headers.get("Cache-Control"), content)

    def stop_codex(self):
        self.codex.terminate()
        self.codex.wait(timeout=5)

    def close(self):
        if hasattr(self, "server") and self.server.poll() is None:
            self.server.terminate()
            self.stopped_code = self.server.wait(timeout=25)
        result = subprocess.run(["tmux", "-L", self.socket, "has-session", "-t", "sentinel"], capture_output=True)
        self.sentinel_survived = result.returncode == 0
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)
        for log in self.logs:
            log.close()
