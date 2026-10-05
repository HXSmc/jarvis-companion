#!/usr/bin/env bash
# AltStore source-feed publisher ("updates over WiFi", docs/HANDOFF.md
# "AltStore updates"). Pulls the latest green `ci-ipa` CI run and republishes
# SERVE_DIR/source.json + SERVE_DIR/JarvisCompanion.ipa so AltStore (added as
# a source once on the phone) can detect and offer the update.
#
# Idempotent: no-ops unless the newest successful ci-ipa run id differs from
# SERVE_DIR/.last_run. Run every 3h by
# launchd/com.jarvis.companion.altstore-publish.plist (+ RunAtLoad).
#
# AltStore source schema verified against faq.altstore.io/developers/
# make-a-source.md and updating-apps.md (2026-10-05): required app-version
# fields are "version" (CFBundleShortVersionString) and "buildVersion"
# (CFBundleVersion); "sha256" is not a schema field (computed here for the
# log only, not written into source.json).
set -euo pipefail

REPO="HXSmc/jarvis-companion"
BRANCH="ci-ipa"
ARTIFACT_NAME="JarvisCompanion-unsigned-ipa"
BUNDLE_ID="personal.jarvis.JarvisCompanion"
IPA_NAME="JarvisCompanion.ipa"
TAILNET_HOST="<mac-host>.<tailnet>.<tld>"
SERVE_PORT="8445"
HTTP_LOOPBACK_PORT="8446"  # must match launchd/com.jarvis.companion.altstore-http.plist

SERVE_DIR="${SERVE_DIR:-$HOME/Library/Application Support/jarvis-companion/altstore}"
LOG="$HOME/Library/Logs/jarvis-altstore-publish.log"

mkdir -p "$SERVE_DIR"
mkdir -p "$(dirname "$LOG")"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$LOG"
}

# Self-heal the :8445 serve mapping (RunAtLoad covers reboot/login; this
# covers a tailscaled restart in between). Scoped to this one port only --
# never "serve reset", never touches 8790/8443/443. Non-fatal: a tailscale
# hiccup here should not block the publish.
if command -v tailscale >/dev/null 2>&1; then
  if ! tailscale serve status 2>/dev/null | grep -q ":$SERVE_PORT"; then
    tailscale serve --bg --https="$SERVE_PORT" "http://127.0.0.1:$HTTP_LOOPBACK_PORT" >/dev/null 2>&1 \
      && log "re-asserted tailscale serve :$SERVE_PORT (was missing)" \
      || log "warn: tailscale serve re-assert for :$SERVE_PORT failed (non-fatal)"
  fi
fi

fail() {
  log "ERROR: $1"
  exit 1
}

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# --- 1. find the newest successful ci-ipa run -------------------------------
RUN_JSON="$(gh run list -R "$REPO" -b "$BRANCH" --status success --limit 1 \
  --json databaseId,headSha,createdAt 2>&1)" || fail "gh run list failed: $RUN_JSON"

RUN_ID="$(printf '%s' "$RUN_JSON" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(data[0]["databaseId"] if data else "")
')"

if [ -z "$RUN_ID" ]; then
  log "no successful $BRANCH run found; nothing to publish"
  exit 0
fi

LAST_RUN_FILE="$SERVE_DIR/.last_run"
LAST_RUN=""
[ -f "$LAST_RUN_FILE" ] && LAST_RUN="$(cat "$LAST_RUN_FILE")"

if [ "$RUN_ID" = "$LAST_RUN" ]; then
  log "up to date (run $RUN_ID already published)"
  exit 0
fi

# --- 2. download the IPA artifact from that run ------------------------------
gh run download "$RUN_ID" -R "$REPO" -n "$ARTIFACT_NAME" -D "$WORKDIR" \
  || fail "gh run download failed for run $RUN_ID"

SRC_IPA="$(find "$WORKDIR" -iname '*.ipa' -print -quit)"
[ -n "$SRC_IPA" ] || fail "no .ipa found in downloaded artifact for run $RUN_ID"

cp "$SRC_IPA" "$WORKDIR/$IPA_NAME" || fail "copy of downloaded ipa failed"

