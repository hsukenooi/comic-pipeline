import { describe, it, expect, vi, afterEach } from "vitest";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mkdtempSync } from "node:fs";
import {
  findSubmittedOrder,
  recordSubmittedOrder,
} from "../src/order-ledger.js";
import { submitNewOrder } from "../src/api.js";
import type { Config } from "../src/types.js";

const CONFIG: Config = {
  cookie: "session=abc",
  userAgent: "UA",
  apiBaseUrl: "https://api.test",
};

function tmpLedger(): string {
  const dir = mkdtempSync(join(tmpdir(), "ezship-ledger-"));
  return join(dir, "submitted-orders.json");
}

function mockFetchJson(status: number, json: unknown): typeof fetch {
  return vi.fn(async () => ({
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    json: async () => json,
    text: async () => JSON.stringify(json),
  })) as unknown as typeof fetch;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("order ledger (BUI-180)", () => {
  it("round-trips a recorded submission", () => {
    const path = tmpLedger();
    expect(findSubmittedOrder("TRK1", path)).toBeUndefined();
    recordSubmittedOrder("TRK1", { orderId: "O1" }, path);
    const got = findSubmittedOrder("TRK1", path);
    expect(got?.orderId).toBe("O1");
    expect(typeof got?.submittedAt).toBe("string");
  });

  it("a missing ledger file reads as empty (not an error)", () => {
    const path = join(tmpdir(), `ezship-missing-${Date.now()}`, "l.json");
    expect(findSubmittedOrder("X", path)).toBeUndefined();
  });
});

describe("submitNewOrder dedup (BUI-180)", () => {
  const opts = {
    trackingNo: "DUP123",
    warehouse: "usa",
    carrierName: "UPS",
    carrierId: "58",
  };

  it("records the tracking number on a successful submit", async () => {
    const path = tmpLedger();
    vi.stubGlobal(
      "fetch",
      mockFetchJson(200, { result: true, data: { orderId: "ORD-9" } })
    );
    await submitNewOrder(CONFIG, opts, path);
    expect(findSubmittedOrder("DUP123", path)?.orderId).toBe("ORD-9");
  });

  it("refuses a duplicate submit and does not POST again", async () => {
    const path = tmpLedger();
    const fetchMock = mockFetchJson(200, {
      result: true,
      data: { orderId: "ORD-9" },
    });
    vi.stubGlobal("fetch", fetchMock);
    await submitNewOrder(CONFIG, opts, path);
    // Second attempt with the same tracking number must throw — no new POST.
    await expect(submitNewOrder(CONFIG, opts, path)).rejects.toThrow(
      /already submitted/
    );
    expect((fetchMock as unknown as { mock: { calls: unknown[] } }).mock.calls.length).toBe(1);
  });
});
