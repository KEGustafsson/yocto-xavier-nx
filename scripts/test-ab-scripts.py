#!/usr/bin/env python3
"""Run the A/B layout's on-board shell scripts against a fake board.

    scripts/test-ab-scripts.py        # needs grub-editenv (grub-common)

boat-grubenv-guard and boat-ab-commit decide, on a board nobody is
watching, which slot stays bootable and what GRUB will try next. shellcheck only proves they parse. This runs each one,
as shipped, against a scratch directory standing in for the board - its
/proc/cmdline, /proc/mounts, the ESP with a real grubenv written by the real
grub-editenv, the GPT names blkid reports - and checks what it writes back
and what it refuses.

Only paths are changed (/proc, /run, /boot/efi, /etc, /data are moved into
the scratch directory) and three commands are stubbed: blkid (the GPT
names), mountpoint and docker. Everything else is the script the image
ships.

Exit status: 0 = all passed, 1 = a case failed, 77 = grub-editenv missing
(scripts/lint.sh reports that as a skip).
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAYER = os.path.join(HERE, "..", "layers", "meta-boat")
AB_FILES = os.path.join(LAYER, "recipes-boat", "ab-boot", "files")

DISK = "/dev/nvme0n1"
PARTS = {"1": "APP", "2": "APP_b", "12": "esp", "17": "SWAP", "18": "DATA"}
NV_RELEASE = "# R35 (release), REVISION: 6.4, GCID: 0, BOARD: t186ref, EABI: aarch64, DATE: x\n"

GOOD_ENV = dict(ORDER="A B", A_OK="1", A_TRY="1", A_COMMITTED="1",
                B_OK="1", B_TRY="0", B_COMMITTED="1", BOAT_STAY="0")


def slurp(path):
    with open(path) as f:
        return f.read()


def env_with(**kw):
    env = dict(GOOD_ENV)
    env.update({k: str(v) for k, v in kw.items()})
    return env


class Board:
    """A scratch directory laid out like the parts of a board the scripts
    touch, plus the scripts themselves with their paths moved into it."""

    def __init__(self, root, cmdline, env=None, bak=None, torn=False,
                 parts=None, mounts=None, mountpoints=None):
        self.root = root
        for d in ("bin", "proc", "run", "etc", "data", "boot/efi/EFI/BOOT"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        self.grubenv = os.path.join(root, "boot/efi/EFI/BOOT/grubenv")
        self.write("proc/cmdline", cmdline + "\n")
        self.write("etc/nv_tegra_release", NV_RELEASE)
        self.write("partnames", "".join("%s %s\n" % kv for kv in
                                        (parts or PARTS).items()))
        if mounts is None:
            mounts = ["%sp12 /boot/efi vfat rw 0 0" % DISK,
                      "%sp18 /data ext4 rw 0 0" % DISK]
        self.write("proc/mounts", "".join(m + "\n" for m in mounts))
        if mountpoints is None:
            mountpoints = [os.path.join(root, "boot/efi"), "/data"]
        self.write("mountpoints", "".join(m + "\n" for m in mountpoints))
        if torn:
            with open(self.grubenv, "wb") as f:
                f.write(b"\0" * 1024)
        elif env is not None:
            self.make_env(self.grubenv, env)
        if bak is not None:
            self.make_env(self.grubenv + ".bak", bak)
        self.stubs()
        self.install_scripts()

    def write(self, rel, text):
        with open(os.path.join(self.root, rel), "w") as f:
            f.write(text)

    def make_env(self, path, env):
        subprocess.run(["grub-editenv", path, "create"], check=True)
        subprocess.run(["grub-editenv", path, "set"] +
                       ["%s=%s" % kv for kv in env.items()], check=True)

    def stubs(self):
        r = self.root
        self.write("bin/blkid", """#!/bin/sh
# blkid -p -o value -s PART_ENTRY_NAME <dev>
for last; do :; done
num="${last##*p}"
awk -v n="$num" '$1 == n { print $2; f = 1 } END { exit !f }' %s/partnames || exit 2
""" % r)
        self.write("bin/mountpoint", """#!/bin/sh
