import { describe, it, expect, vi, afterEach } from "vitest";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mkdtempSync, readFileSync } from "node:fs";
import {
  submitNewOrder,
  getDeclaredCategories,
  checkCategoriesResponse,
  findSubCategory,
  SessionExpiredError,
} from "../src/api.js";
import type { Config } from "../src/types.js";

const CONFIG: Config = {
  cookie: "session=abc",
  userAgent: "UA",
  apiBaseUrl: "https://api.test",
};

// The saved copy the Python report reads. The live call returns this shape.
const SNAPSHOT = JSON.parse(
  readFileSync(
    new URL("../../ebay/src/ezship_categories.json", import.meta.url),
    "utf8"
  )
);

function tmpLedger(): string {
  return join(mkdtempSync(join(tmpdir(), "ezship-cat-")), "submitted-orders.json");
}

function captureFetch(json: unknown) {
  const bodies: any[] = [];
  const fn = vi.fn(async (_url: string, init: { body: string }) => {
    bodies.push(JSON.parse(init.body));
    return {
      status: 200,
      ok: true,
      headers: { get: () => null },
      json: async () => json,
      text: async () => JSON.stringify(json),
    };
  });
  vi.stubGlobal("fetch", fn);
  return bodies;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("submitNewOrder declared minimum (BUI-1425)", () => {
  it("sends the category's own minimum, not the Books one", async () => {
    const bodies = captureFetch({ result: true });
    await submitNewOrder(
      CONFIG,
      {
        trackingNo: "T-TEE",
        warehouse: "usa",
        categoryId: "869",
        categoryName: "Tops & Tee",
        declaredMin: "500",
        declaredValue: "1250",
      },
      tmpLedger()
    );
    const item = bodies[0].order.items[0];
    expect(item.category).toMatchObject({ id: "869", name: "Tops & Tee", declaredMin: "500" });
    expect(item.declaredValue).toBe("1250");
  });

  it("keeps the Books minimum when the category is Books", async () => {
    const bodies = captureFetch({ result: true });
    await submitNewOrder(CONFIG, { trackingNo: "T-BOOK", warehouse: "usa" }, tmpLedger());
    expect(bodies[0].order.items[0].category).toMatchObject({ id: "1063", declaredMin: "200" });
  });

  it("refuses a non-Books category with no minimum, before any request", async () => {
    const bodies = captureFetch({ result: true });
    await expect(
      submitNewOrder(
        CONFIG,
        { trackingNo: "T-X", warehouse: "usa", categoryId: "869", categoryName: "Tops & Tee" },
        tmpLedger()
      )
    ).rejects.toThrow(/declared-min/);
    expect(bodies).toHaveLength(0);
  });

  it("refuses a declared value below the category minimum", async () => {
    const bodies = captureFetch({ result: true });
    await expect(
      submitNewOrder(
        CONFIG,
        {
          trackingNo: "T-LOW",
          warehouse: "usa",
          categoryId: "877",
          categoryName: "Shirt",
          declaredMin: "1000",
          declaredValue: "999",
        },
        tmpLedger()
      )
    ).rejects.toThrow(/below/);
    expect(bodies).toHaveLength(0);
  });
});

describe("declared categories (BUI-1425)", () => {
  it("parses the saved snapshot and finds a subcategory by id", () => {
    const cats = checkCategoriesResponse(SNAPSHOT);
    expect(findSubCategory(cats, "1013")).toMatchObject({ name: "Cables", declaredMin: "200" });
    expect(findSubCategory(cats, "999999")).toBeUndefined();
  });

  it("posts an empty body to GetDeclaredCategories", async () => {
    const bodies = captureFetch(SNAPSHOT);
    const cats = await getDeclaredCategories(CONFIG);
    expect(bodies).toEqual([{}]);
    expect(cats.length).toBe(SNAPSHOT.categories.length);
  });

  it("treats an empty or missing list as a failure, never as no categories", () => {
    expect(() => checkCategoriesResponse({ categories: [] })).toThrow(/no category list/);
    expect(() => checkCategoriesResponse({})).toThrow(/no category list/);
  });

  it("maps a login message to SessionExpiredError", () => {
    expect(() => checkCategoriesResponse({ msg: "please login" })).toThrow(SessionExpiredError);
  });
});
