#!/bin/bash
# The voice desk's Mac link, on the Mac (docs/voice-desk.md, "Mac link":
# "Installing the Mac side" and "Switching it off, revocation and rollback").
# Run after scripts/setup-mac.sh. Every check comes first, and the first two
# steps check both machines and change nothing on either.
#   scripts/setup-mac-desk.sh           install: key, link-authorize on the Pi,
#                                       mcp.json, the relay's launchd job
#   scripts/setup-mac-desk.sh --remove  unload the relay, link-revoke on the Pi,
#                                       delete the key files
# Machine names come from the checkout's local.env (see local.env.example).
set -eu -o pipefail

say() { echo "setup-mac-desk: $*"; }
fail() { echo "setup-mac-desk: $*" >&2; exit 1; }

remove=0
case "$*" in
  "") ;;
  --remove) remove=1 ;;
  *) echo "usage: $0 [--remove]" >&2; exit 2 ;;
esac

# -- checks: nothing changes on either machine before these pass ----------------
[ "$(uname -s)" = Darwin ] || fail "runs only on the Mac; nothing changed"
repo=$(cd "$(dirname "$0")/.." && pwd)
if [ -f "$repo/local.env" ]; then . "$repo/local.env"; fi
pi=${LLM_SWITCHBOARD_PI_SSH:-}
mac=${LLM_SWITCHBOARD_MAC_NAME:-}
alias_rule='^[A-Za-z0-9][A-Za-z0-9_.-]*$'
host_rule='^[a-z][a-z0-9-]{0,31}$'
[[ $pi =~ $alias_rule ]] || fail "LLM_SWITCHBOARD_PI_SSH in $repo/local.env must be the Pi's ssh alias; nothing changed"
[[ $mac =~ $host_rule ]] && [ "$mac" != pi ] \
  || fail "LLM_SWITCHBOARD_MAC_NAME in $repo/local.env must be this Mac's host name (a-z, 0-9, -; not pi); nothing changed"
python=$repo/.venv/bin/python
[ -x "$python" ] || fail "$python is missing (scripts/setup-mac.sh makes it); nothing changed"
# Removal needs only ssh: an off switch shouldn't wait on the install's tools.
programs="ssh ssh-keygen launchctl llm-switchboard"
if [ "$remove" = 1 ]; then programs=ssh; fi
for program in $programs; do
  command -v "$program" >/dev/null || fail "$program is not on PATH (scripts/setup-mac.sh links llm-switchboard); nothing changed"
done
pi_cli=/home/pi/dev/llm-switchboard/bin/llm-switchboard
key=$HOME/.ssh/llm-switchboard-desk-$mac
job=dev.llm-switchboard.desk-relay

if [ "$remove" = 1 ]; then
  # The order the design gives: the switch the Mac enforces first, then the
  # credential on the Pi, then the key files (tidying). Each step is
  # idempotent, so after a failure running --remove again completes it.
  say "1/3 unloading the relay (desk relay-install --unload)"
  "$python" -m llm_switchboard desk relay-install --unload \
    || fail "the relay's unload is not confirmed (above); nothing else changed; run --remove again"
  say "2/3 withdrawing this Mac's link key on the Pi (desk link-revoke $mac)"
  ssh "$pi" "$pi_cli" desk link-revoke "$mac" </dev/null \
    || fail "link-revoke on the Pi failed (above); the relay is unloaded and the key files are kept; run --remove again"
  say "3/3 deleting $key and $key.pub"
  rm -f "$key" "$key.pub"
  say "done: the relay is unloaded and the Pi no longer lists this Mac's key; mcp.json stays (nothing loads it without --desk)"
  exit 0
fi

# 1. This Mac's ssh-keygen signs and verifies (the relay signs each link's
#    proof with it), with a throwaway key in a temporary directory.
mac_self_test() {
  local dir=$1 namespace=llm-switchboard-desk-link
  ssh-keygen -q -t ed25519 -N '' -C desk-self-test -f "$dir/key" </dev/null || return 1
  printf 'llm-switchboard desk self-test\n' >"$dir/message"
  ssh-keygen -Y sign -f "$dir/key" -n "$namespace" "$dir/message" </dev/null || return 1
  printf 'self-test namespaces="%s" %s\n' "$namespace" "$(cat "$dir/key.pub")" >"$dir/signers"
  ssh-keygen -Y verify -f "$dir/signers" -I self-test -n "$namespace" -s "$dir/message.sig" <"$dir/message"
}
say "1/7 checking that this Mac's ssh-keygen signs and verifies (throwaway key)"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
mac_self_test "$scratch" \
  || fail "this Mac's ssh-keygen can't sign and verify SSH signatures (OpenSSH 8.1 or later is needed); nothing changed"
rm -rf "$scratch"
trap - EXIT

# 2. The Pi's verifier, through the code path its hub uses (exit 2: too old).
say "2/7 checking the Pi's ssh-keygen (desk link-authorize --self-test on $pi)"
status=0
ssh "$pi" "$pi_cli" desk link-authorize --self-test </dev/null || status=$?
case $status in
  0) ;;
  2) fail "the Pi's ssh-keygen can't verify SSH signatures (OpenSSH 8.1 or later is needed); nothing changed" ;;
  *) fail "desk link-authorize --self-test on $pi failed (exit $status); nothing changed" ;;
esac

# 3. The link key: used for nothing else, no passphrase (launchd uses it
#    unattended); the private key never leaves this Mac.
mkdir -p -m 700 "$HOME/.ssh"
if [ -f "$key" ]; then
  [ -f "$key.pub" ] || fail "$key exists without $key.pub; move it aside and run this again"
  say "3/7 $key exists; keeping it"
else
  say "3/7 generating $key"
  ssh-keygen -q -t ed25519 -N '' -C "llm-switchboard-desk-$mac" -f "$key" </dev/null
fi

say "4/7 listing the key on the Pi (desk link-authorize $mac)"
ssh "$pi" "$pi_cli" desk link-authorize "$mac" <"$key.pub" \
  || fail "link-authorize on the Pi failed (above); the key is kept; run this again"

say "5/7 writing this Mac's mcp.json (desk install-config)"
llm-switchboard desk install-config

say "6/7 installing the relay's launchd job (desk relay-install)"
"$python" -m llm_switchboard desk relay-install

say "7/7 the relay's job as launchd shows it"
launchctl print "gui/$(id -u)/$job"
say "done: sessions launched with claudep --desk connect through the relay"
