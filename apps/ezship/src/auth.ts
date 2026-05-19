import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import type { Config } from "./types.js";

const CONFIG_PATH = join(homedir(), ".config", "ezship", "config.json");

export function loadConfig(path?: string): Config {
  const configPath = path ?? CONFIG_PATH;

  let raw: string;
  try {
    raw = readFileSync(configPath, "utf-8");
  } catch {
    throw new Error(
      `Config file not found at ${configPath}\n` +
        "Create it with your ezbuy session cookies:\n" +
        '  { "cookie": "...", "userAgent": "...", "apiBaseUrl": "..." }'
    );
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`Config file at ${configPath} is not valid JSON`);
  }

  const config = parsed as Record<string, unknown>;

  const missing: string[] = [];
  if (typeof config.cookie !== "string" || !config.cookie) missing.push("cookie");
  if (typeof config.userAgent !== "string" || !config.userAgent) missing.push("userAgent");
  if (typeof config.apiBaseUrl !== "string" || !config.apiBaseUrl) missing.push("apiBaseUrl");

  if (missing.length > 0) {
    throw new Error(
      `Config file missing required fields: ${missing.join(", ")}\n` +
        "See: https://github.com/user/ezship-cli#setup"
    );
  }

  return {
    cookie: config.cookie as string,
    userAgent: config.userAgent as string,
    apiBaseUrl: config.apiBaseUrl as string,
  };
}

export function getHeaders(config: Config): Record<string, string> {
  return {
    Cookie: config.cookie,
    "User-Agent": config.userAgent,
    "Content-Type": "application/json",
  };
}

export function saveCookie(cookie: string, path?: string): void {
  const configPath = path ?? CONFIG_PATH;
  let raw: string;
  try {
    raw = readFileSync(configPath, "utf-8");
  } catch {
    throw new Error(
      `Config file not found at ${configPath}\n` +
        "Create it first with your ezbuy credentials:\n" +
        '  { "cookie": "...", "userAgent": "...", "apiBaseUrl": "..." }'
    );
  }
  let config: Record<string, unknown>;
  try {
    config = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    throw new Error(`Config file at ${configPath} is not valid JSON`);
  }
  config.cookie = cookie;
  writeFileSync(configPath, JSON.stringify(config, null, 2) + "\n", "utf-8");
}
