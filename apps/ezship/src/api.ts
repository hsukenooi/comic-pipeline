import type { Config, ApiResponse, SubmitNewOrderRequest } from "./types.js";
import { mapWarehouse } from "./types.js";
import { getHeaders } from "./auth.js";

const SESSION_EXPIRED_MSG =
  "Session expired. Run: ezship set-cookie \"<paste from DevTools>\"";

// BUI-184: bound a stalled connection so order submission can't block forever.
const REQUEST_TIMEOUT_MS = 30_000;

// BUI-184: all HTTP redirect statuses. EZShip bounces an expired session to its
// login page; previously only 302/303 mapped to the "session expired" message
// and 301/307/308 fell through to a generic error that hid the real fix.
const REDIRECT_CODES = new Set([301, 302, 303, 307, 308]);

export async function callRpc(
  config: Config,
  endpoint: string,
  body: unknown
): Promise<ApiResponse> {
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
      throw new Error(SESSION_EXPIRED_MSG);
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

  let result: ApiResponse;
  try {
    result = (await response.json()) as ApiResponse;
  } catch {
    throw new Error(`Failed to parse API response as JSON from ${endpoint}`);
  }

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
    throw new Error(SESSION_EXPIRED_MSG);
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
  opts: NewOrderOptions
): Promise<ApiResponse> {
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
            id: opts.categoryId ?? DEFAULT_CATEGORY.id,
            name: opts.categoryName ?? DEFAULT_CATEGORY.name,
            isDefault: DEFAULT_CATEGORY.isDefault,
            declaredMin: DEFAULT_CATEGORY.declaredMin,
          },
          declaredValue: opts.declaredValue ?? "1000",
        },
      ],
      remark: opts.remark ?? "",
    },
  };

  return callRpc(config, "ezShipOrder.OrderPublic/SubmitNewOrder", body);
}
