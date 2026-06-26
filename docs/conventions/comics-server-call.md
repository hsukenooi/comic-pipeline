# Shared comics-server call convention (BUI-172)

**This is the one canonical way every `/comic:*` skill talks to the comics
server (on the Mac Mini; not to be confused with Gixen, the external bidding
service).** Do not hand-roll URL resolution, health checks, or `curl`
error handling in a skill — route through the shared helper at
[`scripts/comics-server.sh`](../../scripts/comics-server.sh) so the
BUI-151/154/157/169/170 divergences (missing URL resolution, missing hostname
fallback, empty tables on a failed call, swallowed error bodies) cannot recur.

## How a skill uses it

At the first point a skill needs the server, run:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # sets + exports COMICS_SERVER_URL
comics_health_gate     || exit 1   # /health must answer (process is up)
```

Then make **every** call through `comics_curl`, always checking its exit:

```bash
# GET
comics_curl "$COMICS_SERVER_URL/api/comics/collection/status" || exit 1

# POST a JSON body
comics_curl -X POST "$COMICS_SERVER_URL/api/comics/wish-list" \
  -H 'Content-Type: application/json' -d @/tmp/body.json || exit 1

# download to a file
comics_curl "$COMICS_SERVER_URL/api/comics/collection/export" -o /tmp/export.json || exit 1
```

## The three functions

- **`comics_resolve_server`** — keeps `COMICS_SERVER_URL` if already set (it also
  honors the deprecated `GIXEN_SERVER_URL` alias and exports both, so older
  callers keep working); otherwise infers it from the hostname (Mac Mini →
  `http://localhost:8080`, MacBook → the Tailscale URL). An unrecognised machine
  **hard-fails** with a clear message rather than guessing.
- **`comics_health_gate`** — `curl -sf $COMICS_SERVER_URL/health`; non-200 or
  unreachable hard-fails. Note `/health` is static `{"status":"ok"}`: it proves
  the process is up, **not** that the collection store is healthy, so you must
  still check the exit code of each real data call.
- **`comics_curl <curl args…>`** — forces `--fail-with-body`, so a non-200
  exits non-zero **and** surfaces the error body on stderr. It never swallows a
  failure into an empty success — a failed call can never be mistaken for a
  legitimate "no results" answer. Stdout carries the response body on success.

## The rule that matters most

**Never render a degraded or "empty" result from a failed call.** If
`comics_curl` (or either gate) returns non-zero, STOP and tell the user the
server call failed — do not fall through to parsing an empty body, which is how
a duplicate gets bought or stale data gets uploaded.
