#!/usr/bin/env python3
"""Run the A/B layout's on-board shell scripts against a fake board.

    scripts/test-ab-scripts.py        # needs grub-editenv (grub-common)

boat-bootmode-guard, boat-grubenv-guard, boat-commit, the grub-editenv
wrapper RAUC runs and RAUC's pre/post-install handlers decide, on a board
nobody is watching, which slot stays bootable and what GRUB will try next. shellcheck only proves they parse. This runs each one,
as shipped, against a scratch directory standing in for the board - its
/proc/cmdline, /proc/mounts, the ESP with a real grubenv written by the real
grub-editenv, the GPT names blkid reports - and checks what it writes back
and what it refuses.

Only paths are changed (/proc, /run, /boot/efi, /etc, /data and efivarfs
are moved into the scratch directory) and a few commands are stubbed: blkid
(the GPT names), mountpoint, docker, systemctl (unit states and the
system's; stop and reboot are logged), efivar (writes the fake efivarfs)
and rauc (status, and mark-good/mark-bad writing grubenv the way RAUC's grub
backend does). Everything else is the script the image ships. The A/B lock
can be held by the test while a script runs, to see that it waits.

Exit status: 0 = all passed, 1 = a case failed, 77 = grub-editenv missing
(scripts/lint.sh reports that as a skip).
"""
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAYER = os.path.join(HERE, "..", "layers", "meta-boat")
AB_FILES = os.path.join(LAYER, "recipes-boat", "ab-boot", "files")
RAUC_FILES = os.path.join(LAYER, "external", "rauc", "recipes-boat",
                          "boat-rauc", "files")

DISK = "/dev/nvme0n1"
PARTS = {"1": "APP", "2": "APP_b", "12": "esp", "17": "SWAP", "18": "DATA"}
NV_RELEASE = "# R35 (release), REVISION: 6.4, GCID: 0, BOARD: t186ref, EABI: aarch64, DATE: x\n"
NV_GUID = "781e084c-a330-417c-b678-38e696380cb9"
# How long the scripts wait for the A/B lock here (60 s on the board).
LOCK_WAIT = 3

# A process id that no longer runs: a finished child's.
_dead = subprocess.Popen(["true"])
_dead.wait()
DEAD_PID = _dead.pid

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
                 late=(), editenv_fails=False, committed_mark=False,
                 failed_mark=False, system_state="running", efivars=None,
                 efi_readonly=(), manifest=None, sysconf=None, expect=None,
                 boat_update=None, setup=None):
        """efivars: {name: bytes} in the fake efivarfs (attributes added).
        efi_readonly: variables efivar fails to write. manifest/sysconf:
        the bundle's [update] and system.conf's [system] keys. expect:
        (release, pid) in boat-update's /run/boat-update/expect.
        boat_update: "ok" or "broken" - a boat-update that starts, or not
        (None: there is none). setup: called with the board once it is
        laid out, to add to it."""
        self.root = root
        for d in ("bin", "proc", "run", "etc", "data", "boot/efi/EFI/BOOT",
                  "efivars", "bundle", "etc/rauc"):
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
        os.makedirs(os.path.join(root, "run/boat-ab"), exist_ok=True)
        if committed_mark:
            # boat-commit's own record that this boot's commit succeeded.
            self.write("run/boat-ab/committed", "")
        if failed_mark:
            # ... and that this boot's trial failed.
            self.write("run/boat-ab/trial-failed", "")
        self.write("system-state", system_state + "\n")
        if expect is not None:
            os.makedirs(os.path.join(root, "run/boat-update"), exist_ok=True)
            self.write("run/boat-update/expect", "%s %d\n" % expect)
        self.boat_update = boat_update
        for name, value in (efivars or {}).items():
            with open(os.path.join(root, "efivars", "%s-%s" % (name, NV_GUID)), "wb") as f:
                f.write(b"\x07\x00\x00\x00" + value)
        self.write("efi-readonly", "".join(n + "\n" for n in efi_readonly))
        update = dict(compatible="boat-xavier-nx", version="1.1.0")
        update.update(manifest or {})
        self.write("bundle/manifest.raucm", "[update]\n" + "".join(
            "%s=%s\n" % kv for kv in update.items()) + "\n[image.rootfs]\nfilename=rootfs.ext4\n")
        system = dict(compatible="boat-xavier-nx", bootloader="grub")
        system.update(sysconf or {})
        self.write("etc/rauc/system.conf", "[system]\n" + "".join(
            "%s=%s\n" % kv for kv in system.items()))
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
        if setup:
            setup(self)

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
# A case's own answers, if it has any (it exits when it answers).
[ -e %(r)s/systemctl-hook ] && . %(r)s/systemctl-hook
case "$1" in
    is-active)
        if grep -qx "$3" %(r)s/late && [ ! -e "%(r)s/asked-$3" ]; then
            : > "%(r)s/asked-$3"
            exit 3
        fi
        grep -qx "$3" %(r)s/active || exit 3 ;;
    is-failed) grep -qx "$3" %(r)s/failed ;;
    is-system-running) cat %(r)s/system-state ;;
    cat) [ "$2" = docker.service ] ;;
    *) exit 0 ;;
esac
""" % dict(r=r))
        self.write("bin/efivar", """#!/bin/sh
# efivar -w -n GUID-NAME -f FILE: the data, after efivarfs's attribute word
echo "efivar $*" >> %(r)s/log
while [ "$#" -gt 0 ]; do
    case "$1" in -n) name="$2"; shift ;; -f) file="$2"; shift ;; esac
    shift
