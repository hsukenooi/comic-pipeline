# launchd jobs (Mac Mini)

Checked-in LaunchAgent plists for scheduled jobs that must run on the Mac
Mini, where `COMICS_SERVER_URL` already points at the local comics server.
`scripts/deploy.sh` does **not** install or reload these automatically —
install once by hand (below), same as `docs/reference/sentinel-probe-scheduling.md`
already documents for `com.comics.sentinel-probe.plist` (that one predates
this directory and isn't duplicated here).

## com.comics.slab-watch-collect.plist (BUI-951)

Runs `comic-fmv --slab-watch-collect` monthly — the closest launchd
calendar slot to "every four weeks" (`StartCalendarInterval` has no
four-weekly unit; see the plist's own comment). Heartbeat contract:
`slab-watch-collect` in `docs/reference/job-heartbeat-contract.md`.

Install:

```bash
cp scripts/launchd/com.comics.slab-watch-collect.plist \
  "$HOME/Library/LaunchAgents/com.comics.slab-watch-collect.plist"

launchctl unload "$HOME/Library/LaunchAgents/com.comics.slab-watch-collect.plist" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/com.comics.slab-watch-collect.plist"
```

Verify the wiring end to end without waiting a month — this runs the job
once, now, and the heartbeat it records is the proof:

```bash
launchctl kickstart -k "gui/$(id -u)/com.comics.slab-watch-collect"
comics-api GET /api/comics/health/heartbeats
```

`slab-watch-collect` should read `status: ok` with `success_count >= 1`. If
it still reads `never`, check `~/.comics-server/slab-watch-collect.error.log`
— a heartbeat is only written on exit 0 (every fetch AND every ledger write
this run attempted succeeded; see the contract doc for the exact rule).

`RunAtLoad` is deliberately `false`: loading the agent during a deploy must
not spend provider budget as a side effect (same reasoning as
`sentinel-probe`'s plist).

Re-run the install snippet above (`cp` + `unload`/`load -w`) after any change
to the checked-in plist, including a `SLAB_WATCH_MAX_REQUESTS` override.
