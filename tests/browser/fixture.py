"""Fake HOME for the browser test: ten sessions across three folders, plus an empty one."""
import json, sys, re, uuid, os, time, shutil
home = sys.argv[1]
shutil.rmtree(home, ignore_errors=True)
specs = [("dev/alpha", ["Alpha dashboard tweaks", "Alpha CAN decoder", "Fix alpha login bug", "Alpha telemetry export"]),
         ("dev/beta", ["Beta release notes", "Beta perf profiling", "Refactor beta parser"]),
         ("scratch", ["Reply with just OK", "Scratch test of tmux", "Quick question about systemd"])]
t = time.time()
for folder, titles in specs:
    cwd = f"{home}/{folder}"; os.makedirs(cwd, exist_ok=True)
    pdir = f"{home}/.claude/projects/" + re.sub(r"[^a-zA-Z0-9]", "-", cwd); os.makedirs(pdir, exist_ok=True)
    for i, title in enumerate(titles):
        sid = str(uuid.uuid4())
        rec = [{"type": "user", "cwd": cwd, "sessionId": sid, "message": {"role": "user", "content": f"prompt for {title}"}},
               {"type": "assistant", "cwd": cwd, "message": {"content": [{"type": "text", "text": f"reply about {title}"}]}},
               {"type": "ai-title", "aiTitle": title, "sessionId": sid}]
        p = f"{pdir}/{sid}.jsonl"
        open(p, "w").write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rec))
        m = t - (len(titles) - i) * 3600 * (3 if folder == "scratch" else 1); os.utime(p, (m, m))
os.makedirs(f"{home}/dev/gamma", exist_ok=True)
# Folder trust as Claude Code records it: everything under ~/dev, not ~/scratch.
json.dump({"projects": {f"{home}/dev": {"hasTrustDialogAccepted": True}}}, open(f"{home}/.claude.json", "w"))
