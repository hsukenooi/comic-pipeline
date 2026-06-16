import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";

/**
 * BUI-180: a local ledger of tracking numbers we've already submitted, so a
 * re-run of `ezship new -t <same tracking>` does not POST a second real
 * shipment. EZShip exposes no idempotency token and no order-query endpoint, so
 * this client-side ledger is the implementable dedup: it prevents the common
 * "re-run after a known success" double-submit. The genuinely ambiguous case (a
 * connection dropped *after* the server accepted, where we never learned it
 * succeeded) cannot be closed without server support and is documented as such.
 */

export const LEDGER_PATH = join(
  homedir(),
  ".config",
  "ezship",
  "submitted-orders.json"
);

export interface SubmittedOrder {
  submittedAt: string;
  orderId?: string;
}

type Ledger = Record<string, SubmittedOrder>;

function readLedger(path: string): Ledger {
  let raw: string;
  try {
    raw = readFileSync(path, "utf-8");
  } catch {
    return {}; // no ledger yet
  }
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Ledger) : {};
  } catch {
    // A corrupt ledger must not block submission silently AND must not be
    // treated as "everything already submitted" — start clean.
    return {};
  }
}

/** Return the prior submission for a tracking number, or undefined. */
export function findSubmittedOrder(
  trackingNo: string,
  path: string = LEDGER_PATH
): SubmittedOrder | undefined {
  return readLedger(path)[trackingNo];
}

/** Record a successful submission so a re-run won't duplicate it. */
export function recordSubmittedOrder(
  trackingNo: string,
  info: { orderId?: string },
  path: string = LEDGER_PATH
): void {
  const ledger = readLedger(path);
  ledger[trackingNo] = {
    submittedAt: new Date().toISOString(),
    orderId: info.orderId,
  };
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(ledger, null, 2) + "\n", "utf-8");
}
