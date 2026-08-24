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

fail_check()  { err "$1"; FAILED=$((FAILED + 1)); }
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
log "shellcheck ..."
if command -v shellcheck >/dev/null 2>&1; then
    if shellcheck -x -S warning scripts/*.sh; then
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
log "YAML syntax ..."
if python3 -c 'import yaml' 2>/dev/null; then
    if python3 - <<'PY'; then
import glob, sys, yaml
bad = 0
files = sorted(glob.glob("kas/*.yml")
               + glob.glob(".github/workflows/*.yml")
               + glob.glob("layers/**/*.yml.example", recursive=True))
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

# --- 3. Relative links in Markdown ----------------------------------------
# This repo is documentation-heavy and the docs cross-reference recipes and
# scripts by relative path, so a rename silently rots them. Only file
# existence is checked, not "#section" anchors.
log "Markdown relative links ..."
if python3 - <<'PY'; then
import os, re, sys, glob
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
docs = sorted(glob.glob("*.md") + glob.glob("docs/*.md") + glob.glob("layers/**/*.md", recursive=True))
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
if python3 - <<'PY'; then
import os, re, sys, glob
SRC = re.compile(r"file://([^\s\"'\;]+)")
bad, checked = [], 0
recipes = sorted(glob.glob("layers/**/*.bb", recursive=True) +
                 glob.glob("layers/**/*.bbappend", recursive=True))
if not recipes:
    print("  no recipes found - has the layout changed?", file=sys.stderr)
    sys.exit(1)
for recipe in recipes:
    rdir = os.path.dirname(recipe)
    pn = re.split(r"[_.]", os.path.basename(recipe))[0]
    for name in SRC.findall(open(recipe, encoding="utf-8").read()):
        if "${" in name:
            continue
        checked += 1
        # bitbake's default FILESPATH: <recipedir>/<pn>/, <recipedir>/files/,
        # <recipedir>/ - plus the machine/distro dirs, which this layer
        # doesn't use.
        if not any(os.path.exists(os.path.join(rdir, sub, name))
                   for sub in (pn, "files", "")):
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
if [[ "${FAILED}" -gt 0 ]]; then
    die "${FAILED} check(s) failed"
fi
log "All checks passed"
