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
        # boat-update's NetworkManager dispatcher and system-sleep hooks
        printf '%s\n' layers/meta-boat/external/rauc/recipes-boat/boat-update/files/90-boat-update \
            layers/meta-boat/external/rauc/recipes-boat/boat-update/files/boat-update-sleep
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
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    soft_skip "no 'docker compose' client - compose examples not schema-checked"
else
    compose_bad=0
    compose_n=0
    # Under REPO_ROOT, not /tmp. Ubuntu's docker is commonly the snap
    # (/snap/bin/docker), and snap confinement cannot read /tmp outside its
    # own sandbox: `docker compose -f /tmp/xxx/docker-compose.yml config`
    # fails with "no such file or directory" for a file that demonstrably
    # exists. The same file under $HOME reads fine. Every example failed this
    # check on such a host, which looks like five broken examples rather than
    # one unreadable directory.
    compose_tmp="$(mktemp -d "${REPO_ROOT}/.lint-compose.XXXXXX")"
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
# Every Markdown file in the repo except the scratch trees. An allowlist of
# directories is how wol/README.md came to be the one document nobody
# link-checked - but the exclusions have to cover BOTH scratch layouts:
# scripts/env.sh puts everything under yocto/, while `kas build` defaults its
# work dir to the CWD and clones poky/ and the meta-* layers into the repo
# ROOT. Same set .gitignore names; without it, one kas run makes this check
# walk thousands of upstream documents and report their broken links as ours.
SCRATCH = ("yocto/", "build/", "poky/", "meta-openembedded/", "meta-tegra/",
           "meta-tegra-community/", "meta-virtualization/")
docs = sorted(d for d in glob.glob("**/*.md", recursive=True)
              if not d.startswith(SCRATCH))
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

