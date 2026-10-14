#!/usr/bin/env python3
"""boat-update: OS updates for the boat computer's A/B layout.

    boat-update [status]          where things stand (read-only; the default)
    boat-update check             fetch and verify the channel's signed index
    boat-update download          fetch the offered bundle, resumably
    boat-update install [BUNDLE] [--reboot]
                                  install the ready bundle (or BUNDLE) into
                                  the other slot; it boots as a trial
    boat-update install --stream [--reboot]
                                  stream the offered bundle straight into the
                                  other slot, fetching only the blocks
                                  neither slot already has
    boat-update rollback [--yes]  switch to the other slot and reboot
    boat-update history           installs, trials and their results
    boat-update debug-hold on|off keep the board up after a failure
                                  (boat.stay=1) instead of resetting
    boat-update auto              what the timer runs: check, then download
                                  as BOAT_UPDATE_AUTO allows

Nothing here ever installs or reboots on its own: an update reaches the
other slot only through `install`, typed by a person (plan D12).

Exit status: 0 = done, 1 = refused or failed (the message says why),
2 = `download` ended without a ready bundle (waiting for an unmetered link,
no progress, or nothing offered).

The update channel is static files on any HTTPS host with Range support:
index.p7 - a CMS-signed JSON index - and the content-addressed bundles it
names. The index is verified against the update CA in /etc/rauc, never
against the system's public CAs, and accepted only if it is for this
channel, newer than the last one accepted (serial), for this board
(compatible, L4T release) and offers a version newer than the running one,
not already in the other slot and not one that failed a trial here. RAUC
then verifies the bundle itself, again, before installing it.

Configuration: /etc/boat-update/channel.conf (the image's defaults) and
/data/boat-update/channel.conf (the operator's overrides, shared by both
slots). State lives in /data/boat-update. See docs/07-updates.md and
docs/plans/ab-os-updates.md sections 5.6 and 5.10.
"""
import configparser
import contextlib
import fcntl
import hashlib
import http.client
import http.server
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

# Filled in at build time by the recipe: the A/B layout's disk and
# partition numbers (BOAT_AB_* in meta-boat's layer.conf).
DISK = "@DISK@"
PART = {"A": "@PARTNUM_A@", "B": "@PARTNUM_B@", "DATA": "@PARTNUM_DATA@"}
PART_NAME = {"A": "APP", "B": "APP_b"}

PATHS = {
    "cmdline": "/proc/cmdline",
    "mounts": "/proc/mounts",
    "grubenv": "/boot/efi/EFI/BOOT/grubenv",
    "release": "/etc/boat-release",
    "nv_release": "/etc/nv_tegra_release",
    "system_conf": "/etc/rauc/system.conf",
    "ca": "/etc/rauc/ca.cert.pem",
    "conf_image": "/etc/boat-update/channel.conf",
    "data": "/data",
    "state": "/data/boat-update",
    "run_ab": "/run/boat-ab",
    "lock": "/run/boat-update.lock",
    # Which release the running install must be, for RAUC's pre-install
    # handler: "VERSION PID" while this boat-update runs.
    "expect": "/run/boat-update/expect",
    "clock_retry": "/run/boat-update/clock-retry",
    "net_tcp": "/proc/net/tcp",
}

DEFAULTS = {
    "BOAT_UPDATE_URL": "",
    "BOAT_UPDATE_CHANNEL": "stable",
    "BOAT_UPDATE_AUTO": "download",
    "BOAT_UPDATE_METERED": "no",
    "BOAT_UPDATE_RATE_LIMIT": "",
    "BOAT_UPDATE_CLIENT_CERT": "",
    "BOAT_UPDATE_CLIENT_KEY": "",
    # The CA the channel's TLS certificate is checked against, when it is
    # not one the image's ca-certificates trusts (a bench server's own CA).
    "BOAT_UPDATE_SERVER_CA": "",
}

INDEX_MAX = 64 * 1024            # bytes; an index is a few hundred
NOTES_MAX = 2000                 # characters shown from an index's notes
WINDOW_SECONDS = 600             # one download window
STALL_BYTES, STALL_SECONDS = 1024, 120
FREE_FLOOR = 2 * 1024 ** 3       # /data headroom beyond the bundle (Docker)
MAX_REFETCH = 3                  # full re-downloads before a bundle is given up
SERIAL_MAX = 2 ** 32             # publish-update.sh uses at most the time in seconds
AUTO_MIN_INTERVAL = 30 * 60      # the timer and the triggers rate-limit to this
LOCK_WAIT = 60                   # seconds an interactive verb waits for the lock
CLOCK_RETRY = 120                # seconds before a check refused for the clock runs again
CLOCK_RETRIES = 5                # such retries per boot, each wait twice the last (2 to 32 min)
VPN_KINDS = ("wireguard", "tun", "vpn", "ip-tunnel")
BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.raucb$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """A refusal or failure with a message meant for the operator."""


class NotNow(UpdateError):
    """A refusal that is the board's state, not a fault: on trial, outside
    GRUB, the clock not set, another boat-update busy. Not an alert."""


class ClockNotSynced(NotNow):
    """The clock gate: TLS cannot work before chrony has set the clock."""


def say(msg):
    print("boat-update: %s" % msg, flush=True)


def warn(msg):
    print("boat-update: WARNING: %s" % msg, file=sys.stderr, flush=True)


def path(name, *more):
    return os.path.join(PATHS[name], *more)


def read(p, default=""):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return default


def write_atomic(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".new"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


# --- versions: RAUC's relaxed semantic versioning ---------------------------
# MAJOR[.MINOR[.PATCH]][-PRERELEASE][+BUILD], compared the way RAUC 1.13's
# r_semver_less_equal() does (src/utils.c), so this tool and RAUC's
# min-bundle-version never disagree about which version is newer.

# No '-' inside the pre-release or build part: RAUC splits at the last '-'.
SEMVER_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.]+))?(?:\+[0-9A-Za-z.]+)?")


def parse_version(v):
    m = SEMVER_RE.fullmatch(v or "")
    if not m:
        raise ValueError("not a version: %r" % v)
    core = tuple(int(x) if x is not None else 0 for x in m.group(1, 2, 3))
    return core, m.group(4)


def version_key(v):
    """A sort key; a later version sorts higher."""
    core, pre = parse_version(v)
    if pre is None:
        return core + ((1,),)
    fields = []
    for f in pre.split("."):
        # Numeric identifiers sort below alphanumeric ones.
        fields.append((0, int(f), "") if f.isdigit() else (1, 0, f))
    return core + ((0, tuple(fields)),)


def newer(a, b):
    """True if version a is strictly newer than version b."""
    return version_key(a) > version_key(b)


# --- the board's state -----------------------------------------------------

def cmdline():
    args = {}
    for tok in read(path("cmdline")).split():
        k, _, v = tok.partition("=")
        args[k] = v
    return args


def booted_slot():
    s = cmdline().get("rauc.slot")
    return s if s in ("A", "B") else None


def other(slot):
    return {"A": "B", "B": "A"}.get(slot)


def part_dev(num):
    return "%sp%s" % (DISK, num)


def gpt_name(num):
    r = run(["blkid", "-p", "-o", "value", "-s", "PART_ENTRY_NAME", part_dev(num)])
    return r.stdout.strip() if r.returncode == 0 else ""


def mount_source(mountpoint):
    src = None
    for line in read(path("mounts")).splitlines():
        f = line.split()
        if len(f) >= 2 and f[1] == mountpoint:
            src = f[0]
    return src


def data_mounted():
    return mount_source(PATHS["data"]) == part_dev(PART["DATA"])


def grubenv():
    r = run(["grub-editenv", path("grubenv"), "list"])
    if r.returncode != 0:
        return None
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


@contextlib.contextmanager
def commit_lock():
    """boat-commit's lock: grubenv has one writer at a time."""
    os.makedirs(PATHS["run_ab"], exist_ok=True)
    with open(path("run_ab", "commit.lock"), "w") as f:
        for _ in range(60):
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(1)
        else:
            raise UpdateError("boat-commit is holding grubenv - try again in a minute")
        yield


