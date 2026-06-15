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

afterEach(() => {
  vi.restoreAllMocks();
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
