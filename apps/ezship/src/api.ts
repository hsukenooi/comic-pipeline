import type { Config, ApiResponse, SubmitNewOrderRequest } from "./types.js";
import { mapWarehouse } from "./types.js";
import { getHeaders } from "./auth.js";

const SESSION_EXPIRED_MSG =
  "Session expired. Run: ezship set-cookie \"<paste from DevTools>\"";

export async function callRpc(
  config: Config,
  endpoint: string,
  body: unknown
): Promise<ApiResponse> {
  const url = `${config.apiBaseUrl}/${endpoint}`;
  const headers = getHeaders(config);

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      redirect: "manual",
    });
  } catch (err) {
    throw new Error(
      `Network error calling ${endpoint}: ${err instanceof Error ? err.message : String(err)}`
    );
  }

  if (response.status === 302 || response.status === 303) {
    const location = response.headers.get("location") ?? "";
    if (location.includes("/Account/Login")) {
      throw new Error(SESSION_EXPIRED_MSG);
    }
    throw new Error(`Unexpected redirect to: ${location || "(empty Location header)"}`);
  }

  if (!response.ok && response.status !== 302 && response.status !== 303) {
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

  if (
    (result as Record<string, unknown>).result === false &&
    typeof (result as Record<string, unknown>).msg === "string" &&
    ((result as Record<string, unknown>).msg as string).includes("please login")
  ) {
    throw new Error(SESSION_EXPIRED_MSG);
  }

  return result;
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
