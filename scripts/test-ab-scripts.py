#!/usr/bin/env python3
"""Run the A/B layout's on-board shell scripts against a fake board.

    scripts/test-ab-scripts.py        # needs grub-editenv (grub-common)

boat-grubenv-guard, boat-commit and RAUC's pre/post-install handlers
decide, on a board nobody is watching, which slot stays bootable and what
GRUB will try next. shellcheck only proves they parse. This runs each one,
as shipped, against a scratch directory standing in for the board - its
/proc/cmdline, /proc/mounts, the ESP with a real grubenv written by the real
grub-editenv, the GPT names blkid reports - and checks what it writes back
and what it refuses.

Only paths are changed (/proc, /run, /boot/efi, /etc, /data are moved into
the scratch directory) and a few commands are stubbed: blkid (the GPT
names), mountpoint, docker, systemctl (unit states; stop and reboot are
logged) and rauc (status, and mark-good/mark-bad writing grubenv the way
RAUC's grub backend does). Everything else is the script the image ships.

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
RAUC_FILES = os.path.join(LAYER, "external", "rauc", "recipes-boat",
                          "boat-rauc", "files")

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
                 parts=None, mounts=None, mountpoints=None, active=None,
                 failed=(), rauc="ok", release="1.1.0", health_deadline=0,
                 late=(), editenv_fails=False):
        self.root = root
        for d in ("bin", "proc", "run", "etc", "data", "boot/efi/EFI/BOOT"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        self.grubenv = os.path.join(root, "boot/efi/EFI/BOOT/grubenv")
        self.write("proc/cmdline", cmdline + "\n")
        self.write("etc/nv_tegra_release", NV_RELEASE)
        self.write("etc/boat-release", release + "\n")
        # The shipped health.conf, retrying nothing unless asked: a check
        # that fails, fails at once.
        os.makedirs(os.path.join(root, "etc/boat-update"), exist_ok=True)
        self.write("etc/boat-update/health.conf",
                   slurp(os.path.join(AB_FILES, "health.conf"))
                   + "BOAT_HEALTH_DEADLINE=%d\n" % health_deadline)
        # Units that are not active the first time they are asked about.
        self.write("late", "".join(u + "\n" for u in late))
        self.editenv_fails = editenv_fails
        self.write("proc/uptime", "100.00 100.00\n")
        if active is None:
            active = ["NetworkManager.service", "systemd-logind.service",
                      "dbus.service", "boat-bootmode-guard.service",
                      "sshd.socket", "docker.service"]
        self.write("active", "".join(u + "\n" for u in active))
        self.write("failed", "".join(u + "\n" for u in failed))
        self.rauc = rauc
        self.write("partnames", "".join("%s %s\n" % kv for kv in
                                        (parts or PARTS).items()))
        if mounts is None:
            rootpart = "2" if "rauc.slot=B" in cmdline else "1"
            mounts = ["%sp%s / ext4 rw,relatime 0 0" % (DISK, rootpart),
                      "%sp12 /boot/efi vfat rw 0 0" % DISK,
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
        self.write("bin/systemctl", """#!/bin/sh
echo "systemctl $*" >> %(r)s/log
case "$1" in
    is-active)
        if grep -qx "$3" %(r)s/late && [ ! -e "%(r)s/asked-$3" ]; then
            : > "%(r)s/asked-$3"
            exit 3
        fi
        grep -qx "$3" %(r)s/active ;;
    is-failed) grep -qx "$3" %(r)s/failed ;;
    cat) [ "$2" = docker.service ] ;;
    *) exit 0 ;;
esac
""" % dict(r=r))
        names = ["blkid", "mountpoint", "docker", "systemctl"]
        if self.editenv_fails:
            # An ESP that will not take the write: reading still works.
            self.write("bin/grub-editenv", """#!/bin/sh
[ "$2" = set ] && exit 1
exec %s "$@"
""" % shutil.which("grub-editenv"))
            names.append("grub-editenv")
        if self.rauc is not None:
            # RAUC's grub backend: mark-good = OK=1 TRY=0, mark-bad = OK=0,
            # for the slot named by rauc.slot=.
            self.write("bin/rauc", """#!/bin/sh
