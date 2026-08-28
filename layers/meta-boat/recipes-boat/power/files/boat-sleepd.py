#!/usr/bin/env python3
"""Authenticated "suspend now" listener for the boat computer.

Wake-on-LAN is asymmetric: waking happens inside the NIC, which pattern-matches
a magic packet while the host is in SC7 and asserts PME to bring it up. There
is no hardware counterpart for sleeping - no NIC filter, no ACPI primitive, and
nothing in the WoL spec - because the OS is running and the NIC simply hands
frames to the kernel. So "sleep by magic packet" cannot exist as such.

What CAN exist is the same shape one layer up: a single UDP datagram that puts
the board to sleep, with no SSH session, no login, and no host key to churn
every time the board is reflashed. That is this. It is deliberately NOT a magic
packet, because a magic packet carries no secret at all - it is the target MAC
repeated sixteen times, which anyone who has seen one frame from the board can
forge. Broadcasting "go to sleep" to a marina wifi on those terms would mean
any guest on the network can suspend the boat's navigation computer at will.

Every packet therefore carries an HMAC-SHA256 over a timestamp and a nonce,
keyed by a secret shared with the sender. The MAC is checked first and in
constant time; the timestamp bounds replay to a short window; the nonce cache
kills replay inside that window.

The action itself is delegated: this only ever execs `boat-sleep`, which
already refuses to suspend unless something can wake the board again. This
program deliberately does not know how to suspend anything.

Protocol v1, 60 bytes, all integers big-endian:

    offset  size  field
    0       8     magic      b"BOATSLP1"
    8       1     version    0x01
    9       1     opcode     0x01 = sleep
    10      2     reserved   0x0000
    12      8     timestamp  unix seconds
    20      8     nonce      random
    28      32    mac        HMAC-SHA256(key, packet[0:28])

Run by boat-sleep-listener.service, socket-activated by
boat-sleep-listener.socket. For testing it can also bind the port itself:

    boat-sleepd --port 9099 --key-file ./test.key --dry-run
"""

import argparse
import hashlib
import hmac
import os
import socket
import struct
import subprocess
import sys
import time

MAGIC = b"BOATSLP1"
VERSION = 1
OP_SLEEP = 1

HEADER_FMT = "!8sBBHQQ"          # magic, version, opcode, reserved, ts, nonce
HEADER_LEN = struct.calcsize(HEADER_FMT)   # 28
MAC_LEN = 32
PACKET_LEN = HEADER_LEN + MAC_LEN          # 60

# systemd hands the first listening socket to the service as fd 3.
SD_LISTEN_FDS_START = 3


def log(msg):
    """Write one line to stderr, which under systemd is the journal."""
    print(msg, file=sys.stderr, flush=True)


