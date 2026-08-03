# Scheduling `comic-fmv --sentinel-probe`

Setup-time reference for the BUI-603 sentinel probe, in the same spirit as
`docs/reference/wishlist-sellers-scheduling.md`. Wired as a heartbeat job by
BUI-624 — see `docs/reference/job-heartbeat-contract.md`.

## Why this file exists

The probe was built in BUI-603 and never scheduled, so it never ran. A
calibration check nobody triggers is indistinguishable from one that passes
every time — the same fails-green shape the heartbeat contract exists to close,
one layer further out. The probe's heartbeat now closes it: if the schedule
below is never installed, `sentinel-probe` goes `stale` in
`GET /api/comics/health/heartbeats` within two weeks and says so.

## Cadence: weekly, not per-run

Run it **weekly**. Each run spends real provider request budget on top of
whatever real FMV batches ran that day, and `comic-fmv` has no `--max-workers`
to bound that spend (BUI-565/570). It does reuse the `ebay-sold-comps` response
cache like any other caller (no `--force`), so a sentinel that happens to
overlap a recent real query costs nothing extra — but the floor is still a
handful of live queries per run. Never wire it into a per-`/comic:fmv`
invocation.

The heartbeat contract's cadence for this job is 168h, flagged stale at 336h —
sized so one skipped week is tolerated and two are not.

## Option A — launchd (recommended on the Mac Mini)

The probe should run on the **Mac Mini**, where `COMICS_SERVER_URL` already
points at the local comics server. Write the LaunchAgent:

```bash
cat > "$HOME/Library/LaunchAgents/com.comics.sentinel-probe.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.comics.sentinel-probe</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.local/bin/comic-fmv</string>
        <string>--sentinel-probe</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>COMICS_SERVER_URL</key>
        <string>http://localhost:8080</string>
        <!-- launchd starts jobs with a minimal PATH that omits ~/.local/bin,
             where uv installs the ebay-sold-comps console script comic-fmv
             shells out to (BUI-27). -->
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <!-- Sundays at 09:00. StartCalendarInterval, not StartInterval: a weekly
         wall-clock slot, and launchd runs a missed slot once at wake rather
         than replaying every skipped tick. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$HOME/.comics-server/sentinel-probe.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.comics-server/sentinel-probe.error.log</string>
</dict>
</plist>
PLIST

launchctl unload "$HOME/Library/LaunchAgents/com.comics.sentinel-probe.plist" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/com.comics.sentinel-probe.plist"
```

Verify the wiring end to end without waiting a week — this runs the probe once,
now, and the heartbeat it records is the proof:

```bash
launchctl kickstart -k "gui/$(id -u)/com.comics.sentinel-probe"
comics-api GET /api/comics/health/heartbeats
```

`sentinel-probe` should read `status: ok` with `success_count >= 1`. If it still
reads `never`, the probe either did not run or did not pass — check
`~/.comics-server/sentinel-probe.error.log`; a heartbeat is only written on
exit 0.

`RunAtLoad` is deliberately `false`: loading the agent during a deploy should
not spend provider budget as a side effect.

## Option B — cron

```bash
# Sundays at 09:00. Notify only when the probe alarms (exit 1) or could not
# complete (exit 2) — a clean run is silent, exactly like wishlist-sellers.
0 9 * * 0 COMICS_SERVER_URL=http://localhost:8080 bash -c '\
  PATH="$HOME/.local/bin:$PATH" comic-fmv --sentinel-probe \
    > /tmp/sentinel-probe-last.txt 2>&1; ec=$?; \
  if [ $ec -ne 0 ]; then \
    terminal-notifier -title "FMV Sentinel Probe" \
      -message "exit $ec — check /tmp/sentinel-probe-last.txt"; \
  fi'
```

## Exit codes

| Exit | Meaning | Pings the heartbeat? | Scheduler action |
|------|---------|---------------------|------------------|
| `0` | Every sentinel and the negative control passed | **yes** | Silent |
| `1` | Ran to completion; at least one check failed — the comp pipeline has drifted | no | Alert; this is the alarm the probe exists to raise |
| `2` | The probe itself could not complete (binary missing, subprocess timeout/crash, result-identity mismatch) | no | Alert; distinct from `1` so "couldn't check" is never read as "checked, and it's broken" |

The exit code is the **primary** alert surface; the heartbeat is the staleness
backstop underneath it. A run that alarms with exit 1 every week will also go
stale here after two weeks — a second, louder signal about the same fact, never
a quieter one.

---

Probe: `apps/fmv/src/sentinel_probe.py` — BUI-603. Heartbeat wiring: BUI-624.
