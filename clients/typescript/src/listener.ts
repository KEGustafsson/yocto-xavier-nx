/**
 * The receiving half, in TypeScript: verify a signed sleep packet and act on it.
 *
 * This is the counterpart to `sleep()` in boat-power.ts, and a peer of the
 * Python `boat-sleepd` that meta-boat actually ships to the boat. Having it
 * here makes this package a complete implementation of the protocol rather
 * than half of one - the tests can exercise a real receiver without reaching
 * outside the package, and anyone embedding this in a Node service (a
 * SignalK plugin, a panel backend) has the server side too.
 *
 * WHICH ONE RUNS ON THE BOAT
 * boat-sleepd.py does. Nothing here is installed by the Yocto image, and this
 * file does not change that: the image already carries python3, and adding a
 * ~40MB Node runtime to a 16 GiB rootfs to replace a working 300-line daemon
 * would be a poor trade. Use this when the receiver belongs inside a Node
 * process you are already running.
 *
 * The wire format is pinned by vectors.ts, which both implementations agree
 * on. If you change anything in verify() below, those vectors are what will
 * tell you whether you changed the protocol too.
 */

import { createSocket, type Socket as UdpSocket } from 'node:dgram';
import { createHmac, timingSafeEqual } from 'node:crypto';

import {
  DEFAULT_SLEEP_PORT,
  PACKET_LEN,
  parseKey,
} from './boat-power.js';

/** Why a packet was refused. Never sent back to the sender - logged only. */
export type RejectReason =
  | 'wrong-length'
  | 'bad-magic'
  | 'bad-version'
  | 'bad-signature'
  | 'stale-timestamp'
  | 'replayed-nonce'
  | 'unknown-opcode';

export interface VerifyResult {
  ok: boolean;
  reason?: RejectReason;
  /** Human-readable detail for the log. Never returned to the sender. */
  detail?: string;
  opcode?: number;
  timestamp?: number;
  nonce?: bigint;
}

const MAGIC = Buffer.from('BOATSLP1', 'ascii');
const VERSION = 1;
const OP_SLEEP = 1;
const HEADER_LEN = 28;

/** Default clock skew accepted, in seconds. Matches boat-sleepd's default. */
export const DEFAULT_WINDOW_SECONDS = 30;

/**
 * Nonces seen inside the acceptance window, so a captured packet works once.
 *
 * Bounded by the window, as in boat-sleepd - an entry older than the window is
 * already refused by the timestamp check, so it can be dropped - and ALSO by a
 * hard entry count, which boat-sleepd does not do.
 *
 * That second bound is the one piece of deliberate divergence here, and it is
 * not about the wire format. Window-only bounding means the cache grows with
 * the rate of *valid* packets, and only a key holder can produce those - so it
 * is not an unauthenticated memory attack. But "only someone with the key can
 * exhaust our heap" is a weaker property than "nobody can", it costs one
 * comparison to close, and in a long-lived Node process that also serves other
 * things, an unbounded Map is a worse neighbour than it is in a dedicated
 * daemon. Eviction is oldest-first and only ever discards entries that a
 * flood put there.
 */
export class ReplayCache {
  private readonly seen = new Map<string, number>();

  constructor(
    private readonly windowSeconds: number,
    private readonly maxEntries = 10_000,
  ) {}

  get size(): number {
    return this.seen.size;
  }

  /** True if fresh; false if already seen. Prunes expired entries as it goes. */
  checkAndAdd(nonce: bigint, nowSeconds: number): boolean {
    const cutoff = nowSeconds - this.windowSeconds;
    for (const [key, seenAt] of this.seen) {
      if (seenAt <= cutoff) this.seen.delete(key);
    }
    const key = nonce.toString(16);
    if (this.seen.has(key)) return false;

    // Map iterates in insertion order, so the first key is the oldest.
    while (this.seen.size >= this.maxEntries) {
      const oldest = this.seen.keys().next();
      if (oldest.done) break;
      this.seen.delete(oldest.value);
    }
    this.seen.set(key, nowSeconds);
    return true;
  }
}

