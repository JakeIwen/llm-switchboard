#!/bin/bash
# Run on the Mac after cloning this checkout. The private key never leaves it.
set -eux -o pipefail
[ "$(uname -s)" = Darwin ]
repo=$(cd "$(dirname "$0")/.." && pwd)
python=/opt/homebrew/bin/python3
"$python" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
mkdir -p -m 700 /Users/jacobr/.ssh
key=/Users/jacobr/.ssh/llm-switchboard-m4mac
if [ ! -f "$key" ]; then
  ssh-keygen -t ed25519 -N '' -f "$key" -C llm-switchboard-m4mac
fi
ssh vanpi /home/pi/dev/llm-switchboard/bin/llm-switchboard host-authorize m4mac < "$key.pub"
cd "$repo"
"$python" -m llm_switchboard host-agent --once
"$python" -m llm_switchboard host-install
launchctl print "gui/$(id -u)/dev.llm-switchboard.host-agent"
