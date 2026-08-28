#!/usr/bin/env bash
# Build with kas, with the environment kirkstone needs on a modern host.
#
# Usage:   scripts/build-kas.sh                      # build the default target
#          scripts/build-kas.sh --share              # reuse the scripts' sstate/downloads
#          scripts/build-kas.sh -- --target core-image-base
#          scripts/build-kas.sh shell                # configured bitbake shell
#          KAS_CONFIG=kas/other.yml scripts/build-kas.sh
#
# This is the kas equivalent of 01-fetch-layers + 02-configure-build +
# 03-build: kas clones the layers, writes conf/, and builds the `target:`
# named in the YAML - so unlike the manual flow there is no IMAGE to export.
#
# WHY A WRAPPER AND NOT JUST `kas build`
# Two environment variables have to be set or the build dies before it parses
# a single recipe, with a traceback that points nowhere near the cause:
#
#   resource_tracker: process died unexpectedly, relaunching
#   AttributeError: 'NoneType' object has no attribute 'terminate'
#
# Python 3.14 changed multiprocessing's POSIX default start method from "fork"
# to "forkserver". Kirkstone's hash-equivalence server hands
# multiprocessing.Process a function local to another function - picklable
# under fork, not under forkserver. scripts/pyfix/sitecustomize.py restores the
# old default (and shims ast.Str, which 3.12 removed and kirkstone's license
# parser still uses); Python auto-imports it when its directory is on
# PYTHONPATH. scripts/03-build.sh does exactly this for the manual flow.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
# shellcheck source=env.sh
source "${HERE}/env.sh"

: "${KAS_CONFIG:=${REPO_ROOT}/kas/xavier-nx-nvme.yml}"
SHARE="${BOAT_KAS_SHARE:-0}"

KAS_ARGS=()
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --share)   SHARE=1 ;;
    --no-share) SHARE=0 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --)        shift; KAS_ARGS+=("$@"); break ;;
    *)         KAS_ARGS+=("$1") ;;
  esac
  shift
done

command -v kas >/dev/null 2>&1 || die "kas not found - install it with: pip3 install kas"
[[ -f "${KAS_CONFIG}" ]] || die "no kas config at ${KAS_CONFIG}"

# --- The host test a static YAML cannot do ----------------------------------
# kirkstone predates current host toolchains, and several -native recipes fail
# to compile under a modern GCC's C23 default. gcc-12 fixes them, but hard-
# coding BUILD_CC in the kas config would break every host that does not have
# gcc-12 - including Ubuntu 20.04, whose archive has none. So the main config
# carries those lines commented out, and kas/host-gcc12.yml holds them for
# real; kas composes configs with ':', so we add that fragment exactly when
# the compiler exists. This is the same test 02-configure-build.sh makes
# before writing the same four lines into local.conf.
KAS_CONFIGS="${KAS_CONFIG}"
if [[ -x /usr/bin/gcc-12 && -x /usr/bin/g++-12 ]]; then
  GCC12_FRAGMENT="${REPO_ROOT}/kas/host-gcc12.yml"
  if [[ -f "${GCC12_FRAGMENT}" ]]; then
    KAS_CONFIGS="${KAS_CONFIG}:${GCC12_FRAGMENT}"
    log "gcc-12 found - adding kas/host-gcc12.yml for -native builds"
  fi
else
  # Only a problem on a host whose default GCC is much newer than kirkstone
  # expects; on an older host the default compiler is fine and this is silent
  # by design.
  warn "no /usr/bin/gcc-12: if -native recipes fail to compile, install it"
  warn "  (sudo apt-get install gcc-12 g++-12) and re-run"
fi

# First non-flag argument is the kas subcommand; default to "build".
SUBCMD="build"
if [[ "${#KAS_ARGS[@]}" -gt 0 && "${KAS_ARGS[0]}" != -* ]]; then
  SUBCMD="${KAS_ARGS[0]}"
  KAS_ARGS=("${KAS_ARGS[@]:1}")
fi

export PYTHONPATH="${HERE}/pyfix${PYTHONPATH:+:${PYTHONPATH}}"

# bitbake scrubs its environment down to a whitelist on startup
# (bb.utils.clean_environment), so PYTHONPATH never reaches the forked server
# process unless it is named here.
#
# The rest of the list is belt-and-braces, and deliberately kept. Reading
# libkas.py at kas 5.5, kas APPENDS its own names (SSTATE_DIR, SSTATE_MIRRORS,
# BB_HASHSERVE*, DL_DIR, TMPDIR, plus every key of the config's `env:` block)
# to whatever this variable already holds, rather than replacing it - so on
# that version naming them here is redundant. It is not free to rely on that:
# the split is one line of a third-party tool, it differs across kas versions,
# and if a version ever replaced instead of appended, dropping these would
# silently disable kas's own DL_DIR/SSTATE_DIR/TMPDIR handling - including the
# --share option below, which works by exporting exactly those. Duplicates in
# the list are harmless; a missing name is not.
export BB_ENV_PASSTHROUGH_ADDITIONS="PYTHONPATH SSTATE_DIR SSTATE_MIRRORS \
BB_HASHSERVE_DB_DIR BB_HASHSERVE BB_HASHSERVE_UPSTREAM DL_DIR TMPDIR"

# --- Optional: reuse the manual flow's downloads and sstate ------------------
# Off by default, deliberately. kas is the reproducible-build entry point: it
# manages its own work directory, and a build that silently reuses another
# tree's sstate is a weaker claim than one that does not. But for interactive
# use the difference is a from-scratch build (hours, and re-fetching multiple
# GB of NVIDIA debs) versus minutes, so it is one flag away.
#
# Safe to share: sstate entries are keyed by task signature, so a differing
# config simply misses rather than colliding, and DL_DIR is content-addressed.
if [[ "${SHARE}" -eq 1 ]]; then
  export DL_DIR="${WORKROOT}/downloads"
  export SSTATE_DIR="${WORKROOT}/sstate-cache"
  mkdir -p "${DL_DIR}" "${SSTATE_DIR}"
  log "sharing with the manual flow:"
  log "  DL_DIR     = ${DL_DIR}"
  log "  SSTATE_DIR = ${SSTATE_DIR}"
else
  log "using kas's own downloads/sstate (pass --share to reuse ${WORKROOT}/)"
fi

log "kas ${SUBCMD} ${KAS_CONFIGS} ${KAS_ARGS[*]:-}"
log "PYTHONPATH carries scripts/pyfix (Python 3.14 / kirkstone shim)"
# kas clones the upstream layers into the repository root, not under yocto/;
# .gitignore covers them. Note this is a SECOND checkout of poky and friends,
# independent of what scripts/01-fetch-layers.sh puts in yocto/layers.
# ${KAS_ARGS[@]+"${KAS_ARGS[@]}"}, not "${KAS_ARGS[@]:-}": with no extra
# arguments the latter expands to one EMPTY string rather than to nothing,
# and kas forwards it to bitbake as a target - so the plain
# `scripts/build-kas.sh` and `scripts/build-kas.sh shell` invocations, the
# two this script exists for, ran `bitbake -c build '' boat-image` and died
# after a full 40-second parse with "Nothing PROVIDES ''". The give-away is
# the double space in the command line kas echoes. The `+` form expands to
# nothing at all for an unset or empty array, and is safe under `set -u`.
exec kas "${SUBCMD}" "${KAS_CONFIGS}" ${KAS_ARGS[@]+"${KAS_ARGS[@]}"}
