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
| `gixen-sync` | 1h | 2h | One completed pass of the comics server's background Gixen snipe-sync loop (`server.main._sync_gixen`) that reached its write phase without raising. `GIXEN_SYNC_INTERVAL` defaults to 600s, so a healthy server pings ~6×/hour; the 1h cadence tolerates the documented flapping/backoff (BUI-562) without alarming. | yes |
| `wishlist-sellers` | 168h | 336h | A `/comic:wishlist-sellers` run that exited 0. Exit 3 (partial — some candidates never verified) must **not** ping: the un-verified books are exactly the ones that would silently stop surfacing. Zero matching sellers on a clean run **is** a success. | yes |
| `collection-sync` | 336h | 672h | A `/comic:collection-sync` round-trip that completed its Step 5 re-import and its Step 6 post-import safety check. An aborted sync (the `Deleted from Collection.` probe tripping, the BUI-122 guard) must **not** ping — an abort is the sync working correctly but *not* having synced. | yes |
| `fmv-refresh` | 168h | 336h | A `comic-fmv` batch that fetched sold comps **and persisted them**. BUI-593 is precisely a run where the fetch succeeded and the write 422'd, so "`comic-fmv` exited 0" alone is not the success definition; the upsert must have been accepted. | yes |
| `sentinel-probe` | 168h | 336h | A `comic-fmv --sentinel-probe` run (BUI-603) where every sentinel book **and** the negative control passed — exit 0. Stricter than the rest on purpose: exit 1 means the probe ran and found the comp pipeline miscalibrated, which already alarms via its own exit code. Exit 2 (could not complete) does not ping either. | yes |

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
does. Before BUI-624 that meant `healthy` was permanently `false`: nothing
pinged, and a version that counted only wired jobs would have handed an external
monitor a green light for a system observing almost nothing. All five are wired
now, so `healthy: true` is finally reachable — and still means what it said. A
consumer wanting the narrower question ("is anything I *am* watching broken?")
reads `stale_jobs` and `never_seen_jobs` directly.

## Where the five jobs ping (BUI-624)

Four of the five ping over HTTP. `gixen-sync` cannot, and the exception is
instructive rather than incidental.

- **`gixen-sync`** — `server.main._sync_gixen`, as the **last statement inside**
  its apply-phase `write_transaction()`. `packages/gixen-cli` cannot import the
  overlay (the dependency runs overlay → gixen-cli, never back), so the ping
  leaves the host through a pluggy hookspec, `on_sync_observed(conn,
  snipe_count)`, which `gixen_overlay.plugin` implements with
  `record_heartbeat`. Two alternatives were rejected: an HTTP self-call (the
  server would need its own bind URL, and a blocking POST from the event loop
  to itself deadlocks under single-worker uvicorn) and a direct write to the
  `heartbeats` table (gixen-cli would encode a plugin-owned schema, inverting
  the one import direction the plugin system protects).

  Firing *inside* the transaction rather than after it is the load-bearing
  part. `_sync_gixen` carries an explicit invariant — nothing after the commit
  may raise, because a raise there converts a healthy cycle into a `_sync_loop`
  backoff or an `api_sync` 500 — and a heartbeat ping is I/O. Putting the write
  in the transaction removes the post-commit I/O entirely, and binds the
  heartbeat to the fate of the cycle's own writes: a cycle that wrote and then
  rolled back takes its heartbeat with it. `_invoke_sync_observed` additionally
  brackets the hook call in a SQLite savepoint, so a raising plugin rolls back
  alone and the sync's DML survives.

  One caveat, pinned by `test_savepoint_is_outermost_when_the_caller_wrote_nothing`:
  a savepoint taken with no DML pending is the *outermost* one, and its
  `RELEASE` commits. On a cycle that changed nothing there is nothing for the
  heartbeat to contradict, so this is harmless — but it is why the hook call
  must stay the **last** statement inside that transaction.
- **`wishlist-sellers`** — `.claude/commands/comic/wishlist-sellers.md`, final
  step, guarded on exit 0 (exit 3 partial and exit 1 verifier-down both skip).
- **`collection-sync`** — `.claude/commands/comic/collection-sync.md`, Step 6b,
  after every Step 6 hard-stop assertion passes. Any abort — the Step 3
  `Deleted from Collection.` probe above all — skips it.
