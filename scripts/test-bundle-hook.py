#!/usr/bin/env python3
"""Run the update bundle's carry-over hook, as shipped, between two fake slots.

    scripts/test-bundle-hook.py

boat-bundle-hook.sh runs inside RAUC as the new slot's slot-post-install
hook and copies this board's own state - identity, keys, network, password
hashes - from the running slot into the one just written. A mistake there
locks people out of the new slot. This runs the hook against two scratch
directories standing in for the running slot and the new one, and checks
what the new slot ends up with.

Only paths are changed (the running slot's files are read from a scratch
directory instead of /). Exit status: 0 = all passed, 1 = a case failed.
"""
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "layers", "meta-boat", "external", "rauc",
                    "recipes-core", "bundles", "files", "boat-bundle-hook.sh")

OLD_SHADOW = ("root:$6$old$roothash:19000:0:99999:7:::\n"
              "daemon:*:19000:0:99999:7:::\n"
              "boat:$6$old$boathash:19000:0:99999:7:::\n")
NEW_SHADOW = ("root:*:20000:0:99999:7:::\n"
              "daemon:*:20000:0:99999:7:::\n"
              "boat:!:20000:0:99999:7:::\n"
              "newsvc:!:20000:0:99999:7:::\n")


def slurp(path):
    with open(path) as f:
        return f.read()


