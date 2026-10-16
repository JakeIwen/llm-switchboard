#!/bin/bash
# Browser test for the picker: a second instance with a fake HOME, its own tmux
# socket and port, driven by headless Chromium. Never touches the real sessions.
#   tests/browser/run.sh [screenshot-dir]
set -eu -o pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
port=8796
work=$(mktemp -d)
trap 'kill "$pid" "$codex" 2>/dev/null || true; tmux -L switchboard-browsertest kill-server 2>/dev/null || true; rm -rf "$work"' EXIT

[ -d "$here/node_modules/puppeteer-core" ] || (cd "$here" && PUPPETEER_SKIP_DOWNLOAD=1 npm install --silent)
python3 "$here/fixture.py" "$work/home"
python3 "$repo/tests/fake_codex.py" "$work/codex.sock" "$work/home/codex-seed.json" &
codex=$!
for _ in $(seq 50); do [ -S "$work/codex.sock" ] && break; sleep 0.1; done
HOME="$work/home" LLM_SWITCHBOARD_TMUX_SOCKET=switchboard-browsertest LLM_SWITCHBOARD_NOTIFY= XDG_CONFIG_HOME= XDG_DATA_HOME= \
  LLM_SWITCHBOARD_CODEX_SOCKET="$work/codex.sock" \
  "$repo/bin/llm-switchboard" --port "$port" serve --host 127.0.0.1 >"$work/serve.log" 2>&1 &
pid=$!
for _ in $(seq 50); do curl -sf "http://127.0.0.1:$port/api/sessions" >/dev/null && break; sleep 0.2; done
SWITCHBOARD_BASE="http://127.0.0.1:$port/" node "$here/ui-test.mjs" "$work/home" "${1:-}"
