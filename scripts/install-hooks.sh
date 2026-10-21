#!/usr/bin/env bash
# install-hooks.sh — point git at .githooks/ and set the public-repo bot identity.
# Run once after cloning (or any time hooksPath/identity needs re-applying).
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "install-hooks: not inside a git repo" >&2
  exit 1
}
cd "$REPO_ROOT" || exit 1

git config core.hooksPath .githooks
git config user.email "74503116+HXSmc@users.noreply.github.com"
git config user.name "HXSmc"

echo "---- privacy-guard install status ----"
echo "core.hooksPath: $(git config core.hooksPath)"
echo "user.name:      $(git config user.name)"
echo "user.email:     $(git config user.email)"

if command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks:       present ($(gitleaks version 2>/dev/null | head -1))"
else
  echo "gitleaks:       NOT installed — guard still runs, but without the gitleaks pass. Install: brew install gitleaks"
fi

if [[ -f "$REPO_ROOT/.env" ]]; then
  echo ".env:           present — .env-value checks are active"
else
  echo ".env:           NOT present — guard will warn and skip .env-value checks (pattern + generic checks still run)"
fi

for f in "$REPO_ROOT/.githooks/pre-commit" "$REPO_ROOT/.githooks/pre-push" "$REPO_ROOT/scripts/privacy-guard.sh"; do
  if [[ -x "$f" ]]; then
    echo "executable:     ${f#"$REPO_ROOT"/}"
  else
    echo "NOT EXECUTABLE: ${f#"$REPO_ROOT"/} — fixing"
    chmod +x "$f"
  fi
done

echo "---------------------------------------"
echo "Privacy guard hooks installed."
