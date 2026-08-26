#!/usr/bin/env bash
# Fast repository checks - no Yocto layers fetched, no bitbake, no build.
# Runs in seconds, both locally and in CI (.github/workflows/lint.yml calls
# exactly this script, so "green in CI" and "green on my machine" mean the
# same thing).
#
#   ./scripts/lint.sh          # skip checks whose tool isn't installed
#   LINT_STRICT=1 ./scripts/lint.sh   # missing tool = failure (what CI uses)
#
# DELIBERATELY LIMITED. These checks cannot catch the interesting Yocto
# failures - recipe parse errors, unresolvable RDEPENDS, or packaging QA like
# "An allarch packagegroup shouldn't depend on packages which are dynamically
# renamed". Those need layers fetched and bitbake run; the packaging QA ones
# need a real build, since `bitbake -n` passes clean on them. Treat a green
# run here as "nothing obviously broken", not as a build gate.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"
cd "${REPO_ROOT}"

FAILED=0
SKIPPED=0
SOFT_SKIPPED=0

fail_check()  { err "$1"; FAILED=$((FAILED + 1)); }
# A skip that never fails, even under LINT_STRICT. Only for a check whose tool
# this project does NOT ask anyone to install - see the compose check below.
# Counted separately so the footer does not tell a LINT_STRICT user to set
# LINT_STRICT, which is the advice a plain SKIPPED count would produce.
soft_skip()   { warn "SKIP: $1"; SOFT_SKIPPED=$((SOFT_SKIPPED + 1)); }
skip_check()  {
  if [[ "${LINT_STRICT:-0}" == "1" ]]; then
    fail_check "$1 (LINT_STRICT=1)"
  else
    warn "SKIP: $1"
    SKIPPED=$((SKIPPED + 1))
  fi
}

# --- 1. Shell scripts ------------------------------------------------------
# -x follows `source`d files (lib.sh/env.sh) so their definitions are known.
# -S warning, not the default: the info level flags several patterns this repo
# uses on purpose - SC2016 (single-quoted '${...}' written verbatim into
# local.conf by 02-configure-build.sh), SC2028 (the literal "\n" separators
# SANITY_TESTED_DISTROS needs), SC1091 (oe-init-build-env, not in this repo).
#
# EVERY shell script in the repo, not just scripts/: wol/ is the host-side
# sleep/wake tooling and layers/meta-boat/**/files/ is what actually runs on
# the boat - including boat-grow-rootfs, which rewrites a partition table.
# Those were the ones a lint pass most needed to cover and the ones it used to
# skip. They are #!/bin/sh targets, so shellcheck also catches bashisms that
# would only fail on the device.
#
# boat-xfce-session has no .sh suffix (it is /usr/bin/boat-xfce-session), so
# it is named explicitly rather than found by the glob.
log "shellcheck ..."
if command -v shellcheck >/dev/null 2>&1; then
    mapfile -t SH_FILES < <(
        printf '%s\n' scripts/*.sh wol/*.sh
        find layers -name '*.sh' -print
        printf '%s\n' layers/meta-boat/recipes-boat/hmi-autostart/files/boat-xfce-session
    )
    # -P: `# shellcheck source=lib.sh` directives are resolved relative to a
    # source path, not to the file holding them, so both directories that hold
    # a sourced helper have to be on that list.
    if shellcheck -x -P scripts -P wol -S warning "${SH_FILES[@]}"; then
        log "  ${#SH_FILES[@]} shell file(s) checked"
        log "  shellcheck: clean"
    else
        fail_check "shellcheck reported problems"
    fi
else
    skip_check "shellcheck not installed (apt-get install shellcheck)"
fi

# --- 2. YAML parses --------------------------------------------------------
# The kas build config, this repo's own CI workflows, and every compose
# example the boat-compose recipe ships to the target. A compose file that
# doesn't parse is only discovered on the boat otherwise.
# One guard for all three python-based checks. Without it a host with no
# python3 does not SKIP them - the heredocs simply fail, and the script reports
# "broken relative link(s)" and "recipe references a file:// that doesn't
# exist", sending the reader after defects that do not exist. A check you
# cannot run must never look like a check that failed.
HAVE_PYTHON=0
command -v python3 >/dev/null 2>&1 && HAVE_PYTHON=1

log "YAML syntax ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - YAML syntax not checked"
elif python3 -c 'import yaml' 2>/dev/null; then
    if python3 - <<'PY'; then
import glob, sys, yaml
bad = 0
files = sorted(set(glob.glob("kas/*.yml") + glob.glob("kas/*.yaml")
                   + glob.glob(".github/workflows/*.yml")
                   + glob.glob(".github/workflows/*.yaml")
                   + glob.glob("layers/**/*.yml.example", recursive=True)
                   + glob.glob("layers/**/*.yaml.example", recursive=True)))