def replace_durably(src, dst):
    """Rename src over dst so a power cut leaves one or the other. On vfat
    the moved file's directory entry is written only by an fsync of the
    file itself (see boat-ab's lib.sh)."""
    with open(src, "rb") as f:
        os.fsync(f.fileno())
    os.replace(src, dst)
    with open(dst, "rb") as f:
        os.fsync(f.fileno())
    d = os.open(os.path.dirname(dst), os.O_RDONLY)
    try:
        os.fsync(d)
    finally:
        os.close(d)


def grubenv_set(**kv):
    """Set variables in grubenv on a copy that replaces it, and keep
    grubenv.bak (GRUB's fallback) a copy of it - like boat-ab's lib.sh,
    under the same lock and never through its temporary file names."""
    env = path("grubenv")
    with commit_lock():
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(env), prefix=".boat-update-")
        os.close(fd)
        try:
            shutil.copyfile(env, tmp)
            r = run(["grub-editenv", tmp, "set"] + ["%s=%s" % i for i in kv.items()])
            if r.returncode != 0:
                raise UpdateError("grub-editenv failed: %s" % r.stderr.strip())
            replace_durably(tmp, env)
            shutil.copyfile(env, tmp)
            replace_durably(tmp, env + ".bak")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)


def running_release():
    return read(path("release")).strip() or "unknown"


def running_l4t():
    m = re.match(r"^# R(\d+) \(release\), REVISION: (\d+)\.(\d+)", read(path("nv_release")))
    return "%s.%s.%s" % m.groups() if m else None


def system_compatible():
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read_string(read(path("system_conf")))
        return cp.get("system", "compatible", fallback=None)
    except configparser.Error:
        return None


def rauc_slots():
    """bootname -> RAUC's detailed slot status (from --detailed JSON)."""
    r = run(["rauc", "status", "--detailed", "--output-format=json"])
    if r.returncode != 0:
        return None           # RAUC is busy installing, or not answering
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return None
    slots = {}
    for entry in doc.get("slots", []):
        for _, info in entry.items():
            if info.get("bootname"):
                slots[info["bootname"]] = info
    return slots


def slot_record(slot, slots):
    """RAUC's record of the last bundle written to a slot: (version,
    status). A write that failed half way is recorded with the new
    bundle's version and status "failed"."""
    status = ((slots or {}).get(slot) or {}).get("slot_status") or {}
    return (status.get("bundle") or {}).get("version"), status.get("status")


def slot_version(slot, slots, may_mount=False):
    """The release a slot holds: RAUC's record of the bundle installed
    there, or - for a slot that was flashed, not installed - the
    /etc/boat-release inside it, read through a read-only mount. Only a
    caller holding the update lock may mount (may_mount): RAUC may be
    writing that slot otherwise, and a mount there would fail its install."""
    if slots is None:
        return None
    v, _ = slot_record(slot, slots)
    if v:
        return v
    if not may_mount or gpt_name(PART[slot]) != PART_NAME[slot]:
        return None
    with tempfile.TemporaryDirectory(prefix="boat-update-") as mnt:
        r = run(["mount", "-o", "ro,noload", "-t", "ext4", part_dev(PART[slot]), mnt])
        if r.returncode != 0:
            return None
        try:
            return read(os.path.join(mnt, "etc/boat-release")).strip() or None
        finally:
            run(["umount", mnt])


def usable_version(slot, env, slots, may_mount=False):
    """The release in a slot a rollback would boot - GRUB has it usable,
    and RAUC's last write there did not fail - else None. A release that
    is only half written there, or in a slot GRUB gave up on (a reflash
    leaves the old slot B in place; a used-up slot), is not "already in
    the other slot": it is offered, and installed, again."""
    if (env or {}).get("%s_OK" % slot) != "1":
        return None
    _, status = slot_record(slot, slots)
    if status not in (None, "", "ok"):
        return None
    return slot_version(slot, slots, may_mount)


def failed_versions():
    return set(l.strip() for l in read(path("state", "failed-versions")).splitlines() if l.strip())


def mark_failed(version):
    os.makedirs(PATHS["state"], exist_ok=True)
    with open(path("state", "failed-versions"), "a") as f:
        f.write(version + "\n")


def record_result(text):
    """A line in last-result and history, as boat-commit and RAUC's
    post-install handler write them."""
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), text)
    try:
        write_atomic(path("state", "last-result"), line)
        with open(path("state", "history"), "a") as f:
            f.write(line)
    except OSError as e:
        warn("could not record it in %s: %s" % (PATHS["state"], e.strerror))


@contextlib.contextmanager
def expecting(version):
    """Tell RAUC's pre-install handler which release this install has to
    be, for as long as this process runs: any other bundle - a channel
    that serves another signed release than its index offered, a file
    swapped after it was checked - is refused before anything is written."""
    p = path("expect")
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    write_atomic(p, "%s %d\n" % (version, os.getpid()))
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(p)


def rauc_handler_messages(since):
    """What RAUC's install handlers said since `since` (a time): RAUC keeps
    their output in its own journal and reports only an exit code."""
    try:
        r = run(["journalctl", "-u", "rauc", "--since", "@%d" % since, "-o", "cat", "--no-pager"])
    except FileNotFoundError:
        return []
    if r.returncode != 0:
        return []
    return [m.group(1) for m in (re.match(r"^# (boat-rauc-[a-z-]+: .*)$", l) for l in r.stdout.splitlines()) if m]


def explain_rauc_failure(since):
    for line in rauc_handler_messages(since):
        warn("RAUC's install handler: %s" % line)


def load_state():
    try:
        state = json.loads(read(path("state", "state.json")) or "{}")
        if isinstance(state, dict):
            return state
    except ValueError:
        pass
    warn("state.json is unreadable - starting over: the next index accepted sets the serial again")
    return {}


def save_state(state):
    write_atomic(path("state", "state.json"), json.dumps(state, indent=1, sort_keys=True) + "\n")


def load_conf():
    """The two configuration layers: the image's, then the operator's."""
    conf = dict(DEFAULTS)
    for p in (path("conf_image"), path("state", "channel.conf")):
        for line in read(p).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in DEFAULTS:
                try:
                    words = shlex.split(value, comments=True)
                except ValueError:
                    raise UpdateError("%s: cannot parse %s" % (p, line))
                conf[key] = words[0] if words else ""
    for key, name in (("BOAT_UPDATE_CLIENT_CERT", "client.crt"), ("BOAT_UPDATE_CLIENT_KEY", "client.key"),
                      ("BOAT_UPDATE_SERVER_CA", "server-ca.pem")):
        if not conf[key]:
            default = path("state", name)
            if os.path.exists(default):
                conf[key] = default
    return conf


def client_key_problem(conf):
    """What is wrong with the channel's client key, or None. It is the
    boat's credential for the channel: root's, and readable by root only."""
    key = conf["BOAT_UPDATE_CLIENT_KEY"]
    if not key:
        return None
    try:
        st = os.stat(key)
    except OSError as e:
        return "cannot read the client key %s: %s" % (key, e.strerror)
    if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o077:
        return ("the client key %s can be read by others (mode %o) - 'chown root:root %s && chmod 600 %s'"
                % (key, st.st_mode & 0o777, key, key))
    return None


def check_client_key(conf):
    problem = client_key_problem(conf)
    if problem:
        raise UpdateError(problem)


# --- the link: which device, and is it metered ------------------------------

def default_device():
    r = run(["ip", "route", "show", "default"])
    m = re.search(r"\bdev (\S+)", r.stdout)
    return m.group(1) if m else None


def nm_metered(dev):
    try:
        r = run(["nmcli", "-g", "GENERAL.METERED,GENERAL.TYPE", "device", "show", dev])
    except FileNotFoundError:
        return "unknown", ""       # no NetworkManager: every link counts as metered
    if r.returncode != 0:
        return "unknown", ""
    lines = r.stdout.splitlines() + ["", ""]
    return lines[0].strip(), lines[1].strip()


def is_metered_value(v):
    # NetworkManager: yes, no, yes (guessed), no (guessed), unknown.
    return not v.startswith("no")


def link():
    """(device, metered, why). A VPN default route hides the link under it,
    so then any metered active device makes the answer metered."""
    dev = default_device()
    if not dev:
        return None, True, "no default route"
    value, kind = nm_metered(dev)
    if kind in VPN_KINDS:
        r = run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"])
        for line in r.stdout.splitlines():
            d, _, st = line.rpartition(":")
            if st == "connected" and d != dev and is_metered_value(nm_metered(d)[0]):
                return dev, True, "%s is metered (behind the VPN on %s)" % (d, dev)
        return dev, False, "VPN on %s, no metered link under it" % dev
    return dev, is_metered_value(value), "%s: Metered=%s" % (dev, value or "unknown")


