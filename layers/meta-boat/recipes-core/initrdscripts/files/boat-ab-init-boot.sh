#!/bin/sh
# /init of tegra-minimal-initramfs for an A/B boat image (BOAT_AB_LAYOUT).
#
# meta-tegra's init-boot.sh, with one behavioural change that the A/B design
# depends on (docs/plans/ab-os-updates.md 5.5): if the root filesystem cannot
# be mounted, this RESETS the board instead of waiting forever. The upstream
# script's mount loop never advances its counter, so a slot whose rootfs will
# not mount sat there indefinitely - no reset, so no fallback to the other
# slot. A bounded retry followed by a reset hands control back to GRUB, which
# counts the failed attempt and moves on.
#
# boat.stay=1 on the kernel command line (GRUB adds it when grubenv has
# BOAT_STAY=1, for bench debugging) drops to a shell instead of resetting.
PATH=/sbin:/bin:/usr/sbin:/usr/bin
mount -t proc proc -o nosuid,nodev,noexec /proc
mount -t devtmpfs none -o nosuid /dev
mount -t sysfs sysfs -o nosuid,nodev,noexec /sys
mount -t efivarfs efivarfs -o nosuid,nodev,noexec /sys/firmware/efi/efivars

rootdev=""
opt="rw"
wait=""
fstype="auto"
boat_stay=""

boat_give_up() {
    echo "boat: $1"
    if [ -n "$boat_stay" ]; then
        echo "boat: boat.stay=1 - dropping to a shell instead of resetting"
        exec sh
    fi
    echo "boat: resetting in 5 s so GRUB can count this attempt and fall back"
    sleep 5
    reboot -f
    # reboot -f should not return; if busybox lacks the applet, force it
    echo b > /proc/sysrq-trigger
    exec sh
}

[ ! -f /etc/platform-preboot ] || . /etc/platform-preboot

if [ -z "$rootdev" ]; then
    for bootarg in $(cat /proc/cmdline); do
        case "$bootarg" in
            root=*) rootdev="${bootarg##root=}" ;;
            ro) opt="ro" ;;
            rootwait) wait="yes" ;;
            rootfstype=*) fstype="${bootarg##rootfstype=}" ;;
        esac
    done
fi
[ -n "$rootdev" ] || boat_give_up "no root device"

if [ -n "$wait" ] && [ ! -b "${rootdev}" ]; then
    echo "Waiting for ${rootdev}..."
    count=0
    while [ $count -lt 100 ]; do
        test -b "${rootdev}" && break
        sleep 0.1
        count=$((count + 1))
    done
fi

echo "Mounting ${rootdev}..."
[ -d /mnt ] || mkdir -p /mnt
count=0
while [ $count -lt 5 ]; do
    if mount -t "${fstype}" -o "${opt}" "${rootdev}" /mnt; then
        break
    fi
    count=$((count + 1))
    sleep 1
done
[ $count -lt 5 ] || boat_give_up "could not mount ${rootdev}"

[ ! -f /etc/platform-pre-switchroot ] || . /etc/platform-pre-switchroot

echo "Switching to rootfs on ${rootdev}..."
mount --move /sys  /mnt/sys
mount --move /proc /mnt/proc
mount --move /dev  /mnt/dev
exec switch_root /mnt /sbin/init
