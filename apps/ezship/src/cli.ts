#!/usr/bin/env node

import { Command } from "commander";
import { loadConfig, saveCookie } from "./auth.js";
import {
  submitNewOrder,
  listOrders,
  getDeclaredCategories,
  findSubCategory,
} from "./api.js";
import { WAREHOUSE_VALUES, CARRIER_MAP } from "./types.js";
import { exitCodeFor, EXIT_SESSION_EXPIRED } from "./exit-codes.js";

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
  .option(
    "--declared-min <cents>",
    "The category's declared minimum in cents (looked up from EZShip when omitted)"
  )
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
      declaredMin?: string;
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

        // BUI-1425: a non-Books category needs its own minimum. Look it up
        // from EZShip when the caller didn't pass one, and check the name.
        let declaredMin = opts.declaredMin;
        if (declaredMin === undefined && opts.categoryId !== "1063") {
          const sub = findSubCategory(
            await getDeclaredCategories(config),
            opts.categoryId
          );
          if (!sub) {
            console.error(
              `Error: EZShip has no category ${opts.categoryId}. Run: ezship categories`
            );
            process.exit(1);
          }
          if (sub.name !== opts.category) {
            console.error(
              `Error: category ${opts.categoryId} is "${sub.name}", not "${opts.category}"`
            );
            process.exit(1);
          }
          declaredMin = String(sub.declaredMin);
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
          declaredMin,
          repack: opts.repack,
          remark: opts.remark,
        });

        console.log("Response:", JSON.stringify(result, null, 2));
      } catch (err) {
        const code = exitCodeFor(err);
        if (code === EXIT_SESSION_EXPIRED) {
          // BUI-966: print exactly the operator guidance, not double-prefixed
          // with "Error:" — the message itself IS the fix (`ezship set-cookie ...`).
          console.error(err instanceof Error ? err.message : String(err));
        } else {
          console.error(
            `Error: ${err instanceof Error ? err.message : String(err)}`
          );
        }
        process.exit(code);
      }
    }
  );

program
  .command("orders")
  .description("List every order on the EZShip account (read-only)")
  .option("--json", "Print JSON")
  .action(async (opts: { json?: boolean }) => {
    try {
      const orders = await listOrders(loadConfig());
      if (opts.json) {
        console.log(JSON.stringify({ orders }, null, 2));
      } else {
        for (const o of orders) {
          console.log(`${o.orderNumber}\t${o.trackingNo}\t${o.status}`);
        }
        console.log(`${orders.length} orders`);
      }
    } catch (err) {
      const code = exitCodeFor(err);
      console.error(
        code === EXIT_SESSION_EXPIRED
          ? err instanceof Error ? err.message : String(err)
          : `Error: ${err instanceof Error ? err.message : String(err)}`
      );
      process.exit(code);
    }
  });

program
  .command("categories")
  .description("List EZShip's declared categories and their minimums (read-only)")
  .option("--json", "Print the raw category tree as JSON")
  .action(async (opts: { json?: boolean }) => {
    try {
      const categories = await getDeclaredCategories(loadConfig());
      if (opts.json) {
        console.log(JSON.stringify({ categories }, null, 1));
      } else {
        for (const top of categories) {
          for (const sub of top.subCategories ?? []) {
            console.log(`${sub.id}\t${sub.declaredMin}\t${top.name} / ${sub.name}`);
          }
        }
      }
    } catch (err) {
      const code = exitCodeFor(err);
      console.error(
        code === EXIT_SESSION_EXPIRED
          ? err instanceof Error ? err.message : String(err)
          : `Error: ${err instanceof Error ? err.message : String(err)}`
      );
      process.exit(code);
    }
  });

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
