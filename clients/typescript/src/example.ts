/**
 * Runnable example: wake the boat, wait for it, then put it back to sleep.
 *
 *   npm install && npm run build
 *   BOAT_HOST=192.168.1.42 BOAT_MAC=48:b0:2d:11:22:33 \
 *     BOAT_SLEEP_KEY_FILE=~/.config/boat/sleep.key node dist/esm/example.js wake
 *
 * Subcommands: wake | sleep | packet
 */

import { readFileSync } from 'node:fs';
import { DEFAULT_SLEEP_PORT, buildSleepPacket, sleep, wake, waitForPort } from './boat-power.js';

function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set`);
  return value;
}

/**
 * Read the key from a FILE, not from an environment variable.
 *
 * An env var is readable from /proc/<pid>/environ, is inherited by every child
 * process, and lands in shell history and in CI logs. A 0600 file is the same
 * posture the board and the shell sender already use, so all three agree.
 */
function readKey(): string {
  return readFileSync(env('BOAT_SLEEP_KEY_FILE'), 'utf8').trim();
}

async function main(): Promise<void> {
  const command = process.argv[2] ?? 'packet';

  switch (command) {
    case 'wake': {
      const mac = env('BOAT_MAC');
      await wake({ mac, broadcast: process.env.BOAT_BROADCAST });
      console.log(`magic packet sent to ${mac}`);
      // Nothing acknowledges a magic packet, so ask the board instead.
      const host = env('BOAT_HOST');
      const up = await waitForPort(host, 22, 'open', 90_000);
      console.log(up ? `${host} is up` : `${host} did not answer within 90s`);
      break;
    }

    case 'sleep': {
      const host = env('BOAT_HOST');
      await sleep({ host, key: readKey(), port: sleepPort() });
      console.log(`signed sleep request sent to ${host}:${sleepPort()}`);
      const down = await waitForPort(host, 22, 'closed', 60_000);
      console.log(
        down
          ? `${host} went quiet`
          : `${host} is still answering - see: journalctl -u boat-sleep-listener`,
      );
      break;
    }

    case 'packet': {
      // Print the packet without sending it: useful for checking that this
      // client and the board agree before pointing it at a real boat.
      const packet = buildSleepPacket({ host: 'unused', key: readKey() });
      console.log(packet.toString('hex'));
      break;
    }

    default:
      throw new Error(`unknown command ${JSON.stringify(command)} (wake | sleep | packet)`);
  }
}

function sleepPort(): number {
  return Number(process.env.BOAT_SLEEP_PORT ?? DEFAULT_SLEEP_PORT);
}

main().catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
