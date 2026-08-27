# `wol/` — sleep and wake the boat computer from this machine

Two commands for the remote power cycle: put the board into SC7 deep sleep,
and wake it again with a Wake-on-LAN magic packet.

```bash
cp wol/boat.conf.example wol/boat.conf   # fill in your board's address + MAC
wol/boat-sleep.sh                        # suspend, and confirm it went quiet
wol/boat-wake.sh                         # wake it, and wait until it answers
```

`wol/boat.conf` is git-ignored — it holds this particular boat's MAC and LAN
addresses, which have no business in a pushed repository. Anything in it can
also be overridden per-invocation:

```bash
BOAT_HOST=192.168.0.99 wol/boat-wake.sh
```

The environment always wins over the file, including for the `BOAT_WOL_*`
sender knobs and including when you deliberately pass an empty value.
`scripts/wake-boat.sh` reads the same `wol/boat.conf`, so running the sender
on its own honours the broadcast address you configured rather than falling
back to `255.255.255.255`.

## What each one does

| | |
|---|---|
| `boat-sleep.sh` | Runs `boat-sleep` on the board over SSH, then watches from outside until it stops answering |
| `boat-sleep-udp.sh` | Same thing without SSH: one signed UDP packet to `boat-sleep-listener` on the board |
| `boat-wake.sh` | Sends the magic packet, then waits until it answers again |
| `common.sh` | Shared config loading and the wait helpers; sourced, not run |
| `boat.conf.example` | Template with every setting explained |

Both scripts are deliberately thin. The decisions live on the board, in
`boat-sleep`, which refuses to suspend unless something can wake it again;
the packet is built by `scripts/wake-boat.sh`, so there is one sender to keep
correct. What these add is the part you cannot see from the far end —
watching from outside to find out whether it *actually* slept, and whether it
*actually* came back. `boat-sleep`'s own SSH connection dies either way, so
its exit status cannot tell you, and nothing acknowledges a magic packet.

Useful flags, all passed straight through to `boat-sleep` on the board:
`--status` (report readiness, suspend nothing), `--dry-run` (run every check,
stop before suspending), `--force` (sleep even with Wake-on-LAN unarmed — it
will not wake over Ethernet), `--delay N`. Plus `--no-wait` on either script,
which is local: send the request and return rather than watching what happens.

`--status` and `--dry-run` skip the "did it go quiet?" wait, because nothing
was asked to go down — and both also skip the ICMP liveness check the
suspending modes do first. That makes it usable on a board that does not answer
ping: one behind a firewall that drops ICMP, or one you are not sure about.
It does **not** make it usable on a board that is off or already asleep — it
still runs `boat-sleep` over SSH, so SSH has to reach the board. On a sleeping
board, wake it first.

## Sleeping without SSH

`wol/boat-sleep-udp.sh` does what `boat-sleep.sh` does, with one UDP packet
instead of a login:

```bash
ssh root@boat cat /etc/boat-sleep.key > wol/boat-sleep.key   # once, per boat
chmod 0600 wol/boat-sleep.key
wol/boat-sleep-udp.sh
```

### Why it is not a magic packet

The obvious question is why sleeping cannot work the way waking does. It
cannot, and the reason is worth stating because it decides everything else
about this:

**Waking happens inside the NIC.** The board is off. Nothing on it is running.
The NIC stays powered in a low-power state, pattern-matches the magic packet in
its own firmware, and asserts PME to bring the host up.

**Sleeping has no hardware counterpart.** There is no "sleep-on-LAN" in the WoL
spec, in ACPI, or in any NIC's filter engine. When the host is up, the NIC just
hands frames to the kernel. A magic packet aimed at a running Xavier is an
ordinary broadcast frame that lands nowhere.

So the sleep direction has to be a service on the running board — and being a
service, it has to be authenticated. A magic packet carries no secret at all:
it is the target MAC repeated sixteen times, forgeable by anyone who has seen a
single frame from the board. An unauthenticated "suspend now" port would mean
any guest on the marina wifi can black out the navigation computer at will.

