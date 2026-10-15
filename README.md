# claude-always-remote

Keeps chosen Claude Code sessions running on the Pi, in tmux, with Remote
Control on, so they can always be reached from the Claude app or
claude.ai/code. A small web page on the Pi picks which sessions those are.

## How it works

- A user systemd service (`claude-always-remote`, started at boot through
  linger) runs a supervisor and the picker web app on port 8777.
- Each enabled session gets a window in a dedicated tmux server
  (`tmux -L claude-always-remote`). The window runs
  `bash -lic 'cd <cwd> && claudepd --resume <id> --remote-control <name>'`,
  so it uses the same `claudepd` function as an interactive shell (bypass
  permissions, no Model Gateway, which is what allows Remote Control).
- If claude exits, the supervisor relaunches it, backing off 10s, 20s, 40s…
  up to 10 minutes after quick failures. A run of 5+ minutes resets that.
- It won't launch a session that is already open in another claude process
  (say, an SSH terminal); it waits until that process exits.
- It waits for `api.anthropic.com:443` to be reachable before launching.
- The tmux server lives in its own systemd scope, so restarting or stopping
  the service leaves running sessions alone; the restarted supervisor adopts
  them.
- Enabled sessions are stored in `~/.config/claude-always-remote/sessions.json`.

## Install

```
./install.sh
loginctl enable-linger "$USER"   # once, so user services start at boot
```

Picker: `http://vanpi:8777` (from the LAN or Tailscale).

## CLI

```
claude-always-remote list                 # sessions, enabled first
claude-always-remote status               # enabled sessions and their state
claude-always-remote enable c96934f7 [--name "van telemetrics"]
claude-always-remote disable c96934f7
claude-always-remote restart c96934f7
claude-always-remote attach c96934f7      # open its tmux window (detach: Ctrl-b d)
claude-always-remote stop-all             # kill the tmux server
journalctl --user -u claude-always-remote -f
```

Ids can be any unique prefix.

## States

| State | Meaning |
| --- | --- |
| running | Window up and the status line shows `/rc active` (a bare `/rc` while a turn runs) |
| starting | Launched in the last 45s, or the status line shows `/rc connecting` / `/rc reconnecting` |
| attention | Running, but a prompt is waiting (e.g. folder trust), `/rc failed`, or no indicator after 45s; check the screen or attach |
| elsewhere | Open in another claude process; starts when that exits |
| offline | API unreachable; waiting |
| backoff | Exited; relaunch scheduled |
| missing | No transcript with that id |

## Notes

- Each session is a full claude process (~280 MB RSS on the Pi 4).
- If you `/clear` inside a kept session, Claude Code starts a new session id;
  the supervisor still relaunches the originally enabled id after an exit.
- The picker has no login. POSTs must be `application/json`, which blocks
  cross-site requests from browsers, but anyone on the LAN/tailnet can use it.

## Tests

```
python3 -m unittest discover -s tests
```