# --- the channel ------------------------------------------------------------

def curl_base(conf, dev=None):
    cmd = ["curl", "--silent", "--show-error", "--fail", "--proto", "=https",
           "--max-redirs", "0", "--connect-timeout", "30"]
    if conf["BOAT_UPDATE_CLIENT_CERT"]:
        cmd += ["--cert", conf["BOAT_UPDATE_CLIENT_CERT"]]
    if conf["BOAT_UPDATE_CLIENT_KEY"]:
        cmd += ["--key", conf["BOAT_UPDATE_CLIENT_KEY"]]
    if conf["BOAT_UPDATE_SERVER_CA"]:
        cmd += ["--cacert", conf["BOAT_UPDATE_SERVER_CA"]]
    if dev:
        cmd += ["--interface", dev]
    return cmd


def curl_capped(cmd, out, limit, append=False):
    """Run curl with its body on stdout, and copy at most `limit` bytes of it
    to `out`. curl 7.82 (kirkstone) applies --max-filesize only to a
    Content-Length it is told; a chunked body is never cut off. So the cap
    is enforced here, and curl is killed the moment a server sends more.
    Returns (curl exit status, HTTP status, curl's messages, over the cap)."""
    with tempfile.TemporaryFile(mode="w+") as err, open(out, "ab" if append else "wb") as f:
        proc = subprocess.Popen(cmd + ["--write-out", "%{stderr}\nHTTP_STATUS=%{http_code}\n",
                                       "--output", "-"],
                                stdout=subprocess.PIPE, stderr=err)
        got, over = 0, False
        for block in iter(lambda: proc.stdout.read(1 << 16), b""):
            if got + len(block) > limit:
                f.write(block[:limit - got])
                over = True
                proc.kill()
                break
            f.write(block)
            got += len(block)
        proc.stdout.close()
        rc = proc.wait()
        f.flush()
        os.fsync(f.fileno())
        err.seek(0)
        text = err.read()
    m = re.search(r"HTTP_STATUS=(\d+)", text)
    return rc, m.group(1) if m else "", re.sub(r"\s*HTTP_STATUS=\d*\s*", " ", text).strip(), over


