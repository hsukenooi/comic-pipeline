#!/usr/bin/env node

import { Command } from "commander";
import { loadConfig, saveCookie } from "./auth.js";
import { submitNewOrder } from "./api.js";
import { WAREHOUSE_VALUES, CARRIER_MAP } from "./types.js";

const program = new Command();

program
  .name("ezship")
  .description("CLI for ezbuy ezShip order management")
  .version("0.1.0");

program
  .command("new")
  .description("Submit a new ezShip order")
  .requiredOption("-t, --tracking <number>", "Tracking number from the merchant")
  .requiredOption("-c, --carrier <name>", "Carrier company name (e.g. UPS, FedEx, USPS)")
  .option("-d, --declared-value <cents>", "Declared value in cents (e.g. 1000 = $10)", "1000")
  .option(
    "-w, --warehouse <type>",
    `Warehouse: ${WAREHOUSE_VALUES.join(", ")}`,
    "usa"
  )
  .option("--carrier-id <id>", "Carrier company ID (auto-resolved from carrier name if omitted)")
  .option("-p, --product <name>", "Product name")
  .option("--category <name>", "Item category name", "Books")
  .option("--category-id <id>", "Item category ID", "1063")
  .option("--repack", "Request repacking (free)")
  .option("-r, --remark <text>", "Order remark")
  .action(
    async (opts: {
      tracking: string;
      warehouse: string;
      carrier: string;
      carrierId: string;
      product?: string;
      category: string;
      categoryId: string;
      declaredValue: string;
      repack?: boolean;
      remark?: string;
    }) => {
      try {
        const config = loadConfig();

        if (!WAREHOUSE_VALUES.includes(opts.warehouse as any)) {
          console.error(
            `Error: Invalid warehouse "${opts.warehouse}". Valid options: ${WAREHOUSE_VALUES.join(", ")}`
          );
          process.exit(1);
        }

        console.log(
          `Submitting order: tracking=${opts.tracking} warehouse=${opts.warehouse} carrier=${opts.carrier}`
        );

        const carrierId = opts.carrierId ?? CARRIER_MAP[opts.carrier];
        if (!carrierId) {
          console.error(
            `Error: Unknown carrier "${opts.carrier}". Known carriers: ${Object.keys(CARRIER_MAP).join(", ")}. Use --carrier-id to specify manually.`
          );
          process.exit(1);
        }

        const result = await submitNewOrder(config, {
          trackingNo: opts.tracking,
          warehouse: opts.warehouse,
          carrierName: opts.carrier,
          carrierId,
          productName: opts.product,
          categoryName: opts.category,
          categoryId: opts.categoryId,
          declaredValue: opts.declaredValue,
          repack: opts.repack,
          remark: opts.remark,
        });

        console.log("Response:", JSON.stringify(result, null, 2));
      } catch (err) {
        console.error(
          `Error: ${err instanceof Error ? err.message : String(err)}`
        );
        process.exit(1);
      }
    }
  );

program
  .command("set-cookie <value>")
  .description("Update the session cookie from a DevTools paste")
  .action((value: string) => {
    try {
      saveCookie(value);
      console.log("Cookie updated.");
    } catch (err) {
      console.error(
        `Error: ${err instanceof Error ? err.message : String(err)}`
      );
      process.exit(1);
    }
  });

program.parse();
