#!/usr/bin/env python3
"""Run the A/B initramfs's /init and root selection, as shipped, on a fake board.

    scripts/test-ab-initramfs.py

On an A/B image, tegra-minimal-initramfs's /init is meta-boat's
boat-ab-init-boot.sh, and its /etc/platform-preboot is
boat-ab-platform-preboot.sh. Together they decide, on every boot, which
partition becomes the root filesystem - exactly the slot GRUB named, after
a GPT name check - and they reset the board when that fails, so GRUB can
count the attempt and fall back. A mistake there fails every slot at once.

This runs both against a scratch directory standing in for the board: its
/proc/cmdline, the block devices that exist and the GPT name blkid reports
for each. mount, blkid, sleep, reboot, switch_root and the rescue shell are
stubs that log what they were asked to do, so each case checks what /init
mounted, and whether it switched to it, reset the board or stopped.

Exit status: 0 = all passed, 1 = a case failed.
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "..", "layers", "meta-boat", "recipes-core",
                     "initrdscripts", "files")
DISK = "/dev/nvme0n1"
SLOT = {"A": DISK + "p1", "B": DISK + "p2"}
NAMES = {"1": "APP", "2": "APP_b", "12": "esp"}


def slurp(path):
    with open(path) as f:
        return f.read()


class Board:
    def __init__(self, root, cmdline, devices=None, names=None, mount_fails=0):
        """devices: the block devices that exist (default: every slot).
        mount_fails: how many times mounting the root filesystem fails
        before it works."""
        self.root = root
        for d in ("bin", "proc", "etc", "mnt"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        self.write("proc/cmdline", cmdline + "\n")
        self.write("devices", "".join(d + "\n" for d in
                                      (SLOT.values() if devices is None else devices)))
        self.write("names", "".join("%s %s\n" % kv for kv in (names or NAMES).items()))
        self.write("mount-fails", "%d\n" % mount_fails)
        self.stubs()
        self.install()

    def write(self, rel, text, mode=None):
        path = os.path.join(self.root, rel)
        with open(path, "w") as f:
            f.write(text)
        if mode is not None:
            os.chmod(path, mode)

    def stubs(self):
        r = self.root
        log = 'echo "%s $*" >> ' + r + '/log\n'
        stubs = {
            # The root filesystem mount fails mount-fails times; the rest
            # (proc, sys, --move) always work.
            "mount": """#!/bin/sh
echo "mount $*" >> %(r)s/log
for last; do :; done
if [ "$last" = %(r)s/mnt ]; then
    n=$(cat %(r)s/mount-fails)
    if [ "$n" -gt 0 ]; then
        echo $((n - 1)) > %(r)s/mount-fails
        exit 32
    fi
fi
exit 0
""" % dict(r=r),
            # blkid -p -o value -s PART_ENTRY_NAME <dev>
            "blkid": """#!/bin/sh
