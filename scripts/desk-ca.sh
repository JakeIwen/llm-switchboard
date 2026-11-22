#!/bin/bash
# One-off, on the Mac: make a single-use CA, sign the voice desk's localhost
# leaf with it, then destroy the CA key before anything is written to <out-dir>.
# No signer survives, so nothing can mint another certificate the tablet trusts.
#   desk-ca.sh <out-dir>                    real CA + leaf
#   desk-ca.sh --constraint-test <out-dir>  disposable CA + a leaf outside the CA's
#                                           name constraints, for the tablet check
set -euo pipefail
umask 077

die() { echo "desk-ca.sh: $*" >&2; exit 1; }

mode=real
if [ "${1:-}" = "--constraint-test" ]; then
  mode=constraint-test
  shift
fi
[ "$#" -eq 1 ] || die "usage: desk-ca.sh [--constraint-test] <out-dir>"
out_dir=$1
[ ! -e "$out_dir" ] || die "$out_dir already exists; refusing to overwrite (choose a new directory)"
[ -d "$(dirname "$out_dir")" ] || die "parent of $out_dir does not exist"

# nameConstraints and the extension syntax below need OpenSSL 3 or later; the
# system LibreSSL on macOS doesn't qualify.
openssl_bin=
for candidate in "${OPENSSL:-}" "$(command -v openssl || true)" \
    /opt/homebrew/opt/openssl@3/bin/openssl /opt/homebrew/bin/openssl \
    /usr/local/opt/openssl@3/bin/openssl; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  if "$candidate" version 2>/dev/null | grep -Eq '^OpenSSL ([3-9]|[1-9][0-9])\.'; then
    openssl_bin=$candidate
    break
  fi
done
[ -n "$openssl_bin" ] || die "needs OpenSSL 3 or later (LibreSSL and 1.1 won't do); try: brew install openssl@3 and rerun with OPENSSL=/opt/homebrew/opt/openssl@3/bin/openssl"

work=$(mktemp -d "${TMPDIR:-/tmp}/desk-ca.XXXXXX")
chmod 700 "$work"
created_out=0
finished=0
cleanup() {
  rm -rf "$work"
  # A half-written out-dir would look usable; remove it if we didn't finish.
  if [ "$created_out" = 1 ] && [ "$finished" != 1 ]; then
    rm -rf "$out_dir"
  fi
}
trap cleanup EXIT
trap 'exit 1' ERR INT TERM

if [ "$mode" = real ]; then
  leaf_san="DNS:localhost, IP:127.0.0.1"
  ca_name=desk-ca.crt
  leaf_pem_name=leaf.pem
  leaf_key_name=leaf.key
else
  leaf_san="DNS:localhost, DNS:outside.example"
  ca_name=constraint-test-ca.crt
  leaf_pem_name=constraint-test-leaf.pem
  leaf_key_name=constraint-test-leaf.key
fi

cat > "$work/ca.cnf" <<'CNF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = llm-switchboard voice desk CA (single use)
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
nameConstraints = critical, permitted;DNS:localhost, permitted;IP:127.0.0.1/255.255.255.255
subjectKeyIdentifier = hash
CNF
cat > "$work/leaf.cnf" <<CNF
[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $leaf_san
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
CNF

"$openssl_bin" genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$work/ca.key" 2>/dev/null
"$openssl_bin" req -x509 -new -key "$work/ca.key" -sha256 -days 1095 \
  -config "$work/ca.cnf" -extensions v3_ca -out "$work/ca.crt"
"$openssl_bin" genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$work/leaf.key" 2>/dev/null
"$openssl_bin" req -new -key "$work/leaf.key" -subj "/CN=localhost" -out "$work/leaf.csr"
"$openssl_bin" x509 -req -in "$work/leaf.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" \
  -CAcreateserial -sha256 -days 820 -extfile "$work/leaf.cnf" -extensions v3_leaf \
  -out "$work/leaf.crt" 2>/dev/null

if [ "$mode" = real ]; then
  "$openssl_bin" verify -CAfile "$work/ca.crt" -purpose sslserver "$work/leaf.crt" >/dev/null \
    || die "the leaf does not verify against the CA"
elif "$openssl_bin" verify -CAfile "$work/ca.crt" -purpose sslserver "$work/leaf.crt" >/dev/null 2>&1; then
  die "the constraint-test leaf verified; the CA's name constraints are not being enforced"
fi

fingerprint=$("$openssl_bin" x509 -in "$work/ca.crt" -noout -fingerprint -sha256)
ca_pem=$(<"$work/ca.crt")
leaf_pem=$(<"$work/leaf.crt")
leaf_key=$(<"$work/leaf.key")

# The signer goes away before the out-dir exists.
rm -f "$work/ca.key"
rm -rf "$work"

mkdir -m 700 "$out_dir"
created_out=1
printf '%s\n' "$ca_pem" > "$out_dir/$ca_name"
printf '%s\n' "$leaf_pem" > "$out_dir/$leaf_pem_name"
printf '%s\n' "$leaf_key" > "$out_dir/$leaf_key_name"
chmod 644 "$out_dir/$ca_name"
chmod 600 "$out_dir/$leaf_pem_name" "$out_dir/$leaf_key_name"
finished=1

echo "CA key destroyed. Wrote $out_dir/{$ca_name,$leaf_pem_name,$leaf_key_name}"
echo "CA $fingerprint"
if [ "$mode" = real ]; then
  cat <<NEXT

Next steps:
  1. Pi: scp the leaf files (key and cert stay mode 0600):
       ssh vanpi 'mkdir -p -m 700 ~/.config/llm-switchboard/desk/tls'
       scp $out_dir/leaf.pem $out_dir/leaf.key vanpi:.config/llm-switchboard/desk/tls/
       ssh vanpi 'chmod 600 ~/.config/llm-switchboard/desk/tls/leaf.*'
  2. Tablet: adb push $out_dir/desk-ca.crt /sdcard/Download/
  3. Tablet: Settings > Security > Install from storage (a screen lock is required);
     compare the SHA-256 fingerprint Android shows with the one printed above.
  4. Delete $out_dir from the Mac once the tablet and Pi have them.
NEXT
else
  echo "Constraint test: serve $out_dir/constraint-test-leaf.* for localhost on the tablet after installing $ca_name; a client enforcing name constraints must reject it."
fi
