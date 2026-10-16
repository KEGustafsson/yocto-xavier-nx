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
import hmac
import http.client
import http.server
import json
import os
import pwd
import re
import secrets
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
    # The kernel device tree this slot was built against (boat-image.bb).
    "firmware_id": "/usr/lib/boat-ab/firmware.id",
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
    # The install unit's exit status, one file per install wrapper.
    "install_result": "/run/boat-update/install-result",
    "net_tcp": "/proc/net/tcp",
    "proc": "/proc",
    "sys_net": "/sys/class/net",
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
# USB modems and phone tethering that the kernel shows as Ethernet. This
# image's NetworkManager has no ModemManager plugin, so it guesses every
# one of them unmetered.
CELLULAR_DRIVERS = ("cdc_ether", "cdc_eem", "cdc_ncm", "huawei_cdc_ncm", "cdc_mbim", "qmi_wwan",
                    "rndis_host", "ipheth")
# curl exits that mean the link failed, not the channel: no name, no
# connection, a timeout, or a connection cut off before any HTTP answer.
NO_ANSWER_EXITS = (5, 6, 7, 18, 28, 35, 52, 55, 56)
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


def append_durably(p, text):
    """Append text to p and fsync it, and its directory when p is new: a
    line that must outlive a power cut (a failed version) has to be on disk
    before state.json, fsynced right after, stops pointing at it."""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    new = not os.path.exists(p)
    with open(p, "a") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if new:
        d = os.open(os.path.dirname(p), os.O_RDONLY)
        try:
            os.fsync(d)
        finally:
            os.close(d)


def file_size(p):
    """p's size, or 0 if it is not there - also when a download renames or
    deletes it in between."""
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


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


def running_firmware():
    """This slot's firmware.id, or None: a slot built before it was
    recorded (or a single-slot image) has none."""
    return read(path("firmware_id")).strip() or None


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


def slot_version(slot, slots, may_read=False):
    """The release a slot holds: RAUC's record of the bundle installed
    there, or - for a slot that was flashed, not installed - the
    /etc/boat-release inside it, read with debugfs. Not through a mount: a
    stop, a closed terminal or a busy mount point could leave the slot
    mounted, and RAUC refuses to install into a mounted slot. Only a caller
    holding the update lock may read it (may_read): RAUC may be writing that
    slot otherwise."""
    if slots is None:
        return None
    v, _ = slot_record(slot, slots)
    if v:
        return v
    if not may_read or gpt_name(PART[slot]) != PART_NAME[slot]:
        return None
    try:
        r = run(["debugfs", "-R", "cat /etc/boat-release", part_dev(PART[slot])])
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return None
    return printable(r.stdout).strip() or None


def usable_version(slot, env, slots, may_read=False):
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
    return slot_version(slot, slots, may_read)


def failed_versions():
    return set(l.strip() for l in read(path("state", "failed-versions")).splitlines() if l.strip())


def mark_failed(version):
    append_durably(path("state", "failed-versions"), version + "\n")


def record_result(text):
    """A line in last-result and history, as boat-commit and RAUC's
    post-install handler write them."""
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), text)
    try:
        write_atomic(path("state", "last-result"), line)
        append_durably(path("state", "history"), line)
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
    """What RAUC's install handlers and the bundle's slot hook said since
    `since` (a time): RAUC keeps their output in its own journal and
    reports only an exit code. RAUC prefixes a system handler's lines with
    '# '; the slot hook's reach the journal as they are."""
    try:
        r = run(["journalctl", "-u", "rauc", "--since", "@%d" % since, "-o", "cat", "--no-pager"])
    except FileNotFoundError:
        return []
    if r.returncode != 0:
        return []
    return [m.group(1) for m in (re.match(r"^(?:# )?((?:boat-rauc-[a-z-]+|boat-bundle-hook): .*)$", l)
                                 for l in r.stdout.splitlines()) if m]


