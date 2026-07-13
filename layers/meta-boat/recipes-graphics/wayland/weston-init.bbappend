# Default keyboard layout for the boat's helm display (confirmed on
# hardware: Weston reads this at compositor startup, not live - a
# keymap change on a running device needs a Weston/session restart).
#
# Also enable XWayland at startup - this is how X11-only containerized GUI
# apps (e.g. OpenCPN) get a display to attach to on this Wayland-only host.
# CONFIRMED ON HARDWARE: weston 10.0.2's own startup warns that the
# "modules=xwayland.so" form (what an older weston.ini comment suggests) is
# deprecated - it wants "xwayland=true" in [core] instead. Insert it after
# the existing uncommented require-input=false line rather than appending a
# second [core] section, since Weston's ini parser isn't guaranteed to
# merge duplicate sections the way you'd want.
do_install:append() {
    sed -i '/^require-input=false$/a xwayland=true' \
        ${D}${sysconfdir}/xdg/weston/weston.ini

    cat >> ${D}${sysconfdir}/xdg/weston/weston.ini <<EOF

[keyboard]
keymap_layout=fi
EOF
}
