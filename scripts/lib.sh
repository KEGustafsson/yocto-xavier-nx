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

# ---------------------------------------------------------------------------
# One build directory can only ever have one bitbake in it, and the stale-
# server cleanup below makes that stricter still: between _stale_bitbake_pids
# finding no server and the `rm -f` of the leftover socket, a bitbake started
# from another terminal can bind that very path - and then have its socket
# unlinked out from under it, leaving a running server no client can reach.
#
# So take an exclusive lock on the build directory and hold it across BOTH the
# cleanup and the build itself. Locking only the cleanup would close a window
# of milliseconds and leave the hours-long one wide open.
#
# The lock lives on a file descriptor of the calling shell, deliberately:
#   - it is held until this script exits, with no trap/cleanup to forget, so
#     "hold it through bitbake" is automatic rather than something the caller
#     has to remember;
#   - the kernel releases it when the process dies, however it dies, so a
#     Ctrl-C or a SIGKILL can never leave a lock nobody can clear (unlike a
#     lock-file-exists scheme, which is exactly how stale bitbake sockets get
#     left behind in the first place).
#
# fd 9 is this file's; nothing else in these scripts uses it.
acquire_build_lock() {
  local build_dir="$1"
  local lock="${build_dir}/boat-build.lock"

  [[ -d "${build_dir}" ]] || die "build directory does not exist: ${build_dir}"

  # util-linux flock(1). Present on every distro these scripts support, but
  # don't make the build hard-fail on a host that somehow lacks it - warn and
  # run unserialized, which is exactly the old behaviour.
  if ! command -v flock >/dev/null 2>&1; then
    warn "flock(1) not found - running WITHOUT the build lock; do not start a"
    warn "second build in ${build_dir} while this one runs."
    return 0
  fi

  # >> not >: never truncate, so two shells opening it concurrently can't
  # race over the (empty, unused) contents. The file is a lock handle only.
  exec 9>>"${lock}" || die "cannot open build lock: ${lock}"

  if flock -n 9; then
    return 0
  fi

  warn "Another build already holds the lock on ${build_dir}."
  warn "Waiting for it to finish (Ctrl-C to give up) ..."
  flock 9 || die "failed to acquire build lock: ${lock}"
  log "Build lock acquired"
  return 0
}

