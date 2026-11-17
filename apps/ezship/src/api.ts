import type { Config, ApiResponse, SubmitNewOrderRequest } from "./types.js";
import { mapWarehouse } from "./types.js";
import { getHeaders } from "./auth.js";
import {
  LEDGER_PATH,
  findSubmittedOrder,
  recordSubmittedOrder,
} from "./order-ledger.js";

const SESSION_EXPIRED_MSG =
  "Session expired. Run: ezship set-cookie \"<paste from DevTools>\"";

// BUI-966: a typed error lets the CLI map this specific failure to a distinct
// exit code (an expired cookie needs a human to paste a fresh one, versus any
// other failure which is just "retry"), instead of pattern-matching the
// message text at the exit boundary.
export class SessionExpiredError extends Error {
  constructor(message: string = SESSION_EXPIRED_MSG) {
    super(message);
    this.name = "SessionExpiredError";
  }
}

// BUI-184: bound a stalled connection so order submission can't block forever.
const REQUEST_TIMEOUT_MS = 30_000;

// BUI-184: all HTTP redirect statuses. EZShip bounces an expired session to its
// login page; previously only 302/303 mapped to the "session expired" message
// and 301/307/308 fell through to a generic error that hid the real fix.
const REDIRECT_CODES = new Set([301, 302, 303, 307, 308]);

// Sends one RPC and returns the parsed JSON body. It maps redirects, timeouts,
// and HTTP errors but makes no success check: success sits in a different
// place per endpoint (BUI-1378), so each caller checks its own.
async function postRpc(
  config: Config,
  endpoint: string,
  body: unknown
): Promise<unknown> {
  const url = `${config.apiBaseUrl}/${endpoint}`;
  const headers = getHeaders(config);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      redirect: "manual",
      signal: controller.signal,
    });
  } catch (err) {
    if (controller.signal.aborted) {
      throw new Error(
        `Request to ${endpoint} timed out after ${REQUEST_TIMEOUT_MS}ms`
      );
    }
    throw new Error(
      `Network error calling ${endpoint}: ${err instanceof Error ? err.message : String(err)}`
    );
  } finally {
    clearTimeout(timer);
  }

  if (REDIRECT_CODES.has(response.status)) {
    const location = response.headers.get("location") ?? "";
    if (location.includes("/Account/Login")) {
      throw new SessionExpiredError();
    }
    throw new Error(`Unexpected redirect to: ${location || "(empty Location header)"}`);
  }

  if (!response.ok && !REDIRECT_CODES.has(response.status)) {
    let body = "";
    try {
      body = await response.text();
    } catch {
      // ignore
    }
    throw new Error(
      `API error ${response.status}: ${body || response.statusText}`
    );
  }

  try {
    return await response.json();
  } catch {
    throw new Error(`Failed to parse API response as JSON from ${endpoint}`);
  }
}

export async function callRpc(
  config: Config,
  endpoint: string,
  body: unknown
): Promise<ApiResponse> {
  const result = (await postRpc(config, endpoint, body)) as ApiResponse;

  // BUI-141 / BUI-181: a SubmitNewOrder success returns `result: true`. The old
  // code only rejected an explicit `result: false`, so a 200 body of
  // `{error:"duplicate tracking"}`, `{result:"false"}` (string), or one omitting
  // `result` entirely passed through and the CLI exited 0 — a failed/rejected
  // order reported as "Submitted". Require success to be the explicit positive
  // `result === true`; treat anything else as a failure so the CLI exits non-zero.
  const r = result as Record<string, unknown>;
  if (r.result === true) {
    return result;
  }

  const msg =
    typeof r.msg === "string"
      ? r.msg
      : typeof r.error === "string"
        ? r.error
        : typeof r.message === "string"
          ? r.message
          : "";
  if (msg.includes("please login")) {
    throw new SessionExpiredError();
  }
  throw new Error(
    `EZShip did not confirm success (no result:true): ${msg || JSON.stringify(result)}`
  );
}

export interface NewOrderOptions {
  trackingNo: string;
  warehouse: string;
  carrierName?: string;
  carrierId?: string;
  productName?: string;
  categoryId?: string;
  categoryName?: string;
  declaredValue?: string;
  // The chosen category's own minimum, in cents (BUI-1425). Required for any
  // category other than Books: a wrong minimum is sent to EZShip as-is.
  declaredMin?: string;
  repack?: boolean;
  remark?: string;
}

