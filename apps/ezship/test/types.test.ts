import { describe, it, expect } from "vitest";
import { mapWarehouse, WAREHOUSE_MAP, WAREHOUSE_VALUES } from "../src/types.js";

describe("WAREHOUSE_VALUES", () => {
  it("contains exactly 4 warehouse options", () => {
    expect(WAREHOUSE_VALUES).toHaveLength(4);
    expect(WAREHOUSE_VALUES).toContain("guangzhou");
    expect(WAREHOUSE_VALUES).toContain("shanghai");
    expect(WAREHOUSE_VALUES).toContain("taiwan");
    expect(WAREHOUSE_VALUES).toContain("usa");
  });
});

describe("WAREHOUSE_MAP", () => {
  it("maps guangzhou to WarehouseTypeGuangzhou", () => {
    expect(WAREHOUSE_MAP.guangzhou.id).toBe("WarehouseTypeGuangzhou");
    expect(WAREHOUSE_MAP.guangzhou.name).toBe("Guangzhou");
  });

  it("maps shanghai to WarehouseTypeShanghai", () => {
    expect(WAREHOUSE_MAP.shanghai.id).toBe("WarehouseTypeShanghai");
  });

  it("maps taiwan to WarehouseTypeTaiwan", () => {
    expect(WAREHOUSE_MAP.taiwan.id).toBe("WarehouseTypeTaiwan");
  });

  it("maps usa to WarehouseTypeUSA", () => {
    expect(WAREHOUSE_MAP.usa.id).toBe("WarehouseTypeUSA");
    expect(WAREHOUSE_MAP.usa.name).toBe("USA");
  });
});

describe("mapWarehouse", () => {
  it("returns correct warehouse info for valid values", () => {
    const gz = mapWarehouse("guangzhou");
    expect(gz.id).toBe("WarehouseTypeGuangzhou");
    expect(gz.name).toBe("Guangzhou");
    expect(gz.isSuportAddItem).toBe(true);

    const usa = mapWarehouse("usa");
    expect(usa.id).toBe("WarehouseTypeUSA");
  });

  it("throws for invalid warehouse value", () => {
    expect(() => mapWarehouse("china")).toThrow('Invalid warehouse "china"');
    expect(() => mapWarehouse("")).toThrow("Invalid warehouse");
    expect(() => mapWarehouse("GUANGZHOU")).toThrow("Invalid warehouse");
  });
});
