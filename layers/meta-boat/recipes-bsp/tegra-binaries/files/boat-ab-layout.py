#!/usr/bin/env python3
"""Derive the boat A/B NVMe partition layout from NVIDIA's own template.

    boat-ab-layout.py SRC.xml DST.xml SWAP_BYTES DATA_MIN_BYTES NUMBERS

SRC is L4T's tools/kernel_flash/flash_l4t_nvme_rootfs_ab.xml (APP + APP_b).
NUMBERS pins the GPT entry number of every partition the boat's boot and
update path addresses, e.g. "APP=1,APP_b=2,esp=12,SWAP=17,DATA=18".

The result differs from the template in exactly these ways - see
docs/plans/ab-os-updates.md section 5.2:

  * APP_b has no <filename>, so the flasher creates it but does not write
    it. GRUB never boots it until the first update has filled it (B_OK=0 in
    the initial grubenv), and leaving it out halves the USB 2.0 write at
    flash time.
  * A SWAP partition and a fill-to-end DATA partition are appended after
    APP_b. make-sdcard grows the first partition carrying the 0x800
    allocation bit to the end of the disk, so DATA takes whatever the SSD
    has beyond the fixed partitions.
  * Every partition named in NUMBERS ends up at exactly that GPT entry
    number - SWAP and DATA by explicit id, the rest verified - or the build
    fails here.

Why numbers and not labels or PARTUUIDs: partition LABELS collide with any
JetPack SD card or USB stick that carries APP/esp too, and meta-tegra's
make-sdcard parses <unique_guid> but never hands it to sgdisk, so fixed
PARTUUIDs do not survive a flash. The devkit has one NVMe drive, always
nvme0n1, while SD cards are mmcblk* and USB media sd* - so "/dev/nvme0n1p<N>",
checked against the GPT name at <N> before anything acts on it, is the
address that cannot be confused with foreign media.

Generated at build time rather than committed, so this repository carries
no copy of NVIDIA's layout and a BSP update's changes to it flow through.
Fails loudly if the template no longer has the shape this expects.
"""
import sys
import xml.etree.ElementTree as ET

LINUX_SWAP_TYPE = "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F"
LINUX_DATA_TYPE = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
PARTITION_TABLE_TYPES = ("GP1", "protective_master_boot_record",
                         "primary_gpt", "GPT", "secondary_gpt")


def die(msg):
    sys.stderr.write("boat-ab-layout: %s\n" % msg)
    sys.exit(1)


def new_partition(name, number, size, alloc, type_guid, description):
    part = ET.Element("partition", {"name": name, "id": str(number),
                                    "type": "data"})
    for tag, text in (
        ("allocation_policy", "sequential"),
        ("filesystem_type", "basic"),
        ("size", str(size)),
        ("file_system_attribute", "0"),
        ("allocation_attribute", alloc),
        ("align_boundary", "4096"),
        ("percent_reserved", "0"),
        ("partition_type_guid", type_guid),
        ("description", description),
    ):
        ET.SubElement(part, tag).text = " %s " % text
    return part


def gpt_numbers(partitions):
    """Number partitions the way meta-tegra's nvflashxmlparse does.

    A partition without an explicit id gets "previous id + 1" - which is
    why anything appended after APP_b (id 2) would default to 3 and collide
    with the kernel partition, and why SWAP and DATA carry explicit ids.
    """
    numbers, nextpart = {}, 1
    for p in partitions:
        if p.get("type") in PARTITION_TABLE_TYPES:
            continue
        num = int(p.get("id", nextpart))
        if num in numbers.values():
            die("partition number %d used twice (at %s)" % (num, p.get("name")))
        numbers[p.get("name")] = num
        nextpart = num + 1
    return numbers


def main(argv):
    if len(argv) != 6:
        die("usage: %s SRC DST SWAP_BYTES DATA_MIN_BYTES NUMBERS" % argv[0])
    src, dst, swap_bytes, data_min, numbers_arg = argv[1:]
    for label, value in (("SWAP_BYTES", swap_bytes),
                         ("DATA_MIN_BYTES", data_min)):
        if not value.isdigit() or int(value) <= 0:
            die("%s must be a positive byte count, got %r" % (label, value))
    try:
        want = {k: int(v) for k, v in
                (item.split("=", 1) for item in numbers_arg.split(","))}
    except ValueError:
        die("NUMBERS must look like APP=1,APP_b=2,..., got %r" % numbers_arg)
    for required in ("APP", "APP_b", "esp", "SWAP", "DATA"):
        if required not in want:
            die("NUMBERS has no entry for %s" % required)

    tree = ET.parse(src)
    devices = tree.getroot().findall("device")
    if len(devices) != 1 or devices[0].get("type") != "external":
        die("%s: expected exactly one external <device>" % src)
    device = devices[0]
    parts = device.findall("partition")
    byname = {p.get("name"): p for p in parts}
    for required in ("APP", "APP_b", "esp"):
        if required not in byname:
            die("%s: no %s partition - not the NVMe rootfs A/B template?"
                % (src, required))
    for name in ("SWAP", "DATA"):
        if name in byname:
            die("%s: already has a %s partition" % (src, name))
    if parts[-1].get("type") != "secondary_gpt":
        die("%s: last partition is %r, expected the secondary GPT"
            % (src, parts[-1].get("name")))
    # Nothing may already fill to the end, or make-sdcard would pick it
    # instead of DATA.
    for p in parts:
        alloc = (p.findtext("allocation_attribute") or "0").strip()
        if int(alloc, 0) & 0x800:
            die("%s: partition %s already fills to end" % (src, p.get("name")))

    fname = byname["APP_b"].find("filename")
    if fname is not None:
        byname["APP_b"].remove(fname)

    used = gpt_numbers(parts)
    for name in ("SWAP", "DATA"):
        if want[name] in used.values():
            die("%s=%d is already taken by %s" % (
                name, want[name],
                [k for k, v in used.items() if v == want[name]][0]))

    index = list(device).index(parts[-1])
    device.insert(index, new_partition(
        "SWAP", want["SWAP"], swap_bytes, "8", LINUX_SWAP_TYPE,
        "boat: swap, mkswap'd on first boot by boat-storage."))
    device.insert(index + 1, new_partition(
        "DATA", want["DATA"], data_min, "0x808", LINUX_DATA_TYPE,
        "boat: /data, shared by both rootfs slots. Fills the rest of the "
        "disk (0x800); its filesystem is grown on first boot."))

    final = gpt_numbers(device.findall("partition"))
    for name, num in sorted(want.items()):
        if final.get(name) != num:
            die("%s would be GPT entry %s, expected %d - the template changed; "
                "update BOAT_AB_PARTNUM_* in meta-boat's layer.conf"
                % (name, final.get(name), num))

    tree.write(dst, encoding="UTF-8", xml_declaration=True)


if __name__ == "__main__":
    main(sys.argv)