const DEFAULT_CATEGORY = {
  id: "1063",
  name: "Books",
  isDefault: false,
  declaredMin: "200",
};

export async function submitNewOrder(
  config: Config,
  opts: NewOrderOptions,
  ledgerPath: string = LEDGER_PATH
): Promise<ApiResponse> {
  // BUI-180: refuse to re-POST a tracking number we've already submitted, so a
  // re-run does not create a second real shipment.
  const prior = findSubmittedOrder(opts.trackingNo, ledgerPath);
  if (prior) {
    throw new Error(
      `Order for tracking ${opts.trackingNo} was already submitted at ` +
        `${prior.submittedAt}${prior.orderId ? ` (order ${prior.orderId})` : ""}. ` +
        `Refusing to submit a duplicate. To override, remove that entry from ` +
        `${ledgerPath}.`
    );
  }

  const categoryId = opts.categoryId ?? DEFAULT_CATEGORY.id;
  // BUI-1425: every order used to carry the Books minimum (200), whatever its
  // category. Send the category's own minimum, and refuse to guess one.
  let declaredMin = opts.declaredMin;
  if (declaredMin === undefined) {
    if (categoryId !== DEFAULT_CATEGORY.id) {
      throw new Error(
        `No declared minimum for category ${categoryId}. Pass --declared-min ` +
          `(see \`ezship categories\`).`
      );
    }
    declaredMin = DEFAULT_CATEGORY.declaredMin;
  }
  if (!/^\d+$/.test(declaredMin)) {
    throw new Error(`Declared minimum must be whole cents, got "${declaredMin}"`);
  }
  const declaredValue = opts.declaredValue ?? "1000";
  if (/^\d+$/.test(declaredValue) && Number(declaredValue) < Number(declaredMin)) {
    throw new Error(
      `Declared value ${declaredValue} is below category ${categoryId}'s ` +
        `minimum of ${declaredMin} cents`
    );
  }

  const warehouseInfo = mapWarehouse(opts.warehouse);

  const body: SubmitNewOrderRequest = {
    order: {
      warehouse: warehouseInfo,
      carrierCompany: {
        id: opts.carrierId ?? "58",
        name: opts.carrierName ?? "UPS",
        trackingNo: opts.trackingNo,
      },
      addedServices: opts.repack
        ? [
            {
              addedServiceType: "AddedServiceTypeRePackage",
              name: "Repacking",
              tips: "Repacking service is Free Of Charge (FOC). Only orders we think they are suitable to repack and ensured with enough room to reduce the volumetric weight will be repacked.",
              fee: "0",
              serviceId: "0",
            },
          ]
        : [],
      items: [
        {
          productName: opts.productName ?? "",
          qty: 1,
          category: {
            id: categoryId,
            name: opts.categoryName ?? DEFAULT_CATEGORY.name,
            isDefault: DEFAULT_CATEGORY.isDefault,
            declaredMin,
          },
          declaredValue,
        },
      ],
      remark: opts.remark ?? "",
    },
  };

  // callRpc throws on any non-success (BUI-181), so reaching past it means the
  // order was accepted — record it so a re-run is deduped.
  const result = await callRpc(
    config,
    "ezShipOrder.OrderPublic/SubmitNewOrder",
    body
  );
  const data = (result as Record<string, unknown>).data;
  const orderId =
    data && typeof data === "object"
      ? ((data as Record<string, unknown>).orderId ??
          (data as Record<string, unknown>).id)
      : undefined;
  recordSubmittedOrder(
    opts.trackingNo,
    { orderId: orderId != null ? String(orderId) : undefined },
    ledgerPath
  );
  return result;
}

// ─── Order list (BUI-1378) ───────────────────────────────────────────────────

export interface EzshipOrder {
  orderNumber: string;
  trackingNo: string;
  status: string;
  createdDate: string;
}

const LIST_ENDPOINT = "ezShipOrder.OrderExpress/GetOrderListByStatus";
// The site lists orders in two ranges: the latest three months and everything
// older. A body with no dateGap returns only one order, so both are asked for.
const DATE_GAPS = ["DateGapLatestThreeMonth", "DateGapBeforeThreeMonth"];
export const ORDER_PAGE_SIZE = 30;
// A guard against a server that ignores offset and repeats one page forever.
const MAX_PAGES = 100;

