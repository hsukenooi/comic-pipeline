import { describe, it, expect } from "vitest";
import { exitCodeFor, EXIT_SESSION_EXPIRED } from "../src/exit-codes.js";
import { SessionExpiredError } from "../src/api.js";

describe("exitCodeFor (BUI-966)", () => {
  it("maps a SessionExpiredError to the distinct exit code", () => {
    expect(exitCodeFor(new SessionExpiredError())).toBe(EXIT_SESSION_EXPIRED);
    expect(EXIT_SESSION_EXPIRED).toBe(3);
  });

  it("maps a plain Error to the generic exit code", () => {
    expect(exitCodeFor(new Error("network blip"))).toBe(1);
  });

  it("maps a non-Error thrown value to the generic exit code", () => {
    expect(exitCodeFor("some string")).toBe(1);
    expect(exitCodeFor(undefined)).toBe(1);
  });
});