def explain_rauc_failure(since):
    for line in rauc_handler_messages(since):
        warn("RAUC's install handler: %s" % line)


def install_warnings(since):
    """The slot hook's warnings from an install that worked - a setting it
    could not carry into the new slot - shown now and kept for status."""
    found = [printable(l) for l in rauc_handler_messages(since) if ": WARNING: " in l]
    for line in found:
        warn("RAUC's install handler: %s" % line)
    return found


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
    try:
        u = urllib.parse.urlsplit(conf["BOAT_UPDATE_URL"])
        userinfo = u.username is not None or u.password is not None
    except ValueError:
        raise UpdateError("BOAT_UPDATE_URL is not a URL (channel.conf)")
    if userinfo:
        # curl would send it as Basic auth, and it would show in ps,
        # state.json and status. Not repeated here, for the same reason.
        raise UpdateError("BOAT_UPDATE_URL carries a user name or password (channel.conf) - not supported: "
                          "it would be readable by every local user; use a client certificate (client.crt, "
                          "client.key) to restrict the channel")
    return conf


def install_conf(rest):
    """channel.conf for an install. Only a streamed one talks to the
    channel: an install from a file (push-update.sh's, offline, or the
    downloaded bundle) goes on with the defaults when channel.conf has a
    typo or a URL it refuses."""
    try:
        return load_conf()
    except UpdateError as e:
        if "--stream" in rest:
            raise
        warn("%s - ignored: an install from a file does not use the channel" % e)
        return dict(DEFAULTS)


def client_key_problem(conf):
    """What is wrong with the channel's client key, or None. It is the
    boat's credential for the channel: root's, and readable by root only.
    With no key file of its own, curl and the relay take the key from the
    certificate file, which then must be root's alone just the same."""
    key = conf["BOAT_UPDATE_CLIENT_KEY"]
    if not key:
        cert = conf["BOAT_UPDATE_CLIENT_CERT"]
        if not cert or "PRIVATE KEY-----" not in read(cert):
            return None
        key = cert
    try:
        st = os.stat(key)
    except OSError as e:
        return "cannot read the client key %s: %s" % (key, e.strerror)
    if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o077:
        return ("the client key in %s can be read by others (mode %o) - 'chown root:root %s && chmod 600 %s'"
                % (key, st.st_mode & 0o777, key, key))
    return None


def check_client_key(conf):
    problem = client_key_problem(conf)
    if problem:
        raise UpdateError(problem)


# --- the link: which device, and is it metered ------------------------------

def route_device(conf):
    """(device, why): the device the kernel routes the channel's host
    through - a split-tunnel route, a policy-routed (WireGuard) default,
    IPv6 - not just the main table's IPv4 default. Transfers are bound to
    it, so it must be the one that reaches the channel."""
    host = urllib.parse.urlsplit(conf["BOAT_UPDATE_URL"]).hostname
    if not host:
        return None, "no channel host"
    try:
        addrs = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        return None, "cannot resolve %s: %s" % (host, getattr(e, "strerror", None) or e)
    for addr in dict.fromkeys(a[4][0] for a in addrs):
        r = run(["ip", "route", "get", addr])
        m = re.search(r"\bdev (\S+)", r.stdout)
        if r.returncode == 0 and m:
            return m.group(1), None
    return None, "no route to %s" % host


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


def cellular_driver(dev):
    """The USB modem or tethering driver behind dev, or None."""
    try:
        driver = os.path.basename(os.readlink(os.path.join(PATHS["sys_net"], dev, "device", "driver")))
    except OSError:
        return None
    return driver if driver in CELLULAR_DRIVERS else None


def device_metered(dev):
    """(metered, NetworkManager's value, kind). NetworkManager guesses a USB
    modem or a tethered phone unmetered - it looks like Ethernet - so only a
    connection marked 'metered no' makes one of those unmetered."""
    value, kind = nm_metered(dev)
    metered = is_metered_value(value)
    if not metered and value != "no" and cellular_driver(dev):
        metered, value = True, "%s, but a USB modem or phone (%s)" % (value, cellular_driver(dev))
    return metered, value, kind