done
var="${name#*-*-*-*-*-}"
grep -qx "$var" %(r)s/efi-readonly && exit 1
{ printf '\\007\\000\\000\\000'; cat "$file"; } > "%(r)s/efivars/$var-${name%%-$var}"
""" % dict(r=r))
        names = ["blkid", "mountpoint", "docker", "systemctl", "efivar"]
        if self.boat_update is not None:
            self.write("bin/boat-update", "#!/bin/sh\nexit %d\n" % (self.boat_update != "ok"))
            names.append("boat-update")
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
                         ("EFIVARS=/sys/firmware/efi/efivars",
                          "EFIVARS=%s/efivars" % r),
                         ("BOAT_AB_LOCK_WAIT=60", "BOAT_AB_LOCK_WAIT=%d" % LOCK_WAIT),
                         ("/proc/cmdline", r + "/proc/cmdline")):
            lib = lib.replace(old, new)
        assert "@" not in lib.replace("$@", ""), "lib.sh has an unfilled placeholder"
        self.write("lib.sh", lib)
        for src_dir, name in ((AB_FILES, "boat-grubenv-guard.sh"),
                              (AB_FILES, "boat-commit.sh"),
                              (AB_FILES, "boat-bootmode-guard.sh"),
                              (AB_FILES, "grub-editenv-wrapper.sh"),
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
                             ("/data/boat-update", r + "/data/boat-update"),
                             ("/run/boat-update", r + "/run/boat-update")):
                text = text.replace(old, new)
            self.write(name, text)

    def run(self, name, extra_env=None, args=()):
        """extra_env and args may name paths under the fake root as @ROOT@."""
        env = dict(os.environ)
        env["PATH"] = os.path.join(self.root, "bin") + ":" + env["PATH"]
        env["BOAT_AB_REAL_EDITENV"] = shutil.which("grub-editenv")
        env.update({k: v.replace("@ROOT@", self.root) for k, v in (extra_env or {}).items()})
        args = [a.replace("@ROOT@", self.root) for a in args]
        p = subprocess.run(["sh", os.path.join(self.root, name)] + args,
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

    def efi(self, name):
        """A fake UEFI variable's data, or None."""
        path = os.path.join(self.root, "efivars", "%s-%s" % (name, NV_GUID))
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()[4:]

    def hold_lock(self, release_after, env_change):
        """Hold the A/B lock as another writer would; after release_after
        seconds (None: only once the script has finished) apply
        env_change to grubenv and let go. Returns the releasing call."""
        f = open(os.path.join(self.root, "run/boat-ab/commit.lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX)

        def release():
            if env_change:
                subprocess.run(["grub-editenv", self.grubenv, "set"] +
                               ["%s=%s" % kv for kv in env_change.items()], check=True)
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()
        if release_after is None:
            return release
        threading.Timer(release_after, release).start()
        return lambda: None


RAUC_TARGET_B = {"RAUC_BUNDLE_MOUNT_POINT": "@ROOT@/bundle",
                 "RAUC_SYSTEM_CONFIG": "@ROOT@/etc/rauc/system.conf",
                 "RAUC_TARGET_SLOTS": "2", "RAUC_SLOT_BOOTNAME_2": "B",
                 "RAUC_SLOT_DEVICE_2": DISK + "p2",
                 "RAUC_SLOT_BOOTNAME_1": "A", "RAUC_SLOT_DEVICE_1": DISK + "p1",
                 "RAUC_META_BOAT_L4T": "35.6.4",
                 "RAUC_META_BOAT_DOCKER": "24.0.5", "RAUC_META_BOAT_RELEASE": "1.1.0"}


def case(name, script, board, expect_rc, want_env=None, want_bak_equal=None,
         says=(), unchanged=False, extra_env=None, args=(), log=(), not_log=(),
         files=None, absent=(), want_efi=None, lock=None, replaced=False,
         not_says=(), check=None):
    """log/not_log: entries that must / must not be in the stubs' command
    log. files: {path under the fake root: a regex its contents must
    match}. absent: paths under the fake root that must not exist.
    want_efi: {UEFI variable: its data afterwards}. lock: (seconds, grubenv
    change) - the lock is held that long by another writer, which makes
    that change meanwhile (seconds None: held throughout). replaced:
    grubenv must be a new file (written by rename), not rewritten in
    place. not_says: regexes the output must not match. check: called
    with the board afterwards, returns a list of problems."""
    return dict(name=name, script=script, board=board, expect_rc=expect_rc,
                want_env=want_env, want_bak_equal=want_bak_equal, says=says,
                unchanged=unchanged, extra_env=extra_env, args=args, log=log,
                not_log=not_log, files=files or {}, absent=absent,
                want_efi=want_efi or {}, lock=lock, replaced=replaced,
                not_says=not_says, check=check)


CASES = [
    # --- boat-grubenv-guard ---------------------------------------------
    case("guard: committed boot - the attempt count is reset at once, and grubenv.bak follows",
         "boat-grubenv-guard.sh",
         dict(cmdline="root=x rauc.slot=A", env=env_with()),
         0, want_env=env_with(A_TRY=0), want_bak_equal=True, replaced=True,
         says=["slot A booted: its attempt count is reset"]),
    case("guard: valid grubenv with nothing to reset is only copied to grubenv.bak",
         "boat-grubenv-guard.sh",
         dict(cmdline="root=x rauc.slot=A", env=env_with(A_TRY=0)),
         0, want_env=env_with(A_TRY=0), want_bak_equal=True, not_says=["reset"]),
    case("guard: trial boot - its attempt stays counted",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         0, want_env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), want_bak_equal=True),
    case("guard: last resort boot - left to boat-commit",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A boat.lastresort=1",
              env=env_with(A_OK=0, A_TRY=2, B_OK=0)),
         0, want_env=env_with(A_OK=0, A_TRY=2, B_OK=0), want_bak_equal=True),
    case("guard: a reset the ESP refuses is an alert, not a failure",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), editenv_fails=True),
         0, want_env=env_with(), says=["could not reset slot A's attempt count"],
         files={"run/boat-ab/health-alert": "could not reset slot A's attempt count"}),
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
    case("guard: ESP not mounted - touches nothing, and raises an alert",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A", torn=True, mountpoints=[]),
         1, unchanged=True, says=["not mounted"],
         files={"run/boat-ab/health-alert": "boat-grubenv-guard failed"}),
    # --- boat-commit ----------------------------------------------------
    case("commit: a healthy trial is committed in one grubenv write; deadline and inhibitor stopped",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1, BOAT_RETRY=""),
         want_bak_equal=True,
         log=["systemctl stop boat-trial-deadline.timer",
              "systemctl stop boat-trial-inhibit.service"],
         not_log=["mark-good", "systemctl reboot"],
         files={"data/boat-update/last-result": "committed slot B", "run/boat-ab/committed": ""}),
    case("commit: a grubenv that will not take the write is NOT reported committed",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), editenv_fails=True),
         1, unchanged=True, says=["NOT committed"],
         not_log=["boat-trial-inhibit", "boat-trial-deadline"], absent=["run/boat-ab/committed"]),
    case("commit: a unit that comes up late is waited for",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0),
              health_deadline=6, late=["NetworkManager.service"]),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1, BOAT_RETRY=""), not_log=["systemctl reboot"]),
    case("commit: without /data a trial still commits - RAUC (which waits for /data) is not asked",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), rauc="broken",
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp2 / ext4 rw 0 0" % DISK],
              active=["NetworkManager.service", "systemd-logind.service",
                      "dbus.service", "sshd.socket", "boat-bootmode-guard.service"]),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1, BOAT_RETRY=""), not_log=["rauc status"],
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
    case("commit: a trial whose update client does not start fails - it could never be updated again",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), boat_update="broken"),
         1, log=["systemctl reboot"], says=["the update client does not start"]),
    case("commit: a working update client passes",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), boat_update="ok"),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1, BOAT_RETRY="")),
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
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1, BOAT_RETRY="")),
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
         says=["slot A is unhealthy: .rauc status. does not answer - staying on it"],
         files={"run/boat-ab/health-alert": "rauc status"}),
    case("commit: committed through grubenv alone when RAUC is not installed",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), rauc=None),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1, BOAT_RETRY="")),
    case("commit: nothing to do outside GRUB",
         "boat-commit.sh",
         dict(cmdline="root=/dev/nvme0n1p1", env=env_with()),
         0, unchanged=True, says=["outside GRUB"]),
    case("commit: a trial failed meanwhile, while it waited for the lock, is not committed",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         1, want_env=env_with(ORDER="B A", B_OK=0, B_TRY=1, B_COMMITTED=0),
         says=["failed meanwhile"], absent=["run/boat-ab/committed"],
         lock=(1, {"B_OK": "0"})),
    case("commit: a lock never released - nothing committed, nothing written without it",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         1, unchanged=True, says=["could not take .*commit.lock in %d s" % LOCK_WAIT, "NOT committed"],
         not_log=["systemctl reboot"], absent=["run/boat-ab/committed"], lock=(None, None)),
    case("deadline: a lock never released - the trial still ends, by GRUB's attempt count",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         1, unchanged=True, args=["--deadline"], log=["systemctl reboot"],
         says=["not marking slot B bad without the A/B lock"], lock=(None, None)),
    case("guard: a lock never released - nothing written, and an alert",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         1, unchanged=True, files={"run/boat-ab/health-alert": "boat-grubenv-guard failed"},
         lock=(None, None)),
    case("commit: a passed trial ends the debug hold it was booted with",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1 boat.stay=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0, BOAT_STAY=1, BOAT_RETRY="B")),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1, BOAT_STAY=0, BOAT_RETRY="")),
    case("commit: a healthy committed boot ends the debug hold it was booted with",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A boat.stay=1", env=env_with(BOAT_STAY=1)),
         0, want_env=env_with(A_TRY=0, BOAT_STAY=0), says=["debug hold"]),
    case("commit: a debug hold set during this boot lasts until the next one",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(BOAT_STAY=1)),
         0, want_env=env_with(A_TRY=0, BOAT_STAY=1)),
    case("commit: an unhealthy boot does not end the debug hold",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A boat.stay=1", env=env_with(BOAT_STAY=1), rauc="broken"),
         1, want_env=env_with(A_TRY=0, BOAT_STAY=1)),
    case("commit: a committed boot whose reset the ESP refuses is an alert",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), editenv_fails=True),
         1, unchanged=True, says=["could not reset slot A's attempt count"],
         not_log=["systemctl reboot"],
         files={"run/boat-ab/health-alert": "one more failed boot, or two reboots"}),
    case("commit: last resort on a committed slot - usable again, and an alert",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.lastresort=1",
              env=env_with(ORDER="B A", A_OK=0, A_COMMITTED=0, B_OK=0, B_TRY=2)),
         1, want_env=env_with(ORDER="B A", A_OK=0, A_COMMITTED=0, B_OK=1, B_TRY=0),
         not_log=["systemctl reboot", "mark-good"],
         files={"run/boat-ab/health-alert": "last resort .* made usable again",
                "data/boat-update/last-result": "last resort"}),
    case("commit: last resort on a slot that never passed a trial - left as it is, and an alert",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A boat.lastresort=1",
              env=env_with(ORDER="B A", A_OK=0, A_TRY=1, A_COMMITTED=0, B_OK=0, B_TRY=2)),
         1, unchanged=True, not_log=["systemctl reboot", "mark-good"],
         files={"run/boat-ab/health-alert": "has not passed a trial"}),
    case("deadline: an uncommitted trial is failed and rebooted",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         1, want_env=env_with(ORDER="B A", B_OK=0, B_TRY=0, B_COMMITTED=0),
         log=["systemctl reboot"], says=["trial deadline"], args=["--deadline"]),
    case("deadline: a trial that has committed is left alone",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1), committed_mark=True),
         0, unchanged=True, not_log=["systemctl reboot"], args=["--deadline"]),
    case("deadline: COMMITTED=1 from a commit whose write failed part-way is not a commit",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1)),
         1, want_env=env_with(ORDER="B A", B_OK=0, B_TRY=0, B_COMMITTED=1),
         log=["systemctl reboot"], says=["never confirmed"], args=["--deadline"]),
    case("deadline: a trial the gate already failed (boat.stay=1) keeps that verdict",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1 boat.stay=1",
              env=env_with(ORDER="B A", B_OK=0, B_TRY=0, B_COMMITTED=0)),
         0, unchanged=True, not_log=["systemctl reboot"], args=["--deadline"],
         absent=["data/boat-update/failed-versions", "data/boat-update/last-result"]),
    # --- boat-commit --shutdown (boat-trial-inhibit stopping) -------------
    case("shutdown: a clean shutdown before the verdict gives the trial its attempt back",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=0, BOAT_RETRY="B"),
         want_bak_equal=True, args=["--shutdown"], says=["tries it again \\(once\\)"],
         files={"data/boat-update/last-result": "retried on the next boot"}),
    case("shutdown: the second one in the same trial counts",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0, BOAT_RETRY="B")),
         0, unchanged=True, args=["--shutdown"], says=["again - this attempt counts"]),
    case("shutdown: nothing when the system is not stopping (the commit stops the unit)",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)),
         0, unchanged=True, args=["--shutdown"]),
    case("shutdown: nothing after this boot's commit",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), committed_mark=True),
         0, unchanged=True, args=["--shutdown"]),
    case("shutdown: nothing after the trial failed, even if its write did not stick",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping",
              env=env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0), failed_mark=True),
         0, unchanged=True, args=["--shutdown"]),
    case("shutdown: nothing for a slot already marked bad",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping",
              env=env_with(ORDER="B A", B_OK=0, B_TRY=0, B_COMMITTED=0)),
         0, unchanged=True, args=["--shutdown"]),
    case("shutdown: nothing on a committed boot",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", system_state="stopping", env=env_with()),
         0, unchanged=True, args=["--shutdown"]),
    # --- the grub-editenv RAUC runs ----------------------------------------
    case("editenv wrapper: RAUC's set on grubenv is a rename, and grubenv.bak follows",
         "grub-editenv-wrapper.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), bak=env_with()),
         0, want_env=env_with(ORDER="B A", B_OK=1, B_TRY=0), want_bak_equal=True,
         replaced=True,
         args=["@ROOT@/boot/efi/EFI/BOOT/grubenv", "set", "ORDER=B A", "B_OK=1", "B_TRY=0"]),
    case("editenv wrapper: it waits for the A/B lock, and keeps the other writer's change",
         "grub-editenv-wrapper.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         0, want_env=env_with(B_OK=0, B_TRY=0, A_TRY=0),
         args=["@ROOT@/boot/efi/EFI/BOOT/grubenv", "set", "B_OK=0", "B_TRY=0"],
         lock=(1, {"A_TRY": "0"})),
    case("editenv wrapper: a lock never released - RAUC is told, nothing written",
         "grub-editenv-wrapper.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         1, unchanged=True, says=["could not take the A/B lock"],
         args=["@ROOT@/boot/efi/EFI/BOOT/grubenv", "set", "B_OK=0"], lock=(None, None)),
    case("editenv wrapper: a write that fails is reported to RAUC",
         "grub-editenv-wrapper.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), editenv_fails=True),
         1, unchanged=True, says=["could not write"],
         args=["@ROOT@/boot/efi/EFI/BOOT/grubenv", "set", "B_OK=0"]),
    case("editenv wrapper: anything else goes to the real grub-editenv",
         "grub-editenv-wrapper.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         0, unchanged=True, says=["^ORDER=A B$"],
         args=["@ROOT@/boot/efi/EFI/BOOT/grubenv", "list"]),
    case("editenv wrapper: a set on another file goes to the real grub-editenv",
         "grub-editenv-wrapper.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         0, unchanged=True, args=["@ROOT@/other.env", "create"],
         files={"other.env": "GRUB Environment Block"}),
    # --- boat-bootmode-guard ----------------------------------------------
    case("bootmode-guard: L4TDefaultBootMode and NewDeviceHierarchy are set to 0",
         "boat-bootmode-guard.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              efivars={"L4TDefaultBootMode": b"\x01\x00\x00\x00", "NewDeviceHierarchy": b"\x01"}),
         0, want_efi={"L4TDefaultBootMode": b"\x00\x00\x00\x00", "NewDeviceHierarchy": b"\x00"},
         files={"run/boat-ab/boot-path": "^grub$"},
         says=["L4TDefaultBootMode is now 0", "NewDeviceHierarchy is now 0"]),
    case("bootmode-guard: both already 0 - nothing written",
         "boat-bootmode-guard.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              efivars={"L4TDefaultBootMode": b"\x00\x00\x00\x00", "NewDeviceHierarchy": b"\x00"}),
         0, not_log=["efivar"]),
    case("bootmode-guard: a NewDeviceHierarchy that will not change is only a warning",
         "boat-bootmode-guard.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), efi_readonly=["NewDeviceHierarchy"],
              efivars={"L4TDefaultBootMode": b"\x00\x00\x00\x00", "NewDeviceHierarchy": b"\x01"}),
         0, want_efi={"NewDeviceHierarchy": b"\x01"},
         says=["could not set NewDeviceHierarchy"]),
    case("bootmode-guard: an L4TDefaultBootMode that will not change fails the unit",
         "boat-bootmode-guard.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), efi_readonly=["L4TDefaultBootMode"],
              efivars={"L4TDefaultBootMode": b"\x01\x00\x00\x00", "NewDeviceHierarchy": b"\x00"}),
         1, says=["cannot fix L4TDefaultBootMode"]),
    case("bootmode-guard: a boot from outside GRUB is recorded",
         "boat-bootmode-guard.sh",
         dict(cmdline="root=/dev/nvme0n1p1", env=env_with(),
              efivars={"L4TDefaultBootMode": b"\x00\x00\x00\x00"}),
         0, files={"run/boat-ab/boot-path": "^outside-grub$"}, says=["did not come through GRUB"]),
    # --- RAUC pre-install handler -----------------------------------------
    # RAUC can still refuse the bundle after this handler, so it may only
    # mark the target not committed - never unusable.
    case("pre-install: marks the target not committed - and only that - in both copies",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         0, want_env=env_with(B_COMMITTED=0, BOAT_RETRY=""),
         want_bak_equal=True, says=["installing 1.1.0 into slot\\(s\\) B"],
         extra_env=RAUC_TARGET_B),
    case("pre-install: refuses on a trial boot before boat-commit has committed it",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A boat.trial=1", env=env_with()),
         1, unchanged=True, says=["on trial"], extra_env=RAUC_TARGET_B),
    case("pre-install: the boot that committed its trial installs (no reboot needed)",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A boat.trial=1", env=env_with(), committed_mark=True),
         0, want_env=env_with(B_COMMITTED=0, BOAT_RETRY=""), extra_env=RAUC_TARGET_B),
    case("pre-install: GRUB's last-resort boot of a never-committed slot may install - it mends the board",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A boat.lastresort=1", env=env_with(A_OK=0, A_COMMITTED=0, B_OK=0)),
         0, want_env=env_with(A_OK=0, A_COMMITTED=0, B_OK=0, B_COMMITTED=0, BOAT_RETRY=""),
         extra_env=RAUC_TARGET_B),
    case("pre-install: refuses another release than the one boat-update is installing",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), expect=("1.2.0", os.getpid())),
         1, unchanged=True, says=["release '1.1.0', but boat-update is installing 1.2.0"],
         extra_env=RAUC_TARGET_B),
    case("pre-install: the release boat-update is installing goes ahead",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), expect=("1.1.0", os.getpid())),
         0, want_env=env_with(B_COMMITTED=0, BOAT_RETRY=""), extra_env=RAUC_TARGET_B),
    case("pre-install: a left-over expect file (its boat-update is gone) means nothing",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), expect=("1.2.0", DEAD_PID)),
         0, want_env=env_with(B_COMMITTED=0, BOAT_RETRY=""), extra_env=RAUC_TARGET_B),
    case("pre-install: a new install gets a fresh shutdown retry",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(BOAT_RETRY="B")),
         0, want_env=env_with(B_COMMITTED=0, BOAT_RETRY=""), extra_env=RAUC_TARGET_B),
    case("pre-install: refuses a bundle for another compatible before demoting anything",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              manifest=dict(compatible="boat-xavier-nx-bench")),
         1, unchanged=True, says=["is for 'boat-xavier-nx-bench'.*RAUC would refuse it"],
         extra_env=RAUC_TARGET_B),
    case("pre-install: refuses a bundle below min-bundle-version before demoting anything",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              manifest=dict(version="1.1.0"), sysconf={"min-bundle-version": "1.2.0-rc.1"}),
         1, unchanged=True, says=["below this board's min-bundle-version 1.2.0-rc.1"],
         extra_env=RAUC_TARGET_B),
    case("pre-install: a bundle at min-bundle-version installs",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              manifest=dict(version="1.2.0"), sysconf={"min-bundle-version": "1.2.0-rc.1"}),
         0, want_env=env_with(B_COMMITTED=0, BOAT_RETRY=""), extra_env=RAUC_TARGET_B),
    case("pre-install: refuses when the bundle's manifest cannot be read",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         1, unchanged=True, says=["cannot read the bundle's manifest"],
         extra_env=dict(RAUC_TARGET_B, RAUC_BUNDLE_MOUNT_POINT="@ROOT@/nothing")),
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
         0, want_env=env_with(B_COMMITTED=0, BOAT_RETRY=""),
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


