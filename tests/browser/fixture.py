"""Fake HOME for the browser test: ten interactive Claude sessions, one headless
run across three folders, an empty folder, and two fake Codex threads."""
import json, sys, re, uuid, os, time, shutil
home = sys.argv[1]
shutil.rmtree(home, ignore_errors=True)
HEADLESS_TITLE = "Headless fixture probe"
specs = [("dev/alpha", ["Alpha dashboard tweaks", "Alpha CAN decoder", "Fix alpha login bug", "Alpha telemetry export"]),
         ("dev/beta", ["Beta release notes", "Beta perf profiling", "Refactor beta parser"]),
         ("scratch", ["Reply with just OK", "Scratch test of tmux", "Quick question about systemd", HEADLESS_TITLE])]
t = time.time()
for folder, titles in specs:
    cwd = f"{home}/{folder}"; os.makedirs(cwd, exist_ok=True)
    pdir = f"{home}/.claude/projects/" + re.sub(r"[^a-zA-Z0-9]", "-", cwd); os.makedirs(pdir, exist_ok=True)
    for i, title in enumerate(titles):
        sid = str(uuid.uuid4())
        rec = [{"type": "user", "cwd": cwd, "sessionId": sid, "message": {"role": "user", "content": f"prompt for {title}"}},
               {"type": "assistant", "cwd": cwd, "message": {"content": [{"type": "text", "text": f"reply about {title}"}]}},
               {"type": "ai-title", "aiTitle": title, "sessionId": sid}]
        if title == HEADLESS_TITLE:
            for record in rec:
                record["entrypoint"] = "sdk-cli"
        p = f"{pdir}/{sid}.jsonl"
        open(p, "w").write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rec))
        m = t - (len(titles) - i) * 3600 * (3 if folder == "scratch" else 1); os.utime(p, (m, m))
os.makedirs(f"{home}/dev/gamma", exist_ok=True)
# Folder trust as Claude Code records it: everything under ~/dev, not ~/scratch.
json.dump({"projects": {f"{home}/dev": {"hasTrustDialogAccepted": True}}}, open(f"{home}/.claude.json", "w"))
# Threads for tests/fake_codex.py, which run.sh starts.
json.dump([{"tid": "01a13da1-a87b-76d0-bf7f-fbe83e9bc6%02d" % i, "cwd": f"{home}/{folder}", "name": name, "preview": preview}
           for i, (folder, name, preview) in enumerate([("dev/alpha", "Codex alpha refactor", "refactor alpha"),
                                                        ("dev/beta", "", "investigate the beta flake")])],
          open(f"{home}/codex-seed.json", "w"))