def link(conf):
    """(device, metered, why) for the route to the channel. A VPN hides the
    link under it, so then any metered active device makes the answer
    metered."""
    dev, why = route_device(conf)
    if not dev:
        return None, True, why
    metered, value, kind = device_metered(dev)
    if kind in VPN_KINDS:
        r = run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"])
        for line in r.stdout.splitlines():
            d, _, st = line.rpartition(":")
            if st == "connected" and d != dev and device_metered(d)[0]:
                return dev, True, "%s is metered (behind the VPN on %s)" % (d, dev)
        return dev, False, "VPN on %s, no metered link under it" % dev
    return dev, metered, "%s: Metered=%s" % (dev, value or "unknown")


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


def transient_http(code):
    """An HTTP status a busy server or proxy answers now and then: a window
    that gets one is a stall, retried at the next window or run."""
    return code in ("408", "429") or code.startswith("5")


def no_answer(rc, code, err):
    """curl got no answer from the channel: the link failed, not the
    channel. A TLS alert is the server's answer (a client certificate it
    wants, say), so it is not counted as none."""
    return rc in NO_ANSWER_EXITS and code in ("", "000") and "alert" not in err.lower()


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
        if no_answer(rc, code, err):
            # No answer at all - a name that does not resolve, nothing
            # listening, a timeout, a link that drops mid-transfer: on a
            # boat, usually just no internet, or a poor one.
            raise NotNow("cannot reach the channel (curl exit %d): %s" % (rc, err))
        if rc in (51, 60):
            raise UpdateError("cannot fetch the index: curl exit %d: %s - the channel's certificate is not "
                              "trusted (server-ca.pem?), or a captive portal (a marina's Wi-Fi login page) "
                              "answered in its place" % (rc, err))
        if rc != 0:
            raise UpdateError("cannot fetch the index: curl exit %d (HTTP %s): %s" % (rc, code or "-", err))
        if code.startswith("3"):
            raise UpdateError("the channel redirects the index (HTTP %s) - redirects are never followed: "
                              "BOAT_UPDATE_URL must name the final location" % code)
        if code != "200":
            raise UpdateError("the channel answered HTTP %s for the index" % (code or "-"))
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
    if not isinstance(idx.get("firmware", ""), str):
        raise UpdateError("index field 'firmware' is not a string")
    for key in ("published", "l4t", "compatible", "version", "channel", "firmware"):
        if key in idx and printable(idx[key]) != idx[key]:
            raise UpdateError("index field %r holds control characters" % key)


def printable(text, keep_newlines=False):
    """text without C0/C1 control characters and DEL - nothing an index
    says can drive the terminal it is shown on."""
    return "".join(c for c in text if (c == "\n" and keep_newlines)
                   or (c >= " " and not "\x7f" <= c <= "\x9f"))


def index_hash(idx):
    return hashlib.sha256(json.dumps(idx, sort_keys=True).encode()).hexdigest()