# --- what GRUB cannot write down itself, and the ESP's own faults ---------

def bak_copy_fails(board):
    """An ESP that takes grubenv's write, then fails grubenv.bak's copy."""
    board.write("bin/cp", """#!/bin/sh
for last; do :; done
case "$last" in *.bak.new) exit 1 ;; esac
exec %s "$@"
""" % shutil.which("cp"))
    os.chmod(os.path.join(board.root, "bin", "cp"), 0o755)


CASES += [
    case("guard: torn grubenv on a last-resort boot - put on trial, never made committed",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=B boat.lastresort=1 boat.esp=1", torn=True,
              bak=env_with(A_OK=0, A_TRY=2, B_OK=0, B_COMMITTED=0)),
         0, want_env=dict(ORDER="B A", B_OK="1", B_TRY="0", B_COMMITTED="0",
                          A_OK="0", A_TRY="0", A_COMMITTED="0", BOAT_STAY="0"),
         want_bak_equal=True, says=["last resort: on trial from the next boot"]),
    case("guard: hand-over boot - the failed slot GRUB could not mark is marked, with an alert",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=B boat.handover=1 boat.esp=1", env=env_with(B_TRY=1)),
         0, want_env=env_with(A_OK=0, B_TRY=0), want_bak_equal=True,
         says=["slot A failed to load in GRUB: it is marked unusable"],
         files={"run/boat-ab/health-alert": "GRUB could not load slot A"}),
    case("guard: hand-over boot over a torn grubenv - booted slot committed, the failed one unusable",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A boat.handover=1 boat.esp=1", torn=True,
              bak=env_with(ORDER="B A")),
         0, want_env=dict(ORDER="A B", A_OK="1", A_TRY="0", A_COMMITTED="1",
                          B_OK="0", B_TRY="0", B_COMMITTED="0", BOAT_STAY="0"),
         want_bak_equal=True,
         files={"run/boat-ab/health-alert": "GRUB could not load slot B"}),
    case("guard: a hand-over mark the ESP refuses is an alert",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=B boat.handover=1 boat.esp=1", env=env_with(B_TRY=0),
              editenv_fails=True),
         0, want_env=env_with(B_TRY=0),
         files={"run/boat-ab/health-alert": "could not mark slot A unusable"}),
    case("guard: an ESP older than this release expects is an alert",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A", env=env_with(A_TRY=0)),
         0, want_env=env_with(A_TRY=0),
         files={"run/boat-ab/health-alert": "boot files on the ESP are version 0, and this release expects 1"}),
    case("guard: the ESP this release expects - no alert",
         "boat-grubenv-guard.sh",
         dict(cmdline="rauc.slot=A boat.esp=1", env=env_with(A_TRY=0)),
         0, want_env=env_with(A_TRY=0), absent=["run/boat-ab/health-alert"]),
    dict(case("editenv wrapper: grubenv written but its .bak copy fails - RAUC is told it worked, and an alert",
              "grub-editenv-wrapper.sh",
              dict(cmdline="rauc.slot=A", env=env_with(), bak=env_with()),
              0, want_env=env_with(B_OK=0),
              args=["@ROOT@/boot/efi/EFI/BOOT/grubenv", "set", "B_OK=0"],
              files={"run/boat-ab/health-alert": "could not copy grubenv to grubenv.bak",
                     "boot/efi/EFI/BOOT/grubenv.bak": "^B_OK=1$"}),
         setup=bak_copy_fails),
    case("bootmode-guard: outside GRUB with L4TDefaultBootMode already 0 - GRUB did not load: an alert",
         "boat-bootmode-guard.sh",
         dict(cmdline="root=/dev/nvme0n1p1", env=env_with(),
              efivars={"L4TDefaultBootMode": b"\x00\x00\x00\x00", "NewDeviceHierarchy": b"\x00"}),
         0, not_log=["efivar"], files={"run/boat-ab/boot-path": "^outside-grub$",
                                       "run/boat-ab/health-alert": "GRUB itself did not load.*reflash"}),
    case("bootmode-guard: outside GRUB after a UEFI reset - the setting is fixed, no ESP alert",
         "boat-bootmode-guard.sh",
         dict(cmdline="root=/dev/nvme0n1p1", env=env_with(),
              efivars={"L4TDefaultBootMode": b"\x01\x00\x00\x00", "NewDeviceHierarchy": b"\x00"}),
         0, want_efi={"L4TDefaultBootMode": b"\x00\x00\x00\x00"},
         absent=["run/boat-ab/health-alert"]),
]


