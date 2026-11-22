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
sed "s|@REPO@|$repo|g" "$repo/systemd/llm-switchboard-desk.service" > "$unit_dir/llm-switchboard-desk.service"
sed "s|@REPO@|$repo|g" "$repo/systemd/llm-switchboard-desk-adb.service" > "$unit_dir/llm-switchboard-desk-adb.service"
ln -sfn "$repo/bin/llm-switchboard" "$HOME/.local/bin/llm-switchboard"
systemctl --user daemon-reload
systemctl --user enable --now llm-switchboard.service
systemctl --user restart llm-switchboard.service
# Voice desk: the pinned MCP config, then the hub (needs its TLS leaf) and the tablet link.
"$repo/bin/llm-switchboard" desk install-config
tls_dir="$HOME/.config/llm-switchboard/desk/tls"
if [ -f "$tls_dir/leaf.pem" ] && [ -f "$tls_dir/leaf.key" ]; then
  systemctl --user enable --now llm-switchboard-desk.service
else
  echo "Voice desk hub not enabled: copy leaf.pem and leaf.key from scripts/desk-ca.sh's output into $tls_dir (mode 0600), then rerun install.sh." >&2
fi
desk_serial=$(sed -n 's/^DESK_ADB_SERIAL=//p' "$repo/local.env" 2>/dev/null | tail -n 1 | tr -d "\"' " || true)
if [ -n "$desk_serial" ]; then
  systemctl --user enable --now llm-switchboard-desk-adb.service
else
  echo "Tablet link not enabled: set DESK_ADB_SERIAL=<adb serial> in $repo/local.env (adb devices lists it), then rerun install.sh."
fi
if [ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]; then
  echo "Linger is off, so the service won't start until you log in. Fix: loginctl enable-linger $USER" >&2
fi
echo "Picker: http://$(tailscale ip -4 2>/dev/null || echo '<tailscale address>'):8777 (Tailscale and localhost only)"