def decide(idx, conf, state, booted_version, other_version, compatible, l4t, failed, firmware=None):
    """What to do about a verified, well-formed index: returns
    (offer, message). offer is the index when its bundle should be fetched,
    else None. firmware: this slot's firmware.id (None: not recorded).
    Pure, so it can be tested on its own."""
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
                          "a replayed or rolled-back index (or a CDN or proxy still serving an older "
                          "index.p7)" % (idx["serial"], seen))
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
        # A newer release on another L4T is a BSP bump for this board, with
        # requires_reflash or without: during a fleet's move to a new L4T,
        # boards not yet reflashed see every later release this way. It
        # needs a person at the dock, not an alert. (requires_reflash means
        # nothing to a board already on that L4T: it takes it over the air.)
        return None, ("%s is a BSP bump for this board (built on L4T %s, this board runs %s): reflash at "
                      "the dock (scripts/05-flash-nvme.sh)" % (v, idx["l4t"], l4t))
    if firmware and idx.get("firmware") and idx["firmware"] != firmware:
        # The same L4T, but another kernel device tree (a meta-tegra or
        # kernel change): the kernel runs on the DTB written at flash, and
        # RAUC's pre-install handler refuses the bundle - after the board
        # has downloaded it. Like a BSP bump, a person at the dock. A side
        # that has no firmware.id cannot tell: the handler only warns then.
        return None, ("%s was built against another kernel device tree than this board was flashed with "
                      "(firmware.id %s, this board's %s): reflash at the dock (scripts/05-flash-nvme.sh)"
                      % (v, idx["firmware"][:12], firmware[:12]))
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
    """stamp: record the attempt for auto's rate limit, once the channel
    has answered - a check the gates refused, or one that got no answer at
    all, did nothing, and the next trigger (the link coming up properly)
    tries again. A channel that answers badly is not asked on every one."""
    if not conf["BOAT_UPDATE_URL"]:
        raise NotNow("no update channel configured (BOAT_UPDATE_URL in channel.conf)")
    slot, env = gates()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path("clock_retry"))          # the clock is set: retries start over
    check_client_key(conf)
    dev, metered, why = link(conf)
    if not dev:
        raise NotNow("no network (%s) - nothing to check" % why)
    try:
        idx = fetch_index(conf, dev)
    except NotNow:
        raise
    except UpdateError:
        stamp_auto(state, stamp)
        raise
    stamp_auto(state, stamp)
    validate_index(idx)
    slots = rauc_slots()
    offer, message = decide(idx, conf, state, running_release(),
                            usable_version(other(slot), env, slots, may_read=True), system_compatible(),
                            running_l4t(), failed_versions(), running_firmware())
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
        if (prev.get("bundle"), prev.get("sha256")) != (offer["bundle"], offer["sha256"]):
            # Content-addressed names: a different name is a different file.
            # So is the same name with another SHA-256 (a channel not
            # published with publish-update.sh): its old bytes are no part
            # of the new file.
            discard_downloads(keep=None)
        if (state.get("ready") or {}).get("sha256") != offer["sha256"]:
            # Downloaded for an earlier offer: not the file this one names.
            # Kept, it would read as ready and stop the new download.
            state.pop("ready", None)
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


def stamp_auto(state, stamp):
    if stamp:
        state["last_auto"] = int(time.time())
        save_state(state)


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


def do_download(conf, state, allow_metered=False, windows=None, background=False):
    with noted("download", state):
        ready_now = download_offer(conf, state, allow_metered, windows, background)
    if ready_now:
        forget_failure(state, "download")
    return ready_now


def still_allowed(conf, offer):
    """For a background download, between windows: the configuration as it
    is now (an operator may have edited it during an hours-long download),
    or None when it no longer allows this download."""
    try:
        now = load_conf()
    except UpdateError as e:
        say("channel.conf has changed and cannot be read (%s) - stopping the background download" % e)
        return None
    if now["BOAT_UPDATE_AUTO"] != "download":
        say("BOAT_UPDATE_AUTO is now %r - stopping the background download" % now["BOAT_UPDATE_AUTO"])
        return None
    if (now["BOAT_UPDATE_URL"], now["BOAT_UPDATE_CHANNEL"]) != (conf["BOAT_UPDATE_URL"], offer.get("channel")):
        say("the channel has changed - stopping the background download until the next check")
        return None
    return now