# --- the health gate, trials, sleep and the wiring around them -------------
POWER_FILES = os.path.join(LAYER, "recipes-boat", "power", "files")
FIREFOX_FILES = os.path.join(LAYER, "recipes-boat", "firefox", "files")
AB_RECIPE = os.path.join(LAYER, "recipes-boat", "ab-boot", "boat-ab-boot.bb")
RAUC_CORE = os.path.join(LAYER, "external", "rauc", "recipes-core", "rauc")

# A trial of slot B, booted from ORDER="B A".
ON_TRIAL = env_with(ORDER="B A", B_TRY=1, B_COMMITTED=0)
TRIAL_PASSED = env_with(ORDER="B A", B_TRY=0, B_COMMITTED=1, BOAT_RETRY="")
TRIAL_FAILED = env_with(ORDER="B A", B_OK=0, B_TRY=0, B_COMMITTED=0)
# RAUC installing into slot A, from a board running slot B.
RAUC_TARGET_A = dict(RAUC_TARGET_B, RAUC_TARGET_SLOTS="1")


def executable(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


def both(*setups):
    def setup(b):
        for s in setups:
            s(b)
    return setup


def add_conf(text):
    """setup: lines added to health.conf (@ROOT@: the fake root)."""
    def setup(b):
        with open(os.path.join(b.root, "etc/boat-update/health.conf"), "a") as f:
            f.write(text.replace("@ROOT@", b.root))
    return setup


def health_d(checks, conf=""):
    """setup: health.d turned on, holding {name: shell script body}."""
    def setup(b):
        d = os.path.join(b.root, "data/boat-update/health.d")
        os.makedirs(d)
        for name, body in checks.items():
            executable(os.path.join(d, name), "#!/bin/sh\n" + body.replace("@ROOT@", b.root))
        add_conf("BOAT_HEALTH_CHECKS_D=1\nBOAT_HEALTH_CHECKS_DIR=@ROOT@/data/boat-update/health.d\n"
                 + conf)(b)
    return setup


def hook(text):
    """setup: the systemctl stub runs this first; it answers by exiting."""
    def setup(b):
        b.write("systemctl-hook", text.replace("@ROOT@", b.root))
    return setup


def put(rel, text, mode=0o644):
    def setup(b):
        path = os.path.join(b.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)
    return setup


def asleep_once(b):
    """setup: the first `sleep` the gate runs lasts 1000 s of /proc/uptime -
    the board suspended in between."""
    executable(os.path.join(b.root, "bin/sleep"), """#!/bin/sh
if [ ! -e %(r)s/slept ]; then
    : > %(r)s/slept
    echo "1105.00 1105.00" > %(r)s/proc/uptime
fi
""" % dict(r=b.root))


def mode_is(rel, want):
    def check(b):
        got = os.stat(os.path.join(b.root, rel)).st_mode & 0o777
        return [] if got == want else ["%s is mode %o, expected %o" % (rel, got, want)]
    return check


def lines_in(rel, *patterns):
    """check: each regex matches a line of the file, in this order."""
    def check(b):
        import re
        lines = b.read(rel).splitlines()
        i = 0
        for p in patterns:
            while i < len(lines) and not re.search(p, lines[i]):
                i += 1
            if i == len(lines):
                return ["%s has no line matching %r after the ones before: %r" % (rel, p, lines)]
            i += 1
        return []
    return check


def install_sleep(b, inhibitors="a(ssssuu) 0"):
    """boat-sleep, with /sys, its config and boat-wol-arm moved into the
    fake root, and stubs for id, ethtool and busctl."""
    r = b.root
    for d in ("sys/power", "sys/class/net/eth0", "etc/default"):
        os.makedirs(os.path.join(r, d), exist_ok=True)
    b.write("sys/power/state", "freeze mem\n")
    b.write("sys/power/mem_sleep", "s2idle [deep]\n")
    b.write("sys/class/net/eth0/address", "00:04:4b:00:00:01\n")
    b.write("inhibitors", inhibitors + "\n")
    executable(os.path.join(r, "bin/id"), "#!/bin/sh\necho 0\n")
    executable(os.path.join(r, "bin/ethtool"), "#!/bin/sh\nprintf '\\tWake-on: g\\n'\n")
    executable(os.path.join(r, "bin/boat-wol-arm"), "#!/bin/sh\nexit 0\n")
    executable(os.path.join(r, "bin/busctl"), """#!/bin/sh
echo "busctl $*" >> %(r)s/log
case "$*" in
    *ListInhibitors*) cat %(r)s/inhibitors ;;
esac
""" % dict(r=r))
    text = slurp(os.path.join(POWER_FILES, "boat-sleep.sh"))
    for old, new in (("/etc/default/boat-power", r + "/etc/default/boat-power"),
                     ("/sys/power/", r + "/sys/power/"),
                     ("/sys/class/net/", r + "/sys/class/net/"),
                     ("/usr/bin/boat-wol-arm", r + "/bin/boat-wol-arm"),
                     ("/proc/cmdline", r + "/proc/cmdline"),
                     ("/run/boat-ab/committed", r + "/run/boat-ab/committed")):
        text = text.replace(old, new)
    b.write("boat-sleep.sh", text)


def sleep_with(inhibitors):
    return lambda b: install_sleep(b, inhibitors)


LOCK_HELD = 'a(ssssuu) 1 "sleep" "xfce4-power-manager" "user activity" "block" 2000 1234'


def install_firefox(installed=True, data=True):
    """boat-install-firefox on an A/B board (its lib.sh is there), with
    /data, /opt and the launcher's paths moved into the fake root. curl
    names 150.0 as the latest; installed: Firefox 150.0 is on /data. data:
    /data is mounted."""
    def setup(b):
        r = b.root
        executable(os.path.join(r, "bin/id"), "#!/bin/sh\necho 0\n")
        executable(os.path.join(r, "bin/curl"), "#!/bin/sh\necho 'https://download-installer.cdn.mozilla.net"
                   "/pub/firefox/releases/150.0/linux-aarch64/en-US/firefox-150.0.tar.xz'\n")
        if installed:
            os.makedirs(os.path.join(r, "data/opt/firefox"))
            executable(os.path.join(r, "data/opt/firefox/firefox"), "#!/bin/sh\n")
            b.write("data/opt/firefox/application.ini", "[App]\nVersion=150.0\n")
        mounts = ["%sp2 / ext4 rw 0 0" % DISK]
        if data:
            mounts.append("%sp18 %s/data ext4 rw 0 0" % (DISK, r))
        b.write("proc/mounts", "".join(m + "\n" for m in mounts))
        text = slurp(os.path.join(FIREFOX_FILES, "boat-install-firefox.sh"))
        for old, new in (("AB_LIB=/usr/lib/boat-ab/lib.sh", "AB_LIB=%s/lib.sh" % r),
                         ("AB_DATA=/data", "AB_DATA=%s/data" % r),
                         ("PREFIX=/opt/firefox", "PREFIX=%s/opt/firefox" % r),
                         ("BINLINK=/usr/local/bin/firefox", "BINLINK=%s/usr/local/bin/firefox" % r),
                         ("DESKTOP=/usr/share/applications/firefox.desktop",
                          "DESKTOP=%s/usr/share/applications/firefox.desktop" % r),
                         ("/proc/mounts", r + "/proc/mounts")):
            text = text.replace(old, new)
        b.write("boat-install-firefox.sh", text)
    return setup


def firefox_linked(b):
    r = b.root
    errors = []
    link = os.path.join(r, "usr/local/bin/firefox")
    if not os.path.islink(link) or os.readlink(link) != r + "/data/opt/firefox/firefox":
        errors.append("the launcher does not point at the Firefox on /data")
    desktop = b.read("usr/share/applications/firefox.desktop")
    if "Exec=%s/data/opt/firefox/firefox %%u" % r not in desktop:
        errors.append("the menu entry does not start the Firefox on /data: %r" % desktop)
    return errors


TRIAL_CASES = [
    # --- the A/B lock: root's alone ---------------------------------------
    case("lock: the A/B lock file is root's alone (0600) - nobody else can hold it",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(A_TRY=1)),
         0, want_env=env_with(A_TRY=0), check=mode_is("run/boat-ab/commit.lock", 0o600)),
    case("lock: a lock file other users could open is closed up before it is used",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(A_TRY=1),
              setup=put("run/boat-ab/commit.lock", "", 0o644)),
         0, want_env=env_with(A_TRY=0), check=mode_is("run/boat-ab/commit.lock", 0o600)),
    # --- verdicts in history, not only in last-result ---------------------
    case("history: a commit is appended to history, after what was there",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=put("data/boat-update/history", "2026-01-01T00:00:00Z installed 1.1.0 into slot(s) B, from slot A\n")),
         0, want_env=TRIAL_PASSED,
         check=lines_in("data/boat-update/history", "installed 1.1.0", r"^\S+ committed slot B \(1\.1\.0\)$")),
    case("history: a failed trial is appended to history, with the check that failed",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              active=["systemd-logind.service", "dbus.service", "sshd.socket",
                      "boat-bootmode-guard.service", "docker.service"]),
         1, files={"data/boat-update/history": r"FAILED: NetworkManager\.service is not active$"}),
    case("history: a shutdown retry is appended to history",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping", env=ON_TRIAL),
         0, args=["--shutdown"], files={"data/boat-update/history": "retried on the next boot"}),
    # --- a failure the board can cause too says how to try again -----------
    case("commit: a commit the ESP will not take leaves why, for the deadline",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL, editenv_fails=True),
         1, unchanged=True, files={"run/boat-ab/commit-failed": "could not write grubenv"}),
    case("deadline: a trial that passed but could not commit is failed - as a possible board fault",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=put("run/boat-ab/commit-failed", "could not write grubenv\n")),
         1, want_env=TRIAL_FAILED, args=["--deadline"], log=["systemctl reboot"],
         files={"data/boat-update/last-result":
                r"its commit was not written \(could not write grubenv\).*boat-update install <bundle file>",
                "data/boat-update/failed-versions": "1.1.0"}),
    case("commit: a Tier 1 failure's record says how to install the release again",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp2 / ext4 ro 0 0" % DISK,
                      "%sp18 /data ext4 rw 0 0" % DISK]),
         1, files={"data/boat-update/last-result": "not read-write - if the board, not the release"}),
    # --- a rollback during a trial is a verdict ---------------------------
    case("shutdown: a rollback off an undecided trial marks it bad - no attempt back, not offered again",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping",
              env=env_with(ORDER="A B", B_TRY=1, B_COMMITTED=0)),
         0, want_env=env_with(ORDER="A B", B_OK=0, B_TRY=0, B_COMMITTED=0),
         want_bak_equal=True, args=["--shutdown"], not_says=["tries it again"],
         files={"data/boat-update/failed-versions": "^1.1.0$",
                "data/boat-update/history": "ended by a rollback to slot A",
                "run/boat-ab/trial-failed": ""}),
    case("shutdown: a slot first in ORDER that cannot boot is no rollback - the trial still gets its retry",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping",
              env=env_with(ORDER="A B", A_OK=0, B_TRY=1, B_COMMITTED=0)),
         0, want_env=env_with(ORDER="A B", A_OK=0, B_TRY=0, B_COMMITTED=0, BOAT_RETRY="B"),
         args=["--shutdown"], says=["tries it again \\(once\\)"]),
    # --- health.conf cannot take the deadline, the retry or the gate down ---
    case("deadline: a health.conf that cannot be read does not stop the deadline",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=add_conf('BOAT_HEALTH_FAILED_UNITS="${BOAT_NO_SUCH_SETTING}"\n')),
         1, want_env=TRIAL_FAILED, args=["--deadline"], log=["systemctl reboot"]),
    case("shutdown: a health.conf that cannot be read does not stop the retry",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", system_state="stopping", env=ON_TRIAL,
              setup=add_conf('BOAT_HEALTH_FAILED_UNITS="${BOAT_NO_SUCH_SETTING}"\n')),
         0, want_env=env_with(ORDER="B A", B_TRY=0, B_COMMITTED=0, BOAT_RETRY="B"), args=["--shutdown"]),
    case("commit: a health.conf that cannot be read is an alert, and the gate decides on its defaults",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=add_conf('BOAT_HEALTH_FAILED_UNITS="${BOAT_NO_SUCH_SETTING}"\n')),
         0, want_env=TRIAL_PASSED,
         files={"run/boat-ab/health-alert": "health.conf cannot be read"}),
    # --- health.d -----------------------------------------------------------
    case("health.d: a check that passes lets the trial commit; its output reaches the journal",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=health_d({"10-ok": "echo 'hello from the check'\n"})),
         0, want_env=TRIAL_PASSED, says=["hello from the check"]),
    case("health.d: a check that fails fails the trial, and says how",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=health_d({"10-bad": "echo 'the plotter is not there' >&2\nexit 3\n"})),
         1, want_env=TRIAL_FAILED, log=["systemctl reboot"],
         says=["the plotter is not there", "10-bad failed, exit 3"]),
    case("health.d: a check that ignores SIGTERM is killed, not waited for",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=health_d({"10-stuck": "trap '' TERM\nsleep 20 </dev/null >/dev/null 2>&1\n"
                                          "echo finished > @ROOT@/check-finished\n"},
                             conf="BOAT_HEALTH_CHECK_TIMEOUT=1\n")),
         1, says=["did not finish within 1 s"], absent=["check-finished"]),
    case("health.d: the checks never run past boat-commit.service's own time limit",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=health_d({"10-first": "echo '688.00 688.00' > @ROOT@/proc/uptime\n",
                              "20-slow": "sleep 30 </dev/null >/dev/null 2>&1\n"})),
         1, want_env=TRIAL_FAILED, says=["20-slow did not finish within 2 s"]),
    case("health: a unit listed as needing /data is required when /data is mounted",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              setup=add_conf('BOAT_HEALTH_DATA_UNITS="boat-compose.service"\n')),
         1, says=["boat-compose.service is not active \\(/data is mounted\\)"]),
    case("health: ... and not when /data is not mounted",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp2 / ext4 rw 0 0" % DISK],
              setup=add_conf('BOAT_HEALTH_DATA_UNITS="boat-compose.service"\n')),
         0, want_env=TRIAL_PASSED),
    # --- DATA that is still on its way is waited for ------------------------
    case("commit: a trial waits for a DATA that is still being checked, then asks RAUC",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL, health_deadline=6,
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp2 / ext4 rw 0 0" % DISK],
              setup=hook("""case "$*" in
    "show -p ActiveState --value data.mount")
        # Still mounting when first asked; mounted after that.
        if [ ! -e @ROOT@/data-asked ]; then
            : > @ROOT@/data-asked
            echo "%sp18 /data ext4 rw 0 0" >> @ROOT@/proc/mounts
            echo activating
        else
            echo active
        fi
        exit 0 ;;
esac
""" % DISK)),
         0, want_env=TRIAL_PASSED, log=["rauc status"], says=["waiting for DATA"]),
    case("commit: a DATA still not mounted at the deadline is a data problem - the trial is decided without it",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL, rauc="broken",
              mounts=["%sp12 /boot/efi vfat rw 0 0" % DISK, "%sp2 / ext4 rw 0 0" % DISK],
              setup=hook("""case "$*" in
    "show -p ActiveState --value boat-data-check.service") echo activating; exit 0 ;;
esac
""")),
         0, want_env=TRIAL_PASSED, not_log=["rauc status"], says=["deciding without"]),
    # --- a board that slept --------------------------------------------------
    case("commit: time the board spent asleep is given back to the checks",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL, health_deadline=6,
              setup=both(asleep_once, hook("""if [ "$1" = is-active ] && [ "$3" = NetworkManager.service ]; then
    n=$(cat @ROOT@/nm-asked 2>/dev/null || echo 0)
    echo $((n + 1)) > @ROOT@/nm-asked
    [ "$n" -ge 2 ] || exit 3
fi
"""))),
         0, want_env=TRIAL_PASSED, says=["asleep for about 1000 s"]),
    # --- a connection flood is not the release's fault -----------------------
    case("commit: sshd.socket stopped by a connection flood is started again, not held against the release",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              active=["NetworkManager.service", "systemd-logind.service", "dbus.service",
                      "boat-bootmode-guard.service", "docker.service"],
              setup=hook("""case "$*" in
    "show -p Result --value sshd.socket") echo trigger-limit-hit; exit 0 ;;
    "start sshd.socket") echo sshd.socket >> @ROOT@/active; exit 0 ;;
esac
""")),
         0, want_env=TRIAL_PASSED,
         log=["systemctl reset-failed sshd.socket", "systemctl start sshd.socket"]),
    case("commit: sshd.socket down for any other reason still fails the trial",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL,
              active=["NetworkManager.service", "systemd-logind.service", "dbus.service",
                      "boat-bootmode-guard.service", "docker.service"]),
         1, want_env=TRIAL_FAILED, says=["sshd.socket is not active"],
         not_log=["systemctl start sshd.socket"]),
    # --- boat-commit --may-sleep (before every suspend) ---------------------
    case("may-sleep: refused on a trial not committed yet",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL),
         1, unchanged=True, args=["--may-sleep"], says=["on trial, not committed yet"]),
    case("may-sleep: allowed once this boot's trial has committed",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=TRIAL_PASSED, committed_mark=True),
         0, unchanged=True, args=["--may-sleep"]),
    case("may-sleep: refused while an install runs",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              active=["boat-update-install.service"]),
         1, args=["--may-sleep"], says=["being installed"]),
    case("may-sleep: refused when systemd cannot say whether an install runs",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with(),
              setup=hook('[ "$1" = is-active ] && exit 1\n')),
         1, args=["--may-sleep"], says=["cannot tell"]),
    case("may-sleep: allowed on a committed boot with nothing going on",
         "boat-commit.sh",
         dict(cmdline="rauc.slot=A", env=env_with()),
         0, unchanged=True, args=["--may-sleep"]),
    # --- boat-sleep: the diagnostics ask what the real run asks -------------
    case("boat-sleep: --dry-run refuses on an undecided trial, boat-trial-inhibit running or not",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL, active=[], setup=install_sleep),
         1, args=["--dry-run", "--delay", "0"], says=["on trial and not committed yet"],
         not_says=["checks done"]),
    case("boat-sleep: --status says a sleep now would be refused on an undecided trial",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL, active=[], setup=install_sleep),
         0, args=["--status"], says=["refused now:  an A/B update is on trial"],
         not_says=["suspend now"]),
    case("boat-sleep: a real run, even --force, never suspends an undecided trial",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=ON_TRIAL, active=[], setup=install_sleep),
         1, args=["--force", "--delay", "0"], says=["on trial"],
         not_log=["SuspendWithFlags", "systemctl suspend"]),
    case("boat-sleep: once this boot's trial has committed, it sleeps",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=B boat.trial=1", env=TRIAL_PASSED, committed_mark=True,
              active=[], setup=install_sleep),
         0, args=["--delay", "0"], log=["SuspendWithFlags"]),
    case("boat-sleep: --dry-run refuses while an install runs",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), active=["boat-update-install.service"],
              setup=install_sleep),
         1, args=["--dry-run", "--delay", "0"], says=["being installed"]),
    case("boat-sleep: --status names a sleep inhibitor lock that would refuse",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), active=[], setup=sleep_with(LOCK_HELD)),
         0, args=["--status"],
         says=["refused now:  a sleep inhibitor lock is held", r"xfce4-power-manager \(uid 2000\)"]),
    case("boat-sleep: --dry-run refuses on a held lock",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), active=[], setup=sleep_with(LOCK_HELD)),
         1, args=["--dry-run", "--delay", "0"], says=["a sleep inhibitor lock is held"]),
    case("boat-sleep: --dry-run --force goes past a lock",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), active=[], setup=sleep_with(LOCK_HELD)),
         0, args=["--dry-run", "--force", "--delay", "0"], says=["checks done"]),
    case("boat-sleep: nothing in the way - --status says how to suspend",
         "boat-sleep.sh",
         dict(cmdline="rauc.slot=A", env=env_with(), active=[], setup=install_sleep),
         0, args=["--status"], says=["suspend now:  boat-sleep"], not_says=["refused now"]),
    # --- RAUC's handlers installing into slot A -----------------------------
    case("pre-install: into slot A, from slot B - A is marked not committed, in both copies",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=B", env=env_with(ORDER="B A")),
         0, want_env=env_with(ORDER="B A", A_COMMITTED=0, BOAT_RETRY=""),
         want_bak_equal=True, says=["installing 1.1.0 into slot\\(s\\) A"],
         extra_env=RAUC_TARGET_A),
    case("pre-install: refuses a slot A that RAUC would write elsewhere",
         "boat-rauc-pre-install.sh",
         dict(cmdline="rauc.slot=B", env=env_with(ORDER="B A")),
         1, unchanged=True, says=["slot A is configured as %sp2, expected %sp1" % (DISK, DISK)],
         extra_env=dict(RAUC_TARGET_A, RAUC_SLOT_DEVICE_1=DISK + "p2")),
    case("post-install: activating slot A - grubenv.bak follows; history names it",
         "boat-rauc-post-install.sh",
         dict(cmdline="rauc.slot=B",
              env=env_with(ORDER="A B", A_OK=1, A_TRY=0, A_COMMITTED=0),
              bak=env_with(ORDER="B A", A_COMMITTED=0)),
         0, want_env=env_with(ORDER="A B", A_OK=1, A_TRY=0, A_COMMITTED=0),
         want_bak_equal=True, extra_env=RAUC_TARGET_A,
         files={"data/boat-update/history": r"^\S+ installed 1\.1\.0 into slot\(s\) A, from slot B$"}),
    # --- Firefox on the A/B layout lives on /data ---------------------------
    case("firefox: on the A/B layout it is looked for on /data",
         "boat-install-firefox.sh",
         dict(cmdline="rauc.slot=B", env=env_with(), setup=install_firefox()),
         0, says=["installed: 150.0", "up to date"]),
    case("firefox: --link points a new slot's launcher and menu entry at the Firefox on /data",
         "boat-install-firefox.sh",
         dict(cmdline="rauc.slot=B", env=env_with(), setup=install_firefox()),
         0, args=["--link"], check=firefox_linked),
    case("firefox: --link with nothing installed does nothing",
         "boat-install-firefox.sh",
         dict(cmdline="rauc.slot=B", env=env_with(), setup=install_firefox(installed=False)),
         0, args=["--link"], absent=["usr/local/bin/firefox", "usr/share/applications/firefox.desktop"]),
    case("firefox: --install refuses while /data is not mounted - the slot would lose it at the next update",
         "boat-install-firefox.sh",
         dict(cmdline="rauc.slot=B", env=env_with(), setup=install_firefox(installed=False, data=False)),
         1, args=["--install"], says=["is not mounted - on the A/B layout Firefox is installed there"]),
]
CASES += TRIAL_CASES