# ---------------------------------------------------------------------------
# bitbake keeps a memory-resident "cooker" server alive between invocations so
# the next command doesn't have to re-parse 3000+ recipes. That server outlives
# its client: if a previous run's client went away without shutting it down -
# Ctrl-C at the wrong moment, a closed terminal, an editor/CI job reaping the
# foreground process, `kill` on the wrong pid - the server keeps running, keeps
# holding the build directory, and the next bitbake burns its retry budget
# failing to connect before dying with:
#
#     ERROR: Command '...buildTargets...' failed: Busy (buildTargets in progress)
#
# CONFIRMED: that is not a build error and re-running doesn't help; the stale
# server has to go first. Clear it out before starting anything.
#
# Only ever targets servers whose command line names THIS build directory's
# socket, so an unrelated bitbake elsewhere on the host is left alone.
#
# WARNING: if you deliberately have a build running in another terminal, this
# kills it. Set BOAT_KEEP_BITBAKE_SERVER=1 to skip and get the "Busy" error
# instead. Call it under acquire_build_lock (above) so a build starting in
# parallel can't bind the socket this function is in the middle of removing.
kill_stale_bitbake() {
  local build_dir="$1"
  local sock="${build_dir}/bitbake.sock"

  if [[ "${BOAT_KEEP_BITBAKE_SERVER:-0}" == "1" ]]; then
    log "BOAT_KEEP_BITBAKE_SERVER=1 - leaving any running bitbake server alone"
    return 0
  fi

  # Identify the server by EXACT argv elements, read from /proc, rather than
  # by substring-matching a flattened `ps`/`pgrep -f` line. CONFIRMED THE HARD
  # WAY: any process whose command line merely CONTAINS "bitbake-server" and
  # the socket path matches a substring search - including the very shell
  # running the search, or an editor/CI wrapper that echoes the command. An
  # exact element match can't be fooled that way, because such a wrapper
  # carries the whole command as ONE argv element, not as the bare socket path.
  #
  # A genuine server has both: an argv element ending in "/bitbake-server",
  # and an element that IS this build directory's socket path - so a bitbake
  # for an unrelated project on the same host never matches.
  _stale_bitbake_pids() {
    local proc pid args
    for proc in /proc/[0-9]*/cmdline; do
      pid="${proc#/proc/}"; pid="${pid%/cmdline}"
      # Skip ourselves and our parent; a dead pid just yields nothing.
      [[ "${pid}" == "$$" || "${pid}" == "${PPID}" ]] && continue
      # 2>/dev/null BEFORE the input redirect, deliberately: redirections are
      # applied left to right, and a pid that exits between the glob expanding
      # and this line makes the SHELL print "No such file or directory" while
      # opening the file - i.e. before a trailing 2>/dev/null would be in
      # effect. Scanning /proc always races like this; it is not an error.
      args="$(tr '\0' '\n' 2>/dev/null < "${proc}")" || continue
      grep -q -- '/bitbake-server$' <<< "${args}" || continue
      grep -qxF -- "${sock}" <<< "${args}" || continue
      printf '%s\n' "${pid}"
    done
  }

  local pids
  pids="$(_stale_bitbake_pids || true)"

  if [[ -z "${pids}" ]]; then
    # No server running, but a socket file left behind by a hard kill still
    # makes the next server's bind() fail. Remove it.
    if [[ -S "${sock}" ]]; then
      warn "Removing stale bitbake socket (no server owns it): ${sock}"
      rm -f "${sock}"
    fi
    return 0
  fi

  warn "A bitbake server is already running for ${build_dir}:"
  # shellcheck disable=SC2086 # deliberate word splitting, one pid per line
  ps -o pid=,etime=,args= -p ${pids} 2>/dev/null | sed 's/^/    /' >&2 || true
  warn "Shutting it down. If that is a build you started in another terminal,"
  warn "it will die - re-run with BOAT_KEEP_BITBAKE_SERVER=1 to keep it."

  # 1. Ask nicely. `bitbake -m` tells the cooker to terminate and tidy up after
  #    itself. Needs the bitbake on PATH, i.e. oe-init-build-env already
  #    sourced, and it can itself hang against a wedged server - hence timeout.
  if command -v bitbake >/dev/null 2>&1; then
    # 9>&-: never let a bitbake inherit the build lock's file descriptor - see
    # the same redirect on the main bitbake call in scripts/03-build.sh.
    timeout 60 bitbake -m >/dev/null 2>&1 9>&- || true
    sleep 2
    pids="$(_stale_bitbake_pids || true)"
  fi

  # 2. SIGTERM, then SIGKILL. Poll rather than sleeping a fixed worst case.
  local sig
  for sig in TERM KILL; do
    [[ -z "${pids}" ]] && break
    warn "bitbake server still up - sending SIG${sig}"
    # shellcheck disable=SC2086
    kill -"${sig}" ${pids} 2>/dev/null || true
    # Poll for up to 10s rather than sleeping a fixed worst case.
    local waited=0
    while [[ "${waited}" -lt 20 ]]; do
      sleep 0.5
      pids="$(_stale_bitbake_pids || true)"
      [[ -z "${pids}" ]] && break
      waited=$(( waited + 1 ))
    done
  done

  if [[ -n "${pids}" ]]; then
    die "could not shut down bitbake server (pid(s): ${pids//$'\n'/ }) - kill it by hand"
  fi

  [[ -S "${sock}" ]] && rm -f "${sock}"
  log "Stale bitbake server cleared"
  return 0
}
