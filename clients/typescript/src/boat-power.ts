/**
 * Wake and sleep the boat computer from TypeScript.
 *
 * Two operations that look symmetrical from the outside and are not remotely
 * symmetrical underneath:
 *
 *   wake()  sends a Wake-on-LAN magic packet. The board is OFF. Nothing on it
 *           is running. The NIC itself, still powered in a low-power state,
 *           pattern-matches the frame and asserts PME to bring the host up.
 *           There is no authentication because there is nowhere to put any -
 *           the payload is defined as the target MAC repeated sixteen times.
 *
 *   sleep() sends a signed datagram to boat-sleep-listener, a service on the
 *           RUNNING board, which verifies it and runs `boat-sleep`. There is
 *           no hardware "sleep on LAN" - no NIC filter, no ACPI primitive,
 *           nothing in the WoL spec - so this has to be a service, and being a
 *           service it has to be authenticated: an unauthenticated "suspend
 *           now" port means anyone on the marina wifi can black out the
 *           navigation computer at will.
 *
 * Zero dependencies: node:dgram and node:crypto only.
 *
 * See wol/README.md and layers/meta-boat/recipes-boat/power/ for the target
 * side. The wire format below must stay in step with boat-sleepd.py, which is
 * where the protocol is documented in full.
 */

import { createSocket } from 'node:dgram';
import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { Socket } from 'node:net';

/** Protocol v1 identifier. Present so a wrong port fails fast, not as security. */
const MAGIC = Buffer.from('BOATSLP1', 'ascii');
const VERSION = 1;
const OP_SLEEP = 1;

const HEADER_LEN = 28;
const MAC_LEN = 32;
export const PACKET_LEN = HEADER_LEN + MAC_LEN; // 60

/** Default UDP port of boat-sleep-listener. Arbitrary; set in the .socket unit. */
export const DEFAULT_SLEEP_PORT = 9099;
/** Wake-on-LAN's conventional port. Also commonly 7; the board listens on neither. */
export const DEFAULT_WOL_PORT = 9;

export interface WakeOptions {
  /** Target MAC, e.g. "48:b0:2d:15:e1:11". Separators may be ':' or '-', or absent. */
  mac: string;
  /**
   * Where to send it. 255.255.255.255 reaches the board only from the SAME
   * layer-2 segment - it is never routed. From another subnet use that
   * subnet's directed broadcast (e.g. "192.168.0.255") AND a router willing to
   * forward directed broadcasts, which most are not by default. Over the
   * internet, wake through a VPN endpoint on the boat's LAN rather than by
   * port-forwarding UDP 9.
   */
  broadcast?: string;
  port?: number;
  /**
   * A magic packet is a single unacknowledged datagram, and a switch that has
   * aged out the port or a NIC still settling after power-on will silently
   * drop it. Three costs nothing.
   */
  count?: number;
}

export interface SleepOptions {
  /** Host or IP of the board. Unicast: this one is not a broadcast. */
  host: string;
  port?: number;
  /**
   * The board's /etc/boat-sleep.key, as the hex string that file contains, or
   * the raw 32 bytes. Each board generates its own on first boot; copy it with
   *   ssh root@boat cat /etc/boat-sleep.key
   */
  key: string | Buffer;
  /** Unix seconds. Override only in tests - the board checks it against its own clock. */
  timestamp?: number;
}

/** Parse "48:b0:2d:15:e1:11", "48-b0-...", or "48b02d15e111" into 6 bytes. */
export function parseMac(mac: string): Buffer {
  const hex = mac.replace(/[:-]/g, '');
  if (!/^[0-9a-fA-F]{12}$/.test(hex)) {
    throw new Error(
      `not a MAC address: ${JSON.stringify(mac)} (expected six hex octets, ` +
        `e.g. 48:b0:2d:15:e1:11)`,
    );
  }
  return Buffer.from(hex, 'hex');
}

/**
 * Accept the key as the hex string from the file, or as raw bytes.
 *
 * Exported because listener.ts needs the identical parse: a receiver that read
 * the key even slightly differently from the sender would fail every packet
 * with "bad signature" and give no hint why.
 */
export function parseKey(key: string | Buffer): Buffer {
  if (Buffer.isBuffer(key)) return key;
  const text = key.trim();
  if (!/^[0-9a-fA-F]+$/.test(text) || text.length % 2 !== 0) {
    throw new Error(
      'the sleep key must be hex - copy /etc/boat-sleep.key from the board verbatim',
    );
  }
  const buf = Buffer.from(text, 'hex');
  if (buf.length < 16) {
    throw new Error(`the sleep key is only ${buf.length} bytes; 32 is expected`);
  }
  return buf;
}

/**
 * Build a Wake-on-LAN magic packet: 6 bytes of 0xFF, then the MAC 16 times.
 *
 * Note what is NOT in here: no timestamp, no nonce, no signature, nowhere to
 * put one. Anyone who has seen a single frame from the board knows its MAC and
 * can forge this. That is tolerable for "wake up" and is exactly why the sleep
 * direction could not be built the same way.
 */