def put(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


class Slots:
    def __init__(self, root, old_shadow=OLD_SHADOW, new_shadow=NEW_SHADOW, fail_copy=None,
                 health=None, shipped=None, old_from=None):
        """health: the running slot's /etc/boat-update/health.conf (None:
        none). shipped: what its image shipped there, as recorded in
        conffile-defaults.sha256 (None: an image without the record).
        old_from: a directory to take the running slot from instead (the
        new slot of an earlier update, for a second one)."""
        self.root = root
        self.old = os.path.join(root, "old")
        self.new = os.path.join(root, "new")
        if old_from:
            shutil.copytree(old_from, self.old, symlinks=True)
        else:
            put(self.o("etc/hostname"), "boat-one\n")
            put(self.o("etc/machine-id"), "1" * 32 + "\n")
            put(self.o("etc/ssh/ssh_host_ed25519_key"), "OLD KEY\n", 0o600)
            put(self.o("etc/NetworkManager/system-connections/marina.nmconnection"), "[wifi]\n", 0o600)
            put(self.o("var/lib/bluetooth/AA:BB/info"), "paired\n")
            put(self.o("home/root/.ssh/authorized_keys"), "ssh-ed25519 AAAA root\n", 0o600)
            put(self.o("etc/shadow"), old_shadow, 0o600)
            put(self.o("etc/boat-sleep.key"), "BOARD SLEEP KEY\n", 0o600)
            os.symlink("/usr/share/zoneinfo/Europe/Stockholm", self.o("etc/localtime"))
            put(self.o("etc/nv_boot_control.conf"), "TNSPEC board-own\n")
            put(self.o("etc/systemd/system/boat-sleep-listener.socket.d/port.conf"),
                "[Socket]\nListenDatagram=9199\n")
            if health is not None:
                put(self.o("etc/boat-update/health.conf"), health)
        # The new slot as the image ships it.
        put(self.n("etc/hostname"), "image-default\n")
        put(self.n("etc/machine-id"), "")
        put(self.n("etc/ssh/ssh_host_ed25519_key"), "IMAGE KEY\n", 0o600)
        put(self.n("etc/NetworkManager/system-connections/default.nmconnection"), "[eth]\n", 0o600)
        put(self.n("var/lib/bluetooth/README"), "image\n")
        put(self.n("etc/shadow"), new_shadow, 0o640)
        put(self.n("etc/boat-update/health.conf"), NEW_HEALTH)
        os.symlink("/usr/share/zoneinfo/UTC", self.n("etc/localtime"))
        put(self.n("etc/nv_boot_control.conf"), "TNSPEC image\n")
        # The units the new release has: it moved sshd from a socket to a
        # service; the app's unit is a template instance.
        for unit in ("sshd.service", "my-app@.service"):
            put(self.n("lib/systemd/system/" + unit), "[Unit]\n")
        if shipped is not None:
            put(self.o("usr/lib/boat-ab/conffile-defaults.sha256"),
                "%s  /etc/hostname\n%s  /etc/boat-update/health.conf\n"
                % (hashlib.sha256(b"image-default\n").hexdigest(),
                   hashlib.sha256(shipped.encode()).hexdigest()))
        # A cp that fails for one path, the way a full disk or an I/O error
        # would, to see what that leaves behind.
        os.makedirs(os.path.join(root, "bin"))
        put(os.path.join(root, "bin", "cp"), """#!/bin/sh
case "$*" in *%s*) echo "cp: simulated failure" >&2; exit 1 ;; esac
exec /bin/cp "$@"
""" % (fail_copy or "NEVER-MATCHES"), 0o755)
        text = slurp(HOOK)
        # The running slot is read from self.old instead of /.
        for old, new in (('\nfrom=""\n', '\nfrom="%s"\n' % self.old),
                         ("for key in /etc/ssh/ssh_host_*; do\n    copy \"$key\"",
                          "for key in %s/etc/ssh/ssh_host_*; do\n    copy \"${key#%s}\""
                          % (self.old, self.old)),
                         ("' /etc/shadow \"$shadow_new\"", "' %s/etc/shadow \"$shadow_new\"" % self.old),
                         ('grep -q "^$user:" /etc/shadow', 'grep -q "^$user:" %s/etc/shadow' % self.old),
                         ("-e '^boat:' /etc/shadow", "-e '^boat:' %s/etc/shadow" % self.old)):
            assert text.count(old) == 1, "hook changed - update the test's path rewrite for %r" % old
            text = text.replace(old, new)
        put(os.path.join(root, "hook.sh"), text)

    def o(self, rel):
        return os.path.join(self.old, rel)

    def n(self, rel):
        return os.path.join(self.new, rel)

    def run(self, arg="slot-post-install", slot_class="rootfs"):
        env = dict(os.environ, RAUC_SLOT_MOUNT_POINT=self.new, RAUC_SLOT_CLASS=slot_class,
                   PATH=os.path.join(self.root, "bin") + ":" + os.environ["PATH"])
        p = subprocess.run(["sh", os.path.join(self.root, "hook.sh"), arg],
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stdout + p.stderr


def expect(errors, cond, what):
    if not cond:
        errors.append(what)


def carried(s, rc, out):
    e = []
    expect(e, rc == 0, "exit %d, expected 0" % rc)
    expect(e, slurp(s.n("etc/hostname")) == "boat-one\n", "hostname not carried")
    expect(e, slurp(s.n("etc/ssh/ssh_host_ed25519_key")) == "OLD KEY\n", "host key not carried")
    conns = os.listdir(s.n("etc/NetworkManager/system-connections"))
    expect(e, conns == ["marina.nmconnection"],
           "system-connections should be the running slot's, whole: %s" % conns)
    expect(e, os.path.exists(s.n("home/root/.ssh/authorized_keys")), "root's .ssh not carried")
    expect(e, slurp(s.n("etc/machine-id")) == "1" * 32 + "\n", "machine-id not carried")
    key = s.n("etc/boat-sleep.key")
    expect(e, os.path.exists(key) and slurp(key) == "BOARD SLEEP KEY\n"
           and stat.S_IMODE(os.stat(key).st_mode) == 0o600, "boat-sleep.key not carried, mode 0600")
    expect(e, os.path.islink(s.n("etc/localtime"))
           and os.readlink(s.n("etc/localtime")) == "/usr/share/zoneinfo/Europe/Stockholm",
           "localtime not carried as the board's link")
    expect(e, slurp(s.n("etc/nv_boot_control.conf")) == "TNSPEC board-own\n", "nv_boot_control.conf not carried")
    dropin = s.n("etc/systemd/system/boat-sleep-listener.socket.d/port.conf")
    expect(e, os.path.exists(dropin) and "9199" in slurp(dropin), "the sleep listener's drop-in not carried")
    expect(e, os.path.exists(s.n("var/lib/bluetooth/AA:BB/info"))
           and not os.path.exists(s.n("var/lib/bluetooth/README")),
           "Bluetooth pairings not carried as the board's directory, whole")
    leftovers = [d for d, _, fs in os.walk(s.new) for f in fs + [d] if ".boat-new" in f]
    expect(e, not leftovers, "temporary copies left in the new slot: %s" % leftovers)
    return e


def passwords(s, rc, out):
    e = []
    expect(e, rc == 0, "exit %d, expected 0" % rc)
    got = slurp(s.n("etc/shadow"))
    want = ("root:$6$old$roothash:20000:0:99999:7:::\n"
            "daemon:*:20000:0:99999:7:::\n"
            "boat:$6$old$boathash:20000:0:99999:7:::\n"
            "newsvc:!:20000:0:99999:7:::\n")
    expect(e, got == want, "shadow is\n%s\nexpected\n%s" % (got, want))
    expect(e, stat.S_IMODE(os.stat(s.n("etc/shadow")).st_mode) == 0o640,
           "shadow's mode was not kept")
    return e


def kept_image(s, rc, out):
    e = []
    expect(e, rc == 0, "exit %d, expected 0" % rc)
    expect(e, slurp(s.n("etc/shadow")) == NEW_SHADOW,
           "an empty running shadow must leave the image's own, whole: got %r"
           % slurp(s.n("etc/shadow")))
    expect(e, re.search("keeps the image's passwords", out), "no warning that nothing was carried")
    return e


NO_ROOT = "".join(l + "\n" for l in NEW_SHADOW.splitlines() if not l.startswith("root:"))


def refused(s, rc, out):
    e = []
    expect(e, rc != 0, "a merged shadow without root must fail the hook (and the install)")
    expect(e, slurp(s.n("etc/shadow")) == NO_ROOT, "the new slot's shadow was changed")
    expect(e, re.search("incomplete", out), "no message saying why")
    expect(e, not os.path.exists(s.n("etc/shadow.boat-new")), "temporary shadow left behind")
    return e


def failed_copy(s, rc, out):
    e = []
    expect(e, rc == 0, "a failed optional copy must not fail the install (exit %d)" % rc)
    expect(e, os.path.exists(s.n("var/lib/bluetooth/README")),
           "a failed copy left the new slot without its own /var/lib/bluetooth")
    expect(e, re.search("could not carry over .*bluetooth", out), "no warning for the failed copy")
    expect(e, slurp(s.n("etc/hostname")) == "boat-one\n", "the other copies did not go on")
    return e


OLD_HEALTH = 'BOAT_HEALTH_UNITS="sshd.socket"\n'
NEW_HEALTH = 'BOAT_HEALTH_UNITS="sshd.service"\n'
# An operator's list that still fits the new release, and one that names a
# unit it no longer has.
EDITED_HEALTH = 'BOAT_HEALTH_UNITS="sshd.service my-app@signalk.service"\n'
STALE_HEALTH = 'BOAT_HEALTH_UNITS="sshd.socket my-app@signalk.service"\nBOAT_HEALTH_DEADLINE=300\n'


def sourced(path, var):
    """What boat-commit gets for var from a health.conf: it sources it."""
    return subprocess.run(["sh", "-c", '. "$1"; printf %s "$' + var + '"', "x", path],
                          capture_output=True, text=True).stdout


def health_is(want, says, units=None, default=None):
    """want: the file's text (None: no file), or a regex when units is
    given: what BOAT_HEALTH_UNITS comes to. default: the .boat-default
    beside it (None: none)."""
    def check(s, rc, out):
        e = []
        expect(e, rc == 0, "exit %d, expected 0" % rc)
        p = s.n("etc/boat-update/health.conf")
        got = slurp(p) if os.path.exists(p) else None
        if units is None:
            expect(e, got == want, "health.conf is %r, expected %r" % (got, want))
        else:
            expect(e, got is not None and re.match(want, got, re.S), "health.conf is %r, expected %r" % (got, want))
            expect(e, got is not None and sourced(p, "BOAT_HEALTH_UNITS") == units,
                   "BOAT_HEALTH_UNITS is %r, expected %r" % (got and sourced(p, "BOAT_HEALTH_UNITS"), units))
        d = p + ".boat-default"
        got_default = slurp(d) if os.path.exists(d) else None
        expect(e, got_default == default, "health.conf.boat-default is %r, expected %r" % (got_default, default))
        expect(e, re.search(says, out), "output never matched %r" % says)
        return e
    return check


def two_updates(root):
    """An update from an image without the record, then the next one: the
    old default carried at the first is carried again at the second (it
    is not the second image's default either), with the release's own
    beside it - and the list a release no longer fits is merged both times."""
    first = Slots(os.path.join(root, "1"), health=OLD_HEALTH, shipped=None)
    rc1, out1 = first.run()
    # The second update runs from the slot the first wrote, whose image
    # recorded its own default.
    put(first.n("usr/lib/boat-ab/conffile-defaults.sha256"), "%s  /etc/boat-update/health.conf\n"
        % hashlib.sha256(NEW_HEALTH.encode()).hexdigest())
    second = Slots(os.path.join(root, "2"), old_from=first.new)
    rc2, out2 = second.run()
    p = second.n("etc/boat-update/health.conf")
    e = []
    expect(e, rc1 == 0 and rc2 == 0, "exit %d, %d" % (rc1, rc2))
    expect(e, re.search(r"carried over \S*/etc/boat-update/health\.conf", out2),
           "the second update did not carry it: " + out2)
    expect(e, sourced(p, "BOAT_HEALTH_UNITS") == "sshd.service", "BOAT_HEALTH_UNITS is %r after two updates"
           % sourced(p, "BOAT_HEALTH_UNITS"))
    expect(e, os.path.exists(p + ".boat-default") and slurp(p + ".boat-default") == NEW_HEALTH,
           "the release's own health.conf is not beside it")
    return e, out1 + out2


def untouched(s, rc, out):
    e = []
    expect(e, rc == 0, "exit %d, expected 0" % rc)
    expect(e, slurp(s.n("etc/hostname")) == "image-default\n", "it acted on a call that is not for it")
    return e


CASES = [
    ("carries identity (hostname, machine-id, host keys), the sleep key and listener port, "
     "connections and pairings (whole), time zone, boot control and root's .ssh", {}, {}, carried),
    ("merges root's and boat's password hashes, keeps the rest and the mode", {}, {}, passwords),
    ("an empty running shadow leaves the image's own (it once wrote an empty one)",
     dict(old_shadow=""), {}, kept_image),
    ("refuses to install a merged shadow without root", dict(new_shadow=NO_ROOT), {}, refused),
    ("a copy that fails leaves the image's own version, and the rest goes on",
     dict(fail_copy="var/lib/bluetooth"), {}, failed_copy),
    ("a conffile nobody changed takes the new image's version",
     dict(health=OLD_HEALTH, shipped=OLD_HEALTH), {},
     health_is(NEW_HEALTH, r"kept the new image's /etc/boat-update/health\.conf")),
    ("a conffile someone changed is carried over, the release's own beside it",
     dict(health=EDITED_HEALTH, shipped=OLD_HEALTH), {},
     health_is(EDITED_HEALTH, r"carried over \S*/etc/boat-update/health\.conf", default=NEW_HEALTH)),
    ("an edited health.conf naming a unit the new release dropped is merged with the release's list",
     dict(health=STALE_HEALTH, shipped=OLD_HEALTH), {},
     health_is("^" + re.escape(STALE_HEALTH), r"names sshd\.socket, which this release does not have",
               units="my-app@signalk.service sshd.service", default=NEW_HEALTH)),
    ("a conffile the image shipped and someone deleted stays deleted",
     dict(health=None, shipped=OLD_HEALTH), {},
     health_is(None, r"left /etc/boat-update/health\.conf out of the new slot")),
    ("a running image without the record carries every conffile, as before (a list the release "
     "no longer fits merged)",
     dict(health=OLD_HEALTH), {},
     health_is("^" + re.escape(OLD_HEALTH), r"carried over \S*/etc/boat-update/health\.conf",
               units="sshd.service", default=NEW_HEALTH)),
    ("two updates from an image without the record: the old default is carried again, the "
     "release's own beside it", None, None, two_updates),
    ("ignores any other hook call", {}, dict(arg="install-check"), untouched),
    ("ignores a slot that is not the rootfs", {}, dict(slot_class="appfs"), untouched),
]


def main():
    failed = 0
    with tempfile.TemporaryDirectory(prefix="bundle-hook-") as work:
        for i, (name, setup, run_args, check) in enumerate(CASES):
            root = os.path.join(work, str(i))
            os.makedirs(root)
            if setup is None:
                errors, out = check(root)
            else:
                s = Slots(root, **setup)
                rc, out = s.run(**run_args)
                errors = check(s, rc, out)
            if errors:
                failed += 1
                print("FAIL: %s" % name)
                for e in errors:
                    print("    " + e)
                print("    output:\n" + "\n".join("      " + l for l in out.splitlines()))
            else:
                print("ok:   %s" % name)
    print("%d of %d cases passed" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