echo "rauc $*" >> %(r)s/log
[ "%(state)s" = ok ] || exit 1
slot=$(sed -n 's/.*rauc\\.slot=\\([AB]\\).*/\\1/p' %(r)s/proc/cmdline)
case "$2" in
    mark-good) grub-editenv %(env)s set "${slot}_OK=1" "${slot}_TRY=0" ;;
    mark-bad) grub-editenv %(env)s set "${slot}_OK=0" "${slot}_TRY=0" ;;
esac
""" % dict(r=r, state=self.rauc, env=self.grubenv))
            names.append("rauc")
        for name in names:
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
                              (AB_FILES, "boat-commit.sh"),
                              (RAUC_FILES, "boat-rauc-pre-install.sh"),
                              (RAUC_FILES, "boat-rauc-post-install.sh")):
            text = slurp(os.path.join(src_dir, name))
            for old, new in (("/usr/lib/boat-ab/lib.sh", r + "/lib.sh"),
                             ("/proc/mounts", r + "/proc/mounts"),
                             ("/proc/uptime", r + "/proc/uptime"),
                             ("/proc/cmdline", r + "/proc/cmdline"),
                             ("/etc/boat-update", r + "/etc/boat-update"),
                             ("/etc/boat-release", r + "/etc/boat-release"),
                             ("/etc/nv_tegra_release", r + "/etc/nv_tegra_release"),
                             ("/data/boat-update", r + "/data/boat-update")):
                text = text.replace(old, new)
            self.write(name, text)

    def run(self, name, extra_env=None, args=()):
        env = dict(os.environ)
        env["PATH"] = os.path.join(self.root, "bin") + ":" + env["PATH"]
        env.update(extra_env or {})
        p = subprocess.run(["sh", os.path.join(self.root, name)] + list(args),
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stdout + p.stderr

    def read(self, rel):
        path = os.path.join(self.root, rel)
        return slurp(path) if os.path.exists(path) else ""

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


RAUC_TARGET_B = {"RAUC_TARGET_SLOTS": "2", "RAUC_SLOT_BOOTNAME_2": "B",
                 "RAUC_SLOT_DEVICE_2": DISK + "p2",
                 "RAUC_SLOT_BOOTNAME_1": "A", "RAUC_SLOT_DEVICE_1": DISK + "p1",
                 "RAUC_META_BOAT_L4T": "35.6.4",
                 "RAUC_META_BOAT_DOCKER": "24.0.5", "RAUC_META_BOAT_RELEASE": "1.1.0"}


def case(name, script, board, expect_rc, want_env=None, want_bak_equal=None,
         says=(), unchanged=False, extra_env=None, args=(), log=(), not_log=(),
         files=None, absent=()):
    """log/not_log: entries that must / must not be in the stubs' command
    log. files: {path under the fake root: a regex its contents must
    match}. absent: paths under the fake root that must not exist."""
    return dict(name=name, script=script, board=board, expect_rc=expect_rc,
                want_env=want_env, want_bak_equal=want_bak_equal, says=says,
                unchanged=unchanged, extra_env=extra_env, args=args, log=log,
                not_log=not_log, files=files or {}, absent=absent)


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
    # --- boat-commit ----------------------------------------------------
    case("commit: a healthy trial is committed in one grubenv write; deadline and inhibitor stopped",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1),
         want_bak_equal=True,
         log=["systemctl stop boat-trial-deadline.timer",
              "systemctl stop boat-trial-inhibit.service"],
         not_log=["mark-good", "systemctl reboot"],
         files={"data/boat-update/last-result": "committed slot B"}),
    case("commit: a grubenv that will not take the write is NOT reported committed",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), editenv_fails=True),
         1, unchanged=True, says=["NOT committed"],
         not_log=["boat-trial-inhibit", "boat-trial-deadline"]),
    case("commit: a unit that comes up late is waited for",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0),
              health_deadline=6, late=["NetworkManager.service"]),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1), not_log=["systemctl reboot"]),
    case("commit: without /data a trial still commits - RAUC (which waits for /data) is not asked",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), rauc="broken",
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp2 / ext4 rw 0 0" % DISK],
              active=["NetworkManager.service", "systemd-logind.service",
                      "dbus.service", "sshd.socket", "boat-bootmode-guard.service"]),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1), not_log=["rauc status"],
         absent=["data/boat-update/last-result"]),
    case("commit: a trial with a dead critical unit is marked bad and rebooted",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0),
              active=["systemd-logind.service", "dbus.service", "sshd.socket",
                      "boat-bootmode-guard.service", "docker.service"]),
         1, want_env=env_with(ORDER="B A", B_OK=0, B_TRY=0, B_COMMITTED=0),
         log=["systemctl reboot"], not_log=["mark-bad"],
         says=["NetworkManager.service is not active"],
         files={"data/boat-update/last-result": "FAILED",
                "data/boat-update/failed-versions": "1.1.0"}),
    case("commit: Docker down fails a trial when /data is mounted",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0),
              active=["NetworkManager.service", "systemd-logind.service",
                      "dbus.service", "sshd.socket", "boat-bootmode-guard.service"]),
         1, log=["systemctl reboot"], says=["docker.service is not active"]),
    case("commit: Docker is not required when /data is not mounted",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0),
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp2 / ext4 rw 0 0" % DISK],
              active=["NetworkManager.service", "systemd-logind.service",
                      "dbus.service", "sshd.socket", "boat-bootmode-guard.service"]),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1)),
    case("commit: boat.stay=1 marks a failed trial bad but does not reboot",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1 boat.stay=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), active=[]),
         1, want_env=env_with(ORDER="B A", B_OK=0, B_TRY=0, B_COMMITTED=0),
         not_log=["systemctl reboot"], says=["staying up"]),
    case("commit: a trial booted from the wrong partition fails Tier 1",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0),
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp1 / ext4 rw 0 0" % DISK,
                      "%sp18 /data ext4 rw 0 0" % DISK]),
         1, log=["systemctl reboot"], says=["not slot B's"]),
    case("commit: a read-only root fails Tier 1",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0),
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp2 / ext4 ro,noatime 0 0" % DISK,
                      "%sp18 /data ext4 rw 0 0" % DISK]),
         1, log=["systemctl reboot"], says=["not read-write"]),
    case("commit: a committed slot gets its attempts reset",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(A_TRY=1)),
         0, want_env=env_with(A_TRY=0), not_log=["mark-bad", "systemctl reboot"]),
    case("commit: a committed slot failing Tier 1 is only an alert - attempts still reset",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(A_TRY=1), rauc="broken"),
         1, want_env=env_with(A_TRY=0), not_log=["mark-bad", "systemctl reboot"],
         says=["committed but unhealthy"],
         files={"run/boat-ab/health-alert": "rauc status"}),
    case("commit: committed through grubenv alone when RAUC is not installed",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), rauc=None),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1)),
    case("commit: nothing to do outside GRUB",
         "boat-commit.sh",
         dict(cmdline="root=/dev/nvme0n1p1", env=env_with()),
         0, unchanged=True, says=["outside GRUB"]),
    case("deadline: an uncommitted trial is failed and rebooted",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         1, want_env=env_with(ORDER="B A", B_OK=0, B_TRY=0, B_COMMITTED=0),
         log=["systemctl reboot"], says=["trial deadline"], args=["--deadline"]),
    case("deadline: a trial that has committed is left alone",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1)),
         0, unchanged=True, not_log=["systemctl reboot"], args=["--deadline"]),
    # --- RAUC pre-install handler -----------------------------------------
    # RAUC can still refuse the bundle after this handler, so it may only
    # mark the target not committed - never unusable.
    case("pre-install: marks the target not committed - and only that - in both copies",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         0, want_env=env_with(B_COMMITTED=0),
         want_bak_equal=True, says=["installing 1.1.0 into slot\\(s\\) B"],
         extra_env=RAUC_TARGET_B),
    case("pre-install: refuses during a trial",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A boat.trial=1", env=env_with(A_COMMITTED=0)),
         1, unchanged=True, says=["on trial"], extra_env=RAUC_TARGET_B),
    case("pre-install: refuses outside GRUB",
         "boat-rauc-pre-install.sh",
         dict(cmdline="root=/dev/nvme0n1p1", env=env_with()),
         1, unchanged=True, says=["did not come through GRUB"],
         extra_env=RAUC_TARGET_B),
    case("pre-install: refuses without /data on DATA",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK]),
         1, unchanged=True, says=["/data is not mounted"],
         extra_env=RAUC_TARGET_B),
    case("pre-install: refuses another L4T release",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         1, unchanged=True, says=["USB reflash"],
         extra_env=dict(RAUC_TARGET_B, RAUC_META_BOAT_L4T="36.4.0")),
    case("pre-install: refuses a bundle without [meta.boat]",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         1, unchanged=True, says=["no \\[meta.boat\\]"],
         extra_env=dict(RAUC_TARGET_B, RAUC_META_BOAT_L4T="")),
    case("pre-install: refuses to write the running slot",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=B", env=env_with(ORDER="B A")),
         1, unchanged=True, says=["the one running"], extra_env=RAUC_TARGET_B),
    case("pre-install: refuses a disk that does not match the layout",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              parts=dict(PARTS, **{"2": "APP_b_old"})),
         1, unchanged=True, says=["does not match"], extra_env=RAUC_TARGET_B),
    case("pre-install: refuses a target slot RAUC would write elsewhere",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         1, unchanged=True, says=["configured as %sp3" % DISK],
         extra_env=dict(RAUC_TARGET_B, RAUC_SLOT_DEVICE_2=DISK + "p3")),
    case("pre-install: warns, but goes on, for a new Docker major version",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         0, want_env=env_with(B_COMMITTED=0),
         says=["Docker 24 -> 25"],
         extra_env=dict(RAUC_TARGET_B, RAUC_META_BOAT_DOCKER="25.0.1")),
    # --- RAUC post-install handler ----------------------------------------
    case("post-install: grubenv.bak follows RAUC's activation; history written",
         "boat-rauc-post-install.sh",
         dict(cmdline="rauc.slot=A",
              env=env_with(ORDER="B A", B_OK=1, B_TRY=0, B_COMMITTED=0),
              bak=env_with(B_OK=0, B_COMMITTED=0)),
         0, want_env=env_with(ORDER="B A", B_OK=1, B_TRY=0, B_COMMITTED=0),
         want_bak_equal=True, says=["installed 1.1.0 into slot\\(s\\) B - it boots as a trial"],
         extra_env=RAUC_TARGET_B,
         files={"data/boat-update/history": r"^\S+ installed 1\.1\.0 into slot\(s\) B, from slot A$"}),
]


def check(c, root):
    import re
    board = Board(root, **c["board"])
    before = board.raw() if os.path.exists(board.grubenv) else None
    bak_path = board.grubenv + ".bak"
    bak_before = board.raw(bak_path) if os.path.exists(bak_path) else None
    rc, out = board.run(c["script"], c["extra_env"], c["args"])
    errors = []
    if rc != c["expect_rc"]:
        errors.append("exit %d, expected %d" % (rc, c["expect_rc"]))
    for pattern in c["says"]:
        if not re.search(pattern, out):
            errors.append("output never matched %r" % pattern)
    log = board.read("log")
    for entry in c["log"]:
        if entry not in log:
            errors.append("never ran %r" % entry)
    for entry in c["not_log"]:
        if entry in log:
            errors.append("ran %r, which it must not" % entry)
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
    if c["want_bak_equal"] and (not os.path.exists(bak_path) or not os.path.exists(board.grubenv)
                                or board.raw(bak_path) != board.raw()):
        errors.append("grubenv.bak is not a copy of grubenv")
    for rel in c["absent"]:
        if os.path.exists(os.path.join(root, rel)):
            errors.append("%s was written, and must not have been" % rel)
    for rel, pattern in c["files"].items():
        path = os.path.join(root, rel)
        text = slurp(path) if os.path.exists(path) else None
        if text is None or not re.search(pattern, text, re.M):
            errors.append("%s does not match %r: %r" % (rel, pattern, text))
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
