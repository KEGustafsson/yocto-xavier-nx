#!/bin/sh
# Phase 0 probe for the A/B update plan (docs/plans/ab-os-updates.md).
#
# READ-ONLY. Runs ON THE BOARD, as root, and changes nothing: no file is
# written, no UEFI variable is set, no service is started or stopped, no
# partition table is touched. It only reads what the board already knows and
# prints it, so the answers to the plan's Phase 0 questions come from this
# hardware instead of from documentation.
#
# Normally driven from the host by scripts/ab-phase0-capture.sh, which pipes
# this file over SSH (`ssh root@boat sh -s`) and saves the output. Running it
# by hand on the board works too:   sh ab-phase0-probe.sh | tee probe.txt
#
# The questions it cannot answer read-only (does a panic reset the board,
# does the watchdog run inside GRUB, does a GRUB hand-off boot this kernel)
# are listed at the end with the manual steps in
# docs/plans/ab-phase0-runbook.md.
#
# Output is sectioned with "=== name" headers and ends with a "--- SUMMARY"
# block of "Q<n> <question>: <answer>" lines that the host script echoes back.

PATH=/usr/sbin:/usr/bin:/sbin:/bin

section() { printf '\n=== %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
# Run a command if it exists, and say so plainly when it does not - a missing
# tool is an answer too (it tells the plan which packages the image lacks).
run() {
    if have "$1"; then
        "$@" 2>&1
    else
        echo "(not installed: $1)"
    fi
}

# --- UEFI variables -------------------------------------------------------
# efivarfs files are a 4-byte attribute word followed by the data. Print the
# data as little-endian u32 when it is 4 bytes (every NVIDIA boot-control
# variable this plan reads is), otherwise as hex bytes.
EFIVARS=/sys/firmware/efi/efivars
efi_u32() {
    # $1 = variable name; prints the u32 value, or "absent"
    f=$(ls "$EFIVARS"/"$1"-* 2>/dev/null | head -n 1)
    if [ -z "$f" ]; then
        echo absent
        return
    fi
    # skip the 4 attribute bytes; od prints the data as one unsigned int
    v=$(od -An -t u4 -j 4 -N 4 "$f" 2>/dev/null | tr -d ' ')
    echo "${v:-unreadable}"
}

SUMMARY=""
answer() { SUMMARY="${SUMMARY}$1
"; }

section "identity"
cat /etc/nv_tegra_release 2>/dev/null || echo "(no /etc/nv_tegra_release)"
uname -a
grep -E '^(PRETTY_NAME|VERSION_ID|BUILD_ID)=' /etc/os-release 2>/dev/null

section "kernel command line"
CMDLINE=$(cat /proc/cmdline)
echo "$CMDLINE"
case " $CMDLINE " in
    *" root="*) answer "Q1 cmdline carries root=: yes ($(echo "$CMDLINE" | tr ' ' '\n' | grep '^root=' | head -n 1))" ;;
    *)          answer "Q1 cmdline carries root=: no" ;;
esac

section "/boot (is the kernel in the rootfs?)"
ls -la /boot 2>&1
ls -la /boot/extlinux 2>/dev/null
if [ -e /boot/extlinux/extlinux.conf ]; then
    answer "Q2 kernel/extlinux in rootfs: yes (/boot/extlinux/extlinux.conf present)"
elif ls /boot/Image* >/dev/null 2>&1; then
    answer "Q2 kernel/extlinux in rootfs: kernel image present, no extlinux.conf"
else
    answer "Q2 kernel/extlinux in rootfs: no - booting boot.img from the kernel partition"
fi

section "block devices"
run lsblk -o NAME,SIZE,TYPE,PARTLABEL,PARTUUID,FSTYPE,MOUNTPOINT
section "NVMe GPT"
run sgdisk -p /dev/nvme0n1
section "QSPI (mtd)"
cat /proc/mtd 2>/dev/null || echo "(no /proc/mtd)"

section "ESP"
run findmnt /boot/efi
run df -h /boot/efi
ls -la /boot/efi/EFI/BOOT 2>&1
ESP_FREE=$(df -Pk /boot/efi 2>/dev/null | awk 'NR==2 {print $4}')
answer "Q3 ESP mounted at /boot/efi, free KiB: ${ESP_FREE:-not mounted}"

section "UEFI boot-control variables"
for v in L4TDefaultBootMode RootfsRedundancyLevel RootfsRetryCountMax \
         RootfsStatusSlotA RootfsStatusSlotB BootChainFwCurrent \
         BootChainOsCurrent BootChainFwNext BootChainFwStatus AutoUpdateBrBct; do
    printf '%-24s %s\n' "$v" "$(efi_u32 "$v")"
