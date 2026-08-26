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
was asked to go down — and `--status` also skips the ICMP liveness check the
other modes do first. That makes it usable on a board that does not answer
ping: one behind a firewall that drops ICMP, or one you are not sure about.
It does **not** make it usable on a board that is off or already asleep — it
still runs `boat-sleep` over SSH, so SSH has to reach the board. On a sleeping
board, wake it first.

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
