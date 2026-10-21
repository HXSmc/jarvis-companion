#!/usr/bin/env bash
# privacy-guard.sh — block private strings from entering a PUBLIC repo.
#
# Usage:
#   scripts/privacy-guard.sh                 # scan staged diff (git diff --cached -U0)
#   scripts/privacy-guard.sh --range A..B    # scan added lines in a commit range,
#                                             # plus commit messages + author/committer emails
#
# Blocks (exit 1) when an added line contains:
#   a) any value from the local gitignored .env (KEY=VALUE, quotes stripped,
#      JSON-ish quoted tokens and comma-separated list items also checked;
#      values shorter than 6 chars or listed in .privacy-allow are ignored)
#   b) any line (extended regex) from the optional gitignored .privacy-patterns
#   c) a small set of generic "safe to keep public" regexes (tailnet host,
#      LAN/CGNAT IPs, home paths, MAC addresses, personal mail domains,
#      private-key headers)
#
# Also runs gitleaks (protect --staged for pre-commit, detect --log-opts for
# pre-push) if installed; warns but does not fail if gitleaks is missing.
#
# Exit 0 = clean, exit 1 = blocked.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "privacy-guard: not inside a git repo" >&2
  exit 1
}
cd "$REPO_ROOT" || exit 1

ENV_FILE="$REPO_ROOT/.env"
PATTERNS_FILE="$REPO_ROOT/.privacy-patterns"
ALLOW_FILE="$REPO_ROOT/.privacy-allow"

MODE="staged"
RANGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --range)
      MODE="range"
      RANGE="${2:-}"
      shift 2
      ;;
    *)
      echo "privacy-guard: unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

FAIL=0