def unit_values(path, key):
    """Every value of KEY= in a unit file (or drop-in)."""
    out = []
    for line in slurp(path).splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            out.append(line[len(key) + 1:])
    return out


# What the recipes' install paths expand to on the board.
RECIPE_DIRS = {"${D}": "", "${sbindir}": "/usr/sbin", "${bindir}": "/usr/bin",
               "${libdir}": "/usr/lib", "${nonarch_libdir}": "/usr/lib",
               "${systemd_system_unitdir}": "/lib/systemd/system"}


def installed_as(recipe, source):
    """Where RECIPE's do_install puts ${WORKDIR}/SOURCE on the board."""
    import re
    m = re.search(r"install -m \d+ \$\{WORKDIR\}/%s\s+(?:\\\n\s*)?(\S+)" % re.escape(source), slurp(recipe))
    if not m:
        return None
    path = m.group(1)
    for var, value in RECIPE_DIRS.items():
        path = path.replace(var, value)
    return path


def wiring_shutdown_retry():
    errors = []
    stop = unit_values(os.path.join(AB_FILES, "boat-trial-inhibit.service"), "ExecStop")
    commit = installed_as(AB_RECIPE, "boat-commit.sh")
    if stop != ["%s --shutdown" % commit]:
        errors.append("boat-trial-inhibit.service ExecStop=%r, expected '%s --shutdown'" % (stop, commit))
    start = unit_values(os.path.join(AB_FILES, "boat-trial-deadline.service"), "ExecStart")
    if start != ["%s --deadline" % commit]:
        errors.append("boat-trial-deadline.service ExecStart=%r, expected '%s --deadline'" % (start, commit))
    post = unit_values(os.path.join(AB_FILES, "boat-trial-deadline.service"), "ExecStopPost")
    if not post or "systemctl reboot" not in post[0] or "SERVICE_RESULT" not in post[0]:
        errors.append("boat-trial-deadline.service does not reboot when its own run fails: %r" % post)
    return errors


