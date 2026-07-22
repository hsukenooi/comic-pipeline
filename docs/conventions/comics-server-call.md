# Shared comics-server call convention (BUI-172, BUI-510)

**This is the one canonical way every `/comic:*` skill talks to the comics
server (on the Mac Mini; not to be confused with Gixen, the external bidding
service).** Do not hand-roll URL resolution, health checks, or `curl`
error handling in a skill — a server call is **one line**:

```bash
comics-api GET /api/comics/collection/status
```

`comics-api` is an installed executable (`scripts/comics-api`, on PATH via
`scripts/install.sh`) that resolves the server, health-gates it, and issues
the call, all in one process — so the BUI-151/154/157/169/170 divergences
(missing URL resolution, missing hostname fallback, empty tables on a failed
call, swallowed error bodies) cannot recur, and the BUI-352/BUI-375 trap
(separate fenced bash blocks don't share shell state, so a block that forgot
to re-source the old helper could curl an empty host) is structurally
impossible — there is nothing to re-source, since every invocation resolves
fresh.

## How a skill uses it

```bash
# GET
comics-api GET /api/comics/collection/status || exit 1

# GET with a query string
comics-api GET /api/comics/collection/check -G \
  --data-urlencode "series=Amazing Spider-Man" --data-urlencode "issue=300"

# POST a JSON body
comics-api POST /api/comics/wish-list \
  -H 'Content-Type: application/json' -d @/tmp/body.json || exit 1

# download to a file
comics-api GET /api/comics/collection/export -o /tmp/export.json || exit 1
```

`<path>` is always server-relative (leading `/`) — never a full URL; passing
one is a usage error, not a silent no-op. Everything after `<path>` is
forwarded to `curl` verbatim (headers, `-d`, `-o`, `-G`, …), so any curl
invocation you'd have hand-rolled still works, just prefixed with
`comics-api <METHOD> <path>` instead of a bare `curl ... "$COMICS_SERVER_URL/..."`.

A non-200 or unreachable server is **always** a non-zero exit with the error
body printed on stderr — never a silent empty success. `|| exit 1` (or a
custom error handler) after a call is defensive redundancy, not load-bearing;
add one when the skill wants its own domain-specific error message.

## When you still need the underlying library directly

`comics-api` covers "make one server call and use the result." A couple of
skills need something `comics-api` structurally cannot provide — an env var
*exported into the current shell* for a child process to inherit (`comic-fmv`
reads `COMICS_SERVER_URL` from its own environment; see `fmv.md`) — those
still `source scripts/comics-server.sh` and call `comics_resolve_server`
directly. The underlying functions:

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
  `comics-api` is a thin wrapper over exactly this function.

`comics_scratch_dir` (BUI-430) is a separate, unrelated helper in the same
file — a deterministic per-run temp directory, nothing to do with server
resolution — skills that need it still source the file for that alone.

## The rule that matters most

**Never render a degraded or "empty" result from a failed call.** If
`comics-api` (or, on the rare direct-library path above, `comics_curl` or
either gate) returns non-zero, STOP and tell the user the server call failed —
do not fall through to parsing an empty body, which is how a duplicate gets
bought or stale data gets uploaded.