# ---- masking helper: never print a raw secret/value, even in our own output ----
mask() {
  local s="$1"
  local len=${#s}
  if (( len <= 2 )); then
    printf '**'
  else
    local stars
    stars=$(printf '%*s' $((len - 2)) '' | tr ' ' '*')
    printf '%s%s%s' "${s:0:1}" "$stars" "${s: -1}"
  fi
}

# ---- (a) load+filter .env values ----
declare -a ENV_VALUES=()
declare -a ALLOW_VALUES=()

if [[ -f "$ALLOW_FILE" ]]; then
  while IFS= read -r a; do
    [[ -z "$a" ]] && continue
    ALLOW_VALUES+=("$a")
  done < "$ALLOW_FILE"
fi

is_allowed() {
  local v="$1"
  local a
  for a in "${ALLOW_VALUES[@]:-}"; do
    [[ "$v" == "$a" ]] && return 0
  done
  return 1
}

add_env_value() {
  local v="$1"
  [[ -z "$v" ]] && return
  (( ${#v} < 6 )) && return
  is_allowed "$v" && return
  ENV_VALUES+=("$v")
}

if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    if [[ "$line" =~ ^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=(.*)$ ]]; then
      raw_val="${BASH_REMATCH[1]}"
      val="$raw_val"
      # strip one layer of matching surrounding quotes
      if [[ "$val" == \"*\" && ${#val} -ge 2 ]]; then
        val="${val:1:${#val}-2}"
      elif [[ "$val" == \'*\' && ${#val} -ge 2 ]]; then
        val="${val:1:${#val}-2}"
      fi
      add_env_value "$val"

      # JSON-ish: pull out quoted tokens from the raw value
      scan="$raw_val"
      while [[ "$scan" =~ \"([^\"]+)\" ]]; do
        tok="${BASH_REMATCH[1]}"
        add_env_value "$tok"
        scan="${scan/\"$tok\"/}"
      done

      # comma-separated list values (e.g. accounts/domains lists)
      if [[ "$val" == *,* ]]; then
        IFS=',' read -ra parts <<< "$val"
        for p in "${parts[@]}"; do
          p="${p#"${p%%[![:space:]]*}"}"   # trim leading space
          p="${p%"${p##*[![:space:]]}"}"   # trim trailing space
          add_env_value "$p"
        done
      fi
    fi
  done < "$ENV_FILE"
else
  echo "privacy-guard: WARN .env not found at $ENV_FILE — skipping .env-value checks (pattern + generic checks still run)" >&2
fi

# ---- (b) load .privacy-patterns ----
declare -a USER_PATTERNS=()
if [[ -f "$PATTERNS_FILE" ]]; then
  while IFS= read -r p || [[ -n "$p" ]]; do
    [[ -z "$p" ]] && continue
    [[ "$p" =~ ^# ]] && continue
    USER_PATTERNS+=("$p")
  done < "$PATTERNS_FILE"
fi

# ---- (c) generic "safe to keep public" regexes ----
# (plain indexed arrays, not associative — macOS ships bash 3.2 with no
# declare -A support, and this script must run on the stock /bin/bash)
GENERIC_NAMES=(tailnet-host lan-ip cgnat-ip mac-home-path linux-home-path mac-address personal-mail-domain private-key-header)
GENERIC_REGEXES=(
  '\.ts\.net'
  '192\.168\.[0-9]+\.[0-9]+'
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+'
  '/Users/[A-Za-z]'
  '/home/[A-Za-z]'
  '([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}'
  '@(gmail|hotmail|outlook|icloud|yahoo)\.'
  'BEGIN [A-Z ]*PRIVATE KEY'
)

MAC_PLACEHOLDER_RE='aa:bb:cc:dd:ee:(ff|02)'

# ---- core line checker ----
# args: file, content(added line, without leading '+')
check_line() {
  local file="$1" content="$2"
  local hit=0
  local v pat name re m

  for v in "${ENV_VALUES[@]:-}"; do
    [[ -z "$v" ]] && continue
    if [[ "$content" == *"$v"* ]]; then
      echo "BLOCKED [.env value, len=${#v}] $file: $(mask "$v")" >&2
      hit=1
    fi
  done

  for pat in "${USER_PATTERNS[@]:-}"; do
    [[ -z "$pat" ]] && continue
    m=$(grep -Eo -- "$pat" <<< "$content" | head -1) || true
    if [[ -n "$m" ]]; then
      echo "BLOCKED [.privacy-patterns rule] $file: $(mask "$m")" >&2
      hit=1
    fi
  done

  local i name
  for i in "${!GENERIC_NAMES[@]}"; do
    name="${GENERIC_NAMES[$i]}"
    re="${GENERIC_REGEXES[$i]}"
    m=$(grep -Eo -- "$re" <<< "$content" | head -1) || true
    if [[ -n "$m" ]]; then
      if [[ "$name" == "mac-address" ]] && echo "$m" | grep -Eqi "$MAC_PLACEHOLDER_RE"; then
        continue
      fi
      echo "BLOCKED [generic:$name] $file: $(mask "$m")" >&2
      hit=1
    fi
  done

  [[ $hit -eq 1 ]] && FAIL=1
}

# ---- run the diff scan ----
if [[ "$MODE" == "range" ]]; then
  [[ -z "$RANGE" ]] && { echo "privacy-guard: --range requires A..B" >&2; exit 2; }
  DIFF_OUTPUT="$(git diff -U0 --no-color "$RANGE" 2>/dev/null)" || DIFF_OUTPUT=""
else
  DIFF_OUTPUT="$(git diff --cached -U0 --no-color 2>/dev/null)" || DIFF_OUTPUT=""
fi

current_file="(unknown)"
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  case "$line" in
    "+++ "*)
      current_file="${line#+++ }"
      current_file="${current_file#b/}"
      continue
      ;;
    "--- "*|"diff --git"*|"index "*|"@@"*|"Binary files"*)
      continue
      ;;
    "+"*)
      check_line "$current_file" "${line#+}"
      ;;
    *)
      continue
      ;;
  esac
done <<< "$DIFF_OUTPUT"

# ---- range mode: also scan commit messages + author/committer emails ----
if [[ "$MODE" == "range" ]]; then
  while IFS= read -r commit; do
    [[ -z "$commit" ]] && continue
    body="$(git log -1 --format='%B' "$commit")"
    author_email="$(git log -1 --format='%ae' "$commit")"
    committer_email="$(git log -1 --format='%ce' "$commit")"

    while IFS= read -r bline; do
      [[ -z "$bline" ]] && continue
      check_line "commit:$commit (message)" "$bline"
    done <<< "$body"

    check_line "commit:$commit (author email)" "$author_email"
    check_line "commit:$commit (committer email)" "$committer_email"
  done < <(git rev-list "$RANGE" 2>/dev/null)
fi

# ---- gitleaks (advisory if missing, blocking if it finds something) ----
if command -v gitleaks >/dev/null 2>&1; then
  if [[ "$MODE" == "range" ]]; then
    if ! gitleaks detect --log-opts="$RANGE" --redact --no-banner --exit-code 1 2>&1 | sed 's/^/gitleaks: /' >&2; then
      FAIL=1
    fi
  else
    if ! gitleaks protect --staged --redact --no-banner --exit-code 1 2>&1 | sed 's/^/gitleaks: /' >&2; then
      FAIL=1
    fi
  fi
else
  echo "privacy-guard: WARN gitleaks not installed — skipping gitleaks scan (install with: brew install gitleaks)" >&2
fi

if [[ "$FAIL" -eq 1 ]]; then
  echo "privacy-guard: BLOCKED — private content detected in added lines. Fix above, then retry." >&2
  exit 1
fi

echo "privacy-guard: clean." >&2
exit 0