def wiring_rauc_editenv():
    errors = []
    dropin = os.path.join(RAUC_CORE, "files", "10-boat-ab-rauc.conf")
    wrapper = installed_as(AB_RECIPE, "grub-editenv-wrapper.sh")
    env = [v for v in unit_values(dropin, "Environment") if v.startswith("PATH=")]
    first = env[0][len("PATH="):].split(":")[0] if env else None
    if wrapper is None or first != os.path.dirname(wrapper) or os.path.basename(wrapper) != "grub-editenv":
        errors.append("rauc.service's PATH starts with %r, but boat-ab-boot installs the wrapper as %r"
                      % (first, wrapper))
    append = slurp(os.path.join(RAUC_CORE, "rauc_%.bbappend"))
    if "file://10-boat-ab-rauc.conf" not in append or \
            "${D}${systemd_system_unitdir}/rauc.service.d/10-boat-ab-rauc.conf" not in append:
        errors.append("rauc_%.bbappend does not install 10-boat-ab-rauc.conf as a rauc.service drop-in")
    # RAUC is started on demand: a Requires= on data.mount (RequiresMountsFor)
    # would rerun DATA's check on every `rauc status` once it has failed.
    if unit_values(dropin, "RequiresMountsFor"):
        errors.append("rauc.service still has RequiresMountsFor= (it would restart DATA's check)")
    if unit_values(dropin, "Requisite") != ["data.mount"] or unit_values(dropin, "After") != ["data.mount"]:
        errors.append("rauc.service is not Requisite= and After= data.mount")
    return errors