/**
 * Validate a packet. The order of these checks is load-bearing.
 *
 * The signature is verified BEFORE the timestamp is trusted and BEFORE the
 * nonce reaches the cache. Reverse either and an unauthenticated packet can
 * reach state a real sender depends on: admit the nonce first and anyone can
 * burn the nonce a legitimate packet is about to use, denying the one command
 * you need when the boat is unreachable.
 *
 * `now` is injectable for tests. Everything else about this function is meant
 * to match boat-sleepd.py exactly; vectors.ts is what holds the two together.
 */
export function verify(
  packet: Buffer,
  key: string | Buffer,
  window: number,
  cache: ReplayCache,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): VerifyResult {
  if (packet.length !== PACKET_LEN) {
    return {
      ok: false,
      reason: 'wrong-length',
      detail: `wrong length (${packet.length}, expected ${PACKET_LEN})`,
    };
  }

  const header = packet.subarray(0, HEADER_LEN);
  const mac = packet.subarray(HEADER_LEN);

  if (!header.subarray(0, 8).equals(MAGIC)) {
    return { ok: false, reason: 'bad-magic', detail: 'bad magic' };
  }
  const version = header.readUInt8(8);
  if (version !== VERSION) {
    return {
      ok: false,
      reason: 'bad-version',
      detail: `unsupported protocol version ${version}`,
    };
  }

  // timingSafeEqual throws on a length mismatch; the length check above makes
  // both operands exactly 32 bytes, so it cannot. Never ===: an early-exiting
  // comparison leaks how much of a guessed MAC was right, which turns forgery
  // from 2^256 work into 32 * 256.
  const expected = createHmac('sha256', parseKey(key)).update(header).digest();
  if (!timingSafeEqual(mac, expected)) {
    return { ok: false, reason: 'bad-signature', detail: 'bad signature' };
  }

  const timestamp = Number(header.readBigUInt64BE(12));
  const skew = timestamp - nowSeconds;
  if (Math.abs(skew) > window) {
    return {
      ok: false,
      reason: 'stale-timestamp',
      detail:
        `timestamp ${Math.abs(skew)}s ${skew > 0 ? 'ahead of' : 'behind'} ours ` +
        `(window is ${window}s - check the clocks)`,
    };
  }

  const nonce = header.readBigUInt64BE(20);
  if (!cache.checkAndAdd(nonce, nowSeconds)) {
    return { ok: false, reason: 'replayed-nonce', detail: 'replayed nonce' };
  }

  const opcode = header.readUInt8(9);
  if (opcode !== OP_SLEEP) {
    return {
      ok: false,
      reason: 'unknown-opcode',
      detail: `unknown opcode ${opcode}`,
    };
  }

  return { ok: true, opcode, timestamp, nonce };
}

export interface ListenerOptions {
  key: string | Buffer;
  port?: number;
  /** Bind address. Default 0.0.0.0; use 127.0.0.1 in tests. */
  address?: string;
  /** Seconds of clock skew accepted. Default 30. */
  window?: number;
  /** Called once per authenticated request. Throwing is logged, not fatal. */
  onSleep: (from: { address: string; port: number }) => void | Promise<void>;
  /** Called for every refusal. Default routes it to `logger`. */
  onReject?: (
    result: VerifyResult,
    from: { address: string; port: number },
  ) => void;
  /**
   * Where log lines go. Defaults to prose on stderr, which is right for a
   * standalone process and wrong inside someone else's.
   *
   * The event is structured rather than preformatted so an embedder can do
   * what its host expects without parsing a string back apart. In a SignalK
   * plugin, for instance:
   *
   *     logger: (e) => (e.level === 'error' ? app.error(e.msg) : app.debug(e.msg))
   */
  logger?: (event: ListenerEvent) => void;
}

/** One thing worth recording. `msg` is prose; the rest is context. */
export interface ListenerEvent {
  level: 'info' | 'error';
  msg: string;
  from?: string;
  reason?: RejectReason;
  detail?: string;
  [key: string]: unknown;
}

export interface Listener {
  /** The port actually bound - useful when you asked for 0. */
  readonly port: number;
  close(): Promise<void>;
}

