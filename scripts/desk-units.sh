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
# Same root as DeskConfig.config_dir: an empty XDG_CONFIG_HOME counts as unset.
desk_dir="${XDG_CONFIG_HOME:-$HOME/.config}/llm-switchboard/desk"
tls_dir="$desk_dir/tls"
applied_file="$desk_dir/adb-link.applied"  # line 1 serial, line 2 checksum of bin/desk-adb-link
mkdir -p "$unit_dir" "$desk_dir"

# Renders a unit template into place. Sets unit_changed=1 when the installed
# file differs. Every step is checked explicitly: this runs under `if !`, where
# errexit is off, so a failed sed or mv must not read as "unchanged".
unit_changed=0
render_unit() {
  local name=$1 target="$unit_dir/$1" tmp
  unit_changed=0
  tmp=$(mktemp "$unit_dir/.$name.XXXXXX") || return 1
  if ! sed "s|@REPO@|$repo|g" "$repo/systemd/$name" > "$tmp" || ! [ -s "$tmp" ]; then
    rm -f "$tmp"
    return 1
  fi
  if [ -f "$target" ] && cmp -s "$tmp" "$target"; then
    rm -f "$tmp"
    return 0
  fi
  if ! mv "$tmp" "$target"; then
    rm -f "$tmp"
    return 1
  fi
  unit_changed=1
}

hub=llm-switchboard-desk.service
adb=llm-switchboard-desk-adb.service
if ! render_unit "$hub"; then
  echo "desk-units: could not install $hub; nothing reloaded or restarted" >&2
  exit 1
fi
if ! render_unit "$adb"; then
  echo "desk-units: could not install $adb; nothing reloaded or restarted" >&2
  exit 1
fi
adb_unit_changed=$unit_changed
systemctl --user daemon-reload

unit_active() { systemctl --user is-active --quiet "$1"; }
unit_enabled() { systemctl --user is-enabled --quiet "$1"; }

# Brings a unit to enabled and running on its current files: a running unit is
# restarted (it keeps its old process, credential and script otherwise) and an
# active-but-disabled one is enabled so it survives a reboot.
ensure_running() {  # ensure_running <unit> <restart message> <start message>
  if unit_active "$1"; then
    unit_enabled "$1" || systemctl --user enable "$1"
    systemctl --user restart "$1"
    echo "$2"
  else
    systemctl --user enable --now "$1"
    echo "$3"
  fi
}

if [ -f "$tls_dir/leaf.pem" ] && [ -f "$tls_dir/leaf.key" ]; then
  ensure_running "$hub" "Voice desk hub: restarted (picks up the current TLS leaf and unit)." \
    "Voice desk hub: enabled and started."
else
  if unit_active "$hub" || unit_enabled "$hub"; then
    systemctl --user disable --now "$hub"
    echo "Voice desk hub: stopped and disabled (its TLS leaf or key is missing)." >&2
  fi
  echo "Voice desk hub not enabled: copy leaf.pem and leaf.key from scripts/desk-ca.sh's output into $tls_dir (mode 0600), then rerun install.sh." >&2
fi

serial=$(sed -n 's/^DESK_ADB_SERIAL=//p' "$env_file" 2>/dev/null | tail -n 1 | tr -d "\"' " || true)
script_sum=$(cksum < "$repo/bin/desk-adb-link" | cut -d' ' -f1,2)
applied=$(cat "$applied_file" 2>/dev/null || true)
wanted=$(printf '%s\n%s' "$serial" "$script_sum")
if [ -n "$serial" ]; then
  if unit_active "$adb" && [ "$wanted" = "$applied" ] && [ "$adb_unit_changed" = 0 ]; then
    unit_enabled "$adb" || systemctl --user enable "$adb"
    echo "Tablet link: already running with this serial and script; left alone."
  else
    ensure_running "$adb" "Tablet link: restarted (serial, script or unit changed)." \
      "Tablet link: enabled and started."
  fi
  printf '%s\n' "$wanted" > "$applied_file"
else
  if unit_enabled "$adb" || unit_active "$adb"; then
    systemctl --user disable --now "$adb"
    echo "Tablet link: disabled and stopped (DESK_ADB_SERIAL is empty)."
  else
    echo "Tablet link not enabled: set DESK_ADB_SERIAL=<adb serial> in $env_file (adb devices lists it), then rerun install.sh."
  fi
  rm -f "$applied_file"
fi