export function buildMagicPacket(mac: string): Buffer {
  const addr = parseMac(mac);
  const packet = Buffer.alloc(6 + 16 * 6, 0xff);
  for (let i = 0; i < 16; i++) addr.copy(packet, 6 + i * 6);
  return packet;
}

/**
 * Build a signed sleep packet. 60 bytes; see boat-sleepd.py for the layout.
 *
 * The nonce is 8 random bytes and the board refuses one it has already seen
 * inside its acceptance window, so a captured packet is good exactly once -
 * and the timestamp bounds how long "once" lasts.
 */
export function buildSleepPacket(options: SleepOptions): Buffer {
  const key = parseKey(options.key);
  const header = Buffer.alloc(HEADER_LEN);
  MAGIC.copy(header, 0);
  header.writeUInt8(VERSION, 8);
  header.writeUInt8(OP_SLEEP, 9);
  header.writeUInt16BE(0, 10); // reserved
  header.writeBigUInt64BE(
    BigInt(options.timestamp ?? Math.floor(Date.now() / 1000)),
    12,
  );
  randomBytes(8).copy(header, 20); // nonce
  const mac = createHmac('sha256', key).update(header).digest();
  return Buffer.concat([header, mac]);
}

/**
 * Verify a sleep packet's signature. Provided for tests and for anyone
 * implementing the board side elsewhere; the real check lives in boat-sleepd.
 *
 * timingSafeEqual, not ===, and it is not paranoia dressed as rigour: a
 * byte-at-a-time comparison that returns early leaks how much of a guessed MAC
 * was right, which turns forgery from 2^256 work into 32 * 256.
 */
export function verifySleepPacket(packet: Buffer, key: string | Buffer): boolean {
  if (packet.length !== PACKET_LEN) return false;
  const header = packet.subarray(0, HEADER_LEN);
  const mac = packet.subarray(HEADER_LEN);
  if (!header.subarray(0, 8).equals(MAGIC)) return false;
  const expected = createHmac('sha256', parseKey(key)).update(header).digest();
  return timingSafeEqual(mac, expected);
}

/** Send one datagram and resolve when the OS has accepted it for delivery. */
function send(
  packet: Buffer,
  host: string,
  port: number,
  broadcast: boolean,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const socket = createSocket('udp4');
    socket.once('error', (err) => {
      socket.close();
      reject(err);
    });
    socket.bind(() => {
      // SO_BROADCAST is off by default; without it a send to a broadcast
      // address fails with EACCES rather than going anywhere.
      if (broadcast) socket.setBroadcast(true);
      socket.send(packet, port, host, (err) => {
        socket.close();
        if (err) reject(err);
        else resolve();
      });
    });
  });
}

/**
 * Wake the board.
 *
 * Resolves once the packets are away. That is all it can tell you: a magic
 * packet is unacknowledged, and the board is not running any software that
 * could answer. To know whether it worked, poll for the board coming back -
 * ICMP, or a TCP connect to 22.
 */
export async function wake(options: WakeOptions): Promise<void> {
  const packet = buildMagicPacket(options.mac);
  const host = options.broadcast ?? '255.255.255.255';
  const port = options.port ?? DEFAULT_WOL_PORT;
  const count = options.count ?? 3;
  for (let i = 0; i < count; i++) {
    await send(packet, host, port, true);
  }
}

/**
 * Ask the board to suspend.
 *
 * Resolves once the packet is away. The listener answers nothing at all -
 * valid or not - so that it cannot be used as a reflection amplifier and so a
 * prober cannot tell "bad signature" from "replayed nonce". The board going
 * quiet is the only confirmation there is; `waitUntilDown` below is one way to
 * watch for it.
 *
 * A rejected or refused request is not silent everywhere, just here: the
 * board's journal has the reason.
 *   journalctl -u boat-sleep-listener -n 20
 */
export async function sleep(options: SleepOptions): Promise<void> {
  const packet = buildSleepPacket(options);
  await send(packet, options.host, options.port ?? DEFAULT_SLEEP_PORT, false);
}

/**
 * Poll a TCP port until it stops accepting (board asleep) or starts (board up).
 *
 * TCP rather than ICMP because raw sockets need privilege in Node and ping(8)
 * is not portable to shell out to. Port 22 is the usual choice: sshd is up
 * whenever the board is. A board that is merely dropping the port - a firewall
 * - looks identical to one that is asleep, which is worth knowing before
 * trusting this as proof of anything.
 */
export function waitForPort(
  host: string,
  port: number,
  want: 'open' | 'closed',
  timeoutMs: number,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;

  const probe = (): Promise<boolean> =>
    new Promise((resolve) => {
      const socket = new Socket();
      const done = (open: boolean) => {
        socket.destroy();
        resolve(open);
      };
      socket.setTimeout(1000);
      socket.once('connect', () => done(true));
      socket.once('timeout', () => done(false));
      socket.once('error', () => done(false));
      socket.connect(port, host);
    });

  return (async () => {
    for (;;) {
      const open = await probe();
      if ((want === 'open') === open) return true;
      if (Date.now() >= deadline) return false;
      await new Promise((r) => setTimeout(r, 1000));
    }
  })();
}
