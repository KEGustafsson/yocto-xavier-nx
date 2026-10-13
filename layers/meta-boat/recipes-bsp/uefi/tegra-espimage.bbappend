# A/B OS updates (docs/plans/ab-os-updates.md 5.1): put GRUB, its grub.cfg
# and the initial grubenv on the ESP next to L4TLauncher, which hands off to
# GRUB once L4TDefaultBootMode is 0. Single-slot builds leave the ESP as
# meta-tegra makes it.
PACKAGE_INSTALL:append = "${@' boat-grub-esp' if d.getVar('BOAT_AB_LAYOUT') == '1' else ''}"