function listMessage(r: Record<string, unknown>): string {
  const inner = r.result;
  if (inner && typeof inner === "object") {
    const msg = (inner as Record<string, unknown>).msg;
    if (typeof msg === "string") return msg;
  }
  for (const k of ["msg", "error", "message"]) {
    if (typeof r[k] === "string") return r[k] as string;
  }
  return "";
}

// The list call reports success at result.result === true (nested), unlike
// SubmitNewOrder's top-level result:true, so it gets its own check.
export function checkListResponse(json: unknown): Record<string, unknown> {
  const r = (json ?? {}) as Record<string, unknown>;
  const inner = r.result as Record<string, unknown> | undefined;
  if (inner && typeof inner === "object" && inner.result === true) {
    return r;
  }
  const msg = listMessage(r);
  if (/log\s*in/i.test(msg)) {
    throw new SessionExpiredError();
  }
  throw new Error(
    `EZShip did not confirm the order list (no result.result:true): ${msg || JSON.stringify(json)}`
  );
}

function ordersOf(r: Record<string, unknown>): Record<string, unknown>[] {
  const groups = Array.isArray(r.orderGroups) ? r.orderGroups : [];
  return groups.flatMap((g) => {
    const orders = (g as Record<string, unknown>)?.orders;
    return Array.isArray(orders) ? (orders as Record<string, unknown>[]) : [];
  });
}

// Every order on the account, both date ranges, deduped by order id.
// Read-only: it never calls a write endpoint.
export async function listOrders(config: Config): Promise<EzshipOrder[]> {
  const byId = new Map<string, EzshipOrder>();
  for (const dateGap of DATE_GAPS) {
    let page = 0;
    for (;; page++) {
      if (page >= MAX_PAGES) {
        throw new Error(
          `Order list for ${dateGap} passed ${MAX_PAGES} pages; refusing a partial list`
        );
      }
      const r = checkListResponse(
        await postRpc(config, LIST_ENDPOINT, {
          dateGap,
          offset: page * ORDER_PAGE_SIZE,
          limit: ORDER_PAGE_SIZE,
        })
      );
      const orders = ordersOf(r);
      for (const o of orders) {
        const carrier = (o.carrierCompany ?? {}) as Record<string, unknown>;
        const id = String(o.id ?? o.orderNumber ?? "");
        byId.set(id, {
          orderNumber: String(o.orderNumber ?? ""),
          trackingNo: String(carrier.trackingNo ?? ""),
          status: String(o.status ?? ""),
          createdDate: String(o.createdDate ?? ""),
        });
      }
      if (orders.length < ORDER_PAGE_SIZE) break;
    }
  }
  return [...byId.values()];
}

// ─── Declared categories (BUI-1425) ─────────────────────────────────────────

export interface DeclaredSubCategory {
  id: string;
  name: string;
  isDefault: boolean;
  declaredMin: string;
}

export interface DeclaredCategory extends DeclaredSubCategory {
  subCategories: DeclaredSubCategory[];
}

const CATEGORIES_ENDPOINT = "ezShipOrder.OrderPublic/GetDeclaredCategories";

// The category list carries no result flag: a good body is a non-empty
// `categories` array. Anything else is a failure, never an empty list.
export function checkCategoriesResponse(json: unknown): DeclaredCategory[] {
  const r = (json ?? {}) as Record<string, unknown>;
  if (Array.isArray(r.categories) && r.categories.length > 0) {
    return r.categories as DeclaredCategory[];
  }
  const msg = listMessage(r);
  if (/log\s*in/i.test(msg)) {
    throw new SessionExpiredError();
  }
  throw new Error(
    `EZShip returned no category list: ${msg || JSON.stringify(json)}`
  );
}

// Read-only: EZShip's full declared-category tree, in the same shape as
// apps/ebay/src/ezship_categories.json (`{"categories": [...]}`).
export async function getDeclaredCategories(
  config: Config
): Promise<DeclaredCategory[]> {
  return checkCategoriesResponse(await postRpc(config, CATEGORIES_ENDPOINT, {}));
}

// The subcategory with this id, or undefined.
export function findSubCategory(
  categories: DeclaredCategory[],
  id: string
): DeclaredSubCategory | undefined {
  for (const top of categories) {
    for (const sub of top.subCategories ?? []) {
      if (String(sub.id) === id) return sub;
    }
  }
  return undefined;
}
