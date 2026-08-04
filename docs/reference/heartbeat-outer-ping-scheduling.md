# Scheduling `scripts/heartbeat-outer-ping.sh`

Setup-time reference for the BUI-672 outer ping, in the same spirit as
`docs/reference/sentinel-probe-scheduling.md`. Closes the outer layer left
open by BUI-602/624 — see `docs/reference/job-heartbeat-contract.md`'s "The
outer layer" section for the design this implements.

## Why this check has to live off-machine

Everything the job-heartbeat watchdog does is a **pull**: something must ask
`GET /api/comics/health/heartbeats` for a stale job to be noticed. A launchd
job on the Mac Mini that asks the question and then decides locally whether to
alarm — logs a line, sends itself a local notification, whatever — fails
green in exactly the scenario that matters most: the Mini is asleep, launchd
never restarted the process, or the comics server is down. Nobody local is
left to read the log in any of those cases.

`scripts/heartbeat-outer-ping.sh` does not raise its own alarm. It feeds a
healthchecks.io **dead-man's-switch**: a check that expects a ping on a
schedule and alarms when a ping is *missing*, not when one arrives complaining.
That inverts where the failure signal lives — off this machine, on absence —
so a dead Mini, a dead launchd, a dead comics server, or a broken copy of the
script itself all trip the same alarm a missed ping would. This is **not**
the "local poller" BUI-672 rejected: the poller and the alarm are different
systems, and only the alarm has to survive the machine being unavailable.

A pull-based *external* monitor (something outside the Mac Mini calling in)
was also rejected — the comics server is not reachable from outside the
machine (no cloudflared/ngrok, and Tailscale has no route from a cloud
sandbox), so nothing off-network can poll it. The dead-man's-switch sidesteps
that entirely: the Mini calls *out* to healthchecks.io, which requires no
inbound route to the Mini at all.

## Step 1 — create the healthchecks.io check (human, one-time)

This is an external account action; no coding agent can perform it.

1. Sign in to [healthchecks.io](https://healthchecks.io) (or a self-hosted
   instance, if the workspace already runs one).
2. Create a new check, e.g. named `comic-pipeline: outer heartbeat`.
3. Set its schedule to **Simple**, with:
   - **Period: 1 hour**
   - **Grace: 1 hour**

   (See "Cadence" below for why 1h/1h.)
4. Copy the check's ping URL (`https://hc-ping.com/<uuid>`). **Treat it as a
   bearer secret** — anyone holding it can phone in a fake "all good" for this
   job — and never paste it into a commit, a doc, or this file. It only ever
   lives in the Mac Mini's environment (the launchd plist below) and in
   healthchecks.io itself.
5. Configure whatever notification channel(s) the workspace already uses for
   alerts (email, Slack, etc.) on that check.

## Cadence: hourly, matching the tightest inner job

The job-heartbeat contract's cadences range from `gixen-sync` at 1h (the
tightest) to `collection-sync` at 336h — two weeks (the loosest). The outer
ping does not need to track either extreme; its only job is to prove the
**asking mechanism itself** — Mini awake, launchd alive, comics server up,
this script intact — is still functioning, independent of which inner jobs
happen to be due.

Unlike `sentinel-probe` (`docs/reference/sentinel-probe-scheduling.md`), which
is deliberately run weekly because each invocation spends real eBay/sold-comps
provider request budget, this check costs nothing beyond a local HTTP call and
a few bytes to healthchecks.io. There is no budget reason to run it any less
often than useful, and there is a strong reason to run it often: a check that
only fires weekly would let the Mini sleep through most of a week before the
*outer* layer noticed, even though the whole point of BUI-672 is to catch that
fast. So: **hourly**, matching the contract's tightest cadence (`gixen-sync`).

The **1h period / 1h grace** healthchecks.io schedule mirrors
`HEARTBEAT_STALE_FACTOR` (2.0) from the inner contract — the same "one missed
run is tolerated, two is not" shape already used throughout
`job-heartbeat-contract.md`, applied one layer up. A single skipped hourly run
(a slow deploy, a brief network blip) does not page anyone; two in a row does.

## Step 2 — install the launchd job (Mac Mini)

Unlike `comic-fmv` (uv-installed to `~/.local/bin`, which launchd's minimal
default `PATH` omits — see `sentinel-probe-scheduling.md`), this script's own
dependencies are `curl` and `python3`, both already on launchd's default
`PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`). No `PATH` override is needed here.
`scripts/comics-api` (which the script shells out to) resolves its own sibling
`comics-server.sh` by real path regardless of caller `cwd`, so it needs no
special environment either.

Invoke the script by its absolute repo path — it has exactly one caller
(this launchd job), so there is no reason to symlink it onto `PATH` the way
`scripts/comics-api` is symlinked for the many `/comic:*` skills that call it
by bare name (`scripts/install.sh:87-99`).

**The plist is documented here, never committed** — same convention as
`sentinel-probe-scheduling.md`: it embeds a secret
(`HEARTBEAT_OUTER_PING_URL`) that must never enter version control.

