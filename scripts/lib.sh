# shellcheck shell=bash
# Shared helpers sourced by every script.

set -euo pipefail

_c() { printf '\033[%sm' "$1"; }
log()   { printf '%s[+]%s %s\n' "$(_c '1;32')" "$(_c 0)" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$(_c '1;33')" "$(_c 0)" "$*" >&2; }
err()   { printf '%s[x]%s %s\n' "$(_c '1;31')" "$(_c 0)" "$*" >&2; }
die()   { err "$*"; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "required tool '$1' not found in PATH"; }

confirm() {
  # confirm "message"  -> returns 0 if user types y/Y
  local reply
  read -r -p "$1 [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]]
}