# --- 3. read version info out of the IPA's own Info.plist -------------------
VERSION="$(unzip -p "$WORKDIR/$IPA_NAME" 'Payload/*.app/Info.plist' \
  | plutil -extract CFBundleShortVersionString raw -o - - 2>/dev/null)" \
  || fail "could not extract CFBundleShortVersionString from IPA"
BUILD="$(unzip -p "$WORKDIR/$IPA_NAME" 'Payload/*.app/Info.plist' \
  | plutil -extract CFBundleVersion raw -o - - 2>/dev/null)" \
  || fail "could not extract CFBundleVersion from IPA"

[ -n "$VERSION" ] && [ -n "$BUILD" ] || fail "empty version/build extracted from IPA Info.plist"

SIZE="$(stat -f%z "$WORKDIR/$IPA_NAME")"
SHA256="$(shasum -a 256 "$WORKDIR/$IPA_NAME" | awk '{print $1}')"
RELEASE_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- 4. icon: reuse SERVE_DIR/icon.png if present; else try to make one from
#        ~/Downloads (non-fatal -- ~/Downloads can be TCC-blocked for a
#        launchd-spawned process; the common case is icon.png already there) --
if [ ! -f "$SERVE_DIR/icon.png" ]; then
  SRC_ICON="$HOME/Downloads/jarvis-icon-1024.png"
  if [ -f "$SRC_ICON" ]; then
    sips -Z 512 "$SRC_ICON" --out "$SERVE_DIR/icon.png" >/dev/null 2>&1 \
      && log "generated icon.png from $SRC_ICON" \
      || log "warn: could not read/scale $SRC_ICON (non-fatal, continuing without icon refresh)"
  else
    log "warn: no icon.png in SERVE_DIR and no source icon at $SRC_ICON (non-fatal)"
  fi
fi

DOWNLOAD_URL="https://$TAILNET_HOST:$SERVE_PORT/$IPA_NAME"
ICON_URL="https://$TAILNET_HOST:$SERVE_PORT/icon.png"

# --- 5. rewrite source.json atomically (values passed as argv, never
#        interpolated into the python source) -------------------------------
python3 - \
  "$WORKDIR/source.json" \
  "$BUNDLE_ID" \
  "$VERSION" \
  "$BUILD" \
  "$RELEASE_DATE" \
  "$DOWNLOAD_URL" \
  "$SIZE" \
  "$ICON_URL" <<'PYEOF' || fail "failed to generate source.json"
import json
import sys

out_path, bundle_id, version, build, date, download_url, size, icon_url = sys.argv[1:9]

source = {
    "name": "Jarvis",
    "identifier": "personal.jarvis.source",
    "apps": [
        {
            "name": "Jarvis Companion",
            "bundleIdentifier": bundle_id,
            "developerName": "Ali",
            "localizedDescription": "Personal Jarvis Companion iPhone app (mirroring, handoff, voice, remote screen control).",
            "iconURL": icon_url,
            "appPermissions": {
                "entitlements": [],
                "privacy": {
                    "NSUserNotificationsUsageDescription": "Notifies you when a handoff arrives from the Mac."
                }
            },
            "versions": [
                {
                    "version": version,
                    "buildVersion": build,
                    "date": date,
                    "downloadURL": download_url,
                    "size": int(size)
                }
            ]
        }
    ],
    "news": []
}

with open(out_path, "w") as f:
    json.dump(source, f, indent=2)
    f.write("\n")
PYEOF

python3 -c "import json; json.load(open('$WORKDIR/source.json'))" \
  || fail "generated source.json failed to re-parse as JSON"

# --- 6. publish atomically: IPA + source.json + .last_run -------------------
mv "$WORKDIR/$IPA_NAME" "$SERVE_DIR/$IPA_NAME"
mv "$WORKDIR/source.json" "$SERVE_DIR/source.json"
printf '%s' "$RUN_ID" >"$LAST_RUN_FILE"

log "published run=$RUN_ID version=$VERSION build=$BUILD size=${SIZE}b sha256=$SHA256"
