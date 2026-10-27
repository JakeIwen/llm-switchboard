#!/bin/bash
# Run on the Mac after cloning this checkout. The private key never leaves it.
set -eux -o pipefail
[ "$(uname -s)" = Darwin ]
repo=$(cd "$(dirname "$0")/.." && pwd)
# Machine names come from the checkout's local.env (see local.env.example).
if [ -f "$repo/local.env" ]; then . "$repo/local.env"; fi
pi=${LLM_SWITCHBOARD_PI_SSH:-pi}
mac=${LLM_SWITCHBOARD_MAC_NAME:-mac}
python=/opt/homebrew/bin/python3
"$python" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
mkdir -p -m 700 "$HOME/.ssh"
key=$HOME/.ssh/llm-switchboard-$mac
if [ ! -f "$key" ]; then
  ssh-keygen -t ed25519 -N '' -f "$key" -C "llm-switchboard-$mac"
fi
ssh "$pi" /home/pi/dev/llm-switchboard/bin/llm-switchboard host-authorize "$mac" < "$key.pub"
cd "$repo"
# bin/llm-switchboard prefers .venv's Python; without it, bare python3 may be macOS's 3.9.
UV_PYTHON_PREFERENCE=only-system uv sync --frozen --python "$python"
mkdir -p "$HOME/.local/bin"
ln -sf "$repo/bin/llm-switchboard" "$HOME/.local/bin/llm-switchboard"
"$python" -m llm_switchboard host-agent --once
"$python" -m llm_switchboard index
"$python" -m llm_switchboard host-install
launchctl print "gui/$(id -u)/dev.llm-switchboard.host-agent"
launchctl print "gui/$(id -u)/dev.llm-switchboard.search-index"