done
BM=$(efi_u32 L4TDefaultBootMode)
case "$BM" in
    0) answer "Q4 L4TDefaultBootMode: 0 (GRUB) - the GRUB hand-off is already enabled" ;;
    1) answer "Q4 L4TDefaultBootMode: 1 (DIRECT/extlinux) - Phase 1 must switch it to 0" ;;
    absent) answer "Q4 L4TDefaultBootMode: absent - L4TLauncher falls back to GRUB mode" ;;
    *) answer "Q4 L4TDefaultBootMode: $BM" ;;
esac
answer "Q5 RootfsRedundancyLevel/RetryCountMax/StatusSlotA: $(efi_u32 RootfsRedundancyLevel)/$(efi_u32 RootfsRetryCountMax)/$(efi_u32 RootfsStatusSlotA)"

section "nvbootctrl"
run nvbootctrl dump-slots-info
run nvbootctrl -t rootfs dump-slots-info

section "NVIDIA boot-control services"
for u in setup-nv-boot-control.service nv_update_verifier.service; do
    printf '%-34s active=%s result=%s\n' "$u" \
        "$(systemctl is-active "$u" 2>/dev/null)" \
        "$(systemctl show -p Result --value "$u" 2>/dev/null)"
done

section "kernel configuration (RAUC + watchdog)"
KCONF=""
if [ -r /proc/config.gz ]; then
    KCONF=$(zcat /proc/config.gz)
elif [ -r "/boot/config-$(uname -r)" ]; then
    KCONF=$(cat "/boot/config-$(uname -r)")
fi
if [ -n "$KCONF" ]; then
    echo "$KCONF" | grep -E '^(# )?CONFIG_(BLK_DEV_LOOP|SQUASHFS|SQUASHFS_XZ|SQUASHFS_ZSTD|DM_VERITY|BLK_DEV_NBD|EFI_STUB|EFIVAR_FS|TEGRA_WATCHDOG|TEGRA186_WATCHDOG|WATCHDOG_NOWAYOUT|PANIC_TIMEOUT|MD|BLK_DEV_DM)[ =]'
    missing=""
    for o in BLK_DEV_LOOP SQUASHFS DM_VERITY BLK_DEV_NBD; do
        echo "$KCONF" | grep -qE "^CONFIG_${o}=(y|m)" || missing="$missing $o"
    done
    answer "Q6 kernel options missing for RAUC verity+streaming:${missing:- none}"
else
    echo "(no /proc/config.gz or /boot/config-*: checking for modules instead)"
    for m in nbd dm-verity loop squashfs; do
        if modinfo "$m" >/dev/null 2>&1; then echo "module $m: available"; else echo "module $m: not found (may be built in)"; fi
    done
    answer "Q6 kernel options for RAUC: no kernel config exposed - see module list"
fi

section "watchdog"
ls /sys/class/watchdog 2>/dev/null || echo "(no /sys/class/watchdog)"
for w in /sys/class/watchdog/watchdog*; do
    [ -d "$w" ] || continue
    for a in identity state timeout nowayout status; do
        [ -r "$w/$a" ] && printf '%s/%s: %s\n' "$w" "$a" "$(cat "$w/$a" 2>/dev/null)"
    done
done
printf 'kernel.panic = %s\n' "$(cat /proc/sys/kernel/panic 2>/dev/null)"
printf 'watchdog.service active=%s\n' "$(systemctl is-active watchdog.service 2>/dev/null)"
printf 'systemd RuntimeWatchdogUSec = %s\n' "$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null)"
WDSTATE=$(cat /sys/class/watchdog/watchdog0/state 2>/dev/null)
answer "Q7 hardware watchdog state now (userspace up): ${WDSTATE:-unknown}; kernel.panic=$(cat /proc/sys/kernel/panic 2>/dev/null)"

section "network / metered"
run nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device
if have busctl; then
    printf 'NetworkManager global Metered = %s\n' \
        "$(busctl get-property org.freedesktop.NetworkManager /org/freedesktop/NetworkManager org.freedesktop.NetworkManager Metered 2>&1)"
fi
if have nmcli; then
    nmcli -t -f DEVICE device 2>/dev/null | while read -r d; do
        printf '%s metered: %s\n' "$d" "$(nmcli -g GENERAL.METERED device show "$d" 2>/dev/null)"
    done
fi

section "clock"
date -u
run chronyc tracking

section "/data and swap"
run findmnt /data
run swapon --show

section "tools the plan relies on"
for t in grub-editenv efibootmgr efivar openssl curl rauc sgdisk resize2fs mkswap; do
    if have "$t"; then echo "$t: $(command -v "$t")"; else echo "$t: MISSING"; fi
done

printf '\n--- SUMMARY\n%s' "$SUMMARY"
cat <<'EOF'
Q8 panic -> reset, watchdog inside GRUB/early kernel, GRUB hand-off, cold-boot
   reset of the rootfs retry register, UEFI-menu path for RootfsStatusSlotA:
   NOT answerable read-only - see docs/plans/ab-phase0-runbook.md
EOF