def channel_url(conf, name):
    base = conf["BOAT_UPDATE_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise UpdateError("BOAT_UPDATE_URL must be an https:// URL (channel.conf), got %r" % base)
    return "%s/%s/%s" % (base, conf["BOAT_UPDATE_CHANNEL"], name)


def fetch_index(conf, dev):
    """Download index.p7 and return the verified JSON inside it."""
    with tempfile.TemporaryDirectory(prefix="boat-update-") as tmp:
        p7 = os.path.join(tmp, "index.p7")
        out = os.path.join(tmp, "index.json")
        rc, code, err, over = curl_capped(
            curl_base(conf, dev) + ["--max-time", "120", "-H", "Cache-Control: no-cache",
                                    channel_url(conf, "index.p7")], p7, INDEX_MAX)
        if over:
            raise UpdateError("the index is larger than %d bytes - refusing it" % INDEX_MAX)
        if rc in (5, 6, 7, 28) and code in ("", "000"):
            # No answer at all - a name that does not resolve, nothing
            # listening, a timeout: on a boat, usually just no internet.
            raise NotNow("cannot reach the channel (curl exit %d): %s" % (rc, err))
        if rc != 0:
            raise UpdateError("cannot fetch the index: curl exit %d (HTTP %s): %s" % (rc, code or "-", err))
        # Every flag is load-bearing (plan 5.10): only our CA, never the
        # system store; the binary content as signed; the emailProtection
        # signer; no time check on a board without an RTC.
        signer = os.path.join(tmp, "signer.pem")
        r = run(["openssl", "cms", "-verify", "-inform", "DER", "-binary",
                 "-in", p7, "-out", out, "-signer", signer, "-CAfile", path("ca"),
                 "-no-CApath", "-no-CAstore", "-purpose", "smimesign", "-no_check_time"])
        if r.returncode != 0:
            raise UpdateError("the index's signature does not verify against %s: %s"
                              % (path("ca"), r.stderr.strip().splitlines()[-1:] or ""))
        # -purpose smimesign also passes a signer with no extended key usage
        # at all; the index-signing certificate must name emailProtection.
        r = run(["openssl", "x509", "-in", signer, "-noout", "-ext", "extendedKeyUsage"])
        if r.returncode != 0 or "E-mail Protection" not in r.stdout:
            raise UpdateError("the index is signed by a certificate without the emailProtection "
                              "extended key usage - not an index-signing certificate")
        try:
            return json.loads(read(out))
        except ValueError:
            raise UpdateError("the signed index is not valid JSON")


def validate_index(idx):
    """Field types and shapes; raises UpdateError naming the bad field."""
    if not isinstance(idx, dict):
        raise UpdateError("the index is not a JSON object")
    need = {"channel": str, "serial": int, "compatible": str, "version": str,
            "l4t": str, "bundle": str, "size": int, "sha256": str, "published": str}
    for key, kind in need.items():
        if not isinstance(idx.get(key), kind) or isinstance(idx.get(key), bool):
            raise UpdateError("index field %r is missing or not a %s" % (key, kind.__name__))
    if not BUNDLE_RE.match(idx["bundle"]) or ".." in idx["bundle"]:
        raise UpdateError("index names an unacceptable bundle file %r" % idx["bundle"])
    if not SHA256_RE.match(idx["sha256"]):
        raise UpdateError("index sha256 is not 64 lower-case hex digits")
    if idx["size"] <= 0 or not 0 <= idx["serial"] < SERIAL_MAX:
        raise UpdateError("index size/serial out of range")
    try:
        parse_version(idx["version"])
    except ValueError:
        raise UpdateError("index version %r is not a version" % idx["version"])
    if not isinstance(idx.get("requires_reflash", False), bool):
        raise UpdateError("index field 'requires_reflash' is not a boolean")
    notes = idx.get("notes", "")
    if not isinstance(notes, str):
        raise UpdateError("index field 'notes' is not a string")
    idx["notes"] = printable(notes, keep_newlines=True)[:NOTES_MAX]
    for key in ("published", "l4t", "compatible", "version", "channel"):
        if printable(idx[key]) != idx[key]:
            raise UpdateError("index field %r holds control characters" % key)


def printable(text, keep_newlines=False):
    """text without C0/C1 control characters and DEL - nothing an index
    says can drive the terminal it is shown on."""
    return "".join(c for c in text if (c == "\n" and keep_newlines)
                   or (c >= " " and not "\x7f" <= c <= "\x9f"))


def index_hash(idx):
    return hashlib.sha256(json.dumps(idx, sort_keys=True).encode()).hexdigest()


def decide(idx, conf, state, booted_version, other_version, compatible, l4t, failed):
    """What to do about a verified, well-formed index: returns
    (offer, message). offer is the index when its bundle should be fetched,
    else None. Pure, so it can be tested on its own."""
    if idx["channel"] != conf["BOAT_UPDATE_CHANNEL"]:
        raise UpdateError("the index is for channel %r, this board follows %r - refusing it"
                          % (idx["channel"], conf["BOAT_UPDATE_CHANNEL"]))
    # Per channel: switching to a channel that last published earlier than
    # the one left must not look like a replay.
    seen = (state.get("serials") or {}).get(idx["channel"], -1)
    # The same index again (a re-check while a download is under way) is
    # fine; anything else must be newer than everything accepted so far.
    same = idx["serial"] == seen and index_hash(idx) == (state.get("index_hashes") or {}).get(idx["channel"])
    if idx["serial"] <= seen and not same:
        raise UpdateError("the index's serial %d is not above %d, the highest accepted - "
                          "a replayed or rolled-back index" % (idx["serial"], seen))
    if idx["compatible"] != compatible:
        raise UpdateError("the index is for %r, this board is %r" % (idx["compatible"], compatible))
    v = idx["version"]
    try:
        is_newer = newer(v, booted_version)
    except ValueError:
        # Nothing can be compared with it - an older release would look
        # newer. Built without scripts/02-configure-build.sh's check.
        raise UpdateError("this board's release %r is not a version, so no offer can be compared "
                          "with it - reflash it with a build whose BOAT_RELEASE is one" % booted_version)
    if not is_newer:
        return None, "up to date: the channel offers %s, this slot runs %s" % (v, booted_version)
    if v in failed:
        return None, "%s failed a trial on this board - not offered again" % v
    if v == other_version:
        return None, "%s is already in the other slot - 'boat-update rollback' switches to it" % v
    given_up = (state.get("given_up") or {}).get(idx["sha256"])
    if given_up and same:
        return None, ("%s was given up: %s - it is fetched again when the channel publishes "
                      "a new index" % (v, given_up))
    if idx["l4t"] != l4t:
        if idx.get("requires_reflash"):
            return None, "%s is a BSP bump (L4T %s): reflash at the dock (scripts/05-flash-nvme.sh)" % (v, idx["l4t"])
        raise UpdateError("the index offers %s for L4T %s, this board runs %s" % (v, idx["l4t"], l4t))
    return idx, "%s is available (this slot runs %s)" % (v, booted_version)


# --- gates ------------------------------------------------------------------

def clock_synced():
    with contextlib.suppress(FileNotFoundError):
        r = run(["chronyc", "-n", "tracking"])
        if r.returncode == 0:
            return re.search(r"^Leap status\s*:\s*Normal", r.stdout, re.M) is not None
    with contextlib.suppress(FileNotFoundError):
        r = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        return r.stdout.strip() == "yes"
    return False


def on_trial(slot, env):
    """The running slot is on trial: GRUB booted it as one and boat-commit
    has not committed it yet this boot (/run/boat-ab/committed), or grubenv
    does not have it committed. GRUB's last resort (boat.lastresort=1) boots
    whatever there is, uncounted - not a trial: an install is how such a
    board is mended."""
    args = cmdline()
    if args.get("boat.trial") == "1":
        return not os.path.exists(path("run_ab", "committed"))
    if args.get("boat.lastresort") == "1":
        return False
    return (env or {}).get("%s_COMMITTED" % slot) != "1"


def gates(for_install=False):
    """Refusals common to every verb that changes anything."""
    slot = booted_slot()
    if not slot:
        raise NotNow("this boot did not come through GRUB (no rauc.slot=) - reboot first")
    env = grubenv()
    if env is None:
        raise UpdateError("grubenv is not readable - reboot so boat-grubenv-guard can rebuild it")
    if on_trial(slot, env):
        raise NotNow("slot %s is still on trial - let it commit, or roll back, first" % slot)
    if not data_mounted():
        raise UpdateError("/data is not mounted from DATA - no updates without it")
    if not for_install and not clock_synced():
        raise ClockNotSynced("the clock is not synchronised yet (chrony) - TLS cannot work before it is")
    return slot, env


@contextlib.contextmanager
def locked(interactive=False):
    """One boat-update at a time. A person's command does not just give up
    on a background check or download, which can hold the lock for hours:
    it stops it - it resumes where it stopped at its next run - and waits
    for the lock."""
    os.makedirs(os.path.dirname(PATHS["lock"]), exist_ok=True)
    with open(PATHS["lock"], "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not interactive:
                raise NotNow("another boat-update is running")
            with contextlib.suppress(FileNotFoundError):
                # A Type=oneshot unit is "activating" for as long as it
                # runs - is-active would say no.
                r = run(["systemctl", "show", "-p", "ActiveState", "--value", "boat-update-check.service"])
                if r.stdout.strip() in ("active", "activating", "reloading"):
                    say("stopping the background check/download - it carries on from where it "
                        "stopped at its next run")
                    run(["systemctl", "stop", "boat-update-check.service"])
            for _ in range(LOCK_WAIT):
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise NotNow("another boat-update is running (an install?) - try again when it has finished")
        yield


# --- verbs ------------------------------------------------------------------

def do_check(conf, state, quiet=False, stamp=False):
    with noted("check", state):
        offer = check_channel(conf, state, quiet, stamp)
    forget_failure(state, "check")
    return offer


def check_channel(conf, state, quiet, stamp):
    """stamp: record the attempt for auto's rate limit, once the gates
    have passed - a check the gates refused did nothing."""
    if not conf["BOAT_UPDATE_URL"]:
        raise NotNow("no update channel configured (BOAT_UPDATE_URL in channel.conf)")
    slot, env = gates()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path("clock_retry"))          # the clock is set: retries start over
    check_client_key(conf)
    dev, metered, why = link()
    if not dev:
        raise NotNow("no network (no default route) - nothing to check")
    if stamp:
        state["last_auto"] = int(time.time())
        save_state(state)
    idx = fetch_index(conf, dev)
    validate_index(idx)
    slots = rauc_slots()
    offer, message = decide(idx, conf, state, running_release(),
                            usable_version(other(slot), env, slots, may_mount=True), system_compatible(),
                            running_l4t(), failed_versions())
    # Like the serial, the index last accepted is kept per channel: coming
    # back to a channel whose index has not changed is the same index.
    h = index_hash(idx)
    if h != (state.get("index_hashes") or {}).get(idx["channel"]):
        # A new index: what was given up under the old one is tried again.
        state.pop("given_up", None)
        state.pop("refetch", None)
    state.setdefault("serials", {})[idx["channel"]] = idx["serial"]
    state.setdefault("index_hashes", {})[idx["channel"]] = h
    state.pop("serial", None)
    state.pop("index_hash", None)
    state["last_check"] = int(time.time())
    state["published"] = idx["published"]
    state["channel"] = conf["BOAT_UPDATE_CHANNEL"]
    state["channel_url"] = conf["BOAT_UPDATE_URL"]
    state["message"] = message
    state["notes"] = idx["notes"]
    if offer:
        prev = state.get("offer") or {}
        if prev.get("bundle") != offer["bundle"]:
            # Content-addressed names: a different name is a different file.
            discard_downloads(keep=None)
        state["offer"] = {k: offer[k] for k in ("version", "bundle", "size", "sha256", "l4t", "channel")}
    else:
        # Nothing to fetch: a bundle downloaded, or half downloaded, for an
        # offer the channel has since withdrawn is of no use any more.
        state.pop("offer", None)
        state.pop("ready", None)
        discard_downloads(keep=None)
    save_state(state)
    if not quiet:
        say(message)
    return offer


def downloads_dir():
    return path("state", "downloads")


def discard_downloads(keep):
    d = downloads_dir()
    if not os.path.isdir(d):
        return
    for f in os.listdir(d):
        if f != keep and f != (keep or "") + ".part":
            os.unlink(os.path.join(d, f))


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def give_up(state, offer, why):
    """Stop fetching this bundle - not this version: a transfer or
    publishing fault is not a failed trial. A new index tries it again."""
    state.setdefault("given_up", {})[offer["sha256"]] = why
    state.pop("offer", None)
    save_state(state)
    raise UpdateError("%s: %s - given up until the channel publishes a new index"
                      % (offer["version"], why))


def refetch(state, offer, why):
    counts = state.setdefault("refetch", {})
    counts[offer["sha256"]] = counts.get(offer["sha256"], 0) + 1
    if counts[offer["sha256"]] > MAX_REFETCH:
        give_up(state, offer, "%s, %d times" % (why, MAX_REFETCH + 1))
    warn("%s - starting the download of %s over (%d of %d)"
         % (why, offer["version"], counts[offer["sha256"]], MAX_REFETCH))
    save_state(state)


def do_download(conf, state, allow_metered=False, windows=None):
    with noted("download", state):
        ready_now = download_offer(conf, state, allow_metered, windows)
    if ready_now:
        forget_failure(state, "download")
    return ready_now


def download_offer(conf, state, allow_metered, windows):
    """Run download windows until the offered bundle is ready, the link may
    not be used, or a window makes no progress. Returns True when ready."""
    offer = state.get("offer")
    if not offer:
        say("nothing to download - %s" % state.get("message", "run 'boat-update check'"))
        return False
    final = os.path.join(downloads_dir(), offer["bundle"])
    part = final + ".part"
    if ready(state):
        say("%s is ready - 'boat-update install' installs it" % offer["version"])
        return True
    check_client_key(conf)
    discard_downloads(keep=offer["bundle"])
    os.makedirs(downloads_dir(), exist_ok=True)
    if os.path.exists(final):
        # Complete and renamed, but not recorded ready: the run was stopped
        # (a power cut, a shutdown) in between.
        if sha256_file(final) == offer["sha256"]:
            return finish_download(state, offer, final)
        os.unlink(final)
    stalled = 0
    whole = False
    n = 0
    while windows is None or n < windows:
        n += 1
        gates()
        dev, metered, why = link()
        if metered and conf["BOAT_UPDATE_METERED"] != "yes" and not allow_metered:
            state["waiting"] = "metered link (%s)" % why
            save_state(state)
            say("%s available, waiting for an unmetered link (%s)" % (offer["version"], why))
            return False
        state.pop("waiting", None)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > offer["size"]:
            os.unlink(part)
            refetch(state, offer, "the partial download is larger than the bundle")
            continue
        free = shutil.disk_usage(PATHS["data"]).free
        if free < offer["size"] - have + FREE_FLOOR:
            raise UpdateError("/data has %d MiB free; this download needs %d MiB plus 2 GiB to spare"
                              % (free >> 20, (offer["size"] - have) >> 20))
        if have < offer["size"]:
            cmd = curl_base(conf, dev) + [
                "--speed-limit", str(STALL_BYTES), "--speed-time", str(STALL_SECONDS)]
            if not whole:
                cmd += ["--max-time", str(WINDOW_SECONDS)]
            if have:
                cmd += ["--continue-at", str(have)]
            if conf["BOAT_UPDATE_RATE_LIMIT"]:
                cmd += ["--limit-rate", conf["BOAT_UPDATE_RATE_LIMIT"]]
            rc, code, err, over = curl_capped(cmd + [channel_url(conf, offer["bundle"])],
                                              part, offer["size"] - have, append=True)
            now = os.path.getsize(part) if os.path.exists(part) else 0
            if over:
                # More than the index's size: never keep, never resume it.
                os.unlink(part)
                refetch(state, offer, "the server sent more than the %d bytes the index promised"
                        % offer["size"])
                continue
            if code == "416":
                # 416: our .part is not a prefix of what the server has.
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(part)
                refetch(state, offer, "the server refused to resume (HTTP 416)")
                continue
            if rc == 33:
                # The server answered the Range request with the whole file:
                # it cannot resume, so a window that ends half way is wasted.
                # Start again with one window for the whole file - and count
                # it, so a server that keeps doing this gives the bundle up
                # instead of downloading it over and over.
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(part)
                refetch(state, offer, "the server does not resume downloads (it ignored the Range request)")
                whole = True
                continue
            if rc not in (0, 28):
                warn("download window ended: curl exit %d (HTTP %s) %s" % (rc, code or "-", err))
            if now > have:
                stalled = 0
            else:
                stalled += 1
            say("%s: %d of %d MiB" % (offer["bundle"], now >> 20, offer["size"] >> 20))
            if now < offer["size"]:
                if stalled >= 2:
                    say("no progress in two windows - stopping until the next run")
                    return False
                continue
        # Complete: check it is the file the index promised, and that RAUC
        # will take it, before calling it ready.
        if sha256_file(part) != offer["sha256"]:
            os.unlink(part)
            refetch(state, offer, "the download's SHA-256 does not match the index")
            continue
        os.replace(part, final)
        return finish_download(state, offer, final)
    return False


def finish_download(state, offer, final):
    """final is the bundle the index named, whole: ready, if RAUC takes it."""
    problem = verify_bundle(final, offer["version"])
    if problem:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(final)
        give_up(state, offer, problem)
    state["ready"] = {"bundle": offer["bundle"], "version": offer["version"], "sha256": offer["sha256"]}
    save_state(state)
    say("%s is ready - 'boat-update install' installs it" % offer["version"])
    return True


def verify_bundle(bundle, want_version):
    """RAUC checks the signature; it does not compare 'compatible' itself.
    Returns what is wrong with the bundle, or None. Downloading the same
    signed bytes again cannot fix it, so the caller gives the bundle up."""
    r = run(["rauc", "info", "--output-format=json", bundle])
    problem = None
    info = {}
    if r.returncode != 0:
        problem = "RAUC rejects it: %s" % r.stderr.strip()
    else:
        try:
            info = json.loads(r.stdout)
        except ValueError:
            problem = "'rauc info' output is unreadable"
    if not problem and info.get("compatible") != system_compatible():
        problem = "it is for %r, this board is %r" % (info.get("compatible"), system_compatible())
    if not problem and info.get("version") != want_version:
        problem = "it is version %r, the index said %r" % (info.get("version"), want_version)
    return problem


def ready(state):
    r = state.get("ready")
    if not r:
        return None
    p = os.path.join(downloads_dir(), r["bundle"])
    return p if os.path.exists(p) else None


def trial_outcome(state):
    """Record a trial that ended without boat-commit saying so - a kernel
    panic, a hang, a power cut: the board is back on the other slot, and the
    slot that was installed is uncommitted and either marked bad or out of
    attempts. That version is not offered again."""
    inst = state.get("installed")
    slot = booted_slot()
    if not inst or not slot:
        return
    env = grubenv() or {}
    t = inst["slot"]
    if env.get("%s_COMMITTED" % t) == "1":
        state.pop("installed", None)            # it passed (whether running or not)
    elif slot != t and (env.get("%s_OK" % t) == "0" or env.get("%s_TRY" % t, "0") not in ("", "0")):
        if inst["version"] not in failed_versions():
            # boat-commit's health gate never decided (it records its own
            # verdicts): the trial ended before it could.
            mark_failed(inst["version"])
            why = ("trial of %s in slot %s ended without a verdict - a kernel panic, a hang, a power cut, "
                   "or a slot GRUB could not load (journalctl -b -1 shows that boot, if it got that far); "
                   "it is not offered again" % (inst["version"], t))
            warn(why)
            record_result(why)
        state.pop("installed", None)
    else:
        return
    save_state(state)


def no_longer_wanted(version):
    """No newer than the running release, or failed a trial here."""
    try:
        stale = not newer(version, running_release())
    except ValueError:
        stale = True          # nothing compares with the running release (decide)
    return stale or version in failed_versions()


def housekeeping(state):
    """A ready bundle - or an offer - that is now running (committed), failed
    its trial or is no newer than the running release is no longer needed:
    the old slot is the rollback, not the file."""
    trial_outcome(state)
    changed = False
    r = state.get("ready")
    if r:
        slot = booted_slot()
        env = grubenv() or {}
        done = (r["version"] == running_release() and slot and env.get("%s_COMMITTED" % slot) == "1")
        if done or no_longer_wanted(r["version"]):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(downloads_dir(), r["bundle"]))
            state.pop("ready", None)
            changed = True
    if state.get("offer") and no_longer_wanted(state["offer"]["version"]):
        state.pop("offer", None)
        changed = True
    if changed:
        save_state(state)


