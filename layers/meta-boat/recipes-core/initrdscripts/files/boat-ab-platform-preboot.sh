# shellcheck shell=sh
# Sourced by /init, which reads every variable set here (rootdev, opt, wait,
# fstype, boat_stay) and defines boat_give_up before sourcing this file.
# shellcheck disable=SC2034
# /etc/platform-preboot of tegra-minimal-initramfs for an A/B boat image,
# sourced by /init before the root filesystem is mounted. Sets rootdev.
#
# GRUB names the slot it chose on the command line as root=@DISK@p<N>.
# That device, and nothing else, is the root filesystem:
#   - no search by partition LABEL - a JetPack SD card or a USB stick may
#     carry an "APP" too, and blkid would happily return it;
#   - no fallback to /dev/mmcblk0p1, which on this board is either nothing
#     or somebody else's SD card.
# Before it is used, the GPT entry name at that number must be APP or APP_b,
# so a mis-numbered layout stops here instead of mounting the wrong thing.
#
# Without a root= (L4TLauncher's own extlinux or boot.img fallback, used only
# when GRUB is missing from the ESP), boot slot A by number.

boat_slot_a="@DISK@p@PARTNUM_A@"
boat_slot_b="@DISK@p@PARTNUM_B@"

for bootarg in $(cat /proc/cmdline); do
    case "$bootarg" in
        root=*) rootdev="${bootarg##root=}" ;;
        ro) opt="ro" ;;
        rootwait) wait="yes" ;;
        rootfstype=*) fstype="${bootarg##rootfstype=}" ;;
        boat.stay=1) boat_stay=1 ;;
    esac
done

case "$rootdev" in
    "$boat_slot_a"|"$boat_slot_b") ;;
    "")
        echo "boat: no root= on the command line (booted outside GRUB) - using slot A"
        rootdev="$boat_slot_a"
        ;;
    *)
        boat_give_up "root=$rootdev is not one of the A/B slots ($boat_slot_a, $boat_slot_b)"
        ;;
esac

# NVMe enumerates late; wait up to 10 s for the node before checking it.
boat_n=0
while [ ! -b "$rootdev" ] && [ $boat_n -lt 100 ]; do
    sleep 0.1
    boat_n=$((boat_n + 1))
done
[ -b "$rootdev" ] || boat_give_up "$rootdev did not appear"

boat_name="$(blkid -p -o value -s PART_ENTRY_NAME "$rootdev" 2>/dev/null)"
case "$rootdev:$boat_name" in
    "$boat_slot_a:APP"|"$boat_slot_b:APP_b")
        echo "boat: root filesystem is $rootdev ($boat_name)"
        ;;
    *)
        boat_give_up "$rootdev is GPT entry '${boat_name:-?}', not the expected slot - refusing to mount it"
        ;;
esac
