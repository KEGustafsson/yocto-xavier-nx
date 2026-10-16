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

# The running slot.
from=""
# What the running slot's image shipped in /etc: "<sha256>  <path>" lines,
# recorded at build time (boat-image.bb). Images before it have none.
DEFAULTS="$from/usr/lib/boat-ab/conffile-defaults.sha256"

# copy PATH: replace the new slot's PATH with this slot's, if this slot has
# it. Directories are replaced whole, so files deleted here stay deleted.
# The copy is made next to it first: one that fails leaves the new image's
# own PATH in place, not nothing.
copy() {
    src="$from$1"
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
# The sha256 of a file's contents.
file_sha256() { sha256sum < "$1" | cut -d' ' -f1; }

# copy_edited PATH: carry PATH only if this board's copy is not the one the
# running slot's image shipped - someone changed it. A file nobody touched
# is left as the new image has it, so a release can change its defaults (a
# new health gate unit list, a daemon.json a new Docker needs), the way a
# package manager treats a conffile. One the image shipped and someone
# deleted stays deleted. Without the record (an older image), or for a file
# the image never shipped, it is carried as before - and then on every
# later update too, since it is not the later image's default either.
#
# A carried file is never merged with the release's: the new release's own
# version is kept next to it as PATH.boat-default (like dpkg's .dpkg-dist),
# to compare with and to take back by copying it over.
copy_edited() {
    shipped=""
    [ -r "$DEFAULTS" ] && shipped="$(awk -v p="$1" '$2 == p { print $1; exit }' "$DEFAULTS")"
    if [ -n "$shipped" ] && [ ! -e "$from$1" ] && [ ! -L "$from$1" ]; then
        if rm -f "$new$1"; then
            say "left $1 out of the new slot: it was deleted on this board"
        else
            say "WARNING: could not delete $1 in the new slot (it was deleted on this board)"
        fi
        return 0
    fi
    if [ -n "$shipped" ] && [ -f "$from$1" ] && [ ! -L "$from$1" ] \
            && [ "$(sha256sum < "$from$1" | cut -d' ' -f1)" = "$shipped" ]; then
        say "kept the new image's $1 (never changed on this board)"
        return 0
    fi
    rm -f "$new$1.boat-default"
    if [ -f "$new$1" ] && [ ! -L "$new$1" ] \
            && ! { [ -f "$from$1" ] && [ "$(file_sha256 "$from$1")" = "$(file_sha256 "$new$1")" ]; }; then
        cp -a "$new$1" "$new$1.boat-default" 2>/dev/null || true
    fi
    copy "$1"
    # A copy that failed left the release's own file: nothing beside it.
    if [ -f "$new$1.boat-default" ] && [ "$(file_sha256 "$new$1")" = "$(file_sha256 "$new$1.boat-default")" ]; then
        rm -f "$new$1.boat-default"
    elif [ -f "$new$1.boat-default" ]; then
        say "the new release's own $1 is beside it, as $1.boat-default"
    fi
}

# unit_in_new UNIT: the new slot has UNIT (or its template, for an
# instance; a bare name is a .service, as systemctl takes it).
unit_in_new() {
    u="$1"
    case "$u" in *.*) ;; *) u="$u.service" ;; esac
    t=""
    case "$u" in *@?*.*) t="${u%%@*}@.${u##*.}" ;; esac
    for d in lib/systemd/system usr/lib/systemd/system etc/systemd/system; do
        for f in "$u" $t; do
            if [ -e "$new/$d/$f" ] || [ -L "$new/$d/$f" ]; then
                return 0
            fi
        done
    done
    return 1
}

# health_units FILE: the BOAT_HEALTH_UNITS a health.conf sets ("-" if it
# sets none) - read the way boat-commit reads it, by sourcing it.
health_units() {
    sh -c '. "$1" >/dev/null 2>&1; printf "%s\n" "${BOAT_HEALTH_UNITS--}"' health "$1" 2>/dev/null || echo -
}

# health.conf's BOAT_HEALTH_UNITS is a whole list, the release's units with
# the board's own: one carried from this board can name a unit the new
# release no longer has (renamed, dropped), and every trial would then fail
# on it - the release marked failed on exactly the boards that extended the
# gate. Such a list is merged with the new release's: this board's units
# that the new slot has, then the release's own. It goes at the end of the
# carried file, where it is what counts; the rest of the file stays as is.
merge_health_units() {
    conf="$new/etc/boat-update/health.conf"
    [ -f "$conf.boat-default" ] || return 0
    units="$(health_units "$conf")"
    [ "$units" != - ] || return 0
    keep="" missing=""
    for u in $units; do
        if unit_in_new "$u"; then keep="$keep $u"; else missing="$missing $u"; fi
    done
    [ -n "$missing" ] || return 0
    release="$(health_units "$conf.boat-default")"
    [ "$release" != - ] || release=""
    for u in $release; do
        case " $keep " in *" $u "*) ;; *) keep="$keep $u" ;; esac
    done
    if printf '\n# Added by an update: this release has no%s, so the list above is merged\n# with the release'"'"'s own (health.conf.boat-default).\nBOAT_HEALTH_UNITS="%s"\n' \
            "$missing" "${keep# }" >> "$conf"; then
        say "WARNING: /etc/boat-update/health.conf names$missing, which this release does not have - merged with its own list: ${keep# }"
    else
        say "WARNING: /etc/boat-update/health.conf names$missing, which this release does not have, and could not be fixed - its trial will fail"
    fi
}

# Operator-edited configuration (the CONFFILES of the boat recipes).
copy_edited /etc/default/boat-power
copy_edited /etc/NetworkManager/conf.d/90-boat-wol.conf
copy_edited /etc/default/boat-bluetooth
copy_edited /etc/bluetooth/main.conf
copy_edited /etc/default/boat-sleep-listener
copy_edited /etc/docker/daemon.json
copy_edited /etc/boat-update/health.conf
merge_health_units
# The sleep listener's port, if moved (systemctl edit boat-sleep-listener.socket):
# only ever made on the board.
copy /etc/systemd/system/boat-sleep-listener.socket.d
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
