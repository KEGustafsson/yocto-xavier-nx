#!/bin/sh
# Hook script inside every boat update bundle (docs/plans/ab-os-updates.md
# 5.6 step 6, 5.8). RAUC runs it as "slot-post-install" after writing the
# new rootfs, with that slot mounted read-write at $RAUC_SLOT_MOUNT_POINT
# and the running slot at /. It carries over the state that belongs to this
# board rather than to the image - identity, keys, network and operator
# settings - so the new slot boots as the same boat.
#
# It runs from the NEW bundle, so the list below is the new release's idea
# of what to carry; the old slot is only read. It stays self-contained per
# slot: nothing here is shared, so the old slot is untouched for a fallback.
# Anything it cannot copy is reported and skipped - a missing optional file
# must not abort an update - except the root and boat password hashes,
# without which nobody could log in to the new slot.
set -eu

case "${1:-}" in
    slot-post-install) ;;
    *) exit 0 ;;
esac
[ "${RAUC_SLOT_CLASS:-}" = rootfs ] || exit 0

new="${RAUC_SLOT_MOUNT_POINT:?RAUC did not say where the new slot is mounted}"
[ -d "$new/etc" ] || { echo "boat-bundle-hook: $new has no /etc - not a rootfs" >&2; exit 1; }

say() { echo "boat-bundle-hook: $*"; }

# copy PATH: replace the new slot's PATH with this slot's, if this slot has
# it. Directories are replaced whole, so files deleted here stay deleted.
# The copy is made next to it first: one that fails leaves the new image's
# own PATH in place, not nothing.
copy() {
    src="$1"
    dst="$new$1"
    if [ ! -e "$src" ] && [ ! -L "$src" ]; then
        return 0
    fi
    mkdir -p "$(dirname "$dst")"
    rm -rf "$dst.boat-new"
    if cp -a "$src" "$dst.boat-new" && rm -rf "$dst" && mv "$dst.boat-new" "$dst"; then
        say "carried over $src"
    else
        rm -rf "$dst.boat-new"
        say "WARNING: could not carry over $src"
    fi
}

copy /etc/machine-id
for key in /etc/ssh/ssh_host_*; do
    copy "$key"
done
copy /etc/boat-sleep.key
copy /etc/NetworkManager/system-connections
copy /var/lib/bluetooth
copy /etc/hostname
copy /etc/localtime
copy /etc/nv_boot_control.conf
# Operator-edited configuration (the CONFFILES of the boat recipes).
copy /etc/default/boat-power
copy /etc/default/boat-bluetooth
copy /etc/docker/daemon.json
copy /etc/boat-update/health.conf
# root's home is in the slot on this distro (/home/root): its SSH keys.
copy /home/root/.ssh

# Passwords (plan D5): keep this board's hashes for root and boat, merged
# into the new slot's /etc/shadow so any account the new image adds or
# removes is left as the image has it.
shadow_new="$new/etc/shadow"
tmp="$shadow_new.boat-new"
awk -F: -v OFS=: '
    FILENAME == ARGV[1] { if ($1 == "root" || $1 == "boat") hash[$1] = $2; next }
    ($1 in hash) { $2 = hash[$1] }
    { print }
' /etc/shadow "$shadow_new" > "$tmp"
# Never install a shadow file that lost lines or root's entry: nobody could
# log in to the new slot with a password. Failing here fails the install,
# which leaves the board on this slot.
lines() { awk 'END { print NR }' "$1"; }
if [ "$(lines "$tmp")" != "$(lines "$shadow_new")" ] || ! grep -q '^root:' "$tmp"; then
    rm -f "$tmp"
    echo "boat-bundle-hook: the merged /etc/shadow is incomplete - refusing to install it" >&2
    exit 1
fi
for user in root boat; do
    if grep -q "^$user:" /etc/shadow && ! grep -q "^$user:" "$tmp"; then
        say "WARNING: the new slot has no '$user' account - its password was not carried"
    fi
done
chmod --reference="$shadow_new" "$tmp" 2>/dev/null || chmod 0600 "$tmp"
chown --reference="$shadow_new" "$tmp" 2>/dev/null || true
mv "$tmp" "$shadow_new"
if grep -q -e '^root:' -e '^boat:' /etc/shadow; then
    say "carried over the root and boat password hashes"
else
    say "WARNING: this slot's /etc/shadow has no root or boat entry - the new slot keeps the image's passwords"
fi
sync
