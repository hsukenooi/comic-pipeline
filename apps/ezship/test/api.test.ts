import { describe, it, expect, vi, afterEach } from "vitest";
import { callRpc } from "../src/api.js";
import type { Config } from "../src/types.js";

const CONFIG: Config = {
  cookie: "session=abc",
  userAgent: "UA",
  apiBaseUrl: "https://api.test",
};

function mockFetchJson(status: number, json: unknown): typeof fetch {
  return vi.fn(async () => ({
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    json: async () => json,
    text: async () => JSON.stringify(json),
  })) as unknown as typeof fetch;
}

function mockFetchRedirect(status: number, location: string): typeof fetch {
  return vi.fn(async () => ({
    status,
    ok: false,
    headers: {
      get: (h: string) => (h.toLowerCase() === "location" ? location : null),
    },
    json: async () => ({}),
    text: async () => "",
  })) as unknown as typeof fetch;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("callRpc business-layer rejection handling (BUI-141)", () => {
  it("throws on a result:false rejection so the CLI exits non-zero", async () => {
    // A 200 body carrying result:false is a business-layer rejection — it must
    // NOT be returned (which let the skill mark a rejected order "Submitted").
    vi.stubGlobal(
      "fetch",
      mockFetchJson(200, { result: false, msg: "declared value below minimum" })
    );
    await expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(
      /declared value below minimum/
    );
  });

  it("still throws the session-expired guidance on a login rejection", async () => {
    vi.stubGlobal("fetch", mockFetchJson(200, { result: false, msg: "please login" }));
    await expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(/set-cookie/);
  });

  it("returns a successful body unchanged", async () => {
    vi.stubGlobal("fetch", mockFetchJson(200, { result: true, data: { id: 1 } }));
    const r = (await callRpc(CONFIG, "ep", {})) as Record<string, unknown>;
    expect(r.result).toBe(true);
  });
});

describe("callRpc requires an explicit result:true success (BUI-181)", () => {
  it("throws on an error-shaped 200 body with no result field", async () => {
    vi.stubGlobal("fetch", mockFetchJson(200, { error: "duplicate tracking" }));
    await expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(/duplicate tracking/);
  });

  it("throws on a string 'false' result (not a real boolean success)", async () => {
    vi.stubGlobal("fetch", mockFetchJson(200, { result: "false" }));
    await expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(/did not confirm success/);
  });

  it("throws on a body omitting result entirely", async () => {
    vi.stubGlobal("fetch", mockFetchJson(200, { data: { id: 1 } }));
    await expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(/did not confirm success/);
  });

  it("still surfaces the session-expired guidance via msg on a non-true body", async () => {
    vi.stubGlobal("fetch", mockFetchJson(200, { msg: "please login first" }));
    await expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(/set-cookie/);
  });
});

describe("callRpc redirect handling (BUI-184)", () => {
  for (const status of [301, 307, 308]) {
    it(`maps a ${status} login redirect to the session-expired message`, async () => {
      vi.stubGlobal(
        "fetch",
        mockFetchRedirect(status, "https://ezship.test/Account/Login")
      );
      await expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(/set-cookie/);
    });
  }

  it("reports a non-login redirect distinctly (not a generic error)", async () => {
    vi.stubGlobal("fetch", mockFetchRedirect(301, "https://ezship.test/elsewhere"));
    await expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(/Unexpected redirect/);
  });
});

describe("callRpc request timeout (BUI-184)", () => {
  it("aborts a stalled request with a timeout error", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_url: string, opts: { signal: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          opts.signal.addEventListener("abort", () =>
            reject(new Error("aborted"))
          );
        })
    );
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    // Attach the rejection expectation BEFORE advancing timers, so the handler
    // is in place when the abort fires (otherwise it reads as unhandled).
    const assertion = expect(callRpc(CONFIG, "ep", {})).rejects.toThrow(/timed out/);
    await vi.advanceTimersByTimeAsync(31_000);
    await assertion;
  });
});
