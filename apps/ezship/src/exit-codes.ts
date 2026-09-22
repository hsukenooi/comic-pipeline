import { SessionExpiredError } from "./api.js";

// BUI-966: distinct exit code for an expired session, so an unattended caller
// (e.g. /comic:ezship-add) can tell an expired cookie apart from any other
// failure (network blip, rejected order, ...) without parsing stderr text.
// 2 is commander's own usage-error exit code; 1 stays the generic error code.
export const EXIT_SESSION_EXPIRED = 3;

// Kept in its own module (rather than cli.ts) so it can be imported by tests
// without triggering cli.ts's module-level `program.parse()`.
export function exitCodeFor(err: unknown): number {
  if (err instanceof SessionExpiredError) {
    return EXIT_SESSION_EXPIRED;
  }
  return 1;
}
