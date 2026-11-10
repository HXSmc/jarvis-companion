#!/usr/bin/env bash
# mac-overlay-sign.sh APP: sign APP with a stable local code-signing identity.
#
# Why: an ad-hoc signature changes on every rebuild, so macOS (TCC) treats each
# reinstall as a new app and silently drops Accessibility / Screen Recording
# grants (the switches still look ON). A fixed self-signed certificate gives the
# app the same designated requirement on every install, so grants survive.
#
# The identity lives in its own keychain with a random password kept 0600 next
# to the overlay token, so signing never needs the login keychain password and
# never shows a keychain prompt. The only prompt is the one-time trust setting below.
set -euo pipefail

APP="${1:?usage: mac-overlay-sign.sh /path/to/App.app}"
NAME="JarvisOverlay Local Signing"
DIR="${JARVIS_SIGN_DIR:-$HOME/Library/Application Support/JarvisOverlay}"
KC="${JARVIS_SIGN_KEYCHAIN:-$HOME/Library/Keychains/jarvis-overlay-signing.keychain-db}"
PASSFILE="$DIR/signing-keychain.pass"
SSL=/usr/bin/openssl  # LibreSSL: its PKCS#12 output is what `security import` accepts

mkdir -p "$DIR"
chmod 700 "$DIR"

if [ ! -f "$KC" ] || [ ! -s "$PASSFILE" ] ||
   ! security unlock-keychain -p "$(cat "$PASSFILE")" "$KC" 2>/dev/null ||
   ! security find-certificate -c "$NAME" "$KC" >/dev/null 2>&1; then
  echo "creating signing identity: $NAME (one time; TCC grants must be given once more after this)"
  security delete-keychain "$KC" 2>/dev/null || rm -f "$KC"
  (umask 077; "$SSL" rand -hex 24 > "$PASSFILE")
  KCPW="$(cat "$PASSFILE")"
  security create-keychain -p "$KCPW" "$KC"
  security set-keychain-settings "$KC"  # no auto-lock timeout
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  cat > "$TMP/req.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
  "$SSL" req -x509 -newkey rsa:2048 -nodes -days 7300 -config "$TMP/req.cnf" \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
  "$SSL" pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" -name "$NAME" \
    -out "$TMP/id.p12" -passout "pass:$KCPW"
  security import "$TMP/id.p12" -k "$KC" -P "$KCPW" -T /usr/bin/codesign >/dev/null
  # Lets codesign use the key without a "codesign wants to access key" dialog.
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KCPW" "$KC" >/dev/null
fi

security unlock-keychain -p "$(cat "$PASSFILE")" "$KC"
# codesign only uses a trusted identity. Trusting it for code signing only is a one-time
# user trust setting: macOS asks for Touch ID or the login password once (never typed here).
if ! security find-identity -v -p codesigning "$KC" | grep -q "\"$NAME\""; then
  CERT="$(mktemp)"
  security find-certificate -c "$NAME" -p "$KC" > "$CERT"
  echo "trusting $NAME for code signing: approve the macOS Touch ID / password box (one time)"
  if [ -n "${JARVIS_SIGN_ADMIN_TRUST:-}" ]; then  # CI: no user to approve, passwordless sudo
    sudo security add-trusted-cert -d -r trustRoot -p codeSign -k /Library/Keychains/System.keychain "$CERT"
  else
    security add-trusted-cert -r trustRoot -p codeSign -k "$KC" "$CERT"
  fi
  rm -f "$CERT"
fi
HASH="$(security find-certificate -c "$NAME" -Z "$KC" | awk '/SHA-1 hash/ {print $NF; exit}')"
codesign --force --deep --keychain "$KC" --sign "$HASH" "$APP"
codesign --verify --deep --strict "$APP"
echo "signed with $NAME: $(codesign -d -r- "$APP" 2>&1 | grep -o 'certificate leaf = H"[0-9a-f]*"')"