def ready_to_install(state):
    """The downloaded bundle, if it is still the one to install: the current
    offer, newer than the running release and not a version that failed."""
    housekeeping(state)
    r, offer = state.get("ready"), state.get("offer") or {}
    bundle = ready(state)
    if not bundle or r.get("sha256") != offer.get("sha256"):
        return None
    if r["version"] in failed_versions():
        return None
    try:
        if not newer(r["version"], running_release()):
            return None
    except ValueError:
        return None
    return bundle


def offer_to_stream(state, conf):
    """The current offer, if it is still worth installing - the checks
    ready_to_install makes for a downloaded bundle - and still from the
    channel it was checked on: the bundle is fetched by name from the
    channel configured now."""
    housekeeping(state)
    offer = state.get("offer")
    if not offer:
        raise UpdateError("the channel offers nothing newer to stream (or it failed a trial here) - "
                          "'boat-update check' first")
    if offer.get("channel") != conf["BOAT_UPDATE_CHANNEL"] or state.get("channel_url") != conf["BOAT_UPDATE_URL"]:
        raise UpdateError("the channel has changed since this offer was checked - 'boat-update check' first")
    return offer


def forget_installed(state):
    """The other slot is about to be overwritten: whatever was installed
    there before is no longer waiting for its trial (and must not be
    recorded as failing one when this install leaves the slot bad)."""
    if state.pop("installed", None) is not None:
        save_state(state)


