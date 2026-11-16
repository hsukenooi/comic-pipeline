import { describe, it, expect, vi, afterEach } from "vitest";
import {
  checkListResponse,
  listOrders,
  ORDER_PAGE_SIZE,
  SessionExpiredError,
} from "../src/api.js";
import type { Config } from "../src/types.js";

const CONFIG: Config = {
  cookie: "session=abc",
  userAgent: "UA",
  apiBaseUrl: "https://api.test",
};

const OK = { result: true, msg: "ok" };

function order(n: number) {
  return {
    id: `id${n}`,
    orderNumber: `SG${n}`,
    status: "Complete_Code",
    createdDate: "1787817261",
    carrierCompany: { trackingNo: `TRK${n}` },
  };
}

// Answers each request from `pages[dateGap][offset / page size]`.
function mockPages(pages: Record<string, unknown[][]>) {
  const fn = vi.fn(async (_url: string, init: { body: string }) => {
    const body = JSON.parse(init.body);
    const list = pages[body.dateGap] ?? [];
    const orders = list[body.offset / ORDER_PAGE_SIZE] ?? [];
    const json = { result: OK, orderGroups: [{ orders }] };
    return {
      status: 200,
      ok: true,
      headers: { get: () => null },
      json: async () => json,
      text: async () => JSON.stringify(json),
    };
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("checkListResponse (BUI-1378)", () => {
  it("accepts the nested result.result:true", () => {
    const r = { result: OK, orderGroups: [] };
    expect(checkListResponse(r)).toBe(r);
  });

  it("rejects a top-level result:true, which is the submit shape", () => {
    expect(() => checkListResponse({ result: true })).toThrow(/result\.result/);
  });

  it("rejects a nested result:false", () => {
    expect(() =>
      checkListResponse({ result: { result: false, msg: "busy" } })
    ).toThrow(/busy/);
  });

  it("maps a log-in message to SessionExpiredError", () => {
    expect(() =>
      checkListResponse({ result: { result: false, msg: "Please log in first" } })
    ).toThrow(SessionExpiredError);
  });
});

describe("listOrders (BUI-1378)", () => {
  it("pages both date ranges and dedups by id", async () => {
    const full = Array.from({ length: ORDER_PAGE_SIZE }, (_, i) => order(i));
    const fn = mockPages({
      DateGapLatestThreeMonth: [full, [order(100)]],
      DateGapBeforeThreeMonth: [[order(0), order(200)]],
    });
    const orders = await listOrders(CONFIG);
    expect(orders).toHaveLength(ORDER_PAGE_SIZE + 2);
    expect(orders.find((o) => o.orderNumber === "SG200")?.trackingNo).toBe("TRK200");
    const bodies = fn.mock.calls.map((c) => JSON.parse(c[1].body));
    expect(bodies).toEqual([
      { dateGap: "DateGapLatestThreeMonth", offset: 0, limit: ORDER_PAGE_SIZE },
      { dateGap: "DateGapLatestThreeMonth", offset: ORDER_PAGE_SIZE, limit: ORDER_PAGE_SIZE },
      { dateGap: "DateGapBeforeThreeMonth", offset: 0, limit: ORDER_PAGE_SIZE },
    ]);
  });

  it("only ever calls the read-only list endpoint", async () => {
    const fn = mockPages({});
    await listOrders(CONFIG);
    for (const call of fn.mock.calls) {
      expect(call[0]).toBe("https://api.test/ezShipOrder.OrderExpress/GetOrderListByStatus");
    }
  });

  it("refuses a partial list when the server repeats a full page forever", async () => {
    const full = Array.from({ length: ORDER_PAGE_SIZE }, (_, i) => order(i));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        status: 200,
        ok: true,
        headers: { get: () => null },
        json: async () => ({ result: OK, orderGroups: [{ orders: full }] }),
      }))
    );
    await expect(listOrders(CONFIG)).rejects.toThrow(/refusing a partial list/);
  });
});