/**
 * Bind a UDP socket and run `onSleep` for each authenticated request.
 *
 * Answers NOTHING, ever - not to a valid packet and not to an invalid one.
 * Two reasons, and both matter more than the convenience of an ack: a UDP
 * service that replies to unverified input is a reflection amplifier, and a
 * reply that differs between "bad signature" and "replayed nonce" tells a
 * prober which of the two they achieved.
 */
export function listen(options: ListenerOptions): Promise<Listener> {
  const key = parseKey(options.key);
  const window = options.window ?? DEFAULT_WINDOW_SECONDS;
  const cache = new ReplayCache(window);

  const rawLog: (event: ListenerEvent) => void =
    options.logger ??
    ((event) => {
      const { level, msg, ...rest } = event;
      const tail = Object.entries(rest)
        .filter(([, v]) => v !== undefined)
        .map(([k, v]) => `${k}=${String(v)}`)
        .join(' ');
      console.error(`boat-sleep-listener: ${msg}${tail ? ` ${tail}` : ''}`);
      void level;
    });

  // `logger` is supplied by the embedding application, so it can throw too -
  // and a logger that throws from inside the catch below would re-escape and
  // undo the whole point. Swallowing is the correct last resort here: there is
  // nowhere left to report to.
  const log = (event: ListenerEvent): void => {
    try {
      rawLog(event);
    } catch {
      /* nothing left to log to */
    }
  };

  /**
   * Run an embedder's callback so that throwing cannot kill the host process.
   *
   * Not defensive programming for its own sake. `socket.on('message')` runs
   * these on the dgram event loop, and an exception escaping there is an
   * uncaught exception, which by default terminates Node. Every one of these
   * callbacks is reachable from an UNAUTHENTICATED packet - onReject most of
   * all, since it fires precisely on the forged and malformed ones - so
   * without this, anyone able to send a datagram can stop a SignalK server by
   * sending it rubbish. Verified: before this guard, one forged packet exited
   * the process with code 1.
   */
  const guard = (what: string, fn: () => void): void => {
    try {
      fn();
    } catch (err) {
      log({
        level: 'error',
        msg: `the ${what} callback threw`,
        detail: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const onReject =
    options.onReject ??
    ((result, from) =>
      log({
        level: 'info',
        msg: `rejected packet from ${from.address}:${from.port}`,
        reason: result.reason,
        detail: result.detail,
      }));

  return new Promise((resolveBind, rejectBind) => {
    const socket: UdpSocket = createSocket({ type: 'udp4', reuseAddr: true });

    socket.once('error', rejectBind);

    socket.on('message', (packet, rinfo) => {
      const from = { address: rinfo.address, port: rinfo.port };
      // Length is checked before anything else inside verify(): a datagram can
      // be 64KiB and ours is exactly 60, so an oversized one costs a single
      // comparison rather than an HMAC over attacker-chosen bytes.
      const result = verify(packet, key, window, cache);
      if (!result.ok) {
        guard('onReject', () => onReject(result, from));
        return;
      }
      log({
        level: 'info',
        msg: `authenticated sleep request from ${from.address}:${from.port}`,
      });
      // onSleep may be async, so its rejection needs catching separately from
      // a synchronous throw - guard() covers the latter, .catch the former.
      guard('onSleep', () => {
        void Promise.resolve(options.onSleep(from)).catch((err: unknown) =>
          log({
            level: 'error',
            msg: 'the sleep action threw',
            detail: err instanceof Error ? err.message : String(err),
          }),
        );
      });
    });

    socket.bind(options.port ?? DEFAULT_SLEEP_PORT, options.address ?? '0.0.0.0', () => {
      socket.removeListener('error', rejectBind);
      socket.on('error', (err) =>
        log({ level: 'error', msg: 'socket error', detail: err.message }));
      const bound = socket.address();
      log({
        level: 'info',
        msg: `listening on ${bound.address}:${bound.port}`,
        window,
      });
      resolveBind({
        port: bound.port,
        close: () =>
          new Promise<void>((done) => {
            socket.close(() => done());
          }),
      });
    });
  });
}
