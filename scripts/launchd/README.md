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

## com.comics.unpriced-rerun.plist (BUI-1080)

Runs `comic-fmv --unpriced-rerun` on the 1st of the month at 10:00, after
slab-watch-collect's 09:30. It re-runs the default pricing path (never
`--force`, so hand-priced rows are skipped) over every unpriced row, oldest
`updated_at` first, capped at `UNPRICED_RERUN_MAX_REQUESTS` books per run
(default 150). It sets `FMV_CEILING_CAP=1` in the plist because launchd does
not inherit shell exports; provider keys resolve through `ebay-sold-comps`
exactly as for slab-watch-collect. The log prints BEFORE and AFTER counts
(flagged by reason, capped). Heartbeat contract: `unpriced-rerun` in
`docs/reference/job-heartbeat-contract.md`.

Install:

```bash
cp scripts/launchd/com.comics.unpriced-rerun.plist \
  "$HOME/Library/LaunchAgents/com.comics.unpriced-rerun.plist"

launchctl unload "$HOME/Library/LaunchAgents/com.comics.unpriced-rerun.plist" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/com.comics.unpriced-rerun.plist"
```

For a small first run, uncomment `UNPRICED_RERUN_MAX_REQUESTS` with a small
value (for example `5`) in the installed copy, reload, then:

```bash
launchctl kickstart -k "gui/$(id -u)/com.comics.unpriced-rerun"
comics-api GET /api/comics/health/heartbeats
```

`unpriced-rerun` should read `status: ok`. A heartbeat is only written on exit
0; a failed run POSTs the failure ping instead. Check
`~/.comics-server/unpriced-rerun.log` for the counts and
`unpriced-rerun.error.log` for failures.

## com.comics.ebay-shipped-submit.plist (BUI-1238)

Runs `ebay-shipped --auto-submit` every 3 days. It reads the buyer's eBay
orders (Trading `GetOrders`, 30 days), groups them by tracking number, and
runs `ezship new` once per tracking number the EZShip ledger has not seen.
Non-US ship-from orders and carriers outside EZShip's seven are held with a
macOS notification and never submitted. An expired EZShip cookie (exit 3)
stops the batch and notifies. Heartbeat contract: `ebay-shipped-submit` in
`docs/reference/job-heartbeat-contract.md`.

Preconditions: `ebay-auth login` done (user token), `ezship` cookie set,
`apps/ezship` dependencies installed (`npm install`), and `ebay-shipped`
deployed by `scripts/deploy.sh` / `scripts/install.sh`.

Rehearse first, then install. The dry run never calls ezship:

```bash
ebay-shipped --auto-submit --dry-run

cp scripts/launchd/com.comics.ebay-shipped-submit.plist \
  "$HOME/Library/LaunchAgents/com.comics.ebay-shipped-submit.plist"
launchctl unload "$HOME/Library/LaunchAgents/com.comics.ebay-shipped-submit.plist" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/com.comics.ebay-shipped-submit.plist"
```

Logs: `~/.comics-server/ebay-shipped-submit.log` and `.error.log`.

Since BUI-1267 the job runs `scripts/ebay-shipped-and-wins.sh`, which runs
`ebay-shipped --auto-submit` and then `gixen record-wins`. That second step
records new Gixen wins in the collection on the comics server (the
unattended half of `/comic:collection-add`). Wins that need review stay
unseen and raise a notification; run `/comic:collection-add` to resolve
them. It writes no CSV: `/comic:collection-sync` exports pending rows
itself. Each step pings its own heartbeat (`ebay-shipped-submit`,
`record-wins`) and runs whether or not the other failed. Rehearse the
second step without writing anything with `gixen record-win-prep`.

## com.comics.em-batch-nightly.plist (BUI-972)

Runs `scripts/em-batch-nightly.sh` at 01:00 every night: picks up to
`EM_BATCH_NIGHTLY_CAP` (default 8) unassigned `comics` tickets in Today,
Soon, or Someday, and runs `/em-batch mode:autonomous` over them in a fresh
detached worktree, headless, through implement, review, CI, merge, deploy,
and close. Nothing is pushed to the user: each ticket's closing comment in
Linear is the record, and the run's summary is saved under the state dir.
With fewer than three tickets the prompt forces one Sonnet sub-agent per
ticket (`EM_BATCH_NIGHTLY_SMALL_DELEGATE=0` disables, BUI-1297).
The wrapper's header documents the knobs and the `--dry-run` / `--tickets` /
`--resume-run` switches.

Preconditions, all user-authored (the run never widens its own permissions):

- The allow rules in `.claude/settings.local.json` of the shared checkout
  (listed in `.claude/em-batch.md` under **Unattended**). The run loads
  that file with `--settings` and runs with `--permission-prompts none`, so
  a missing rule is a denied step and a held ticket, never a hung job.
- `gh auth status` green and `~/.config/linear/credentials.toml` readable
  for the launchd user.

Install:

```bash
cp scripts/launchd/com.comics.em-batch-nightly.plist \
  "$HOME/Library/LaunchAgents/com.comics.em-batch-nightly.plist"
launchctl unload "$HOME/Library/LaunchAgents/com.comics.em-batch-nightly.plist" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/com.comics.em-batch-nightly.plist"
```

Check the wiring without a model run, then run it for real once:

```bash
scripts/em-batch-nightly.sh --dry-run
launchctl kickstart -k "gui/$(id -u)/com.comics.em-batch-nightly"
tail -f ~/Library/Logs/em-batch-nightly.log
```

Per-run state (prompt, raw result JSON, summary, handoff) lands in
`~/.local/state/em-batch-nightly/runs/<date>/`. Turn the job off with
`launchctl unload -w` on the installed plist; a lock directory under
`~/.local/state/em-batch-nightly/` keeps two runs from overlapping.