# mountpoint -q <path>
for last; do :; done
grep -qx "$last" %s/mountpoints
""" % r)
        self.write("bin/docker", "#!/bin/sh\necho 'Docker version 24.0.5, build x'\n")
        for name in ("blkid", "mountpoint", "docker"):
            os.chmod(os.path.join(r, "bin", name), 0o755)

    def install_scripts(self):
        r = self.root
        lib = slurp(os.path.join(AB_FILES, "boat-ab-lib.sh"))
        for old, new in (("@DISK@", DISK), ("@PARTNUM_A@", "1"),
                         ("@PARTNUM_B@", "2"), ("@PARTNUM_ESP@", "12"),
                         ("@PARTNUM_SWAP@", "17"), ("@PARTNUM_DATA@", "18"),
                         ("BOAT_AB_ESP_MOUNT=/boot/efi",
                          "BOAT_AB_ESP_MOUNT=%s/boot/efi" % r),
                         ("BOAT_AB_RUNDIR=/run/boat-ab",
                          "BOAT_AB_RUNDIR=%s/run/boat-ab" % r),
                         ("/proc/cmdline", r + "/proc/cmdline")):
            lib = lib.replace(old, new)
        assert "@" not in lib.replace("$@", ""), "lib.sh has an unfilled placeholder"
        self.write("lib.sh", lib)
        for src_dir, name in ((AB_FILES, "boat-grubenv-guard.sh"),
                              (AB_FILES, "boat-ab-commit.sh")):
            text = slurp(os.path.join(src_dir, name))
            for old, new in (("/usr/lib/boat-ab/lib.sh", r + "/lib.sh"),
                             ("/proc/mounts", r + "/proc/mounts"),
                             ("/etc/nv_tegra_release", r + "/etc/nv_tegra_release"),
                             ("/data/boat-update", r + "/data/boat-update")):
                text = text.replace(old, new)
            self.write(name, text)

    def run(self, name, extra_env=None):
        env = dict(os.environ)
        env["PATH"] = os.path.join(self.root, "bin") + ":" + env["PATH"]
        env.update(extra_env or {})
        p = subprocess.run(["sh", os.path.join(self.root, name)],
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stdout + p.stderr

    def env(self, path=None):
        """grubenv's variables, or None if grub-editenv cannot read it."""
        path = path or self.grubenv
        p = subprocess.run(["grub-editenv", path, "list"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return None
        return dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)

    def raw(self, path=None):
        with open(path or self.grubenv, "rb") as f:
            return f.read()


def case(name, script, board, expect_rc, want_env=None, want_bak_equal=None,
         says=(), unchanged=False, extra_env=None):
    return dict(name=name, script=script, board=board, expect_rc=expect_rc,
                want_env=want_env, want_bak_equal=want_bak_equal, says=says,
                unchanged=unchanged, extra_env=extra_env)


CASES = [
    # --- boat-grubenv-guard ---------------------------------------------
    case("guard: valid grubenv is copied to grubenv.bak",
         "boat-grubenv-guard.sh",
         dict(cmdline="root=x rauc.slot=A", env=env_with()),
         0, want_env=env_with(), want_bak_equal=True),
    case("guard: torn grubenv on a committed boot - booted slot first, other unusable",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=B", torn=True,
              bak=env_with(ORDER="B A", A_OK=1, A_COMMITTED=1)),
         0, want_env=dict(ORDER="B A", B_OK="1", B_TRY="0", B_COMMITTED="1",
                          A_OK="0", A_TRY="0", A_COMMITTED="0", BOAT_STAY="0"),
         want_bak_equal=True, says=["unusable until an update"]),
    case("guard: torn grubenv on a trial boot - stays on trial, other is its fallback",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", torn=True),
         0, want_env=dict(ORDER="B A", B_OK="1", B_TRY="1", B_COMMITTED="0",
                          A_OK="1", A_TRY="0", A_COMMITTED="1", BOAT_STAY="0"),
         want_bak_equal=True, says=["still on trial"]),
    case("guard: torn grubenv, booted outside GRUB - leaves it alone",
         "boat-grubenv-guard.sh",
         dict(cmdline="root=/dev/nvme0n1p1", torn=True),
         1, unchanged=True, says=["outside GRUB"]),
    case("guard: a different disk layout - touches nothing",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A", torn=True,
              parts={"1": "APP", "2": "UDA", "12": "esp"}),
         1, unchanged=True, says=["does not match"]),
    case("guard: ESP not mounted - touches nothing",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A", torn=True, mountpoints=[]),
         1, unchanged=True, says=["not mounted"]),
    # --- boat-ab-commit --------------------------------------------------
    case("commit: confirms the booted trial slot, and grubenv.bak follows",
         "boat-ab-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1),
         want_bak_equal=True),
    case("commit: refuses an invalid grubenv",
         "boat-ab-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(A_TRY=7)),
         1, unchanged=True, says=["not valid"]),
    case("commit: nothing to do outside GRUB",
         "boat-ab-commit.sh",
         dict(cmdline="root=/dev/nvme0n1p1", env=env_with()),
         0, unchanged=True, says=["outside GRUB"]),
]


def check(c, root):
    import re
    board = Board(root, **c["board"])
    before = board.raw() if os.path.exists(board.grubenv) else None
    bak_path = board.grubenv + ".bak"
    bak_before = board.raw(bak_path) if os.path.exists(bak_path) else None
    rc, out = board.run(c["script"], c["extra_env"])
    errors = []
    if rc != c["expect_rc"]:
        errors.append("exit %d, expected %d" % (rc, c["expect_rc"]))
    for pattern in c["says"]:
        if not re.search(pattern, out):
            errors.append("output never matched %r" % pattern)
    if c["unchanged"]:
        after = board.raw() if os.path.exists(board.grubenv) else None
        bak_after = board.raw(bak_path) if os.path.exists(bak_path) else None
        if after != before or bak_after != bak_before:
            errors.append("grubenv or grubenv.bak was changed")
    if c["want_env"] is not None:
        got = board.env()
        if got is None:
            errors.append("grubenv is not readable afterwards")
        elif got != c["want_env"]:
            errors.append("grubenv\n      got:  %s\n      want: %s" % (
                " ".join("%s=%s" % kv for kv in sorted(got.items())),
                " ".join("%s=%s" % kv for kv in sorted(c["want_env"].items()))))
        if os.path.exists(board.grubenv) and len(board.raw()) != 1024:
            errors.append("grubenv is no longer a 1 KiB block")
    if c["want_bak_equal"] and board.raw(bak_path) != board.raw():
        errors.append("grubenv.bak is not a copy of grubenv")
    leftovers = [f for f in os.listdir(os.path.dirname(board.grubenv))
                 if f not in ("grubenv", "grubenv.bak")]
    if leftovers:
        errors.append("temporary files left on the ESP: %s" % leftovers)
    return errors, out


def main():
    if not shutil.which("grub-editenv"):
        print("SKIP: grub-editenv not installed (apt-get install grub-common)")
        return 77
    failed = 0
    with tempfile.TemporaryDirectory(prefix="ab-scripts-") as work:
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
