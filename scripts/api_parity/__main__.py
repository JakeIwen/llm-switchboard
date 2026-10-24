"""Compare every legacy API route against commit 647e4a6 on isolated fixtures.

Run from the checkout after uv sync --frozen (tmux must be installed):
    .venv/bin/python -m scripts.api_parity --workspace /absolute/scratch/parity
The workspace must not exist. The Markdown report defaults to its parent's
api-parity-report.md. Ports 8794/8795 must be free. All fixture processes and
tmux servers stop, and the detached baseline worktree is removed on exit.
The fixture homes and logs stay in the workspace as reproducible evidence.
"""

import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import uuid

from .cases import Case, get_cases, post_cases
from .compare import Comparison
from .fixtures import BASELINE, PORTS, FixtureServer, fixture_homes


def substitute(value, variables):
    if isinstance(value, str):
        return value.format_map(variables)
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    return value


def exercise(cases, servers, comparison, variables):
    for case in cases:
        responses, paths = [], []
        for server, names in zip(servers, variables):
            path = substitute(case.path, names)
            paths.append(path)
            response = server.request(case.method, path, substitute(case.body, names), case.content_type, case.raw)
            responses.append(response)
            if case.save and response[0] == 200:
                names[case.save] = response[3]["id"]
        if not comparison.compare(case, responses, paths):
            print(f"DIFF {case.method} {case.path}: {case.label}", flush=True)


def run(repo, workspace, report):
    baseline = workspace / "baseline"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(baseline), BASELINE], check=True)
    servers, comparison = [], None
    try:
        homes, ids, trashed = fixture_homes(repo, workspace)
        sid = next(sid for title, sid in ids.items() if title.startswith("Alpha ") and sid != trashed)
        comparison = Comparison(homes)
        tag = uuid.uuid4().hex[:10]
        for tree, home, port in zip((baseline, repo), homes, PORTS):
            server = FixtureServer(tree, home, port, f"parity-{tag}-{port}", sys.executable)
            servers.append(server)
            server.start()
        codex = json.loads((homes[0] / "codex-seed.json").read_text())[0]["tid"]
        variables = [{"home": str(home), "codex": codex} for home in homes]
        exercise(get_cases(repo, sid, trashed) + post_cases(sid, trashed), servers, comparison, variables)
        for server in servers:
            server.stop_codex()
        unavailable = [Case("Codex unavailable GET", "/api/sessions/{codex}/messages", status=503),
                       Case("Codex unavailable POST", "/api/sessions/new", "POST",
                            {"tool": "codex", "name": "Unavailable", "cwd": "{home}/dev/alpha", "prompt": "x"}, 503)]
        exercise(unavailable, servers, comparison, variables)
    finally:
        for server in servers:
            server.close()
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(baseline)], check=True)
        if comparison is not None:
            report.write_text(comparison.report(servers))
    failures = [row for row in comparison.results if not row["matched"]]
    lifecycle = all(server.sentinel_survived and server.stopped_code in (0, -15) for server in servers)
    print(f"{len(comparison.results) - len(failures)}/{len(comparison.results)} cases matched; report: {report}")
    return 1 if failures or not lifecycle else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not shutil.which("tmux"):
        parser.error("tmux is required")
    args.workspace = args.workspace.resolve()
    if args.workspace.exists():
        parser.error("workspace already exists; use a new scratch directory")
    for port in PORTS:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    args.workspace.mkdir(parents=True)
    report = args.report or args.workspace.parent / "api-parity-report.md"
    return run(Path(__file__).resolve().parents[2], args.workspace, report)


if __name__ == "__main__":
    raise SystemExit(main())
