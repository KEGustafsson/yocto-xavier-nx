#!/usr/bin/env bash
# Clone (or update) all the layers needed for an Xavier NX / NVMe build,
# pinned to the branch defined in env.sh (default: kirkstone).
#
# Layers:
#   poky                  -> OpenEmbedded-Core + bitbake (the build system)
#   meta-openembedded     -> meta-oe / meta-python / meta-networking /
#                            meta-filesystems (chrony, avahi, networkmanager,
#                            nftables, ...) plus meta-xfce and its own layer
#                            dependencies meta-gnome / meta-multimedia, which
#                            provide the Phase-2 XFCE helm desktop. All are
#                            sublayers of this single clone; which ones get
#                            enabled is decided in 02-configure-build.sh.
#   meta-tegra            -> the NVIDIA Jetson BSP (kernel, bootloader, tegraflash)
#   meta-virtualization   -> Docker (docker-moby) + container runtime; also unlocks
#                            meta-tegra's external/virtualization-layer overlay
#                            (nvidia-container-toolkit, libnvidia-container-*)
#   meta-tegra-community  -> extra Jetson userspace tools (python3-jetson-stats)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"
# shellcheck source=env.sh
source "${HERE}/env.sh"

need git
mkdir -p "${LAYERS_DIR}"

# repo_name  git_url
LAYERS=(
  "poky|https://git.yoctoproject.org/poky"
  "meta-openembedded|https://github.com/openembedded/meta-openembedded.git"
  "meta-tegra|https://github.com/OE4T/meta-tegra.git"
  "meta-virtualization|https://git.yoctoproject.org/meta-virtualization"
  "meta-tegra-community|https://github.com/OE4T/meta-tegra-community.git"
)

clone_or_update() {
  local name="$1" url="$2" dst="${LAYERS_DIR}/$1"
  if [[ -d "${dst}/.git" ]]; then
    # The update below is `reset --hard`, which discards local modifications
    # without asking. That matters here: this project's own docs tell you to
    # patch fetched layers (README "Package names ... adjust", docs/06), and
    # losing such a patch silently produces a build failure somewhere else
    # entirely.
    if [[ -n "$(git -C "${dst}" status --porcelain)" ]]; then
      warn "${name} has local modifications:"
      git -C "${dst}" status --short | sed 's/^/    /' >&2
      confirm "Discard them and reset ${name} to origin/${YOCTO_BRANCH}?" \
        || die "aborted - ${name} left as it is"
    fi
    log "Updating ${name} (${YOCTO_BRANCH})..."
    # An explicit refspec, not a bare branch name: `git clone --depth 1 -b X`
    # implies --single-branch, so remote.origin.fetch covers only X. Changing
    # YOCTO_BRANCH afterwards then leaves no origin/<new> ref for the checkout
    # below to use, and the script dies with an unexplained
    # "pathspec did not match any file(s) known to git".
    git -C "${dst}" fetch --depth 1 origin \
      "+refs/heads/${YOCTO_BRANCH}:refs/remotes/origin/${YOCTO_BRANCH}"
    # --force, and then clean: between them these are the two halves of what
    # the prompt above promised, and neither is implied by a plain
    # `checkout -B`. Without --force, checkout carries local modifications
    # forward and REFUSES outright when they would be overwritten - which under
    # `set -e` aborts the whole fetch rather than discarding anything. Without
    # clean, untracked files survive; that matters here specifically because a
    # stray .bbappend left in a layer is picked up by that layer's BBFILES and
    # silently changes the build.
    #
    # Safe to force unconditionally: the porcelain check above reports tracked
    # AND untracked changes, so we are either past an explicit confirmation or
    # there was nothing to discard.
    git -C "${dst}" checkout -q --force -B "${YOCTO_BRANCH}" "origin/${YOCTO_BRANCH}"
    git -C "${dst}" clean -qfd
  else
    log "Cloning ${name} (${YOCTO_BRANCH})..."
    git clone --depth 1 -b "${YOCTO_BRANCH}" "${url}" "${dst}"
  fi
  log "  ${name} @ $(git -C "${dst}" rev-parse --short HEAD)"
}

for entry in "${LAYERS[@]}"; do
  clone_or_update "${entry%%|*}" "${entry##*|}"
done

log "All layers present under ${LAYERS_DIR}"
log "Pinned to branch: ${YOCTO_BRANCH}"
warn "For a reproducible product build, replace the branch checkouts above with"
warn "fixed tags/commits once you have a combination that works for you."