def resolve_bundle(state, bundle):
    if bundle is None:
        bundle = ready_to_install(state)
        if not bundle:
            raise UpdateError("no bundle is ready - 'boat-update check' and 'download' first, or name one")
    bundle = os.path.abspath(bundle)
    if not os.path.isfile(bundle):
        raise UpdateError("%s does not exist" % bundle)
    return bundle


def do_install(conf, state, bundle=None, reboot=False, stream=False, allow_metered=False):
    slot, _ = gates(for_install=not stream)
    if stream:
        if bundle is not None:
            raise UpdateError("--stream installs what the channel offers; it does not take a file")
        check_client_key(conf)
        dev, metered, why = link()
        if conf["BOAT_UPDATE_METERED"] != "yes" and not allow_metered:
            if metered:
                raise UpdateError("the link is metered (%s) - --allow-metered to stream over it anyway" % why)
            if nm_metered(dev)[1] in VPN_KINDS:
                # The stream is bound to the tunnel, and the tunnel follows
                # whatever link carries it - which can become a metered one
                # half way through. A download is checked window by window.
                raise UpdateError("the default route is a VPN (%s): a stream would follow the VPN onto "
                                  "whatever link carries it, metered or not - 'boat-update download' and "
                                  "then 'install' instead, or --allow-metered" % dev)
    if os.environ.get("BOAT_UPDATE_INNER") != "1":
        # Run the install as its own systemd unit, so a dropped SSH session
        # cannot kill this wrapper while RAUC carries on without it. The
        # lock is taken here too: to fail fast if a download holds it, and
        # because choosing the bundle tidies the state. A streamed install
        # holds off sleep and shutdown from here; a local one takes the
        # inhibitor around 'rauc install' itself.
        with locked(interactive=True):
            if stream:
                offer_to_stream(load_state(), conf)
            else:
                bundle = resolve_bundle(load_state(), bundle)
        unit = "boat-update-install"
        run(["systemctl", "reset-failed", unit])
        inner = [sys.argv[0], "install"] + (["--stream"] if stream else [bundle]) \
            + (["--reboot"] if reboot else []) + (["--allow-metered"] if allow_metered else [])
        if stream:
            inner = ["systemd-inhibit", "--what=sleep:shutdown", "--who=boat-update",
                     "--why=streaming into the other A/B slot", "--mode=block"] + inner
        args = ["systemd-run", "--unit=" + unit, "--setenv=BOAT_UPDATE_INNER=1",
                "--description=boat-update install"] + inner
        r = run(args)
        if r.returncode != 0:
            raise UpdateError("could not start the install unit: %s" % r.stderr.strip())
        say("installing in the background (unit %s); following its log - "
            "losing this session does not stop it" % unit)
        follow = subprocess.Popen(["journalctl", "-f", "-o", "cat", "-u", unit])
        try:
            while run(["systemctl", "is-active", "--quiet", unit]).returncode == 0:
                time.sleep(2)
        finally:
            time.sleep(1)
            follow.terminate()
        # A unit that failed stays loaded with its result (no --collect); one
        # that succeeded is gone, and reads as Result=success.
        result = run(["systemctl", "show", "-p", "Result", "--value", unit]).stdout.strip()
        if result not in ("", "success"):
            run(["systemctl", "reset-failed", unit])
            raise UpdateError("the install failed (%s) - see above; the board stays on this slot" % result)
        return
    if stream:
        version = stream_install(conf, state, slot, dev)
        # Remembered so a trial that never reaches boat-commit is still recorded.
        state["installed"] = {"version": version, "slot": other(slot)}
        save_state(state)
        if reboot:
            say("rebooting now")
            run(["systemctl", "reboot"])
        return
    bundle = resolve_bundle(state, bundle)
    info = verify_bundle_info_only(bundle)
    version = info.get("version") or "?"
    say("installing %s (%s) into the other slot" % (bundle, version))
    forget_installed(state)
    # Sleep and shutdown are held off for the whole write (plan 5.6); the
    # trial's own inhibitor takes over after the reboot.
    started = time.time()
    with expecting(version):
        r = subprocess.run(["systemd-inhibit", "--what=sleep:shutdown", "--who=boat-update",
                            "--why=writing the other A/B slot", "--mode=block",
                            "rauc", "install", bundle])
    if r.returncode != 0:
        explain_rauc_failure(started)
        raise UpdateError("RAUC did not install %s (exit %d) - the board stays on this slot" % (bundle, r.returncode))
    # Remembered so a trial that never reaches boat-commit is still recorded.
    state["installed"] = {"version": info.get("version", "?"), "slot": other(slot)}
    save_state(state)
    say("installed: the other slot boots as a trial on the next reboot, and falls back if it fails")
    if reboot:
        say("rebooting now")
        run(["systemctl", "reboot"])


class BoundHTTPS(http.client.HTTPSConnection):
    """An HTTPS connection that can only go out through one network device:
    when that link goes away, it fails - it never moves to another route."""

    def __init__(self, host, port, context, dev, timeout=60):
        super().__init__(host, port, context=context, timeout=timeout)
        self.dev = dev

    def connect(self):
        err = OSError("cannot resolve %s" % self.host)
        for family, kind, proto, _, addr in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM):
            sock = socket.socket(family, kind, proto)
            try:
                if self.dev:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.dev.encode())
                sock.settimeout(self.timeout)
                sock.connect(addr)
            except OSError as e:
                sock.close()
                err = e
                continue
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            return
        raise err


RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)$")
CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(?:\d+|\*)", re.IGNORECASE)
RELAY_MAX = 64 << 20      # far above any one request RAUC's streaming client makes
RELAY_CONCURRENT = 8      # requests in flight at once (RAUC's client uses curl multi)


def relay_uids():
    """Who may use the stream relay: RAUC's streaming client, which runs as
    its sandbox user (rauc-stream), and root - not another local user, or
    a container running as one, with the channel's client certificate."""
    uids = {0, os.geteuid()}
    with contextlib.suppress(KeyError):
        uids.add(pwd.getpwnam("rauc-stream").pw_uid)
    return uids


def tcp_peer_uid(peer, local):
    """The uid that owns the local TCP socket at peer (ip, port), connected
    to local (ip, port) - from /proc/net/tcp; None if it is not there."""
    def enc(addr):
        return "%s:%04X" % ("".join("%02X" % int(b) for b in reversed(addr[0].split("."))), addr[1])
    want = (enc(peer), enc(local))
    for line in read(path("net_tcp")).splitlines()[1:]:
        f = line.split()
        if len(f) > 7 and (f[1], f[2]) == want:
            return int(f[7])
    return None