Every packet therefore carries an HMAC-SHA256 over a timestamp and a nonce,
keyed by `/etc/boat-sleep.key` — 32 random bytes the board generates for itself
on first boot, never baked into the image. The board checks the signature in
constant time first, then the timestamp against a 30-second window, then the
nonce against everything it has seen inside that window. A captured packet is
good exactly once, and only for 30 seconds.

Nothing is ever sent back — not to a valid packet and not to an invalid one —
so the port cannot be used as a reflection amplifier and a prober cannot tell
"bad signature" from "replayed nonce". The board going quiet is the only
confirmation there is, which is why `boat-sleep-udp.sh` watches for it by
default. When it does not go quiet, the reason is in the board's journal:

```bash
ssh root@boat journalctl -u boat-sleep-listener -n 20
```

### Which one to use

| | over SSH (`boat-sleep.sh`) | signed UDP (`boat-sleep-udp.sh`) |
|---|---|---|
| Needs | an SSH credential | `wol/boat-sleep.key` |
| After a reflash | host key changed — fails until you clear it | keeps working |
| Tells you *why* it refused | yes, on your terminal | no, only in the board's journal |
| Works when sshd is down | no | yes |
| Network surface | sshd, already open | one more UDP port (9099) |

`boat-sleep.sh` remains the one to reach for when something is wrong, because
it can answer `--status`. `boat-sleep-udp.sh` is the one for scripts, phones
and boat-panel buttons, where re-accepting a host key is not something the
caller can do.

Change the port in `boat-sleep-listener.socket` on the board
(`systemctl edit boat-sleep-listener.socket`) and set `BOAT_SLEEP_PORT` here to
match. `wol/boat-sleep.key` is git-ignored, like `wol/boat.conf` and rather
more so.

## Other clients

`clients/typescript/` is a dependency-free TypeScript implementation of both
directions — `wake()` builds the magic packet, `sleep()` builds the signed
packet — for a phone app, a boat-panel web UI, or a Node service. Its
`cross-check.mjs` runs the real `boat-sleepd.py` on loopback and verifies that
what it sends is accepted and that a magic packet is not.

`BOAT_SSH_USER` may be `root` or `boat`; for anything other than root the
wrapper prefixes `sudo -n`, which works because the `boat` account has
passwordless sudo. SSH runs with `BatchMode=yes`, so a changed host key (which
reflashing guarantees) fails immediately instead of hanging on a prompt — set
`BOAT_SSH_OPTS` to point at a separate `known_hosts` while a board is being
reflashed.

## Measured behaviour

On a Jetson Xavier NX devkit, both directions confirmed on hardware:

```
18:58:37  boat-sleep.sh  → SC7
18:58:46  board silent
18:58:58  boat-wake.sh   → magic packet to 192.168.0.255
18:59:07  awake  (~7 seconds)
```

The kernel names the cause on resume, which is the only real proof the packet
did it rather than something else:

```
tegra-pmc: Resume caused by WAKE20, 2490000.ethernet:01
```

## When the wake does nothing

In the order these actually go wrong:

1. **Wake-on-LAN was not armed at the moment it slept.** `ethtool eth0 | grep
   Wake-on` must read `g`, not `d`. Note NetworkManager clears this flag on
   every re-activation, which is why the image re-arms from an NM dispatcher
   script rather than only at boot — see
   [`../docs/05-phase2-boat-computer-layer.md`](../docs/05-phase2-boat-computer-layer.md)
   under "Power".
2. **This machine is not on the boat's layer-2 segment.**
   `255.255.255.255` is never routed. A directed broadcast
   (`BOAT_BROADCAST=192.168.0.255`) crosses subnets only if the router
   forwards directed broadcasts, and most do not. From ashore, wake through a
   VPN endpoint on the boat's LAN — `wireguard-tools` is on the image for
   exactly this.
3. **The board is off, not asleep.** Nothing wakes a powered-down board over
   Ethernet.

Wi-Fi cannot be used for this: the image arms Wake-on-LAN on Ethernet only,
and `wlan0` on this hardware does not support it.
