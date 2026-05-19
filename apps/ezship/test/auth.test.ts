import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { loadConfig, getHeaders } from "../src/auth.js";

const TEST_DIR = join(tmpdir(), "ezship-test-" + Date.now());
const TEST_CONFIG = join(TEST_DIR, "config.json");

const VALID_CONFIG = {
  cookie: "session=abc123; token=xyz",
  userAgent: "Mozilla/5.0 Test",
  apiBaseUrl: "https://api.ezbuy.sg",
};

beforeEach(() => {
  mkdirSync(TEST_DIR, { recursive: true });
});

afterEach(() => {
  rmSync(TEST_DIR, { recursive: true, force: true });
});

describe("loadConfig", () => {
  it("loads valid config file", () => {
    writeFileSync(TEST_CONFIG, JSON.stringify(VALID_CONFIG));
    const config = loadConfig(TEST_CONFIG);
    expect(config.cookie).toBe(VALID_CONFIG.cookie);
    expect(config.userAgent).toBe(VALID_CONFIG.userAgent);
    expect(config.apiBaseUrl).toBe(VALID_CONFIG.apiBaseUrl);
  });

  it("throws when file does not exist", () => {
    expect(() => loadConfig(join(TEST_DIR, "nonexistent.json"))).toThrow(
      "Config file not found"
    );
  });

  it("throws when file is invalid JSON", () => {
    writeFileSync(TEST_CONFIG, "not json{{{");
    expect(() => loadConfig(TEST_CONFIG)).toThrow("not valid JSON");
  });

  it("throws when cookie is missing", () => {
    writeFileSync(
      TEST_CONFIG,
      JSON.stringify({ userAgent: "test", apiBaseUrl: "https://x" })
    );
    expect(() => loadConfig(TEST_CONFIG)).toThrow("cookie");
  });

  it("throws when userAgent is missing", () => {
    writeFileSync(
      TEST_CONFIG,
      JSON.stringify({ cookie: "test", apiBaseUrl: "https://x" })
    );
    expect(() => loadConfig(TEST_CONFIG)).toThrow("userAgent");
  });

  it("throws when apiBaseUrl is missing", () => {
    writeFileSync(
      TEST_CONFIG,
      JSON.stringify({ cookie: "test", userAgent: "test" })
    );
    expect(() => loadConfig(TEST_CONFIG)).toThrow("apiBaseUrl");
  });

  it("throws when fields are empty strings", () => {
    writeFileSync(
      TEST_CONFIG,
      JSON.stringify({ cookie: "", userAgent: "test", apiBaseUrl: "https://x" })
    );
    expect(() => loadConfig(TEST_CONFIG)).toThrow("cookie");
  });
});

describe("getHeaders", () => {
  it("returns correct headers from config", () => {
    const headers = getHeaders(VALID_CONFIG);
    expect(headers.Cookie).toBe(VALID_CONFIG.cookie);
    expect(headers["User-Agent"]).toBe(VALID_CONFIG.userAgent);
    expect(headers["Content-Type"]).toBe("application/json");
  });
});