if not files:
    print("  no YAML files found - has the layout changed?", file=sys.stderr)
    sys.exit(1)
for f in files:
    try:
        with open(f) as fh:
            yaml.safe_load(fh)
    except Exception as e:
        print(f"  {f}: {e}", file=sys.stderr)
        bad += 1
print(f"  {len(files)} YAML file(s) checked")
sys.exit(1 if bad else 0)
PY
        log "  YAML: clean"
    else
        fail_check "YAML syntax errors"
    fi
else
    skip_check "PyYAML not installed (pip install pyyaml)"
fi

# --- 2b. Compose examples against the real Compose schema -------------------
# The YAML check above only proves the file parses. `docker compose config`
# proves it is a valid COMPOSE file: unknown top-level keys, a malformed
# `devices:` or `volumes:` entry, a bad `logging:` block - none of which
# yaml.safe_load has any opinion about. These examples are copied verbatim onto
# a boat and run, so a schema error in one is only discovered there.
#
# soft_skip, not skip_check: unlike shellcheck and PyYAML, a Compose client is
# not something this project asks a contributor or the CI image to install. The
# YAML parse is the floor that must always run; this is the stronger check when
# a client happens to be present.
log "Compose examples ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    soft_skip "python3 not installed - compose examples not schema-checked"
elif ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    soft_skip "no 'docker compose' client - compose examples not schema-checked"
else
    compose_bad=0
    compose_n=0
    compose_tmp="$(mktemp -d)"
    # shellcheck disable=SC2064 # expand compose_tmp now, not at trap time
    trap "rm -rf '${compose_tmp}'" EXIT
    while IFS= read -r example; do
        compose_n=$((compose_n + 1))
        # -f names the file, but Compose derives the project name from the
        # directory, so a copy into a scratch dir keeps the examples' own
        # filenames out of the picture and avoids touching the repo.
        cp "${example}" "${compose_tmp}/docker-compose.yml"
        if ! out="$(docker compose -f "${compose_tmp}/docker-compose.yml" config --quiet 2>&1)"; then
            err "  ${example}"
            printf '%s\n' "${out}" | sed 's/^/      /' >&2
            compose_bad=$((compose_bad + 1))
        fi
    done < <(find layers -name '*.yml.example' -print | sort)
    echo "  ${compose_n} compose example(s) checked"
    if [[ "${compose_bad}" -eq 0 ]]; then
        log "  compose: clean"
    else
        fail_check "${compose_bad} compose example(s) rejected by 'docker compose config'"
    fi
fi

# --- 3. Relative links in Markdown ----------------------------------------
# This repo is documentation-heavy and the docs cross-reference recipes and
# scripts by relative path, so a rename silently rots them. Only file
# existence is checked, not "#section" anchors.
log "Markdown relative links ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - Markdown links not checked"
elif python3 - <<'PY'; then
import os, re, sys, glob
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# Every Markdown file in the repo except anything under the git-ignored
# yocto/ working tree. An allowlist of directories is how wol/README.md
# came to be the one document nobody link-checked.
docs = sorted(d for d in glob.glob("**/*.md", recursive=True)
              if not d.startswith("yocto/"))