for last; do :; done
grep -qx "$last" %(r)s/devices || exit 2
awk -v n="${last##*p}" '$1 == n { print $2; f = 1 } END { exit !f }' %(r)s/names || exit 2
""" % dict(r=r),
            "sleep": "#!/bin/sh\nexit 0\n",
            "mkdir": "#!/bin/sh\nexit 0\n",
            # A reset ends the run the way the real one ends the boot.
            "reboot": "#!/bin/sh\n" + (log % "reboot") + "kill -TERM $PPID\n",
            "switch_root": "#!/bin/sh\n" + (log % "switch_root"),
            "rescue-shell": "#!/bin/sh\n" + (log % "shell"),
        }
        for name, text in stubs.items():
            self.write("bin/" + name, text, 0o755)

    def install(self):
        r = self.root
        isdev = "isdev() { grep -qx \"$1\" %s/devices; }\n" % r
        preboot = slurp(os.path.join(FILES, "boat-ab-platform-preboot.sh"))
        for old, new in (("@DISK@", DISK), ("@PARTNUM_A@", "1"), ("@PARTNUM_B@", "2"),
                         ("/proc/cmdline", r + "/proc/cmdline"),
                         ('[ ! -b "$rootdev" ]', '! isdev "$rootdev"'),
                         ('[ -b "$rootdev" ]', 'isdev "$rootdev"')):
            assert old in preboot, "preboot changed - update this test's rewrite of %r" % old
            preboot = preboot.replace(old, new)
        assert "@" not in preboot, "preboot has an unfilled placeholder"
        self.write("etc/platform-preboot", preboot)
        init = slurp(os.path.join(FILES, "boat-ab-init-boot.sh"))
        for old, new in (("PATH=/sbin:/bin:/usr/sbin:/usr/bin\n",
                          "PATH=%s/bin:$PATH\n%s" % (r, isdev)),
                         ("/etc/platform-preboot", r + "/etc/platform-preboot"),
                         ("/etc/platform-pre-switchroot", r + "/etc/platform-pre-switchroot"),
                         ("/proc/cmdline", r + "/proc/cmdline"),
                         ("/proc/sysrq-trigger", r + "/proc/sysrq-trigger"),
                         ('[ ! -b "${rootdev}" ]', '! isdev "${rootdev}"'),
                         ('test -b "${rootdev}"', 'isdev "${rootdev}"'),
                         (" /mnt", " %s/mnt" % r),
                         ("exec sh\n", "exec rescue-shell\n")):
            assert old in init, "/init changed - update this test's rewrite of %r" % old
            init = init.replace(old, new)
        self.write("init", init)

    def run(self):
        p = subprocess.run(["sh", os.path.join(self.root, "init")],
                           capture_output=True, text=True, timeout=30)
        log = os.path.join(self.root, "log")
        return p.stdout + p.stderr, slurp(log).splitlines() if os.path.exists(log) else []


def case(name, cmdline, ends, root_mounts=(), says=(), **board):
    """ends: "switch_root", "reboot" or "shell" - what the run ends in.
    root_mounts: the root filesystem mounts tried, as "-t FS -o OPT DEV"."""
    return dict(name=name, cmdline=cmdline, ends=ends, root_mounts=list(root_mounts),
                says=says, board=board)


GRUB_B = "root=%s rootwait rw rootfstype=ext4 rauc.slot=B" % SLOT["B"]
CASES = [
    case("mounts the slot GRUB named, and switches to it", GRUB_B, "switch_root",
         ["-t ext4 -o rw " + SLOT["B"]], says=[r"root filesystem is %s \(APP_b\)" % SLOT["B"]]),
    case("slot A by number", "root=%s rootwait rauc.slot=A" % SLOT["A"], "switch_root",
         ["-t auto -o rw " + SLOT["A"]]),
    case("'ro' on the command line is honoured", GRUB_B + " ro", "switch_root",
         ["-t ext4 -o ro " + SLOT["B"]]),
    case("no root= (booted outside GRUB) boots slot A by number", "console=ttyTCU0",
         "switch_root", ["-t auto -o rw " + SLOT["A"]], says=["booted outside GRUB"]),
    case("a root= that is not a slot resets, mounting nothing", "root=/dev/mmcblk0p1 rootwait",
         "reboot", says=["not one of the A/B slots"]),
    case("root=LABEL=APP is refused - a label is not a slot", "root=LABEL=APP", "reboot",
         says=["not one of the A/B slots"]),
    case("a slot whose GPT name is wrong resets, mounting nothing", GRUB_B, "reboot",
         names=dict(NAMES, **{"2": "UDA"}), says=["GPT entry 'UDA', not the expected slot"]),
    case("A's number holding APP_b is refused", "root=%s" % SLOT["A"], "reboot",
         names=dict(NAMES, **{"1": "APP_b", "2": "APP"})),
    case("a slot device that never appears resets", GRUB_B, "reboot",
         devices=[SLOT["A"]], says=["did not appear"]),
    case("a root filesystem that will not mount is retried, then resets", GRUB_B, "reboot",
         ["-t ext4 -o rw " + SLOT["B"]] * 5, mount_fails=99, says=["could not mount"]),
    case("a mount that works on a retry still boots", GRUB_B, "switch_root",
         ["-t ext4 -o rw " + SLOT["B"]] * 3, mount_fails=2),
    case("boat.stay=1 stops at a shell instead of resetting", GRUB_B + " boat.stay=1", "shell",
         ["-t ext4 -o rw " + SLOT["B"]] * 5, mount_fails=99, says=["dropping to a shell"]),
]


def check(c, root):
    board = Board(root, c["cmdline"], **c["board"])
    out, log = board.run()
    errors = []
    ends = [l.split()[0] for l in log if l.split()[0] in ("switch_root", "reboot", "shell")]
    if ends[:1] != [c["ends"]]:
        errors.append("ended in %s, expected %s" % (ends[:1] or "nothing", c["ends"]))
    mnt = " %s/mnt" % root
    tried = [l[len("mount "):-len(mnt)] for l in log
             if l.startswith("mount ") and l.endswith(mnt) and "--move" not in l]
    if tried != c["root_mounts"]:
        errors.append("root mounts tried %r, expected %r" % (tried, c["root_mounts"]))
    if c["ends"] == "switch_root" and ("switch_root %s/mnt /sbin/init" % root) not in log:
        errors.append("did not switch_root into the mounted slot")
    for pattern in c["says"]:
        if not re.search(pattern, out):
            errors.append("output never matched %r" % pattern)
    return errors, out


def main():
    failed = 0
    with tempfile.TemporaryDirectory(prefix="ab-initramfs-") as work:
        for i, c in enumerate(CASES):
            root = os.path.join(work, str(i))
            os.makedirs(root)
            errors, out = check(c, root)
            if errors:
                failed += 1
                print("FAIL: %s" % c["name"])
                for e in errors:
                    print("    " + e)
                print("    output:\n" + "\n".join("      " + l for l in out.splitlines()))
            else:
                print("ok:   %s" % c["name"])
    print("%d of %d cases passed" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