def download_offer(conf, state, allow_metered, windows, background=False):
    """Run download windows until the offered bundle is ready, the link may
    not be used, or a window makes no progress. Returns True when ready.
    background: the timer's run, which follows channel.conf's policy as it
    is at each window."""
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
        if background and n > 1:
            conf = still_allowed(conf, offer)
            if conf is None:
                return False
        gates()
        dev, metered, why = link(conf)
        if not dev:
            say("%s available, no network (%s) - the download resumes at the next run" % (offer["version"], why))
            return False
        if metered and conf["BOAT_UPDATE_METERED"] != "yes" and not allow_metered:
            state["waiting"] = "metered link (%s)" % why
            save_state(state)
            say("%s available, waiting for an unmetered link (%s)" % (offer["version"], why))
            return False
        if state.pop("waiting", None) is not None:
            save_state(state)
        have = file_size(part)
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
            if code != "416" and (code.startswith("3") or (rc == 22 and not transient_http(code))
                                  or (rc == 0 and code not in ("200", "206"))):
                # Not the bundle: a redirect (curl does not follow it, and
                # wrote its body), a missing file, a refusal. Its bytes are
                # no part of the bundle; the status is an alert, not a
                # stall that repeats unseen for ever.
                with contextlib.suppress(FileNotFoundError):
                    os.truncate(part, have)
                if code.startswith("3"):
                    raise UpdateError("the channel redirects %s (HTTP %s) - redirects are never followed: "
                                      "BOAT_UPDATE_URL must name the final location" % (offer["bundle"], code))
                raise UpdateError("the channel answered HTTP %s for %s%s"
                                  % (code or "-", offer["bundle"], " (no such file)" if code == "404" else ""))
            now = file_size(part)
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
            if rc == 33 or (rc == 0 and code == "200" and have):
                # The server answered the Range request with the whole file:
                # it cannot resume, so a window that ends half way is wasted.
                # Start again with one window for the whole file - and count
                # it, so a server that keeps doing this gives the bundle up
                # instead of downloading it over and over. (A whole file no
                # longer than what is here already: curl exits 0, unwritten.)
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
        # Its partial download goes with it: nothing else would delete it
        # before the next check that reaches the channel.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(downloads_dir(), state["offer"]["bundle"] + ".part"))
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
        failed = state.get("last_error") or {}
        if failed.get("stage") == "check":
            # Only a signed index can withdraw an offer: a missing index
            # (404) or a stale one leaves the download in place. Whoever
            # installs it is told the channel's word on it is not current.
            warn("the last check failed (%s) - the channel may have withdrawn %s since it was "
                 "downloaded; 'boat-update check' first if in doubt"
                 % (failed.get("message", "?"), state["ready"]["version"]))
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
        dev, metered, why = link(conf)
        if not dev:
            raise UpdateError("no network (%s) - nothing to stream from" % why)
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
        # The unit's name is shared by every install, and another install's
        # reset-failed can clear its Result: the inner run reports its exit
        # status to a file only this wrapper knows.
        result = "%s-%s" % (PATHS["install_result"], secrets.token_hex(8))
        os.makedirs(os.path.dirname(result), mode=0o700, exist_ok=True)
        args = ["systemd-run", "--unit=" + unit, "--setenv=BOAT_UPDATE_INNER=1",
                "--setenv=BOAT_UPDATE_RESULT=" + result, "--description=boat-update install"] + inner
        started = int(time.time())
        r = run(args)
        if r.returncode != 0:
            raise UpdateError("could not start the install unit: %s" % r.stderr.strip())
        say("installing in the background (unit %s); following its log - "
            "losing this session does not stop it" % unit)
        # --since: the unit's journal also holds every earlier install's
        # lines, and -f would start with the last of them.
        follow = subprocess.Popen(["journalctl", "-f", "-o", "cat", "--since", "@%d" % started, "-u", unit])
        try:
            while not os.path.exists(result) \
                    and run(["systemctl", "is-active", "--quiet", unit]).returncode == 0:
                time.sleep(2)
        finally:
            time.sleep(1)
            follow.terminate()
        status = read(result).strip()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(result)
        if status != "0":
            why = ("exit status %s" % status if status else
                   "it ended without an exit status: %s" % (
                       run(["systemctl", "show", "-p", "Result", "--value", unit]).stdout.strip() or "stopped"))
            run(["systemctl", "reset-failed", unit])
            raise UpdateError("the install failed (%s) - see above; the board stays on this slot" % why)
        return
    if stream:
        started = time.time()
        version = stream_install(conf, state, slot, dev)
        # Remembered so a trial that never reaches boat-commit is still recorded.
        state["installed"] = {"version": version, "slot": other(slot)}
        remember_warnings(state, started)
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
    remember_warnings(state, started)
    save_state(state)
    say("installed: the other slot boots as a trial on the next reboot, and falls back if it fails")
    if reboot:
        say("rebooting now")
        run(["systemctl", "reboot"])


