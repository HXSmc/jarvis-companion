#!/bin/bash
# T1.3 — expose the gateway on the tailnet (DESIGN.md "Auth model").
# Publishes https://<this-mac>.<tailnet>:8790 -> http://127.0.0.1:8788 (the gateway, T1.1).
# Idempotent: re-running replaces the serve config for this port (PLAN "T1.3").
set -euo pipefail

# Replace semantics per PLAN: (re)sets the serve config for :8790.
tailscale serve --https=8790 http://127.0.0.1:8788

# Print the resulting public URL (same Self.DNSName pattern as PLAN's manual check).
DNS_NAME=$(tailscale status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
echo "https://${DNS_NAME}:8790"