def load_key(path):
    """Read the shared secret from *path* and return it as raw bytes.

    The file holds a hex string - `openssl rand -hex 32 > /etc/boat-sleep.key`.
    Hex rather than raw bytes so the file stays copy-pasteable into a sender's
    config and survives being edited by an editor that insists on a trailing
    newline; the whole point is that both ends can be configured by hand
    without a binary-safe transport between them.

    Refuses a key that any user other than the owner can read: a shared secret
    in a world-readable file is not a shared secret, and failing loudly here
    beats running for months in a state where every local account can suspend
    the boat.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise SystemExit(
            f"boat-sleepd: no key at {path}. Each board generates its own on "
            f"first boot; make one with: openssl rand -hex 32 > {path} && "
            f"chmod 0600 {path}"
        )
    except OSError as exc:
        raise SystemExit(f"boat-sleepd: cannot read {path}: {exc}")
    if st.st_mode & 0o077:
        raise SystemExit(
            f"boat-sleepd: {path} is mode {st.st_mode & 0o777:04o} - it must not be "
            f"readable by group or others. Fix with: chmod 0600 {path}"
        )
    with open(path, "r") as fh:
        text = fh.read().strip()
    if not text:
        raise SystemExit(f"boat-sleepd: {path} is empty - generate one with: "
                         f"openssl rand -hex 32 > {path}")
    try:
        key = bytes.fromhex(text)
    except ValueError:
        raise SystemExit(
            f"boat-sleepd: {path} is not hex. Generate one with: "
            f"openssl rand -hex 32 > {path}"
        )
    if len(key) < 16:
        raise SystemExit(
            f"boat-sleepd: the key in {path} is {len(key)} bytes; 32 is expected "
            f"and fewer than 16 is refused. openssl rand -hex 32 > {path}"
        )
    return key


def build_packet(key, opcode=OP_SLEEP, timestamp=None, nonce=None):
    """Return a signed 60-byte packet. Shared with the test suite and senders."""
    if timestamp is None:
        timestamp = int(time.time())
    if nonce is None:
        nonce = int.from_bytes(os.urandom(8), "big")
    header = struct.pack(HEADER_FMT, MAGIC, VERSION, opcode, 0, timestamp, nonce)
    return header + hmac.new(key, header, hashlib.sha256).digest()


class ReplayCache:
    """Nonces seen inside the acceptance window, so a captured packet is usable once.

    Bounded by the window rather than by count: an entry older than the window
    is already rejected by the timestamp check, so it can be dropped. That
    makes the cache self-limiting without a maximum size to tune - the only way
    to grow it is to send valid packets, which requires the key.
    """

    def __init__(self, window):
        self._window = window
        self._seen = {}

    def check_and_add(self, nonce, now):
        """True if *nonce* is fresh; False if it has been seen. Prunes as it goes."""
        cutoff = now - self._window
        if self._seen:
            self._seen = {n: t for n, t in self._seen.items() if t > cutoff}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


def verify(packet, key, window, cache, now=None):
    """Validate *packet*; return (opcode, None) if good or (None, reason) if not.

    Order is deliberate. The MAC is checked before the timestamp and before the
    nonce is admitted to the cache, so an unauthenticated packet can neither
    steer the clock comparison nor add an entry to a table that a valid sender
    depends on.
    """
    if now is None:
        now = int(time.time())
    if len(packet) != PACKET_LEN:
        return None, f"wrong length ({len(packet)}, expected {PACKET_LEN})"

    header, mac = packet[:HEADER_LEN], packet[HEADER_LEN:]
    magic, version, opcode, _reserved, timestamp, nonce = struct.unpack(
        HEADER_FMT, header)

    if magic != MAGIC:
        return None, "bad magic"
    if version != VERSION:
        return None, f"unsupported protocol version {version}"

    expected = hmac.new(key, header, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return None, "bad signature"

    skew = timestamp - now
    if abs(skew) > window:
        return None, (f"timestamp {abs(skew)}s "
                      f"{'ahead of' if skew > 0 else 'behind'} ours "
                      f"(window is {window}s - check the clocks)")

    if not cache.check_and_add(nonce, now):
        return None, "replayed nonce"

    if opcode != OP_SLEEP:
        return None, f"unknown opcode {opcode}"

    return opcode, None


def listen_socket(port, bind_addr):
    """Return the socket to read from: systemd's if activated, else a fresh bind.

    LISTEN_PID guards against a passed-down fd being mistaken for ours in a
    child process; systemd sets it to the PID it started, so a fork that
    inherits the variable does not also inherit the claim to the socket.
    """
    if os.environ.get("LISTEN_PID") == str(os.getpid()):
        count = int(os.environ.get("LISTEN_FDS", "0"))
        if count >= 1:
            sock = socket.socket(fileno=SD_LISTEN_FDS_START)
            log(f"boat-sleepd: using socket-activated fd {SD_LISTEN_FDS_START}")
            return sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_addr, port))
    log(f"boat-sleepd: listening on {bind_addr}:{port}")
    return sock


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="boat-sleepd",
        description="Suspend the boat computer on receipt of one signed UDP packet.")
    ap.add_argument("--key-file", default=os.environ.get(
        "BOAT_SLEEP_KEY_FILE", "/etc/boat-sleep.key"))
    ap.add_argument("--port", type=int, default=int(os.environ.get(
        "BOAT_SLEEP_PORT", "9099")), help="ignored when socket-activated")
    ap.add_argument("--bind", default=os.environ.get("BOAT_SLEEP_BIND", "0.0.0.0"),
                    help="ignored when socket-activated")
    ap.add_argument("--window", type=int, default=int(os.environ.get(
        "BOAT_SLEEP_WINDOW", "30")),
        help="seconds of clock skew to accept (default 30)")
    ap.add_argument("--command", default=os.environ.get(
        "BOAT_SLEEP_COMMAND", "/usr/bin/boat-sleep"))
    ap.add_argument("--args", default=os.environ.get("BOAT_SLEEP_ARGS", ""),
                    help="extra arguments for boat-sleep, split on whitespace")
    ap.add_argument("--dry-run", action="store_true",
                    help="log what would happen, suspend nothing")
    args = ap.parse_args(argv)

    if args.window < 1:
        raise SystemExit("boat-sleepd: --window must be at least 1 second")

    key = load_key(args.key_file)
    cache = ReplayCache(args.window)
    sock = listen_socket(args.port, args.bind)
    command = [args.command] + args.args.split()

    log(f"boat-sleepd: ready; window {args.window}s, action: {' '.join(command)}"
        + (" (dry run)" if args.dry_run else ""))

    while True:
        try:
            packet, peer = sock.recvfrom(4096)
        except InterruptedError:
            continue
        except OSError as exc:
            log(f"boat-sleepd: socket error: {exc}")
            return 1

        # A packet that arrived while the board was suspended is read here on
        # resume, because the socket buffer outlives the suspend. The timestamp
        # check is what stops that from bouncing the board straight back to
        # sleep: anything older than the window is refused, so the worst case
        # is a genuine request sent seconds before resume being honoured, which
        # is what the sender asked for.
        where = f"{peer[0]}:{peer[1]}"
        opcode, reason = verify(packet, key, args.window, cache)
        if opcode is None:
            # Deliberately terse and deliberately not echoed back: an attacker
            # who can see which of "bad signature" and "replayed nonce" they
            # got learns something, and a UDP service that answers unverified
            # packets is a reflection amplifier. The journal gets the detail;
            # the sender gets silence either way.
            log(f"boat-sleepd: rejected packet from {where}: {reason}")
            continue

        log(f"boat-sleepd: authenticated sleep request from {where}")
        if args.dry_run:
            log(f"boat-sleepd: dry run - not running {' '.join(command)}")
            continue

        try:
            result = subprocess.run(command, check=False)
        except OSError as exc:
            log(f"boat-sleepd: could not run {command[0]}: {exc}")
            continue
        if result.returncode == 0:
            # boat-sleep returns as soon as logind accepts the request, so this
            # means "accepted", not "the board is now asleep". Nothing here
            # waits for that: the process is about to be frozen with the rest
            # of userspace, and on resume the loop simply continues.
            log("boat-sleepd: boat-sleep accepted the request")
        else:
            log(f"boat-sleepd: boat-sleep refused (exit {result.returncode}) - "
                f"it logs why; --status over SSH shows the same checks")


if __name__ == "__main__":
    sys.exit(main() or 0)
