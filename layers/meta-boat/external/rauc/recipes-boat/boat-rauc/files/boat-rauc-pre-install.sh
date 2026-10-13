#!/bin/sh
# RAUC system pre-install handler for the boat computer's A/B layout
# (docs/plans/ab-os-updates.md 5.6, step 2). RAUC 1.13 runs it after the
# bundle's signature and format have been checked but BEFORE its compatible
# string and min-bundle-version are, and before anything is written; a
# non-zero exit stops the install with nothing changed. It refuses unless:
#
#   - the board came up through GRUB (rauc.slot= on the command line), from
#     a slot that is committed: no install starts during a trial;
#   - /data is the DATA partition (RAUC's data directory lives there);
#   - every partition RAUC is about to write is the one this layout puts at
#     that number (GPT name APP or APP_b), and is not the booted slot;
#   - the bundle was built for the L4T release this board runs - a BSP bump
#     changes firmware RAUC does not update, and needs a USB reflash.
#
# Then it marks the target slot not committed - and only that. RAUC may
# still refuse the bundle after this handler (compatible, min-bundle-version),
# and the slot must then stay usable as the fallback it is. RAUC itself marks
# the target unusable (OK=0) right before it writes it, and its activation
# leaves COMMITTED=0, so the new slot boots as a trial. A slot left "not
# committed" by a refused bundle still boots if it is ever needed - as a
# trial, which it passes.
set -eu
SELF=boat-rauc-pre-install
# shellcheck source=SCRIPTDIR/../../../../../recipes-boat/ab-boot/files/boat-ab-lib.sh
. /usr/lib/boat-ab/lib.sh

slot="$(booted_slot)"
[ -n "$slot" ] || die "this boot did not come through GRUB (no rauc.slot=) - reboot first; installing now could leave nothing bootable"
mountpoint -q "$BOAT_AB_ESP_MOUNT" || die "the ESP is not mounted at $BOAT_AB_ESP_MOUNT"
check_layout || die "the disk does not match this image's A/B layout"
grubenv_valid "$BOAT_AB_GRUBENV" || die "grubenv is not valid - reboot so boat-grubenv-guard can rebuild it"
[ "$(grubenv_get "$BOAT_AB_GRUBENV" "${slot}_COMMITTED")" = 1 ] \
    || die "slot $slot is still on trial - let it commit (or roll back) before installing"

data_dev="$(part_dev "$BOAT_AB_PART_DATA")"
awk -v d="$data_dev" '$1 == d && $2 == "/data" { f = 1 } END { exit !f }' /proc/mounts \
    || die "/data is not mounted from DATA ($data_dev)"

# The L4T release, as "35.6.4", from nv_tegra_release's first line
# ("# R35 (release), REVISION: 6.4, ...").
running_l4t="$(sed -n '1s/^# R\([0-9]*\) (release), REVISION: \([0-9]*\)\.\([0-9]*\).*/\1.\2.\3/p' /etc/nv_tegra_release 2>/dev/null)"
bundle_l4t="${RAUC_META_BOAT_L4T:-}"
[ -n "$bundle_l4t" ] || die "the bundle has no [meta.boat] l4t= - it was not built by meta-boat's boat-bundle"
[ -n "$running_l4t" ] || die "cannot read the running L4T release from /etc/nv_tegra_release"
if [ "$bundle_l4t" != "$running_l4t" ]; then
    die "the bundle is for L4T $bundle_l4t, this board runs $running_l4t: a BSP change also changes firmware that only a USB reflash updates (scripts/05-flash-nvme.sh)"
fi

# Docker keeps its data on the shared /data: a newer major version may
# migrate it in a way the other slot's Docker cannot read after a fallback.
running_docker="$(docker --version 2>/dev/null | sed -n 's/^Docker version \([0-9]*\)\..*/\1/p')"
bundle_docker="${RAUC_META_BOAT_DOCKER:-}"
if [ -n "$running_docker" ] && [ -n "$bundle_docker" ] && [ "${bundle_docker%%.*}" != "$running_docker" ]; then
    warn "Docker $running_docker -> ${bundle_docker}: a new major version may migrate /data/docker so that this slot's Docker cannot read it after a fallback"
fi

targets=""
for n in ${RAUC_TARGET_SLOTS:-}; do
    # RAUC numbers the slots; their details are in RAUC_SLOT_*_<n>.
    case "$n" in ''|*[!0-9]*) die "unexpected RAUC_TARGET_SLOTS entry '$n'" ;; esac
    bootname="" device=""
    eval "bootname=\${RAUC_SLOT_BOOTNAME_$n:-}"
    eval "device=\${RAUC_SLOT_DEVICE_$n:-}"
    case "$bootname" in
        A) num="$BOAT_AB_PART_A" name=APP ;;
        B) num="$BOAT_AB_PART_B" name=APP_b ;;
        *) die "target slot $n has bootname '$bootname' - expected A or B" ;;
    esac
    [ "$bootname" != "$slot" ] || die "RAUC would write slot $bootname, which is the one running"
    [ "$device" = "$(part_dev "$num")" ] || die "slot $bootname is configured as $device, expected $(part_dev "$num")"
    check_part "$num" "$name" || die "not writing $device"
    targets="$targets $bootname"
done
[ -n "$targets" ] || die "RAUC named no target slot"

for t in $targets; do
    grubenv_set "${t}_COMMITTED=0"
    say "slot $t: marked not committed - it boots only as a trial until one passes"
done
# System handlers get no RAUC_MF_*: the version comes from [meta.boat].
say "installing ${RAUC_META_BOAT_RELEASE:-?} into slot(s)$targets"
