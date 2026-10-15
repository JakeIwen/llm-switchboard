#!/bin/bash
# Installs the user service (started at boot via linger) and the CLI link.
set -eu -o pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir" "$HOME/.local/bin"
sed "s|@REPO@|$repo|g" "$repo/systemd/claude-always-remote.service" > "$unit_dir/claude-always-remote.service"
ln -sfn "$repo/bin/claude-always-remote" "$HOME/.local/bin/claude-always-remote"
systemctl --user daemon-reload
systemctl --user enable --now claude-always-remote.service
systemctl --user restart claude-always-remote.service
if [ "$(loginctl show-user "$USER" -p Linger --value)" != "yes" ]; then
  echo "Linger is off, so the service won't start until you log in. Fix: loginctl enable-linger $USER" >&2
fi
echo "Picker: http://$(hostname):8777"