```bash
cat > "$HOME/Library/LaunchAgents/com.comics.heartbeat-outer-ping.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.comics.heartbeat-outer-ping</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/Projects/comic-pipeline/scripts/heartbeat-outer-ping.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HEARTBEAT_OUTER_PING_URL</key>
        <string>PASTE_THE_HEALTHCHECKS_IO_PING_URL_HERE</string>
        <key>COMICS_SERVER_URL</key>
        <string>http://localhost:8080</string>
    </dict>
    <!-- Hourly, matching the contract's tightest cadence (gixen-sync). See
         "Cadence" above for why this differs from sentinel-probe's weekly
         StartCalendarInterval: this check is free to run, that one is not. -->
    <key>StartInterval</key>
    <integer>3600</integer>
    <!-- Unlike sentinel-probe (RunAtLoad=false, to avoid spending provider
         budget on every deploy/login), this ping is free — RunAtLoad=true
         gives an immediate signal on install/reload instead of waiting up to
         an hour for the first one. -->
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.comics-server/heartbeat-outer-ping.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.comics-server/heartbeat-outer-ping.error.log</string>
</dict>
</plist>
PLIST

launchctl unload "$HOME/Library/LaunchAgents/com.comics.heartbeat-outer-ping.plist" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/com.comics.heartbeat-outer-ping.plist"
```

Replace `PASTE_THE_HEALTHCHECKS_IO_PING_URL_HERE` with the URL copied in Step
1 before running this — the plist file itself lives only on disk on the Mac
Mini, never in this repo.

## Step 3 — verify

`RunAtLoad=true` means loading the plist above already fired one ping. Confirm
it landed:

```bash
launchctl kickstart -k "gui/$(id -u)/com.comics.heartbeat-outer-ping"
cat "$HOME/.comics-server/heartbeat-outer-ping.log"
cat "$HOME/.comics-server/heartbeat-outer-ping.error.log"
```

The log should read `heartbeats report healthy=... — pinged success.` (or, on
the honest current state — see "Expect red at first" below — a `healthy=false`
line naming the offending jobs). Then check the healthchecks.io dashboard: the
check should show a ping (or a `/fail`) matching what the log says, and its
status should flip from "new"/"late" to "up" (or "down", matching a genuine
`/fail`) within a minute.

To test the script's own logic without touching the real check, run it
directly with a throwaway `HEARTBEAT_OUTER_PING_URL` — e.g. a
[requestbin](https://requestbin.com)-style catcher — and confirm: unsetting
the variable exits non-zero with **no** request sent; pointing
`COMICS_SERVER_URL` at an unreachable host exits non-zero with **no** request
sent; only a live server reporting `healthy: true` produces a GET to the ping
URL, and `healthy: false` produces a POST to `<url>/fail` naming the stale/
never-seen/pending-instrumentation jobs.

## Expect red at first — that is correct, not a bug

As of this writing, `GET /api/comics/health/heartbeats` reports
`healthy: false`: `gixen-sync` is `ok`, but `wishlist-sellers`,
`collection-sync`, `fmv-refresh`, and `sentinel-probe` were only wired
yesterday (BUI-624) and have not run once yet, so they read `never`. A freshly
created healthchecks.io check will therefore alarm (`/fail`) on its very first
ping, and keep alarming until each of those four jobs has completed at least
one real run on its own schedule.

**The fix is to let the jobs run, never to mute the check or weaken
`heartbeat-outer-ping.sh`'s alarm condition.** A monitor tuned to be quiet on
day one is the exact bug class this project exists to close.

Each job clears itself the first time it completes successfully — but only one
of the four clears **unattended**, and it is worth being precise about which,
because "it will go green on its own" is exactly the false expectation that
gets a monitor muted three weeks later:

| job | what clears it | unattended? |
| --- | --- | --- |
| `sentinel-probe` | `com.comics.sentinel-probe` launchd job, Sundays 09:00 | **yes** |
| `wishlist-sellers` | a `/comic:wishlist-sellers` run exiting 0 | no — user-invoked |
| `collection-sync` | a `/comic:collection-sync` round-trip through Step 6 | no — user-invoked |
| `fmv-refresh` | a `comic-fmv` batch that persisted at least one upsert | no — user-invoked |

The contract's cadences for those three (168h, 336h, 168h) describe **how often
they are expected to be run**, not automation that runs them. So the check goes
green as the normal buying workflow exercises them, not on a timer. If one of
them stays `never` for well past its cadence, the heartbeat is telling the truth
about a workflow that has quietly stopped being used — which is the signal, not
noise.

## Uninstalling

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.comics.heartbeat-outer-ping.plist"
rm "$HOME/Library/LaunchAgents/com.comics.heartbeat-outer-ping.plist"
```

Pause or delete the corresponding check on healthchecks.io too — an installed
plist pointed at a deleted check pings into the void; a deleted plist pointed
at a live check alarms forever on silence, which is correct but noisy if the
teardown was intentional.

---

Script: `scripts/heartbeat-outer-ping.sh` — BUI-672. Inner heartbeat contract
and wiring: BUI-602/624, `docs/reference/job-heartbeat-contract.md`.