def remember_warnings(state, started):
    """Keep the slot hook's warnings with the install, so status shows them
    until its trial is decided."""
    found = install_warnings(started)
    if found:
        state["installed"]["warnings"] = found


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
RELAY_CONNECTIONS = 32    # RAUC's connections held open at once
RELAY_IDLE = 60           # seconds a connection may sit idle (curl reconnects)
RELAY_WARNINGS = 5        # refusals logged per stream; a flood is not


def relay_uids():
    """Who may use the stream relay: RAUC's streaming client, which runs as
    its sandbox user (rauc-stream), and root - not another local user, or
    a container running as one, with the channel's client certificate."""
    uids = {0, os.geteuid()}
    with contextlib.suppress(KeyError):
        uids.add(pwd.getpwnam("rauc-stream").pw_uid)
    return uids


def tcp_peer(peer, local):
    """(uid, socket inode) of the local TCP socket at peer (ip, port),
    connected to local (ip, port) - from /proc/net/tcp; (None, None) if it
    is not there."""
    def enc(addr):
        return "%s:%04X" % ("".join("%02X" % int(b) for b in reversed(addr[0].split("."))), addr[1])
    want = (enc(peer), enc(local))
    for line in read(path("net_tcp")).splitlines()[1:]:
        f = line.split()
        if len(f) > 9 and (f[1], f[2]) == want:
            return int(f[7]), f[9]
    return None, None


def socket_cgroups(inode):
    """The cgroup paths (/proc/<pid>/cgroup) of the process holding socket
    inode; an empty set if no process does. A process is found by its open
    files, which only root may list for other users' processes."""
    target = "socket:[%s]" % inode
    with contextlib.suppress(OSError):
        for pid in os.listdir(path("proc")):
            fd_dir = os.path.join(path("proc"), pid, "fd")
            if not pid.isdigit():
                continue
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                with contextlib.suppress(OSError):
                    if os.readlink(os.path.join(fd_dir, fd)) == target:
                        return {line.split(":", 2)[2] for line in
                                read(os.path.join(path("proc"), pid, "cgroup")).splitlines()
                                if line.count(":") >= 2}
    return set()


