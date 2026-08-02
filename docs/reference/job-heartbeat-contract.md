# Job heartbeat contract (BUI-602)

This table is the design doc for the **Silent-Failure Observability** project.
There is no separate plan.

Its machine-readable twin is `JOB_CONTRACTS` in
`plugins/gixen-overlay/src/gixen_overlay/db.py`.
`plugins/gixen-overlay/tests/test_heartbeat_contract_doc.py` pins this file to
that constant, so the two cannot drift.

## Why heartbeats and not alerts

The repo's dominant trap class is **fails green**: a job that dies, or silently
no-ops, looks exactly like a healthy one. At least nine `docs/solutions/` docs
describe an instance. Error-based alerting structurally cannot see this class,
because there is no error to alert on — the wish-list scan that stopped running
emits nothing, and the wish-list scan that ran and matched nobody also emits
nothing.

The only thing that separates the two is a **positive signal on success**. A job
pings when it worked; the watchdog complains when a ping is overdue. Absence of
a ping is the alarm.

## The contract

| job | cadence | stale after | success means | wired? |
| --- | --- | --- | --- | --- |
| `gixen-sync` | 1h | 2h | One completed pass of the comics server's background Gixen snipe-sync loop (`server.main._sync_gixen`) that reached its write phase without raising. `GIXEN_SYNC_INTERVAL` defaults to 600s, so a healthy server pings ~6×/hour; the 1h cadence tolerates the documented flapping/backoff (BUI-562) without alarming. | no |
| `wishlist-sellers` | 168h | 336h | A `/comic:wishlist-sellers` run that exited 0. Exit 3 (partial — some candidates never verified) must **not** ping: the un-verified books are exactly the ones that would silently stop surfacing. Zero matching sellers on a clean run **is** a success. | no |
| `collection-sync` | 336h | 672h | A `/comic:collection-sync` round-trip that completed its Step 6 re-import and post-import safety check. An aborted sync (the `Deleted from Collection.` probe tripping, the BUI-122 guard) must **not** ping — an abort is the sync working correctly but *not* having synced. | no |
| `fmv-refresh` | 168h | 336h | A `comic-fmv` batch that fetched sold comps **and persisted them**. BUI-593 is precisely a run where the fetch succeeded and the write 422'd, so "`comic-fmv` exited 0" alone is not the success definition; the upsert must have been accepted. | no |

Cadences are sized to the **slowest normal run**, not the average, and a job is
only flagged once it is `HEARTBEAT_STALE_FACTOR` (2×) cadences late. A watchdog
that cries wolf gets muted, and a muted watchdog is another fails-green
instance.

### "Zero results" is a success

Every success definition above counts a clean run that found nothing as a
success. This is the whole point: distinguishing *ran and found nothing* from
*did not run* is the one thing the heartbeat exists to do. A job that only pings
when it produced output would go dark during exactly the quiet stretch a human
is least likely to notice.

## Endpoints

```sh
# Ping (call ONLY on success, as defined above)
comics-api POST /api/heartbeat/<job>

# Watchdog verdict for every job in the contract
comics-api GET /api/comics/health/heartbeats
```

An unknown job name is a **404**, never a silent accept: a typo'd ping that
stored fine would leave the real job looking dead forever while the watchdog
stayed green. To add a job, add it to `JOB_CONTRACTS` **and** to the table
above.

A job with no heartbeat row is never reported as healthy. It reports:

- `never` — the caller is wired but has not pinged. This is an alarm.
- `pending_instrumentation` — the contract is declared but nothing pings yet.
  Health is **unknown**, not good; the dashboard shows it amber, never green.

The report's top-level `healthy` means *every job in the contract is verified
to be running*, so an uninstrumented job makes it `false` exactly as a stale one
does. **Today that means `healthy` is `false`, by design** — nothing pings yet,
and a version that counted only wired jobs would hand an external monitor a
green light for a system observing almost nothing. A consumer wanting the
narrower question ("is anything I *am* watching broken?") reads `stale_jobs` and
`never_seen_jobs` directly.

## Wiring the four jobs

None of the four ping yet — every row above says `wired: no`, and the endpoint
reports them as `pending_instrumentation` rather than pretending they are fine.

- **`gixen-sync`** — the ping belongs in `server.main._sync_gixen` after its
  write phase commits. That file is owned by `packages/gixen-cli`, which
  BUI-601/602 was scoped not to modify.
- **`wishlist-sellers`** — add to `.claude/commands/comic/wishlist-sellers.md`
  as a final step, guarded on exit 0 (see that skill's exit-code table; exit 3
  must not ping).
- **`collection-sync`** — add to `.claude/commands/comic/collection-sync.md`
  after the Step 6 re-import reconciles and the post-import safety check
  passes.
- **`fmv-refresh`** — add to `apps/fmv`'s runner after the `/api/comics` upsert
  returns 2xx. Pair it with the BUI-601 ledger: the heartbeat says the refresh
  ran, the ledger says what it failed to store.

## The outer layer — NOT WIRED

Everything above is a **pull**. Something has to ask
`GET /api/comics/health/heartbeats` for a stale job to be noticed. If the comics
server is down, the Mac Mini is asleep, or launchd never restarted the process,
nobody asks — and the watchdog fails green in exactly the way it was built to
prevent.

**A watchdog with no outer ping is its own worst bug class.** Closing the gap
needs a check that lives *outside* this machine and alarms on silence:

1. Create a check on healthchecks.io (or any uptime pinger / cloud `/schedule`
   agent) with a period matching the tightest cadence you care about.
2. Have it poll the endpoint and fail on a non-200 **or** on any job not `ok`:

   ```sh
   curl -fsS "$COMICS_SERVER_URL/api/comics/health/heartbeats" \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["healthy"] else 1)'
   ```

3. Flip `HEARTBEAT_OUTER_PING_STATE` in `db.py` to `"wired"` and update this
   section.

Wire the jobs **before** the outer ping, or in the same change. `healthy` is
`false` while anything is `pending_instrumentation`, so an outer check added
first would alarm continuously — and a monitor you have muted is another
fails-green instance.

Until then the endpoint declares the gap in its own response
(`"outer_ping": "unwired"`) and the dashboard prints it under the heartbeat
tile, rather than implying a health it cannot vouch for.

## Related: the rejected-writes ledger (BUI-601)

The heartbeat answers *did the job run?* The ledger answers *what did the server
refuse to store?* Both were needed because BUI-593 failed on both axes at once:
the FMV fetch succeeded, the write 422'd, the book was stored nowhere, and
nothing surfaced either fact.

```sh
comics-api GET /api/comics/health/rejections           # last 24h
comics-api GET "/api/comics/health/rejections?hours=720"  # full retention window
```

Every 4xx/5xx on a **mutating** overlay request is persisted automatically by
`LedgerRoute` in `routes.py` — a custom `APIRoute` class, not middleware
(`app.add_middleware` is impossible from a plugin, whose `register_routes` hook
fires inside the host lifespan after Starlette has sealed its middleware stack).
New overlay endpoints are covered with zero per-endpoint code. Rows are pruned
after `REJECTED_WRITES_RETENTION_DAYS` (30).
