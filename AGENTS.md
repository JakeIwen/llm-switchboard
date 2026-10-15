# claude-always-remote

Python 3.11 standard library only. Run tests with
`python3 -m unittest discover -s tests`. The service runs from this checkout
(`bin/claude-always-remote` → `python3 -m claude_always_remote`), so after a
code change: `systemctl --user restart claude-always-remote` (sessions keep
running; the supervisor adopts their windows).

## Where things live

| Module | Concern |
| --- | --- |
| `claude_always_remote/paths.py` | Shared locations, names, the managed-process env marker, default port |
| `claude_always_remote/session_id.py` | Session id validation and short ids |
| `claude_always_remote/store.py` | The enabled-session list (locked, atomic JSON) |
| `claude_always_remote/transcripts.py` | Finding and summarizing `~/.claude/projects/*/*.jsonl` (cached) |
| `claude_always_remote/live_registry.py` | Which claude processes are running (`~/.claude/sessions`, `/proc`) |
| `claude_always_remote/tmux.py` | The dedicated tmux server and its windows |
| `claude_always_remote/launcher.py` | The pane command (goes through `claudepd`) |
| `claude_always_remote/connectivity.py` | API reachability gate |
| `claude_always_remote/supervisor.py` | Reconcile loop, `State` enum, backoff |
| `claude_always_remote/catalog.py` | Row shape shared by the web API and CLI |
| `claude_always_remote/web.py` | HTTP API and static page |
| `claude_always_remote/cli.py` | argparse entry point |
| `claude_always_remote/static/index.html` | The picker UI (vanilla JS) |
| `systemd/`, `install.sh` | User unit template and installer |

## Rules

- Only the supervisor thread starts or kills tmux windows; the web app and
  CLI edit the store and call `wake()` / `request_restart()`.
- Sessions must launch through the `claudepd` bash function, never a copy
  of its flags: Remote Control depends on its settings.
- State names are the `State` enum in `supervisor.py`; the page's `STATE`
  table mirrors them for labels.