def rauc_cgroup():
    """rauc.service's cgroup, as systemd names it; "" when it is not running,
    or systemd does not answer within 10 s (the relay then refuses)."""
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        return run(["systemctl", "show", "-p", "ControlGroup", "--value", "rauc.service"],
                   timeout=10).stdout.strip()
    return ""


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
    few at a time. A uid alone does not say it is RAUC - a host-networked
    container can run as root - so the socket must also belong to a process
    in rauc.service's own cgroup (where RAUC runs its streaming client),
    which no container's process is, whatever namespaces it shares with the
    host. And the URL carries a token made for this stream. A connection
    from anyone else is closed as it is accepted, before it has a thread;
    a request that fails a check closes its connection, and an idle one is
    closed after RELAY_IDLE - so no local process can tie up the relay's
    threads or file descriptors and starve RAUC. Returns (server, the URL
    to hand RAUC)."""
    u = urllib.parse.urlsplit(url)
    expected = ("/%s%s" % (secrets.token_urlsafe(18), u.path)).encode()
    rauc = []                   # rauc.service's cgroup, read at the first request: RAUC runs by then
    ctx = ssl.create_default_context(cafile=conf["BOAT_UPDATE_SERVER_CA"] or None)
    if conf["BOAT_UPDATE_SERVER_CA"]:
        # Trust what curl --cacert trusts (curl sets this by default): a
        # server-ca.pem holding an intermediate CA, as Caddy's is.
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    if conf["BOAT_UPDATE_CLIENT_CERT"]:
        ctx.load_cert_chain(conf["BOAT_UPDATE_CLIENT_CERT"], conf["BOAT_UPDATE_CLIENT_KEY"] or None)
    local = threading.local()
    in_flight = threading.BoundedSemaphore(RELAY_CONCURRENT)
    lock = threading.Lock()
    held = set()                # the connections that passed the check
    refused = [0]

    def note_refusal(why):
        with lock:
            refused[0] += 1
            n = refused[0]
        if n <= RELAY_WARNINGS:
            warn("stream relay: %s" % why)
        elif n == RELAY_WARNINGS + 1:
            warn("stream relay: more refusals - not logged")

    def from_rauc(client_address, server_address):
        """(whether this connection's socket is held by a process of
        rauc.service, its uid)."""
        uid, inode = tcp_peer(client_address, server_address)
        if uid not in relay_uids():
            return False, uid
        if not rauc:
            rauc.append(rauc_cgroup())
        return bool(rauc[0]) and rauc[0] in socket_cgroups(inode), uid

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def verify_request(self, request, client_address):
            with lock:
                full = len(held) >= RELAY_CONNECTIONS
            if full:
                note_refusal("%d connections open already - one more refused" % RELAY_CONNECTIONS)
                return False
            ok, uid = from_rauc(client_address, self.server_address)
            if not ok:
                note_refusal("a connection from uid %s that is not RAUC's streaming client "
                             "(rauc.service) - closed" % uid)
                return False
            with lock:
                held.add(request)
            return True

        def shutdown_request(self, request):
            with lock:
                held.discard(request)
            super().shutdown_request(request)

    class Relay(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = RELAY_IDLE

        def log_message(self, *args):
            pass

        def refuse(self, code, why):
            self.send_response_only(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if code < 500:
                note_refusal(why)
            else:
                warn("stream relay: %s" % why)       # RAUC's own request, failing: always said

        def do_GET(self):
            if not hmac.compare_digest(self.path.encode("latin-1", "replace"), expected):
                return self.refuse(403, "a request without this stream's token - refused")
            m = RANGE_RE.match(self.headers.get("Range", ""))
            if not m or not 0 <= int(m.group(2)) - int(m.group(1)) < RELAY_MAX:
                return self.refuse(400, "unexpected range %r" % self.headers.get("Range"))
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

    server = Server(("127.0.0.1", 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d%s" % (server.server_address[1], expected.decode())


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
    except (UpdateError, OSError) as e:
        if isinstance(e, OSError):
            # A full or read-only /data, a file gone mid-run: a failure
            # like any other, not a traceback.
            e = UpdateError(os_error(e))
        with contextlib.suppress(OSError):
            if data_mounted():
                failed = load_state()
                failed["last_error"] = {"stage": stage, "time": int(time.time()),
                                        "message": printable(str(e))}
                save_state(failed)
        raise e


def os_error(e):
    return "%s%s" % ("%s: " % e.filename if getattr(e, "filename", None) else "", e.strerror or e)


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
    if 0 <= since < AUTO_MIN_INTERVAL:
        # Within the rate limit nothing asks the channel again; only a
        # download that is not finished goes on (a link that came back).
        if policy == "download" and state.get("offer") and not ready(state):
            do_download(conf, state, background=True)
        return
    try:
        offer = do_check(conf, state, stamp=True)
    except ClockNotSynced:
        retry_after_clock_sync()
        raise
    if offer and policy == "download":
        do_download(conf, state, background=True)


def age(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return "%d %s ago" % (s // n, unit)
    return "%d s ago" % s


def do_status(conf, state, conf_problem=None):
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
    else:
        # A full /data stops every check and download before either can
        # record why, so it is looked at here, as it is now.
        with contextlib.suppress(OSError):
            free = shutil.disk_usage(PATHS["data"]).free
            if free < FREE_FLOOR:
                print("  ALERT     /data has only %d MiB free: no download fits (it needs its size plus "
                      "2 GiB), and a full /data stops checks too" % (free >> 20))
    last = read(path("state", "last-result")).strip()
    if last:
        print("  last      %s" % last)
    if (env or {}).get("BOAT_STAY") == "1":
        print("  debug-hold is ON: failures stop for inspection instead of resetting")
    for line in (state.get("installed") or {}).get("warnings", []):
        print("  ALERT     the install of %s: %s" % (state["installed"].get("version", "?"), line))
    print("OTA")
    if conf_problem:
        print("  ALERT     %s - OTA is off until it is fixed" % conf_problem)
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
            have = file_size(os.path.join(downloads_dir(), offer["bundle"] + ".part"))
            print("  download  %s: %d of %d MiB%s" % (offer["version"], have >> 20, offer["size"] >> 20,
                                                      ", waiting: " + state["waiting"] if state.get("waiting") else ""))
            print("  transfer  download-then-install: %d MiB more; 'install --stream': only the blocks"
                  " neither slot has - typically a fraction of that (docs/07-updates.md)"
                  % ((offer["size"] - have) >> 20))
    if state.get("notes"):
        print("  notes     " + state["notes"].replace("\n", "\n            "))


def terminated(signum, frame):
    """systemctl stop sends SIGTERM, and closing the terminal SIGHUP, which
    by default end Python without running a single finally: RAUC's expected
    release left behind in /run, the install wrapper never told how the
    install ended. Unwind instead."""
    raise SystemExit(128 + signum)


def main(argv):
    rc = 1
    try:
        rc = run_verb(argv)
        return rc
    finally:
        tell_wrapper(rc)


def tell_wrapper(rc):
    """The install unit's run: leave its exit status where the wrapper that
    started it looks (BOAT_UPDATE_RESULT) - also when it was stopped."""
    report = os.environ.get("BOAT_UPDATE_RESULT")
    if report and os.environ.get("BOAT_UPDATE_INNER") == "1":
        with contextlib.suppress(OSError):
            write_atomic(report, "%d\n" % rc)


def run_verb(argv):
    args = argv[1:] or ["status"]
    verb, rest = args[0], args[1:]
    signal.signal(signal.SIGTERM, terminated)
    signal.signal(signal.SIGHUP, terminated)
    if verb in ("-h", "--help", "help"):
        print(__doc__.split("\n\n")[0].split("\n", 1)[1])
        return 0
    try:
        # channel.conf is read only by the verbs that use it: a typo in it
        # must not stop rollback, history, debug-hold or an install from a
        # file - nor status, which shows it as an alert instead.
        if verb == "status":
            note_trial_outcome()
            state = load_state() if os.path.isdir(PATHS["state"]) else {}
            try:
                conf, problem = load_conf(), None
            except UpdateError as e:
                conf, problem = dict(DEFAULTS), str(e)
            do_status(conf, state, problem)
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
            do_install(install_conf(rest), {}, files[0] if files else None, reboot="--reboot" in rest,
                       stream="--stream" in rest, allow_metered="--allow-metered" in rest)
        elif verb in ("check", "download", "auto", "install"):
            if os.geteuid() != 0:
                raise UpdateError("run as root")
            # The timer's run gives way; a person's command does not.
            with locked(interactive=verb != "auto"):
                conf = install_conf(rest) if verb == "install" else load_conf()
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
    except OSError as e:
        print("boat-update: %s" % os_error(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