def wiring_gate_unit():
    errors = []
    unit = os.path.join(AB_FILES, "boat-commit.service")
    conflicts = " ".join(unit_values(unit, "Conflicts")).split()
    for want in ("emergency.service", "emergency.target", "rescue.service", "rescue.target"):
        if want not in conflicts:
            errors.append("boat-commit.service does not conflict with %s" % want)
    for key in ("ConditionPathExists", "ConditionPathIsDirectory", "ConditionFileNotEmpty"):
        if unit_values(unit, key):
            errors.append("boat-commit.service has %s= - a switch the trial deadline does not share" % key)
    return errors


def wiring_sleep():
    import re
    errors = []
    commit = installed_as(AB_RECIPE, "boat-commit.sh")
    pre = unit_values(os.path.join(AB_FILES, "10-boat-ab-sleep.conf"), "ExecStartPre")
    if pre != ["%s --may-sleep" % commit]:
        errors.append("10-boat-ab-sleep.conf ExecStartPre=%r" % pre)
    m = re.search(r"for u in ([^;]*); do\s+install -d \$\{D\}\$\{systemd_system_unitdir\}/\$u\.service\.d\s+"
                  r"install -m 0644 \$\{WORKDIR\}/10-boat-ab-sleep\.conf", slurp(AB_RECIPE))
    units = set(m.group(1).replace("\\", " ").split()) if m else set()
    for want in ("systemd-suspend", "systemd-hibernate", "systemd-hybrid-sleep",
                 "systemd-suspend-then-hibernate"):
        if want not in units:
            errors.append("boat-ab-boot.bb does not install the sleep drop-in for %s.service" % want)
    return errors