bad, checked = [], 0
for doc in docs:
    base = os.path.dirname(doc)
    for target in LINK.findall(open(doc, encoding="utf-8").read()):
        target = target.strip()
        if target.startswith(("http://", "https://", "mailto:", "#", "<")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        checked += 1
        if not os.path.exists(os.path.normpath(os.path.join(base, path))):
            bad.append(f"  {doc} -> {target}")
print(f"  {checked} relative link(s) in {len(docs)} file(s) checked")
if bad:
    print("\n".join(bad), file=sys.stderr)
sys.exit(1 if bad else 0)
PY
    log "  links: clean"
else
    fail_check "broken relative link(s) in Markdown"
fi

# --- 4. Recipe file:// entries exist --------------------------------------
# CONFIRMED WORTH HAVING: renaming a file under a recipe's files/ directory
# without updating its SRC_URI is a real and easy mistake (this repo renamed
# boat-weston-autostart.sh -> boat-xfce-autostart.sh), and bitbake only
# reports it as a do_fetch failure minutes into a build.
# Entries containing ${...} are skipped - those are LIC_FILES_CHKSUM pointing
# at ${COMMON_LICENSE_DIR} in poky, not files in this layer.
log "Recipe file:// references ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - recipe file:// references not checked"
elif python3 - <<'PY'; then
import os, re, sys, glob
SRC = re.compile(r"file://([^\s\"'\;]+)")
bad, checked = [], 0
recipes = sorted(glob.glob("layers/**/*.bb", recursive=True) +
                 glob.glob("layers/**/*.bbappend", recursive=True))
if not recipes:
    print("  no recipes found - has the layout changed?", file=sys.stderr)
    sys.exit(1)
PV_ASSIGN = re.compile(r"(?m)^\s*PV\s*(?::\w+)?\s*[?:+]?=\s*[\"']([^\"']+)[\"']")
for recipe in recipes:
    rdir = os.path.dirname(recipe)
    stem = re.sub(r"\.bbappend$|\.bb$", "", os.path.basename(recipe))
    pn, _, pv = stem.partition("_")
    text = open(recipe, encoding="utf-8").read()
    # ${BP} = ${BPN}-${PV}. PV comes from the recipe filename when it carries
    # a version, else from an explicit assignment, else bitbake's own default
    # of "1.0". A .bbappend's version may be a "%" wildcard, which names no
    # directory - drop those.
    if not pv:
        m = PV_ASSIGN.search(text)
        pv = m.group(1) if m else "1.0"
    bp = f"{pn}-{pv}" if "%" not in pv and "${" not in pv else None
    for name in SRC.findall(text):
        if "${" in name:
            continue
        checked += 1
        # bitbake's default FILESPATH: <recipedir>/<BP>/, <recipedir>/<BPN>/,
        # <recipedir>/files/, <recipedir>/ - plus the machine/distro dirs,
        # which this layer doesn't use. BP (name-version) is searched before
        # BPN, so a versioned subdirectory is a legitimate place to put files
        # and must not be reported as missing.
        subs = ([bp] if bp else []) + [pn, "files", ""]
        if not any(os.path.exists(os.path.join(rdir, sub, name))
                   for sub in subs):
            bad.append(f"  {recipe} -> file://{name}")
print(f"  {checked} file:// reference(s) in {len(recipes)} recipe(s) checked")
if bad:
    print("\n".join(bad), file=sys.stderr)
sys.exit(1 if bad else 0)
PY
    log "  recipe files: clean"
else
    fail_check "recipe references a file:// that doesn't exist"
fi

# --- Result ----------------------------------------------------------------
echo
[[ "${SKIPPED}" -gt 0 ]] && warn "${SKIPPED} check(s) skipped - install the tools above, or set LINT_STRICT=1 to treat this as failure"
[[ "${SOFT_SKIPPED}" -gt 0 ]] && warn "${SOFT_SKIPPED} optional check(s) skipped (tool absent); LINT_STRICT does not make these fatal"
if [[ "${FAILED}" -gt 0 ]]; then
    die "${FAILED} check(s) failed"
fi
log "All checks passed"