- **`fmv-refresh`** — `apps/fmv/src/fmv_runner.py`'s `run()`, once per batch,
  gated on at least one `/api/comics` upsert having returned 2xx (a persisted
  row carries a non-null `fmv_id`). A batch that fetch-erred, was 422'd, or came
  entirely from the DB cache refreshed nothing and stays silent. Pairs with the
  BUI-601 ledger: the heartbeat says the refresh ran, the ledger says what it
  failed to store.
- **`sentinel-probe`** — `apps/fmv/src/sentinel_probe.py`'s `_ping_heartbeat`,
  on the all-pass branch only. Best-effort: a failed ping never changes the
  probe's exit code, which is the primary alert surface. It needs a schedule to
  be worth anything — see `docs/reference/sentinel-probe-scheduling.md`.

Every ping is **advisory to its caller**: none of the five may fail, block, or
alter the job it reports on. A watchdog that can break the work it watches has
bought nothing.

## The outer layer (BUI-672)

Everything above is a **pull**. Something has to ask
`GET /api/comics/health/heartbeats` for a stale job to be noticed. If the comics
server is down, the Mac Mini is asleep, or launchd never restarted the process,
nobody asks — and the watchdog fails green in exactly the way it was built to
prevent.

**A watchdog with no outer ping is its own worst bug class.** BUI-672 closes
it with `scripts/heartbeat-outer-ping.sh`, an hourly launchd job on the Mac
Mini that feeds a healthchecks.io **dead-man's-switch** — a check that alarms
on a *missing* ping, never one that arrives complaining:

```
launchd job on the Mac Mini, hourly
  └─ GET $COMICS_SERVER_URL/api/comics/health/heartbeats   (via scripts/comics-api)
       healthy == true   → GET  https://hc-ping.com/<uuid>          ("still breathing")
       healthy == false  → POST https://hc-ping.com/<uuid>/fail     (body names the offending jobs)
       anything else     → NO PING AT ALL                            (unset config, an
                                                                       unreachable server,
                                                                       non-200, curl error,
                                                                       parse failure, a bug
                                                                       in the script itself)
```

The bottom row is not a gap, it's the mechanism: healthchecks.io alarms on
**silence**, so every one of those "anything else" cases — including the
script or the whole Mini being unable to run at all — trips the exact same
off-machine alarm a stale job would. That is what makes this different from a
*local* poller (rejected explicitly): a check that decides locally whether to
alarm fails green precisely when the Mini is asleep or launchd is dead,
because nobody local is left to read the result. Here the alarm lives outside
this machine and fires on absence, so a dead Mini, a dead launchd, a dead
comics server, and a broken copy of the script are all indistinguishable from
"forgot to ping" — which is exactly what they are.

Full setup — the healthchecks.io check (period 1h / grace 1h), the launchd
plist recipe (documented, **never committed**, since it carries the ping URL
as a secret), and how to verify one end-to-end run — lives in
`docs/reference/heartbeat-outer-ping-scheduling.md`. The chosen cadence is
**hourly**, matching the contract's tightest inner cadence (`gixen-sync`);
unlike `sentinel-probe`'s deliberately-weekly schedule, this check spends no
provider budget, so there's no cost reason to run it any less often than
useful.

`HEARTBEAT_OUTER_PING_STATE` in `db.py` now reads `"wired"`. As with every
other claim in this file, that describes **deployed reality, not intent**: it
was flipped only once a real healthchecks.io check existed and the launchd job
was installed and verified end to end — the PR that flipped it was held
unmerged until a human confirmed both, the same discipline BUI-624 used when
it left the flag at `"unwired"` rather than claim a monitor that did not exist
yet. If the check or the launchd job is ever removed, flip this back to
`"unwired"` in the same change: a stale `"wired"` claiming a monitor nobody is
running is the identical lie this project exists to close.

The endpoint's own response still carries this flag verbatim
(`"outer_ping": "wired"`) and the dashboard reads it directly — nothing here
is a second, hand-maintained copy of the claim.

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
`LedgerRoute` in `ledger.py` (wired into `routes.py`'s `router = APIRouter(route_class=LedgerRoute)`,
BUI-630) — a custom `APIRoute` class, not middleware
(`app.add_middleware` is impossible from a plugin, whose `register_routes` hook
fires inside the host lifespan after Starlette has sealed its middleware stack).
New overlay endpoints are covered with zero per-endpoint code. Rows are pruned
after `REJECTED_WRITES_RETENTION_DAYS` (30).
