#!/bin/bash
# Installs and reconciles the voice desk's two user units; install.sh calls it
# after the picker unit. Usage: desk-units.sh <repo> <local.env path>.
# Reconciling matters because `enable --now` leaves an active unit running on
# its old process: a rotated TLS leaf, a changed unit file or a new adb serial
# only takes effect after a restart. Every action is printed.
set -eu -o pipefail
repo=$1
env_file=$2
unit_dir="$HOME/.config/systemd/user"
desk_dir="$HOME/.config/llm-switchboard/desk"
tls_dir="$desk_dir/tls"
applied_serial_file="$desk_dir/adb-serial.applied"
mkdir -p "$unit_dir" "$desk_dir"

# Renders a unit template; returns 0 when the installed file changed.
render_unit() {
  local name=$1 target="$unit_dir/$1" tmp
  tmp=$(mktemp "$unit_dir/.$name.XXXXXX")
  sed "s|@REPO@|$repo|g" "$repo/systemd/$name" > "$tmp"
  if [ -f "$target" ] && cmp -s "$tmp" "$target"; then
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$target"
}

hub=llm-switchboard-desk.service
adb=llm-switchboard-desk-adb.service
hub_unit_changed=0
adb_unit_changed=0
render_unit "$hub" && hub_unit_changed=1
render_unit "$adb" && adb_unit_changed=1
systemctl --user daemon-reload

if [ -f "$tls_dir/leaf.pem" ] && [ -f "$tls_dir/leaf.key" ]; then
  if systemctl --user is-active --quiet "$hub"; then
    systemctl --user restart "$hub"
    echo "Voice desk hub: restarted (picks up the current TLS leaf and unit)."
  else
    systemctl --user enable --now "$hub"
    echo "Voice desk hub: enabled and started."
  fi
else
  echo "Voice desk hub not enabled: copy leaf.pem and leaf.key from scripts/desk-ca.sh's output into $tls_dir (mode 0600), then rerun install.sh." >&2
fi

serial=$(sed -n 's/^DESK_ADB_SERIAL=//p' "$env_file" 2>/dev/null | tail -n 1 | tr -d "\"' " || true)
applied=$(cat "$applied_serial_file" 2>/dev/null || true)
if [ -n "$serial" ]; then
  if systemctl --user is-active --quiet "$adb"; then
    if [ "$serial" != "$applied" ] || [ "$adb_unit_changed" = 1 ]; then
      systemctl --user restart "$adb"
      echo "Tablet link: restarted (serial or unit changed)."
    else
      echo "Tablet link: already running with this serial; left alone."
    fi
  else
    systemctl --user enable --now "$adb"
    echo "Tablet link: enabled and started."
  fi
  printf '%s\n' "$serial" > "$applied_serial_file"
else
  if systemctl --user is-enabled --quiet "$adb" || systemctl --user is-active --quiet "$adb"; then
    systemctl --user disable --now "$adb"
    echo "Tablet link: disabled and stopped (DESK_ADB_SERIAL is empty)."
  else
    echo "Tablet link not enabled: set DESK_ADB_SERIAL=<adb serial> in $env_file (adb devices lists it), then rerun install.sh."
  fi
  rm -f "$applied_serial_file"
fi
