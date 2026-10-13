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
import os
import re
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
    def __init__(self, root, old_shadow=OLD_SHADOW, new_shadow=NEW_SHADOW, fail_copy=None):
        self.root = root
        self.old = os.path.join(root, "old")
        self.new = os.path.join(root, "new")
        put(self.o("etc/hostname"), "boat-one\n")
        put(self.o("etc/machine-id"), "1" * 32 + "\n")
        put(self.o("etc/ssh/ssh_host_ed25519_key"), "OLD KEY\n", 0o600)
        put(self.o("etc/NetworkManager/system-connections/marina.nmconnection"), "[wifi]\n", 0o600)
        put(self.o("var/lib/bluetooth/AA:BB/info"), "paired\n")
        put(self.o("home/root/.ssh/authorized_keys"), "ssh-ed25519 AAAA root\n", 0o600)
        put(self.o("etc/shadow"), old_shadow, 0o600)
        # The new slot as the image ships it.
        put(self.n("etc/hostname"), "image-default\n")
        put(self.n("etc/machine-id"), "")
        put(self.n("etc/ssh/ssh_host_ed25519_key"), "IMAGE KEY\n", 0o600)
        put(self.n("etc/NetworkManager/system-connections/default.nmconnection"), "[eth]\n", 0o600)
        put(self.n("var/lib/bluetooth/README"), "image\n")
        put(self.n("etc/shadow"), new_shadow, 0o640)
        # A cp that fails for one path, the way a full disk or an I/O error
        # would, to see what that leaves behind.
        os.makedirs(os.path.join(root, "bin"))
        put(os.path.join(root, "bin", "cp"), """#!/bin/sh
case "$*" in *%s*) echo "cp: simulated failure" >&2; exit 1 ;; esac
exec /bin/cp "$@"
""" % (fail_copy or "NEVER-MATCHES"), 0o755)
        text = slurp(HOOK)
        # The running slot is read from self.old instead of /.
        for old, new in (('src="$1"', 'src="%s$1"' % self.old),
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


def untouched(s, rc, out):
    e = []
    expect(e, rc == 0, "exit %d, expected 0" % rc)
    expect(e, slurp(s.n("etc/hostname")) == "image-default\n", "it acted on a call that is not for it")
    return e


CASES = [
    ("carries hostname, host keys, connections (whole) and root's .ssh", {}, {}, carried),
    ("merges root's and boat's password hashes, keeps the rest and the mode", {}, {}, passwords),
    ("an empty running shadow leaves the image's own (it once wrote an empty one)",
     dict(old_shadow=""), {}, kept_image),
    ("refuses to install a merged shadow without root", dict(new_shadow=NO_ROOT), {}, refused),
    ("a copy that fails leaves the image's own version, and the rest goes on",
     dict(fail_copy="var/lib/bluetooth"), {}, failed_copy),
    ("ignores any other hook call", {}, dict(arg="install-check"), untouched),
    ("ignores a slot that is not the rootfs", {}, dict(slot_class="appfs"), untouched),
]


def main():
    failed = 0
    with tempfile.TemporaryDirectory(prefix="bundle-hook-") as work:
        for i, (name, setup, run_args, check) in enumerate(CASES):
            root = os.path.join(work, str(i))
            os.makedirs(root)
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