def wiring_sshd():
    errors = []
    dropin = os.path.join(AB_FILES, "10-boat-ab-sshd.conf")
    if unit_values(dropin, "TriggerLimitIntervalSec") != ["0"]:
        errors.append("sshd.socket keeps a trigger limit a connection flood can trip")
    if installed_as(AB_RECIPE, "10-boat-ab-sshd.conf") != "/lib/systemd/system/sshd.socket.d/10-boat-ab-sshd.conf":
        errors.append("boat-ab-boot.bb does not install 10-boat-ab-sshd.conf as an sshd.socket drop-in")
    return errors


def wiring_lock_files():
    errors = []
    lines = [l.split() for l in slurp(os.path.join(AB_FILES, "boat-ab-tmpfiles.conf")).splitlines()
             if l.strip() and not l.startswith("#")]
    for path in ("/run/boat-ab/commit.lock", "/run/boat-update.lock"):
        if ["f", path, "0600", "root", "root", "-"] not in lines:
            errors.append("tmpfiles.d does not make %s root's alone" % path)
    if installed_as(AB_RECIPE, "boat-ab-tmpfiles.conf") != "/usr/lib/tmpfiles.d/boat-ab.conf":
        errors.append("boat-ab-boot.bb does not install boat-ab-tmpfiles.conf into tmpfiles.d")
    return errors


def recipe_parse_checks():
    """Run boat-ab-boot.bb's anonymous python against a few configurations."""
    import re
    m = re.search(r"^python \(\) \{\n(.*?)^\}", slurp(AB_RECIPE), re.M | re.S)
    if not m:
        return ["boat-ab-boot.bb has no anonymous python"]

    class Fatal(Exception):
        pass

    class BB:
        @staticmethod
        def fatal(msg):
            raise Fatal(msg)

    class D:
        def __init__(self, values):
            self.values = values

        def getVar(self, name):
            return self.values.get(name)

    scope = {}
    exec("def anon(d, bb):\n" + m.group(1), scope)
    base = dict(BOAT_AB_DISK="/dev/nvme0n1", BOAT_AB_LAYOUT="1",
                BBFILE_COLLECTIONS="core tegra rauc meta-boat", UBOOT_EXTLINUX="1")
    errors = []
    for change, want in ((dict(), None),
                         (dict(UBOOT_EXTLINUX=""), "UBOOT_EXTLINUX"),
                         (dict(BBFILE_COLLECTIONS="core tegra meta-boat"), "meta-rauc"),
                         (dict(BOAT_AB_LAYOUT="0", UBOOT_EXTLINUX="",
                               BBFILE_COLLECTIONS="core tegra meta-boat"), None)):
        try:
            scope["anon"](D(dict(base, **change)), BB)
            got = None
        except Fatal as e:
            got = str(e)
        if want is None and got is not None:
            errors.append("%r: unexpected bb.fatal: %s" % (change, got))
        elif want is not None and (got is None or want not in got):
            errors.append("%r: expected a bb.fatal naming %s, got %r" % (change, want, got))
    return errors


WIRING = [
    ("wiring: boat-trial-inhibit's stop runs boat-commit --shutdown; the deadline runs --deadline, and reboots if that fails",
     wiring_shutdown_retry),
    ("wiring: RAUC finds the grub-editenv wrapper first on its PATH, and waits for /data without restarting its check",
     wiring_rauc_editenv),
    ("wiring: boat-commit.service stops for emergency and rescue mode, and has no switch the deadline lacks",
     wiring_gate_unit),
    ("wiring: every kind of suspend asks boat-commit --may-sleep first", wiring_sleep),
    ("wiring: sshd.socket has no trigger limit a connection flood could trip", wiring_sshd),
    ("wiring: the lock files are root's alone from the start of the boot", wiring_lock_files),
    ("recipe: BOAT_AB_LAYOUT=1 without UBOOT_EXTLINUX=1, or without meta-rauc, fails the parse",
     recipe_parse_checks),
]


def check(c, root):
    import re
    board = Board(root, **c["board"])
    # A case built as dict(case(...), setup=f) changes the board first.
    c.get("setup", lambda b: None)(board)
    before =board.raw() if os.path.exists(board.grubenv) else None
    inode = os.stat(board.grubenv).st_ino if os.path.exists(board.grubenv) else None
    bak_path = board.grubenv + ".bak"
    bak_before = board.raw(bak_path) if os.path.exists(bak_path) else None
    release = board.hold_lock(*c["lock"]) if c["lock"] else (lambda: None)
    started = time.time()
    rc, out = board.run(c["script"], c["extra_env"], c["args"])
    took = time.time() - started
    release()
    errors = []
    if c["lock"] and c["lock"][0] and took < c["lock"][0]:
        errors.append("finished after %.1f s, before the lock was let go" % took)
    for pattern in c["not_says"]:
        if re.search(pattern, out, re.M):
            errors.append("output matched %r, which it must not" % pattern)
    for name, want in c["want_efi"].items():
        got = board.efi(name)
        if got != want:
            errors.append("UEFI %s is %r, expected %r" % (name, got, want))
    if c["replaced"] and inode == os.stat(board.grubenv).st_ino:
        errors.append("grubenv was rewritten in place, not replaced")
    if rc != c["expect_rc"]:
        errors.append("exit %d, expected %d" % (rc, c["expect_rc"]))
    for pattern in c["says"]:
        if not re.search(pattern, out, re.M):
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
    if c["check"]:
        errors.extend(c["check"](board))
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
    # The wiring the scripts depend on: units, drop-ins, install paths.
    for name, fn in WIRING:
        try:
            errors = fn()
        except (OSError, AttributeError) as e:
            errors = ["%s: %s" % (type(e).__name__, e)]
        if errors:
            failed += 1
            print("FAIL: %s" % name)
            for e in errors:
                print("    " + e)
        else:
            print("ok:   %s" % name)
    total = len(CASES) + len(WIRING)
    print("%d of %d cases passed" % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
