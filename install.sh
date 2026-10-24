#!/bin/bash
# Installs the user service (started at boot via linger) and the CLI link.
set -eu -o pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required; install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun install.sh" >&2
  exit 1
fi
(cd "$repo" && UV_PYTHON_PREFERENCE=only-system UV_PYTHON_DOWNLOADS=never uv sync --frozen)
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir" "$HOME/.local/bin"
sed "s|@REPO@|$repo|g" "$repo/systemd/llm-switchboard.service" > "$unit_dir/llm-switchboard.service"
ln -sfn "$repo/bin/llm-switchboard" "$HOME/.local/bin/llm-switchboard"
systemctl --user daemon-reload
systemctl --user enable --now llm-switchboard.service
systemctl --user restart llm-switchboard.service
if [ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]; then
  echo "Linger is off, so the service won't start until you log in. Fix: loginctl enable-linger $USER" >&2
fi
echo "Picker: http://$(tailscale ip -4 2>/dev/null || echo '<tailscale address>'):8777 (Tailscale and localhost only)"