# --- 3b. Python shipped to the target parses -------------------------------
# boat-sleepd is Python that runs as root on the boat, parsing packets from the
# network. A SyntaxError in it is not caught by anything else here: bitbake
# installs the file without executing it, so the first thing to notice would be
# the service failing to start on a board that is already flashed. py_compile
# is the same check python(1) makes at import, without running a line of it.
log "Python syntax ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - shipped Python not syntax-checked"
else
    mapfile -t PY_FILES < <(find layers scripts -name '*.py' -print | sort)
    if (( ${#PY_FILES[@]} == 0 )); then
        echo "  no Python files under layers/"
    elif python3 -m py_compile "${PY_FILES[@]}"; then
        echo "  ${#PY_FILES[@]} Python file(s) checked"
        log "  Python: clean"
        # py_compile leaves __pycache__ next to each source; .gitignore covers
        # it, but leaving build droppings in the tree after a lint run is rude.
        find layers scripts -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    else
        fail_check "  Python: syntax errors above"
    fi
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

# --- 5. The A/B GRUB state machine, run in GRUB ----------------------------
# grub.cfg.in decides, with nobody at the console, which OS the boat boots
# and when it gives up on a new one. grub-script-check (run at build time)
# only proves it parses; scripts/test-grub-ab.py runs it in grub-emu - GRUB's
# own script engine and load_env/save_env - against a table of grubenv
# states and checks the slot, the kernel command line and what it writes
# back. About 20 seconds, almost all of it grub-emu starting up.
log "A/B GRUB state machine ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - GRUB state machine not run"
else
    grub_rc=0
    python3 scripts/test-grub-ab.py || grub_rc=$?
    if [[ "${grub_rc}" -eq 0 ]]; then
        log "  GRUB state machine: clean"
    elif [[ "${grub_rc}" -eq 77 ]]; then
        skip_check "grub-emu or a disk-image tool is missing (apt-get install grub-emu gdisk dosfstools mtools)"
    else
        fail_check "the A/B GRUB state machine failed a scenario"
    fi
fi

# --- 5a. boat-storage, run against a fake board ----------------------------
# boat-storage decides at every boot what is made swap, whether DATA is
# mounted and what is copied onto it. scripts/test-boat-storage.py runs it
# as shipped with blkid, mkswap, swapon, e2fsck, mkfs.ext4 and sync stubbed,
# and checks exactly what it would have run - and what it refused to.
log "boat-storage ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - boat-storage not run"
elif python3 scripts/test-boat-storage.py; then
    log "  boat-storage: clean"
else
    fail_check "boat-storage failed a case"
fi

# --- 5b. The A/B on-board scripts, run against a fake board -----------------
# The grubenv guard and the commit step decide, unattended, which slot stays
# bootable. scripts/test-ab-scripts.py runs them as shipped against a scratch
# directory standing in for the board (/proc, the ESP with a real grubenv,
# the GPT names) and checks what they write and what they refuse. This is
# the check that caught POSIX sh's missing local variables clobbering the
# guard's own state.
log "A/B on-board scripts ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - A/B on-board scripts not run"
else
    ab_rc=0
    python3 scripts/test-ab-scripts.py || ab_rc=$?
    if [[ "${ab_rc}" -eq 0 ]]; then
        log "  A/B on-board scripts: clean"
    elif [[ "${ab_rc}" -eq 77 ]]; then
        skip_check "grub-editenv is missing (apt-get install grub-common)"
    else
        fail_check "an A/B on-board script failed a case"
    fi
fi

# --- 5c. boat-update against a real HTTPS channel ----------------------------
# boat-update decides, from files on a web server, what is written into the
# boat's other slot. scripts/test-boat-update.py runs it against a local
# HTTPS channel with throwaway keys: an index signed by another CA, by the
# bundle key, replayed or for another channel must be refused; a good one
# accepted, its bundle downloaded across a dropped connection and verified.
log "boat-update ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - boat-update not exercised"
else
    bu_rc=0
    python3 scripts/test-boat-update.py || bu_rc=$?
    if [[ "${bu_rc}" -eq 0 ]]; then
        log "  boat-update: clean"
    elif [[ "${bu_rc}" -eq 77 ]]; then
        skip_check "openssl, curl or grub-editenv is missing - boat-update not exercised"
    else
        fail_check "boat-update failed a check"
    fi
fi

# --- 6. No private key is ever committed -------------------------------------
# The update CA and signing keys (scripts/gen-update-keys.sh) live outside the
# repository on purpose; a key that slips in here is in every clone and every
# fork for good. Checks the files git tracks (or, outside a git checkout,
# everything), for the PEM marker every private key format shares.
log "Private keys ..."
if git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    key_files="$(git -C "${REPO_ROOT}" grep -l -I -E -e '-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY-----' -- . ':!scripts/lint.sh' 2>/dev/null || true)"
else
    key_files="$(grep -r -l -I -E --exclude=lint.sh -e '-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY-----' . 2>/dev/null || true)"
fi
if [[ -z "${key_files}" ]]; then
    log "  private keys: none"
else
    printf '%s\n' "${key_files}" | sed 's/^/  /' >&2
    fail_check "a tracked file contains a private key - remove it, and treat the key as compromised"
fi

# --- 6b. The bundle's carry-over hook, run between two fake slots -----------
# boat-bundle-hook.sh copies this board's identity, keys and password hashes
# into the slot an update has just written; a mistake there locks people out
# of it. scripts/test-bundle-hook.py runs it as shipped and checks what the
# new slot ends up with.
log "Bundle carry-over hook ..."
if [[ "${HAVE_PYTHON}" == "0" ]]; then
    skip_check "python3 not installed - bundle hook not run"
elif python3 scripts/test-bundle-hook.py; then
    log "  bundle hook: clean"
else
    fail_check "the bundle's carry-over hook failed a case"
fi

# --- Result ----------------------------------------------------------------
echo
[[ "${SKIPPED}" -gt 0 ]] && warn "${SKIPPED} check(s) skipped - install the tools above, or set LINT_STRICT=1 to treat this as failure"
[[ "${SOFT_SKIPPED}" -gt 0 ]] && warn "${SOFT_SKIPPED} optional check(s) skipped (tool absent); LINT_STRICT does not make these fatal"
if [[ "${FAILED}" -gt 0 ]]; then
    die "${FAILED} check(s) failed"
fi
log "All checks passed"