def start_relay(conf, url, dev):
    """RAUC streams from a relay inside boat-update on 127.0.0.1, never from
    the channel itself. Each of RAUC's Range requests is fetched from the
    verified URL the way boat-update's own transfers are made: https only,
    no redirect followed (RAUC's own client would follow one, to http too),
    over the link checked when the stream started and nothing else (not
    metered unless that was allowed), with the client certificate - which RAUC never sees, so the key stays
    root-only. Anything but a 206 of exactly the range asked for (its
    Content-Range naming the same offsets, its body that long) is a 502 to
    RAUC, which then fails the install: RAUC checks Content-Range only on
    its first request, so bytes from elsewhere in the bundle would be
    written at the offset it asked for. Only RAUC's streaming client (and
    root) may use it, and only for a range of at most RELAY_MAX bytes, a
    few at a time. Returns (server, the URL to hand RAUC)."""
    u = urllib.parse.urlsplit(url)
    ctx = ssl.create_default_context(cafile=conf["BOAT_UPDATE_SERVER_CA"] or None)
    if conf["BOAT_UPDATE_CLIENT_CERT"]:
        ctx.load_cert_chain(conf["BOAT_UPDATE_CLIENT_CERT"], conf["BOAT_UPDATE_CLIENT_KEY"] or None)
    local = threading.local()
    in_flight = threading.BoundedSemaphore(RELAY_CONCURRENT)

    class Relay(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def refuse(self, code, why):
            self.send_response_only(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            warn("stream relay: %s" % why)

        def do_GET(self):
            uid = tcp_peer_uid(self.client_address, self.server.server_address)
            if uid not in relay_uids():
                return self.refuse(403, "a request from uid %s, which is not RAUC's streaming client - refused" % uid)
            m = RANGE_RE.match(self.headers.get("Range", ""))
            if not m or self.path != u.path or not 0 <= int(m.group(2)) - int(m.group(1)) < RELAY_MAX:
                return self.refuse(400, "unexpected request %s %r" % (self.path, self.headers.get("Range")))
            if not in_flight.acquire(timeout=120):
                return self.refuse(503, "too many requests at once")
            try:
                self.relay(m)
            finally:
                in_flight.release()

        def relay(self, m):
            want = int(m.group(2)) - int(m.group(1)) + 1
            for attempt in (1, 2):           # a kept-alive connection may have been closed
                conn = getattr(local, "conn", None) or BoundHTTPS(u.hostname, u.port or 443, ctx, dev)
                local.conn = conn
                try:
                    conn.request("GET", u.path, headers={"Range": m.group(0)})
                    resp = conn.getresponse()
                    body = resp.read(want + 1)
                    break
                except (OSError, http.client.HTTPException) as e:
                    conn.close()
                    local.conn = None
                    if attempt == 2:
                        return self.refuse(502, "the channel did not answer: %s" % e)
            got = CONTENT_RANGE_RE.fullmatch(resp.getheader("Content-Range", "").strip())
            if resp.status != 206 or len(body) != want or not got \
                    or tuple(map(int, got.group(1, 2))) != tuple(map(int, m.group(1, 2))):
                conn.close()
                local.conn = None
                if resp.status != 206:
                    why = "HTTP %d%s" % (resp.status, " (a redirect)" if 300 <= resp.status < 400 else "")
                else:
                    why = "%d bytes as %r for %s" % (len(body), resp.getheader("Content-Range"), m.group(0))
                return self.refuse(502, "the channel answered %s - not passed on to RAUC" % why)
            self.send_response_only(206)
            for name in ("Content-Range", "Date", "Last-Modified"):
                if resp.getheader(name):
                    self.send_header(name, resp.getheader(name))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d%s" % (server.server_address[1], u.path)


def stream_preflight(conf, url, dev):
    """Ask for the bundle's first byte the way a download does - https
    only, no redirects, the same client certificate, over the link that was
    checked. RAUC's streaming client follows redirects, to http too, and
    finds a missing Range support only after it has marked the slot
    unbootable; both are refused here, before RAUC starts."""
    r = run(curl_base(conf, dev) + ["--range", "0-0", "--max-filesize", "65536", "--max-time", "60",
                                    "--output", "/dev/null", "--write-out", "%{http_code}", url])
    code = r.stdout.strip()
    if code == "206":
        return
    if code.startswith("3"):
        raise UpdateError("the channel redirects the bundle (HTTP %s) - not streamed, since RAUC would "
                          "follow it; 'boat-update download' and then 'install' never follow one" % code)
    if code == "200":
        raise UpdateError("the channel ignores Range requests, which streaming needs - "
                          "'boat-update download' and then 'install' works without them")
    raise UpdateError("the channel did not serve the bundle (HTTP %s): %s" % (code or "-", r.stderr.strip()))


def stream_install(conf, state, slot, dev):
    """The inner half of 'install --stream': RAUC streams the offered bundle
    into the other slot, which must then hold the release the index named.
    Returns that release."""
    offer = offer_to_stream(state, conf)
    url = channel_url(conf, offer["bundle"])
    stream_preflight(conf, url, dev)
    forget_installed(state)
    say("streaming %s into the other slot (only the blocks it does not already have)" % offer["version"])
    relay, local_url = start_relay(conf, url, dev)
    started = time.time()
    try:
        # 'rauc install', not a bare D-Bus call: it waits for RAUC's
        # Completed signal and fails if the service goes away mid-install,
        # so a RAUC that died is never read as one that finished. The
        # pre-install handler refuses any other release than the offer.
        with expecting(offer["version"]):
            p = subprocess.Popen(["rauc", "install", local_url],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            error = ""
            for line in p.stdout:
                line = line.rstrip("\n")
                print(line, flush=True)
                if "LastError:" in line:
                    error = line.split("LastError:", 1)[1].strip()
            rc = p.wait()
    finally:
        relay.shutdown()
        relay.server_close()
    if rc != 0:
        explain_rauc_failure(started)
        raise UpdateError("the streamed install failed - the board stays on this slot: %s. Streaming needs "
                          "a link that stays up and a channel with Range support; 'boat-update download' "
                          "and then 'install' is the robust path." % (error or "rauc exit %d" % rc))
    # RAUC checks the bundle's signature and compatible, not that it is the
    # release the index offered: an older signed bundle served at this URL
    # would install. The pre-install handler refuses one before anything is
    # written; this checks the slot again, before anything boots it (plan
    # 5.7).
    t = other(slot)
    got = slot_version(t, rauc_slots())
    if got != offer["version"]:
        grubenv_set(**{"ORDER": "%s %s" % (slot, t), "%s_OK" % t: "0", "%s_TRY" % t: "0"})
        raise UpdateError("the channel streamed %s, the index offered %s - slot %s is marked bad and "
                          "the board stays on this slot" % (got or "an unknown release", offer["version"], t))
    say("installed %s: the other slot boots as a trial on the next reboot, and falls back if it fails"
        % offer["version"])
    return offer["version"]


def verify_bundle_info_only(bundle):
    r = run(["rauc", "info", "--output-format=json", bundle])
    if r.returncode != 0:
        raise UpdateError("RAUC rejects %s: %s" % (bundle, r.stderr.strip()))
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def do_rollback(yes=False):
    """Switch to the other slot. GRUB's state is written here, the way RAUC's
    mark-active would write it - not through RAUC, whose service needs
    /data: a release that broke /data still commits (the health gate never
    fails the OS over data), and must still be rolled back."""
    if os.geteuid() != 0:
        raise UpdateError("run as root")
    slot = booted_slot()
    if not slot:
        raise UpdateError("this boot did not come through GRUB - nothing to roll back from")
    with locked(interactive=True):
        env = grubenv()
        if env is None:
            raise UpdateError("grubenv is not readable - reboot so boat-grubenv-guard can rebuild it")
        o = other(slot)
        if env.get("%s_OK" % o) != "1":
            raise UpdateError("slot %s is not usable (%s_OK=%s) - there is nothing to roll back to"
                              % (o, o, env.get("%s_OK" % o, "?")))
        say("this switches the board to slot %s (%s) and reboots"
            % (o, slot_version(o, rauc_slots()) or "unknown version"))
        if not yes:
            sys.stdout.write("Type 'yes' to go ahead: ")
            sys.stdout.flush()
            if sys.stdin.readline().strip() != "yes":
                raise UpdateError("not confirmed - nothing changed")
        grubenv_set(**{"ORDER": "%s %s" % (o, slot), "%s_OK" % o: "1", "%s_TRY" % o: "0"})
    run(["systemctl", "reboot"])


def do_debug_hold(on):
    if os.geteuid() != 0:
        raise UpdateError("run as root")
    with locked(interactive=True):
        grubenv_set(BOAT_STAY="1" if on else "0")
    say("BOAT_STAY=%d: %s" % (on, "from the next boot, a failure stops for inspection instead of "
                               "resetting (boat.stay=1); it ends by itself after a boot that is healthy"
                               if on else "failures reset the board again"))


def do_history():
    note_trial_outcome()
    for name in ("history", "last-result", "failed-versions"):
        text = read(path("state", name)).rstrip()
        if text:
            print("== %s" % name)
            print(text)


def note_trial_outcome():
    """Record a trial that ended without a verdict as soon as anyone looks
    (status, history) - not only at the next background check. Only root
    writes, and never while another boat-update is busy."""
    if os.geteuid() != 0 or not os.path.isdir(PATHS["state"]):
        return
    with contextlib.suppress(UpdateError):
        with locked():
            trial_outcome(load_state())


@contextlib.contextmanager
def noted(stage, state):
    """Keep a failed check or download in state.json, for status to show
    as an alert - a channel serving an index that does not verify or is a
    replay, a download /data has no room for - until it works again
    (forget_failure). A refusal that is only the board's state (NotNow)
    is not kept, and nothing is written while /data is not mounted."""
    try:
        yield
    except NotNow:
        raise
    except UpdateError as e:
        with contextlib.suppress(OSError):
            if data_mounted():
                failed = load_state()
                failed["last_error"] = {"stage": stage, "time": int(time.time()),
                                        "message": printable(str(e))}
                save_state(failed)
        raise


def forget_failure(state, stage):
    """A check that worked clears a failed check; one that leaves nothing
    to download also clears a failed download. A download clears its own
    once the bundle is ready."""
    failed = state.get("last_error")
    if failed and (failed.get("stage") == stage or (stage == "check" and not state.get("offer"))):
        del state["last_error"]
        save_state(state)


def retry_after_clock_sync():
    """A check the clock gate refused runs again in a few minutes, by
    which time chrony has normally set the clock - not hours later. Each
    wait is twice the last, and after CLOCK_RETRIES the timer's own
    schedule takes over: a boat out of reach of any time source would
    otherwise run a refused check every two minutes for days. The count
    starts over once a check finds the clock set."""
    try:
        n = int(read(path("clock_retry")).strip() or 0)
    except ValueError:
        n = 0
    if n >= CLOCK_RETRIES:
        return
    with contextlib.suppress(FileNotFoundError):
        # RemainAfterElapse=no: an elapsed transient timer stays loaded
        # otherwise, and the next retry could not use its name again.
        r = run(["systemd-run", "--unit=boat-update-check-retry", "--on-active=%d" % (CLOCK_RETRY << n),
                 "--timer-property=AccuracySec=10s", "--timer-property=RemainAfterElapse=no",
                 "--description=boat-update: check once the clock is set",
                 "systemctl", "start", "--no-block", "boat-update-check.service"])
        if r.returncode == 0:
            os.makedirs(os.path.dirname(path("clock_retry")), exist_ok=True)
            write_atomic(path("clock_retry"), "%d\n" % (n + 1))


def do_auto(conf, state):
    """What the timer and the network/sleep triggers run."""
    housekeeping(state)
    policy = conf["BOAT_UPDATE_AUTO"]
    if policy == "off":
        return
    # A stamp from the future (a clock that was ahead) does not count.
    since = time.time() - state.get("last_auto", 0)
    if 0 <= since < AUTO_MIN_INTERVAL and not state.get("offer"):
        return
    try:
        offer = do_check(conf, state, stamp=True)
    except ClockNotSynced:
        retry_after_clock_sync()
        raise
    if offer and policy == "download":
        do_download(conf, state)


def age(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return "%d %s ago" % (s // n, unit)
    return "%d s ago" % s


def do_status(conf, state):
    slot = booted_slot()
    env = grubenv()
    slots = rauc_slots()
    print("A/B update status")
    if not slot:
        print("  booted OUTSIDE GRUB (no rauc.slot=): updates are refused until a reboot through GRUB")
    else:
        trial = env is not None and on_trial(slot, env)
        print("  running   slot %s, %s, release %s" % (slot, "ON TRIAL" if trial else "committed", running_release()))
        o = other(slot)
        ok = (env or {}).get("%s_OK" % o) == "1"
        _, written = slot_record(o, slots)
        print("  other     slot %s, %s, release %s%s" % (
            o, "usable" if ok else "not usable", slot_version(o, slots) or "unknown",
            " (its last install failed half way)" if written not in (None, "", "ok") else ""))
    if env is None:
        print("  ALERT     grubenv is unreadable")
    if os.path.exists(path("run_ab", "grubenv-repaired")):
        print("  ALERT     grubenv was torn and rebuilt this boot")
    # One line per problem the A/B scripts (boat-commit, the guards) found
    # this boot, each saying what it is.
    for line in read(path("run_ab", "health-alert")).splitlines():
        if line.strip():
            print("  ALERT     %s" % line.strip())
    if "boat.lastresort=1" in read(path("cmdline")).split():
        print("  ALERT     GRUB found no usable slot: this is its last resort boot")
    if not data_mounted():
        print("  ALERT     /data is not mounted from DATA: no updates, and no Docker, until it is")
    last = read(path("state", "last-result")).strip()
    if last:
        print("  last      %s" % last)
    if (env or {}).get("BOAT_STAY") == "1":
        print("  debug-hold is ON: failures stop for inspection instead of resetting")
    print("OTA")
    if not conf["BOAT_UPDATE_URL"]:
        print("  no channel configured (BOAT_UPDATE_URL)")
        return
    if client_key_problem(conf):
        print("  ALERT     %s" % client_key_problem(conf))
    failed = state.get("last_error")
    if failed:
        print("  ALERT     the last %s failed, %s: %s" % (failed.get("stage", "check"), age(failed.get("time")),
                                                       failed.get("message", "?")))
    print("  channel   %s at %s (automatic: %s, metered links: %s)"
          % (conf["BOAT_UPDATE_CHANNEL"], conf["BOAT_UPDATE_URL"], conf["BOAT_UPDATE_AUTO"], conf["BOAT_UPDATE_METERED"]))
    print("  checked   %s; channel published %s" % (age(state.get("last_check")), state.get("published", "?")))
    if state.get("message"):
        print("  result    %s" % state["message"])
    offer = state.get("offer")
    if offer:
        r = ready(state)
        if r:
            print("  READY     %s - 'boat-update install --reboot' installs it" % offer["version"])
        else:
            part = os.path.join(downloads_dir(), offer["bundle"] + ".part")
            have = os.path.getsize(part) if os.path.exists(part) else 0
            print("  download  %s: %d of %d MiB%s" % (offer["version"], have >> 20, offer["size"] >> 20,
                                                      ", waiting: " + state["waiting"] if state.get("waiting") else ""))
            print("  transfer  download-then-install: %d MiB more; 'install --stream': only the blocks"
                  " neither slot has - typically a fraction of that (docs/07-updates.md)"
                  % ((offer["size"] - have) >> 20))
    if state.get("notes"):
        print("  notes     " + state["notes"].replace("\n", "\n            "))


def terminated(signum, frame):
    """systemctl stop sends SIGTERM, which by default ends Python without
    running a single finally: the other slot left mounted, which RAUC then
    refuses to install into. Unwind instead."""
    raise SystemExit(128 + signum)


def main(argv):
    args = argv[1:] or ["status"]
    verb, rest = args[0], args[1:]
    signal.signal(signal.SIGTERM, terminated)
    if verb in ("-h", "--help", "help"):
        print(__doc__.split("\n\n")[0].split("\n", 1)[1])
        return 0
    try:
        conf = load_conf()
        state = load_state() if os.path.isdir(PATHS["state"]) else {}
        if verb == "status":
            note_trial_outcome()
            state = load_state() if os.path.isdir(PATHS["state"]) else {}
            do_status(conf, state)
        elif verb == "history":
            do_history()
        elif verb == "install" and os.environ.get("BOAT_UPDATE_INNER") != "1":
            if os.geteuid() != 0:
                raise UpdateError("run as root")
            # The outer wrapper only starts and follows the install unit;
            # the unit's own run takes the lock.
            files = [a for a in rest if not a.startswith("--")]
            if len(files) > 1:
                raise UpdateError("install takes at most one bundle")
            do_install(conf, state, files[0] if files else None, reboot="--reboot" in rest,
                       stream="--stream" in rest, allow_metered="--allow-metered" in rest)
        elif verb in ("check", "download", "auto", "install"):
            if os.geteuid() != 0:
                raise UpdateError("run as root")
            # The timer's run gives way; a person's command does not.
            with locked(interactive=verb != "auto"):
                state = load_state()
                if verb == "check":
                    do_check(conf, state)
                elif verb == "download":
                    if not do_download(conf, state, allow_metered="--allow-metered" in rest):
                        return 2
                elif verb == "auto":
                    do_auto(conf, state)
                else:
                    files = [a for a in rest if not a.startswith("--")]
                    if len(files) > 1:
                        raise UpdateError("install takes at most one bundle")
                    do_install(conf, state, files[0] if files else None, reboot="--reboot" in rest,
                               stream="--stream" in rest, allow_metered="--allow-metered" in rest)
        elif verb == "rollback":
            do_rollback(yes="--yes" in rest)
        elif verb == "debug-hold":
            if rest not in (["on"], ["off"]):
                raise UpdateError("usage: boat-update debug-hold on|off")
            do_debug_hold(rest == ["on"])
        else:
            raise UpdateError("unknown verb %r - see boat-update --help" % verb)
    except NotNow as e:
        print("boat-update: %s" % e, file=sys.stderr)
        # The timer's run gives way to the board's state (on trial, no
        # network, the clock): that is not a failed unit.
        return 0 if verb == "auto" else 1
    except UpdateError as e:
        print("boat-update: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
