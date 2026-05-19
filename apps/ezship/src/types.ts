export interface Config {
  cookie: string;
  userAgent: string;
  apiBaseUrl: string;
}

export const WAREHOUSE_VALUES = ["guangzhou", "shanghai", "taiwan", "usa"] as const;
export type WarehouseValue = (typeof WAREHOUSE_VALUES)[number];

export interface WarehouseInfo {
  id: string;
  name: string;
  isSuportAddItem: boolean;
}

export const WAREHOUSE_MAP: Record<WarehouseValue, WarehouseInfo> = {
  guangzhou: { id: "WarehouseTypeGuangzhou", name: "Guangzhou", isSuportAddItem: true },
  shanghai: { id: "WarehouseTypeShanghai", name: "Shanghai", isSuportAddItem: true },
  taiwan: { id: "WarehouseTypeTaiwan", name: "Taiwan", isSuportAddItem: true },
  usa: { id: "WarehouseTypeUSA", name: "USA", isSuportAddItem: true },
};

export const CARRIER_MAP: Record<string, string> = {
  DHL: "55",
  FedEx: "56",
  Ontrac: "57",
  UPS: "58",
  USPS: "59",
  Other: "60",
  Amazon: "67",
};

export interface CarrierCompany {
  id: string;
  name: string;
  trackingNo: string;
}

export interface ItemCategory {
  id: string;
  name: string;
  isDefault: boolean;
  declaredMin: string;
}

export interface OrderItem {
  productName: string;
  qty: number;
  category: ItemCategory;
  declaredValue: string;
}

export interface SubmitNewOrderRequest {
  order: {
    warehouse: WarehouseInfo;
    carrierCompany: CarrierCompany;
    addedServices: unknown[];
    items: OrderItem[];
    remark: string;
  };
}

export interface ApiResponse {
  code?: number;
  message?: string;
  data?: unknown;
}

export function mapWarehouse(value: string): WarehouseInfo {
  if (!(value in WAREHOUSE_MAP)) {
    throw new Error(
      `Invalid warehouse "${value}". Valid options: ${WAREHOUSE_VALUES.join(", ")}`
    );
  }
  return WAREHOUSE_MAP[value as WarehouseValue];
}
